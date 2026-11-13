"""BBMP pipeline, part 1: schema parsing and normalisation, the connector (page discovery, HTTP behaviour) and database ingestion.
Everything runs on saved fixtures; no test touches the network. The CSV/XLSX fixtures follow the layout of the real BBMP files but contain invented
rows (see tests/make_bbmp_fixtures.py); they are not genuine BBMP data."""
import csv, io
from datetime import datetime

import httpx
import pytest

from app import models as m
from app.bbmp_schema import (parse_contractor, parse_bill_payment, parse_wo_details, map_columns, missing_required, normalise_row, parse_amount_strict,
                             regime_from_text, fiscal_label, row_hash)
from app.bbmp_ingest import FileContext, import_rows, read_csv_rows, read_rows, SchemaError, ImportAborted
from app.connectors.bbmp_connector import (BbmpConnector, Fetcher, FetchError, parse_dataset_page, detect_format, looks_restricted)
from app.db import SessionLocal
from bbmp_helpers import (Q, Site, DS, SLUG24, SLUG23, SLUG25, R24, R23, R25, dl_url, fixture, wipe)


@pytest.fixture()
def db(client):  # `client` starts the application, which creates the schema
    d = SessionLocal()
    wipe(d)
    yield d
    wipe(d)
    d.close()


def ctx(fy="2024-25", regime="243", name="f.csv", **kw):
    return FileContext(fiscal_year=fy, ward_regime=regime, filename=name, dataset_slug="t", dataset_url="https://data.opencity.in/dataset/t", **kw)


def counts(db):
    return {"works": Q(db, m.Roadwork).count(), "src": Q(db, m.SourceRecord).filter(m.SourceRecord.external_id.isnot(None)).count(),
            "pay": Q(db, m.Payment).filter(m.Payment.external_key.isnot(None)).count(), "bills": Q(db, m.Bill).filter(m.Bill.external_key.isnot(None)).count(),
            "links": Q(db, m.WorkSourceLink).join(m.SourceRecord, m.SourceRecord.id == m.WorkSourceLink.source_record_id).filter(m.SourceRecord.external_id.isnot(None)).count(), "jobs": db.query(m.ImportJob).filter(m.ImportJob.kind == "bbmp_open_data").count()}


def make_csv(rows, header=("slno", "id", "Ward", "wodetails", "contractor", "brnumber", "amount", "nett", "deduction")):
    b = io.StringIO()
    w = csv.writer(b, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return b.getvalue().encode()


GOOD = ["1", "100001", "5", "005-23-000001</a>Drilling of borewells in Hebbal ward No-05", "023258 RRENGINEERINGWORKS8880709398",
        "BR - 000042 / 10-Jul-2023CBR - / Rtgs - 004097 / 19-Jun-2024", "1000000", "950000", "50000"]


def row(**over):
    r = list(GOOD)
    idx = {"slno": 0, "id": 1, "ward": 2, "wo": 3, "contractor": 4, "br": 5, "amount": 6, "nett": 7, "ded": 8}
    for k, v in over.items():
        r[idx[k]] = v
    return r


# ============================================================================= schema and normalisation
class TestParsing:
    def test_work_order_code_and_html_tag(self):
        assert parse_wo_details("001-23-000012</a>Drilling of borewells in kempegowda ward No-01") == ("001-23-000012", "Drilling of borewells in kempegowda ward No-01")
        assert parse_wo_details("160-23-000005</a>Comprehensive Development of Roads & Drains in Javare")[0] == "160-23-000005"
        assert parse_wo_details("Footpath repair") == (None, "Footpath repair")
        assert parse_wo_details("001-23-000012&nbsp;</a> Desilting &amp; widening")[1] == "Desilting & widening"

    @pytest.mark.parametrize("raw,code,name,phone,cut", [
        ("023258 RRENGINEERINGWORKS8880709398", "023258", "RRENGINEERINGWORKS", True, False),
        ("035531 SRI VENKATESWARA CON9980071159", "035531", "SRI VENKATESWARA CON", True, True),
        ("AKSHAY CONSTRUCTIONS9164858389", None, "AKSHAY CONSTRUCTIONS", True, True),
        ("022380 A G NARAYANASWAMY &9738780002", "022380", "A G NARAYANASWAMY &", True, True),
        ("KRIDL BHUSIRI ACCOU0000000000", None, "KRIDL BHUSIRI ACCOU", False, True),
        ("026261 SRINIVAS.M.R 9739988642", "026261", "SRINIVAS.M.R", True, False),
        ("M L R INFRA 9845307275", None, "M L R INFRA", True, False)])
    def test_contractor_field_taken_apart(self, raw, code, name, phone, cut):
        c = parse_contractor(raw)
        assert (c["vendor_code"], c["name"], c["has_phone"], c["truncated"]) == (code, name, phone, cut)
        assert "phone" not in c  # the number is not carried into normalised data

    def test_empty_contractor(self):
        assert parse_contractor("")["name"] is None and parse_contractor(None)["vendor_code"] is None
        assert parse_contractor("123456")["name"] is None  # digits alone are not a name

    def test_bill_cheque_and_rtgs_references(self):
        r = parse_bill_payment("BR - 000042 / 10-Jul-2023CBR - / Rtgs - 004097 / 19-Jun-2024")
        assert (r["br_no"], r["br_date_text"], r["cbr_no"], r["rtgs_no"], r["rtgs_date_text"]) == ("000042", "10-Jul-2023", None, "004097", "19-Jun-2024")
        r = parse_bill_payment("BR - 7 / 01-Jan-2024CBR - 55 / 02-Jan-2024Rtgs - / ")
        assert r["cbr_no"] == "55" and r["cbr_date_text"] == "02-Jan-2024" and r["rtgs_no"] is None
        assert parse_bill_payment("something else")["parsed"] is False

    def test_amounts_are_strict(self):
        assert parse_amount_strict("1,214,000") == 1214000.0 and parse_amount_strict("Rs. 5,000/-") == 5000.0
        for bad in ("twelve", "12 34", "", None, "nan"):
            with pytest.raises(ValueError):
                parse_amount_strict(bad)

    def test_regime_and_fiscal_year_from_text(self):
        assert regime_from_text("BBMP Work Orders and Payments for 243 Wards Regime") == "243"
        assert regime_from_text("Work Orders and Payments for Common Wards") == "common"
        assert regime_from_text("BBMP Work Orders 2023-24") is None
        assert fiscal_label("FY 2024-2025") == "2024-25" and fiscal_label("bbmp-work-orders-2023-24") == "2023-24" and fiscal_label("2024-27") is None


class TestColumnMapping:
    def test_header_spellings_of_different_years(self):
        for header in ("Ward", "Ward No", "ward"):
            cols = ["slno", "id", header, "wodetails", "contractor", "brnumber", "amount", "nett", "deduction"]
            mp, un = map_columns(cols)
            assert mp["ward"] == header and mp["wo_details"] == "wodetails" and mp["net_amount"] == "nett" and un == []
            assert missing_required(mp) == []

    def test_a_different_schema_maps_to_the_same_fields(self):
        cols = ["Sl No", "Work ID", "Work Description", "Ward Number", "Department", "Contractor Name", "Estimated Amount", "Approved Amount", "Bill Number",
                "Payment Amount", "Payment Date", "Work Order Date", "Financial Year"]
        mp, un = map_columns(cols)
        assert mp["work_id"] == "Work ID" and mp["description"] == "Work Description" and mp["estimated_amount"] == "Estimated Amount"
        assert mp["approved_amount"] == "Approved Amount" and mp["payment_date"] == "Payment Date" and mp["work_order_date"] == "Work Order Date"
        assert missing_required(mp) == []

    def test_override_wins_and_unknown_columns_are_reported(self):
        cols = ["slno", "id", "WARDNO", "details", "vendor", "brnumber", "amount", "nett", "deduction", "remarks"]
        mp, un = map_columns(cols, {"ward": "WARDNO", "wo_details": "details", "contractor": "vendor"})
        assert mp["ward"] == "WARDNO" and mp["wo_details"] == "details" and un == ["remarks"]

    def test_missing_required_columns_are_named(self):
        mp, _ = map_columns(["a", "b", "c"])
        assert len(missing_required(mp)) == 2


class TestRowNormalisation:
    cols = ["slno", "id", "Ward", "wodetails", "contractor", "brnumber", "amount", "nett", "deduction"]

    def norm(self, **over):
        raw = dict(zip(self.cols, GOOD))
        raw.update({self.cols[{"amount": 6, "nett": 7, "ded": 8, "br": 5, "contractor": 4, "ward": 2, "wo": 3}[k]]: v for k, v in over.items()})
        return normalise_row(raw, map_columns(self.cols)[0], fiscal_year="2024-25", today=datetime(2026, 11, 12))

    def test_a_good_row(self):
        r = self.norm()
        v = r.values
        assert r.ok and not r.flags
        assert v["work_order_code"] == "005-23-000001" and v["ward"] == "5" and v["contractor_id"] == "023258" and v["contractor_name"] == "RRENGINEERINGWORKS"
        assert (v["gross_amount"], v["net_amount"], v["deduction"]) == (1000000.0, 950000.0, 50000.0)
        assert v["bill_number"] == "000042" and v["bill_date"] == datetime(2023, 7, 10) and v["payment_date"] == datetime(2024, 6, 19) and v["has_payment"]
        assert v["paid_amount"] == 950000.0  # the net amount is what was paid

    def test_nothing_is_invented_for_missing_data(self):
        v = self.norm().values
        assert v["estimated_amount"] is None and v["approved_amount"] is None and v["work_order_date"] is None and v["department"] is None
        assert v["road_name"] is None and v["location"] is None

    def test_missing_values_are_listed_not_filled(self):
        r = self.norm(contractor="", ward="")
        assert r.ok and "contractor" in r.missing and "ward" in r.missing and r.values["contractor_name"] is None

    @pytest.mark.parametrize("over,code", [({"amount": "twelve thousand"}, "bad_amount"), ({"amount": "-5", "nett": "-5", "ded": "0"}, "negative_amount"),
                                           ({"amount": "99999999999", "nett": "1"}, "implausible_amount")])
    def test_invalid_rows_are_rejected_with_a_reason(self, over, code):
        r = self.norm(**over)
        assert not r.ok and r.errors[0][0] == code

    def test_row_without_any_amount_is_rejected(self):
        r = self.norm(amount="", nett="", ded="")
        assert not r.ok and r.errors[0][0] == "no_amount"

    def test_flags_do_not_reject(self):
        r = self.norm(nett="900000")
        assert r.ok and [c for c, _ in r.flags] == ["amount_mismatch"]
        assert "ward_not_numeric" in [c for c, _ in self.norm(ward="R").flags]
        assert "ref_not_parsed" in [c for c, _ in self.norm(br="garbage").flags]

    def test_invalid_dates_are_dropped_with_a_note(self):
        r = self.norm(br="BR - 1 / 31-Feb-2024CBR - / Rtgs - 22 / 19-Jun-2024")
        assert r.ok and r.values["bill_date"] is None and "bad_date" in [c for c, _ in r.flags]

    def test_payment_outside_the_year_and_paid_before_billed_are_flagged(self):
        r = self.norm(br="BR - 1 / 10-Jul-2024CBR - / Rtgs - 22 / 19-Jun-2023")
        assert {"payment_outside_fy", "paid_before_bill"} <= {c for c, _ in r.flags}

    def test_bill_without_rtgs_is_not_a_payment(self):
        r = self.norm(br="BR - 000111 / 01-Aug-2023CBR - / Rtgs - / ")
        assert r.ok and not r.values["has_payment"] and "no_payment_reference" in [c for c, _ in r.flags]

    def test_row_hash_ignores_blank_values_and_order(self):
        assert row_hash({"a": "1", "b": ""}) == row_hash({"b": None, "a": "1"}) and row_hash({"a": "1"}) != row_hash({"a": "2"})


# ============================================================================= connector
class TestDiscovery:
    def test_2024_25_page(self):
        info, nxt = parse_dataset_page(fixture("dataset_2024_25.html").decode(), f"{DS}/dataset/{SLUG24}")
        assert info.title == "BBMP Work Orders and Payments 2024-25" and info.fiscal_year == "2024-25" and nxt == []
        assert info.meta["Author"] == "Vaidya R" and info.meta["Created"].startswith("July 14, 2025") and "Public Domain" in info.meta["License"]
        got = {r.ward_regime: r for r in info.resources}
        assert set(got) == {"198", "225", "243"}
        r = got["243"]
        assert r.resource_id == R24["243"][0] and r.download_url == dl_url(R24["243"]) and r.filename == R24["243"][2] and r.file_format == "csv"
        assert r.preview_url.endswith(f"/resource/{R24['243'][0]}") and r.name == "BBMP Work Orders and Payments for 243 Wards Regime"

    def test_2023_24_and_2025_26_pages(self):
        a, _ = parse_dataset_page(fixture("dataset_2023_24.html").decode(), f"{DS}/dataset/{SLUG23}")
        assert a.fiscal_year == "2023-24" and len(a.resources) == 1 and a.resources[0].ward_regime is None and "old ward numbers" in a.resources[0].description
        b, _ = parse_dataset_page(fixture("dataset_2025_26.html").decode(), f"{DS}/dataset/{SLUG25}")
        assert b.fiscal_year == "2025-26" and {r.ward_regime for r in b.resources} == {"198", "243", "225", "common"}

    def test_download_link_extraction_ignores_preview_and_dropdown_links(self):
        info, _ = parse_dataset_page(fixture("dataset_2024_25.html").decode(), f"{DS}/dataset/{SLUG24}")
        assert all("/download/" in r.download_url for r in info.resources) and len({r.download_url for r in info.resources}) == 3

    def test_pagination_is_followed_and_merged(self):
        site = Site()
        site.add(f"{DS}/dataset/{SLUG24}", fixture("dataset_paginated_p1.html"))
        site.add(f"{DS}/dataset/{SLUG24}?page=2", fixture("dataset_paginated_p2.html"))
        info = BbmpConnector(site.fetcher()).discover_dataset(f"{DS}/dataset/{SLUG24}")
        assert len(info.resources) == 3 and len(info.pages) == 2

    def test_connector_end_to_end_with_fetcher(self):
        site = Site().standard(only=("24",))
        info = BbmpConnector(site.fetcher()).discover_dataset(f"{DS}/dataset/{SLUG24}", "2024-25")
        assert info.slug == SLUG24 and info.discovered_via == "html" and len(info.resources) == 3

    def test_page_without_files_is_an_error_not_an_empty_success(self):
        site = Site()
        site.add(f"{DS}/dataset/x", "<html><body><h1>Empty</h1></body></html>")
        with pytest.raises(FetchError) as e:
            BbmpConnector(site.fetcher()).discover_dataset(f"{DS}/dataset/x")
        assert e.value.kind == "not_data"

    def test_playwright_only_when_static_html_has_no_links(self):
        calls = []
        site = Site()
        site.add("https://site.bbmp.gov.in/", fixture("site_js_shell.html"))
        site.add(f"{DS}/dataset/{SLUG24}", fixture("dataset_2024_25.html"))
        render = lambda url: calls.append(url) or fixture("site_js_rendered.html").decode()
        # a page whose static HTML is complete never starts a browser
        BbmpConnector(site.fetcher(), use_playwright=True, renderer=render).discover_dataset(f"{DS}/dataset/{SLUG24}")
        assert calls == []
        # a shell without links does, when enabled ...
        info = BbmpConnector(site.fetcher(), use_playwright=True, renderer=render).discover_site("https://site.bbmp.gov.in/")
        assert calls == ["https://site.bbmp.gov.in/"] and info.discovered_via == "playwright" and info.resources[0].file_format == "csv"
        # ... and does not when disabled
        calls.clear()
        assert BbmpConnector(site.fetcher(), use_playwright=False, renderer=render).discover_site("https://site.bbmp.gov.in/").resources == [] and calls == []

    def test_official_site_scan_lists_data_links_only(self):
        site = Site()
        site.add("https://site.bbmp.gov.in/", fixture("site_bbmp_home.html"))
        info = BbmpConnector(site.fetcher()).discover_site("https://site.bbmp.gov.in/")
        assert [r.file_format for r in info.resources] == ["xlsx", "pdf"] and "mailto" not in " ".join(r.download_url for r in info.resources)


class TestFetcher:
    URL = f"{DS}/dataset/x"

    def test_retries_a_busy_server_then_succeeds(self):
        site = Site(); site.add(self.URL, "<html>ok</html>"); site.fail_next[self.URL] = 2
        f = site.fetcher(retries=3)
        assert "ok" in f.get_text(self.URL)[0] and site.hits[self.URL] == 3

    def test_gives_up_after_the_retry_limit(self):
        site = Site(); site.add(self.URL, "<html>ok</html>"); site.fail_next[self.URL] = 10
        with pytest.raises(FetchError) as e:
            site.fetcher(retries=2).get_text(self.URL)
        assert e.value.kind == "http" and site.hits[self.URL] == 3

    def test_timeouts_are_retried_and_reported(self):
        def boom(request):
            raise httpx.ReadTimeout("slow", request=request)
        f = Fetcher(client=httpx.Client(transport=httpx.MockTransport(boom)), delay=0, sleep=lambda s: None, backoff=0, retries=2, allowed_hosts=None, respect_robots=False)
        with pytest.raises(FetchError) as e:
            f.get_text(self.URL)
        assert e.value.kind == "timeout"

    @pytest.mark.parametrize("status", [401, 403])
    def test_restricted_access_is_reported_and_not_retried_or_bypassed(self, status):
        site = Site(); site.add(self.URL, "denied", status=status)
        with pytest.raises(FetchError) as e:
            site.fetcher(retries=3).get_text(self.URL)
        assert e.value.kind == "blocked" and "not bypassed" in str(e.value) and site.hits[self.URL] == 1

    def test_a_missing_file_is_not_retried(self):
        site = Site()
        with pytest.raises(FetchError) as e:
            site.fetcher(retries=3).download(f"{DS}/nothing.csv")
        assert e.value.kind == "http" and e.value.status == 404 and site.hits[f"{DS}/nothing.csv"] == 1

    def test_pause_between_requests(self):
        site = Site(); site.add(self.URL, "<html>ok</html>")
        pauses, t = [], [0.0]
        f = Fetcher(client=httpx.Client(transport=httpx.MockTransport(site.handler)), delay=2.0, sleep=pauses.append, clock=lambda: t[0], allowed_hosts=None, respect_robots=False)
        f.get_text(self.URL); f.get_text(self.URL)
        assert pauses and pauses[0] == pytest.approx(2.0)

    def test_robots_txt_is_honoured(self):
        site = Site(); site.add(self.URL, "<html>ok</html>"); site.add(f"{DS}/robots.txt", "User-agent: *\nDisallow: /dataset/", ctype="text/plain")
        with pytest.raises(FetchError) as e:
            site.fetcher().get_text(self.URL)
        assert e.value.kind == "robots" and site.hits[self.URL] == 0

    def test_only_allowed_hosts_and_never_private_addresses(self):
        site = Site(); site.add("https://example.org/a.csv", "a,b"); site.add("http://127.0.0.1/a.csv", "a,b"); site.add("http://10.0.0.5/a.csv", "a,b")
        with pytest.raises(FetchError) as e:
            site.fetcher().download("https://example.org/a.csv")
        assert e.value.kind == "host"
        for u in ("http://127.0.0.1/a.csv", "http://10.0.0.5/a.csv", "http://localhost/a.csv"):
            with pytest.raises(FetchError) as e:
                site.fetcher(allowed_hosts=None).download(u)
            assert e.value.kind == "host"
        assert site.fetcher(allowed_hosts=None).download("https://example.org/a.csv").content == b"a,b"

    def test_size_limit(self):
        site = Site(); site.add(f"{DS}/big.csv", "x" * 5000)
        with pytest.raises(FetchError) as e:
            site.fetcher(max_bytes=1000).download(f"{DS}/big.csv")
        assert e.value.kind == "too_large"

    def test_conditional_request_answers_not_modified(self):
        site = Site(); site.add(f"{DS}/a.csv", "a,b\n1,2\n")
        f = site.fetcher()
        first = f.download(f"{DS}/a.csv")
        again = f.download(f"{DS}/a.csv", etag=first.etag)
        assert first.status == "ok" and again.status == "not_modified" and again.content == b""


def test_file_type_detection():
    assert detect_format(fixture("wo_payments_2024_25_243.xlsx")) == "xlsx"
    assert detect_format(fixture("wo_payments_2024_25_243.csv")) == "csv"
    assert detect_format(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 50) == "xls"
    assert detect_format(fixture("login_page.html")) == "html" and looks_restricted(fixture("login_page.html").decode())
    assert detect_format(b"") is None and detect_format(b"\x00\x01\x02binary") is None


# ============================================================================= reading files
class TestReadingFiles:
    def test_unbalanced_quote_does_not_hide_the_lines_after_it(self):
        cols, rows, issues = read_csv_rows(fixture("wo_payments_2024_25_243.csv").decode())
        assert len(rows) == 41 and issues == []
        repaired = [r for r in rows if r[2]]
        assert len(repaired) == 1 and repaired[0][1]["wodetails"].startswith("012-23-000099</a>Repair of retaining wall, near school")
        assert repaired[0][1]["amount"] == "450000"

    def test_quoted_commas_and_embedded_html_survive(self):
        cols, rows, _ = read_csv_rows('slno,id,Ward,wodetails,contractor,brnumber,amount,nett,deduction\n1,2,3,"001-23-000001</a>Roads, drains & footpaths","A B9999999999","BR - 1 / 01-Jan-2024CBR - / Rtgs - 2 / 02-Jan-2024",10,9,1\n')
        assert rows[0][1]["wodetails"] == "001-23-000001</a>Roads, drains & footpaths"

    def test_row_numbers_are_file_line_numbers(self):
        _, rows, _ = read_csv_rows(fixture("wo_payments_2024_25_198.csv").decode())
        assert rows[0][0] == 2 and rows[-1][0] == len(rows) + 1

    def test_xlsx_and_csv_give_the_same_columns(self):
        c1, r1, _ = read_rows(fixture("wo_payments_2024_25_243.xlsx"), "a.xlsx")
        c2, r2, _ = read_rows(fixture("wo_payments_2024_25_198.csv"), "a.csv")
        assert c1 == c2 and len(r1) == 10

    def test_a_file_with_the_wrong_layout_is_refused_with_the_columns_found(self, db):
        with pytest.raises(SchemaError) as e:
            import_rows(db, b"alpha,beta\n1,2\n3,4\n", ctx())
        assert "Columns not recognised" in str(e.value) and "alpha" in str(e.value)
        assert counts(db)["jobs"] == 0


# ============================================================================= database ingestion
class TestIngestion:
    def test_one_file_end_to_end(self, db):
        data = fixture("wo_payments_2024_25_243.csv")
        job, c = import_rows(db, data, ctx(name="wo_payments_2024_25_243.csv"))
        assert (c.processed, c.inserted, c.duplicate, c.failed, c.updated) == (41, 38, 1, 2, 0)
        assert job.status == "done_with_errors" and job.rows_total == 41 and job.rows_ok == 38 and job.rows_failed == 2 and job.rows_duplicate == 1
        assert job.fiscal_year == "2024-25" and job.ward_regime == "243" and job.is_demo is False and job.kind == "bbmp_open_data"
        n = counts(db)
        assert n["src"] == 38 and n["bills"] == 38 and n["pay"] == 37 and n["links"] == 38 and n["works"] == 37  # one bill had no RTGS; two rows share a work; the no-code row has its own
        # the failed rows are reported, with the original row
        failed = db.query(m.ImportIssue).filter_by(kind="failed").all()
        assert sorted(i.code for i in failed) == ["bad_amount", "negative_amount"] and all(i.raw for i in failed)
        assert db.query(m.ImportIssue).filter_by(kind="duplicate", code="repeated_row_in_file").count() == 1

    def test_original_normalised_and_work_values_are_kept_apart(self, db):
        import_rows(db, make_csv([GOOD]), ctx())
        s = Q(db, m.SourceRecord).one()
        assert s.raw["wodetails"] == GOOD[3] and s.raw["contractor"] == GOOD[4] and s.raw["brnumber"] == GOOD[5]  # exactly as published
        assert s.normalised["work_order_code"] == "005-23-000001" and s.normalised["contractor_id"] == "023258" and s.normalised["payment_reference"] == "004097"
        assert s.external_id == "bbmp|2024-25|243|100001|1" and s.fiscal_year == "2024-25" and s.row_hash and s.source_file_id is None
        w = Q(db, m.Roadwork).one()
        assert w.description == "Drilling of borewells in Hebbal ward No-05" and w.work_order_id == "005-23-000001" and w.external_key == "bbmp:243:005-23-000001"
        assert w.ward_number == "5" and w.ward_regime == "243" and w.data_origin == "bbmp_open_data" and w.is_demo is False and w.verification_status == "official"
        assert w.estimated_cost is None and w.sanctioned_amount is None and w.awarded_amount is None and w.work_order_date is None  # not published, not invented
        assert w.fiscal_year == "2024-25" and w.contractor_id and w.verified_fields is None

    def test_bill_and_payment_amounts(self, db):
        import_rows(db, make_csv([GOOD]), ctx())
        b, p = Q(db, m.Bill).filter(m.Bill.external_key.isnot(None)).one(), Q(db, m.Payment).filter(m.Payment.external_key.isnot(None)).one()
        assert (b.amount, b.deduction, b.net_amount, b.bill_number, b.bill_date) == (1000000.0, 50000.0, 950000.0, "000042", datetime(2023, 7, 10))
        assert (p.amount, p.gross_amount, p.deduction, p.paid_on, p.reference, p.bill_id) == (950000.0, 1000000.0, 50000.0, datetime(2024, 6, 19), "Rtgs 004097", b.id)
        assert p.duplicate_of_id is None

    def test_contractor_code_name_and_no_phone_number_stored(self, db):
        import_rows(db, make_csv([GOOD, row(id="100002", slno="2", wo="005-23-000002</a>Other", contractor="023258 RRENGINEERINGWORKS8880709398")]), ctx())
        c = Q(db, m.Contractor).one()
        assert (c.name, c.vendor_code, c.is_demo) == ("RRENGINEERINGWORKS", "023258", False)
        assert "8880709398" not in str(c.__dict__) and "8880709398" not in str(Q(db, m.Roadwork).first().__dict__)

    def test_same_contractor_is_one_record_across_files_and_spellings(self, db):
        import_rows(db, make_csv([GOOD]), ctx())
        import_rows(db, make_csv([row(id="7", wo="009-23-000001</a>X", contractor="023258 RRENGINEERINGWORKS1111111111")]), ctx(regime="198"))
        assert Q(db, m.Contractor).count() == 1

    def test_rerun_creates_nothing(self, db):
        data = fixture("wo_payments_2024_25_243.csv")
        import_rows(db, data, ctx())
        before = counts(db)
        issues_before = db.query(m.ImportIssue).filter(m.ImportIssue.kind.in_(["duplicate", "updated", "conflict"])).count()
        job2, c2 = import_rows(db, data, ctx())
        after = counts(db)
        assert {k: v for k, v in after.items() if k != "jobs"} == {k: v for k, v in before.items() if k != "jobs"}
        assert (c2.inserted, c2.updated, c2.works_created, c2.payments_created) == (0, 0, 0, 0) and c2.duplicate == 39 and c2.failed == 2
        assert db.query(m.ImportIssue).filter(m.ImportIssue.kind.in_(["duplicate", "updated", "conflict"])).count() == issues_before + 1  # only the in-file repeat is logged again

    def test_xlsx_gives_the_same_result_as_csv_shape(self, db):
        job, c = import_rows(db, fixture("wo_payments_2024_25_243.xlsx"), ctx(name="a.xlsx", file_format="xlsx"))
        assert c.failed == 0 and c.inserted == 10 and counts(db)["pay"] == 10
        assert Q(db, m.SourceRecord).first().raw["amount"].replace(".0", "").isdigit()

    def test_different_financial_year_schemas(self, db):
        a = import_rows(db, fixture("wo_2023_24_198.csv"), ctx(fy="2023-24", regime="198", name="a.csv"))[1]          # "Ward No" header
        b = import_rows(db, fixture("wo_payments_2025_26_common.csv"), ctx(fy="2025-26", regime="common", name="b.csv"))[1]   # "ward" header, "R" ward, empty contractor
        assert a.failed == 0 and b.failed == 0 and a.inserted == 20 and b.inserted == 12
        w = Q(db, m.Roadwork).filter(m.Roadwork.ward_number == "R").first()
        assert w is not None and w.ward_is_multi is False
        assert Q(db, m.Roadwork).filter(m.Roadwork.ward_regime == "common", m.Roadwork.ward_is_multi.is_(True)).count() >= 1
        assert Q(db, m.Roadwork).filter(m.Roadwork.ward_regime == "common", m.Roadwork.contractor_id.is_(None)).count() == 1
        assert {f for (f,) in Q(db, m.SourceRecord).with_entities(m.SourceRecord.fiscal_year).distinct()} == {"2023-24", "2025-26"}

    def test_a_later_schema_with_estimates_and_dates(self, db):
        job, c = import_rows(db, fixture("alt_schema_2025_26.csv"), ctx(fy="2025-26", regime="243", name="alt.csv"))
        assert c.failed == 0 and c.inserted == 2
        w = Q(db, m.Roadwork).filter_by(work_order_id="WO/ALT/001").one()
        assert (w.estimated_cost, w.awarded_amount, w.sanctioned_amount, w.department) == (1200000.0, 1150000.0, 1150000.0, "Roads")
        assert w.work_order_date == datetime(2025, 4, 2) and "5th Main Road" in w.description and w.road_name_extracted
        p = Q(db, m.Payment).filter(m.Payment.external_key.isnot(None)).one()  # the second work has no payment date or amount: no payment invented
        assert p.amount == 1000000.0 and p.paid_on == datetime(2025, 7, 15)

    def test_two_payments_for_one_work_stay_two_payments(self, db):
        r1 = row(); r2 = row(id="100002", slno="2", br="BR - 000043 / 11-Jul-2023CBR - / Rtgs - 004098 / 20-Jun-2024", amount="500000", nett="480000", ded="20000")
        import_rows(db, make_csv([r1, r2]), ctx())
        assert counts(db)["works"] == 1 and counts(db)["links"] == 2
        pays = Q(db, m.Payment).order_by(m.Payment.id).all()
        assert [p.amount for p in pays] == [950000.0, 480000.0] and all(p.duplicate_of_id is None for p in pays)
        roles = sorted(l.role for l in Q(db, m.WorkSourceLink))
        assert roles == ["merged_row", "primary"]

    def test_same_work_in_two_years_is_one_work_with_two_source_rows(self, db):
        import_rows(db, make_csv([GOOD]), ctx(fy="2023-24", regime="243"))
        later = row(id="555", slno="9", br="BR - 000099 / 05-Jan-2025CBR - / Rtgs - 009999 / 10-Feb-2025")
        import_rows(db, make_csv([later]), ctx(fy="2024-25", regime="243"))
        assert counts(db)["works"] == 1
        w = Q(db, m.Roadwork).one()
        assert w.fiscal_year == "2023-24"  # first financial year it appeared in
        links = Q(db, m.WorkSourceLink).filter_by(roadwork_id=w.id).all()
        assert len(links) == 2 and {db.get(m.SourceRecord, l.source_record_id).fiscal_year for l in links} == {"2023-24", "2024-25"}
        assert counts(db)["pay"] == 2

    def test_same_code_in_a_different_ward_regime_is_a_different_work(self, db):
        import_rows(db, make_csv([GOOD]), ctx(regime="243"))
        import_rows(db, make_csv([GOOD]), ctx(regime="198"))
        assert counts(db)["works"] == 2

    def test_a_payment_published_again_in_another_file_is_not_counted_twice(self, db):
        import_rows(db, make_csv([GOOD]), ctx(fy="2024-25"))
        again = row(id="999", slno="77")   # another file lists the identical payment (same work, RTGS reference, date and amount)
        import_rows(db, make_csv([again]), ctx(fy="2025-26", name="other.csv"))
        pays = Q(db, m.Payment).order_by(m.Payment.id).all()
        assert len(pays) == 2 and pays[0].duplicate_of_id is None and pays[1].duplicate_of_id == pays[0].id
        from app.sources import counted_payments
        assert counted_payments(db).filter(m.Payment.external_key.isnot(None)).count() == 1
        assert db.query(m.ImportIssue).filter_by(code="payment_already_recorded").count() == 1
        assert counts(db)["src"] == 2  # the second source row is kept for provenance

    def test_changed_published_row_updates_its_own_bill_and_payment(self, db):
        import_rows(db, make_csv([GOOD]), ctx())
        job, c = import_rows(db, make_csv([row(amount="1100000", nett="1045000", ded="55000")]), ctx())
        assert (c.updated, c.inserted, c.duplicate) == (1, 0, 0)
        assert counts(db)["pay"] == 1 and Q(db, m.Payment).filter(m.Payment.external_key.isnot(None)).one().amount == 1045000.0 and Q(db, m.Bill).filter(m.Bill.external_key.isnot(None)).one().amount == 1100000.0
        assert Q(db, m.SourceRecord).one().raw["amount"] == "1100000"
        assert db.query(m.ImportIssue).filter_by(kind="updated").count() == 1

    def test_row_without_a_work_identifier_is_kept_as_unmatched(self, db):
        job, c = import_rows(db, make_csv([row(wo="", contractor="")]), ctx())
        assert c.unmatched == 1 and counts(db)["src"] == 1 and counts(db)["works"] == 0 and counts(db)["links"] == 0
        assert counts(db)["pay"] == 0

    def test_existing_work_from_another_source_is_adopted_not_duplicated_or_overwritten(self, db):
        w = m.Roadwork(work_order_id="005-23-000001", ward_regime="243", description="Description typed in earlier", data_origin="import", is_demo=False, verification_status="official")
        db.add(w); db.commit()
        import_rows(db, make_csv([GOOD]), ctx())
        db.refresh(w)
        assert counts(db)["works"] == 1 and w.external_key == "bbmp:243:005-23-000001" and w.description == "Description typed in earlier"
        assert w.ward_number == "5" and Q(db, m.WorkSourceLink).filter_by(roadwork_id=w.id).one().role == "merged_row"

    def test_officer_verified_fields_are_never_overwritten(self, db):
        import_rows(db, make_csv([GOOD]), ctx())
        w = Q(db, m.Roadwork).one()
        w.description, w.verified_fields = "Corrected by an officer", ["description"]
        db.commit()
        other = row(id="200", slno="3", wo="005-23-000001</a>Imported text that differs", br="BR - 5 / 01-Jan-2024CBR - / Rtgs - 77 / 02-Feb-2024")
        import_rows(db, make_csv([other]), ctx(fy="2025-26", name="next.csv"))
        db.refresh(w)
        assert w.description == "Corrected by an officer"
        i = db.query(m.ImportIssue).filter_by(kind="conflict", code="verified_field_kept").one()
        assert "Imported text that differs" in i.message

    def test_empty_fields_are_filled_but_existing_values_are_kept(self, db):
        import_rows(db, make_csv([row(ward="")]), ctx())
        w = Q(db, m.Roadwork).one()
        assert w.ward_number is None
        import_rows(db, make_csv([row(id="2", slno="2", ward="9", br="BR - 5 / 01-Jan-2024CBR - / Rtgs - 8 / 02-Feb-2024")]), ctx())
        db.refresh(w)
        assert w.ward_number == "9"
        import_rows(db, make_csv([row(id="3", slno="3", ward="11", br="BR - 6 / 01-Jan-2024CBR - / Rtgs - 9 / 02-Feb-2024")]), ctx())
        db.refresh(w)
        assert w.ward_number == "9" and db.query(m.ImportIssue).filter_by(code="ward_number_differs").count() == 1

    def test_year_and_regime_are_required(self, db):
        with pytest.raises(SchemaError):
            import_rows(db, make_csv([GOOD]), ctx(regime=""))
        with pytest.raises(SchemaError):
            import_rows(db, make_csv([GOOD]), ctx(fy="soon"))
        assert counts(db)["jobs"] == 0

    def test_source_links_use_the_existing_many_to_many_table(self, db):
        import_rows(db, make_csv([GOOD, row(id="2", slno="2", br="BR - 5 / 01-Jan-2024CBR - / Rtgs - 8 / 02-Feb-2024")]), ctx())
        from app.sources import work_sources
        w = Q(db, m.Roadwork).one()
        src = work_sources(db, w.id)
        assert len(src) == 2 and {s["role"] for s in src} == {"primary", "merged_row"}
        assert all(s["import"]["kind"] == "bbmp_open_data" and s["raw"] and s["normalised"] and s["external_id"].startswith("bbmp|2024-25|243|") for s in src)
        assert src[0]["evidence"]["work_key"] == "bbmp:243:005-23-000001"


class TestTransactions:
    def test_a_database_error_part_way_rolls_back_the_whole_file(self, db, monkeypatch):
        import_rows(db, make_csv([row(id="1", wo="001-23-000001</a>Existing work")]), ctx(fy="2023-24"))
        before = counts(db)
        import app.bbmp_ingest as ing
        real, n = ing._write_bill_and_payment, [0]

        def flaky(*a, **k):
            n[0] += 1
            if n[0] == 5:
                raise RuntimeError("disk full")
            return real(*a, **k)
        monkeypatch.setattr(ing, "_write_bill_and_payment", flaky)
        rows = [row(id=str(i), slno=str(i), wo=f"00{i}-23-00000{i}</a>Work {i}") for i in range(2, 9)]
        with pytest.raises(RuntimeError):
            import_rows(db, make_csv(rows), ctx(fy="2024-25"))
        assert counts(db) == before   # nothing from the failed file, and the earlier file is intact
        assert Q(db, m.Contractor).count() == 1

    def test_too_many_invalid_rows_means_nothing_is_stored(self, db):
        bad = [row(id=str(i), slno=str(i), amount="x") for i in range(30)]
        with pytest.raises(ImportAborted) as e:
            import_rows(db, make_csv(bad + [GOOD]), ctx())
        assert "nothing was stored" in str(e.value) and counts(db)["src"] == 0 and counts(db)["jobs"] == 0 and db.query(m.ImportIssue).count() == 0

    def test_a_few_invalid_rows_do_not_stop_the_rest(self, db):
        rows = [row(id=str(i), slno=str(i), wo=f"00{i % 9}-23-0000{i}</a>Work {i}", amount="x" if i == 3 else "1000", nett="900", ded="100") for i in range(1, 30)]
        job, c = import_rows(db, make_csv(rows), ctx())
        assert c.failed == 1 and c.inserted == 28 and job.status == "done_with_errors"
