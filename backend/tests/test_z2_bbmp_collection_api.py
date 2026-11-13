"""BBMP pipeline, part 2: the collection workflow (discover -> download -> import), run history, retries, manual import, the scheduler, the API
and the dashboard numbers. A pretend website serves saved fixtures; nothing touches the network."""
import hashlib, io, os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func

from app import collection as col
from app import models as m
from app import scheduler as sched
from app.db import SessionLocal
from app.routers import collection as colr
from bbmp_helpers import (Site, DS, SLUG24, SLUG23, SLUG25, R24, R23, R25, dl_url, fixture, only_config, wipe)

BASE = "/api/gov/collection"


@pytest.fixture()
def db(client):
    d = SessionLocal()
    wipe(d)
    yield d
    wipe(d)
    d.close()


@pytest.fixture()
def setup(db, monkeypatch):
    """A pretend BBMP website plus a sources file naming the datasets under test."""
    site = Site().standard(only=("24",))
    cfg = {"value": only_config(SLUG24)}
    monkeypatch.setattr(col, "FETCHER_FACTORY", lambda: site.fetcher())
    monkeypatch.setattr(col, "load_sources", lambda *a, **k: cfg["value"])
    monkeypatch.setattr(colr, "load_sources", lambda *a, **k: cfg["value"])
    return site, cfg


def run(client, officer, **body):
    r = client.post(f"{BASE}/run", json=body, headers=officer)
    assert r.status_code == 200, r.text
    d = client.get(f"{BASE}/runs/{r.json()['run_id']}", headers=officer)
    assert d.status_code == 200
    return d.json()


def all_counts(db):
    db.rollback()
    return {"works": db.query(m.Roadwork).filter(m.Roadwork.data_origin == "bbmp_open_data").count(), "src": db.query(m.SourceRecord).filter(m.SourceRecord.external_id.isnot(None)).count(),
            "pay": db.query(m.Payment).filter(m.Payment.external_key.isnot(None)).count(), "bills": db.query(m.Bill).filter(m.Bill.external_key.isnot(None)).count(),
            "links": db.query(m.WorkSourceLink).join(m.SourceRecord, m.SourceRecord.id == m.WorkSourceLink.source_record_id).filter(m.SourceRecord.external_id.isnot(None)).count(), "files": db.query(m.SourceFile).count()}


# ============================================================================= the 2024-25 workflow
class TestWorkflow2024_25:
    def test_discovery_download_validation_insertion(self, client, officer, setup, db):
        site, _ = setup
        r = run(client, officer)
        assert r["trigger"] == "manual" and r["status"] == "done_with_errors" and r["finished_at"]
        assert (r["datasets_found"], r["files_found"], r["files_downloaded"], r["files_failed"], r["files_unchanged"]) == (1, 3, 2, 1, 0)
        files = {f["ward_regime"]: f for f in r["files"]}
        assert set(files) == {"198", "225", "243"}
        f = files["243"]
        # source tracking
        assert f["status"] == "imported" and f["fiscal_year"] == "2024-25" and f["file_format"] == "csv"
        assert f["source_website"] == DS and f["dataset_url"] == f"{DS}/dataset/{SLUG24}" and f["download_url"] == dl_url(R24["243"]) and f["original_filename"] == R24["243"][2]
        assert f["sha256"] == hashlib.sha256(fixture("wo_payments_2024_25_243.csv")).hexdigest() and f["size_bytes"] == len(fixture("wo_payments_2024_25_243.csv"))
        assert f["downloaded_at"] and f["imported_at"] and f["stored"] is True
        assert (f["rows_processed"], f["rows_inserted"], f["rows_duplicate"], f["rows_failed"]) == (41, 38, 1, 2)
        assert files["198"]["rows_processed"] == 15 and files["198"]["status"] == "imported"
        assert files["225"]["status"] == "download_failed" and "404" in files["225"]["error"]
        # records are in the existing tables
        n = all_counts(db)
        assert n["works"] > 0 and n["src"] == 38 + 15 and n["pay"] >= 50 and n["links"] == n["src"] and n["links"] > 0
        assert r["records_processed"] == 56 and r["records_inserted"] == 53 and r["records_failed"] == 2 and r["records_duplicate"] == 1
        assert db.query(m.ImportJob).filter_by(kind="bbmp_open_data").count() == 2
        # the stored copy is the downloaded file, byte for byte
        got = client.get(f"{BASE}/files/{f['id']}/download", headers=officer)
        assert got.status_code == 200 and got.content == fixture("wo_payments_2024_25_243.csv")

    def test_run_log_explains_what_happened(self, client, officer, setup):
        r = run(client, officer)
        text = " | ".join(x["message"] for x in r["log"])
        assert "3 file(s) listed" in text and "Downloaded" in text and "Download of" in text and "404" in text
        assert any(x["level"] == "error" for x in r["log"])

    def test_dashboard_shows_the_imported_records(self, client, officer, setup, db):
        run(client, officer)
        s = client.get(f"{BASE}/summary", headers=officer).json()
        t = s["totals"]
        assert t["works"] == db.query(m.Roadwork).filter_by(data_origin="bbmp_open_data").count()
        assert t["payments"] == db.query(m.Payment).filter(m.Payment.external_key.isnot(None), m.Payment.duplicate_of_id.is_(None)).count()
        assert t["paid_net"] == pytest.approx(db.query(func.sum(m.Payment.amount)).filter(m.Payment.external_key.isnot(None)).scalar())
        assert [x["fiscal_year"] for x in s["by_fiscal_year"]] == ["2024-25"]
        assert {x["file_id"] for x in s["sources"]} and all(x["download_url"] and x["fiscal_year"] == "2024-25" for x in s["sources"])
        assert "Records collected from BBMP open data" in s["origin"]["label"]


# ============================================================================= repeating the collection
class TestRepeatedRuns:
    def test_second_run_downloads_and_imports_nothing(self, client, officer, setup, db):
        site, _ = setup
        run(client, officer)
        before = all_counts(db)
        r2 = run(client, officer)
        assert (r2["files_downloaded"], r2["files_unchanged"], r2["records_processed"], r2["records_inserted"]) == (0, 2, 0, 0)
        after = all_counts(db)
        assert {k: v for k, v in after.items() if k != "files"} == {k: v for k, v in before.items() if k != "files"}
        # conditional requests were used: the server was asked "has it changed?" and said not modified
        csv_requests = [h for u, h in site.requests if u == dl_url(R24["243"])]
        assert len(csv_requests) == 2 and "if-none-match" in csv_requests[1]
        assert db.query(m.ImportJob).filter_by(kind="bbmp_open_data").count() == 2

    def test_unchanged_is_detected_by_checksum_when_the_server_gives_no_etag(self, client, officer, monkeypatch, db):
        site = Site(etags=False).standard(only=("24",))
        monkeypatch.setattr(col, "FETCHER_FACTORY", lambda: site.fetcher())
        monkeypatch.setattr(col, "load_sources", lambda *a, **k: only_config(SLUG24))
        run(client, officer)
        before = all_counts(db)
        r2 = run(client, officer)
        assert r2["files_unchanged"] == 2 and r2["files_downloaded"] == 0 and r2["records_inserted"] == 0
        assert site.hits[dl_url(R24["243"])] == 2   # downloaded again, recognised by its checksum, not imported
        assert all_counts(db)["src"] == before["src"] and all_counts(db)["files"] == before["files"]

    def test_force_reimports_but_creates_no_duplicates(self, client, officer, setup, db):
        run(client, officer)
        before = all_counts(db)
        r = run(client, officer, force=True)
        assert r["records_inserted"] == 0 and r["records_duplicate"] > 0
        assert {k: v for k, v in all_counts(db).items() if k != "files"} == {k: v for k, v in before.items() if k != "files"}

    def test_changed_file_upstream_is_a_new_file_and_updates_rows(self, client, officer, setup, db):
        site, _ = setup
        run(client, officer)
        text = fixture("wo_payments_2024_25_198.csv").decode().split("\n")
        first = text[1].split(",")
        first[-3] = str(int(first[-3]) + 1000)   # amount of the first row changes
        text[1] = ",".join(first)
        site.add(dl_url(R24["198"]), "\n".join(text))
        before = all_counts(db)
        r2 = run(client, officer)
        assert r2["files_downloaded"] == 1 and r2["files_unchanged"] == 1 and r2["records_updated"] == 1 and r2["records_inserted"] == 0
        assert db.query(m.SourceFile).filter_by(resource_id=R24["198"][0]).count() == 2    # both versions of the file are on record
        assert all_counts(db)["pay"] == before["pay"] and all_counts(db)["src"] == before["src"]

    def test_scheduled_run_goes_through_the_same_path(self, client, officer, setup, db):
        sched.run_scheduled()
        last = db.query(m.CollectionRun).order_by(m.CollectionRun.id.desc()).first()
        assert last.trigger == "scheduled" and last.status == "done_with_errors" and all_counts(db)["src"] == 53
        sched.run_scheduled()
        db.expire_all()
        assert db.query(m.CollectionRun).order_by(m.CollectionRun.id.desc()).first().records_inserted == 0


# ============================================================================= failures and retries
class TestFailures:
    def test_retry_fetches_what_failed_and_nothing_else(self, client, officer, setup, db):
        site, _ = setup
        run(client, officer)
        bad = db.query(m.SourceFile).filter_by(status="download_failed").one()
        site.add(dl_url(R24["225"]), fixture("wo_payments_2025_26_225.csv"))
        r = client.post(f"{BASE}/retry", json={}, headers=officer)
        assert r.status_code == 200 and r.json()["files"] == 1
        d = client.get(f"{BASE}/runs/{r.json()['run_id']}", headers=officer).json()
        assert d["trigger"] == "retry" and d["status"] == "done" and d["files_downloaded"] == 1 and d["files_failed"] == 0
        db.expire_all()
        files = db.query(m.SourceFile).filter_by(resource_id=R24["225"][0]).all()
        assert len(files) == 1 and files[0].status == "imported" and files[0].rows_inserted == 8   # the failed record was replaced by the good one
        assert site.hits[dl_url(R24["198"])] == 1   # the files that were fine were not touched

    def test_nothing_to_retry_is_a_clear_404(self, client, officer, setup):
        assert client.post(f"{BASE}/retry", json={}, headers=officer).status_code == 404

    def test_login_or_captcha_page_is_reported_and_not_worked_around(self, client, officer, setup, db):
        site, _ = setup
        site.add(dl_url(R24["243"]), fixture("login_page.html"))
        r = run(client, officer)
        f = {x["ward_regime"]: x for x in r["files"]}["243"]
        assert f["status"] == "download_failed" and "CAPTCHA" in f["error"] and "not bypassed" in f["error"]
        assert site.hits[dl_url(R24["243"])] == 1
        assert db.query(m.SourceRecord).filter(m.SourceRecord.source_file_id == f["id"]).count() == 0

    def test_forbidden_download_is_a_failure_with_a_reason(self, client, officer, setup):
        site, _ = setup
        site.add(dl_url(R24["198"]), "forbidden", status=403)
        r = run(client, officer)
        f = {x["ward_regime"]: x for x in r["files"]}["198"]
        assert f["status"] == "download_failed" and f["error"].startswith("blocked") and site.hits[dl_url(R24["198"])] == 1

    def test_a_file_with_the_wrong_layout_fails_alone_and_stores_nothing(self, client, officer, setup, db):
        site, _ = setup
        site.add(dl_url(R24["198"]), "alpha,beta\n1,2\n3,4\n")
        r = run(client, officer)
        f = {x["ward_regime"]: x for x in r["files"]}["198"]
        assert f["status"] == "import_failed" and "Columns not recognised" in f["error"] and f["rows_inserted"] in (0, None)
        ok = {x["ward_regime"]: x for x in r["files"]}["243"]
        assert ok["status"] == "imported"
        assert db.query(m.SourceRecord).filter(m.SourceRecord.source_file_id == f["id"]).count() == 0
        listed = client.get(f"{BASE}/files?status=import_failed", headers=officer).json()
        assert listed["total"] == 1 and listed["items"][0]["id"] == f["id"]
        # retrying the stored copy fails again, visibly, and does not duplicate anything
        n = all_counts(db)
        r2 = client.post(f"{BASE}/retry", json={"file_ids": [f["id"]]}, headers=officer).json()
        again = client.get(f"{BASE}/files/{f['id']}", headers=officer).json()
        assert again["status"] == "import_failed" and again["attempts"] == 2 and all_counts(db) == n

    def test_dataset_page_that_cannot_be_read_does_not_stop_other_datasets(self, client, officer, monkeypatch, db):
        site = Site().standard(only=("24", "23"))
        site.remove(f"{DS}/dataset/{SLUG24}")
        monkeypatch.setattr(col, "FETCHER_FACTORY", lambda: site.fetcher())
        monkeypatch.setattr(col, "load_sources", lambda *a, **k: only_config(SLUG24, SLUG23))
        r = run(client, officer)
        assert r["datasets_found"] == 1 and r["files_failed"] == 1 and any("could not be read" in x["message"] for x in r["log"])
        assert db.query(m.SourceFile).filter_by(status="imported").count() == 1

    def test_only_one_run_at_a_time(self, client, officer, setup, db):
        db.add(m.CollectionRun(trigger="manual", status="running", started_at=datetime.utcnow())); db.commit()
        r = client.post(f"{BASE}/run", json={}, headers=officer)
        assert r.status_code == 409 and "still running" in r.json()["detail"]

    def test_an_interrupted_run_does_not_block_forever(self, client, officer, setup, db):
        old = m.CollectionRun(trigger="manual", status="running", started_at=datetime.utcnow() - timedelta(hours=5)); db.add(old); db.commit()
        r = run(client, officer)
        assert r["status"] in ("done", "done_with_errors")
        db.expire_all()
        assert db.get(m.CollectionRun, old.id).status == "failed"


# ============================================================================= the other two years
def test_2023_24_and_2025_26_after_2024_25(client, officer, monkeypatch, db):
    site = Site().standard(only=("24", "23", "25"))
    monkeypatch.setattr(col, "FETCHER_FACTORY", lambda: site.fetcher())
    monkeypatch.setattr(col, "load_sources", lambda *a, **k: only_config(SLUG24, SLUG23, SLUG25))
    monkeypatch.setattr(colr, "load_sources", lambda *a, **k: only_config(SLUG24, SLUG23, SLUG25))
    r = run(client, officer)
    assert r["datasets_found"] == 3 and r["files_found"] == 3 + 1 + 4 and r["files_downloaded"] == 7 and r["files_failed"] == 1
    by = {(f["fiscal_year"], f["ward_regime"]): f for f in r["files"]}
    assert by[("2023-24", "198")]["status"] == "imported"                      # regime not named on the page: taken from the sources file
    assert by[("2025-26", "common")]["status"] == "imported" and by[("2025-26", "243")]["status"] == "imported"
    assert by[("2024-25", "225")]["status"] == "download_failed"
    s = client.get(f"{BASE}/summary", headers=officer).json()
    assert [x["fiscal_year"] for x in s["by_fiscal_year"]] == ["2023-24", "2024-25", "2025-26"]
    assert sum(x["paid"] for x in s["by_fiscal_year"]) == pytest.approx(s["totals"]["paid_net"])
    assert sum(x["payments"] for x in s["by_fiscal_year"]) == s["totals"]["payments"]
    st = client.get(f"{BASE}/status", headers=officer).json()
    assert st["last_run"]["id"] == r["id"] and st["last_successful_collection"] is None   # the run had one failure, so it is not "successful"
    assert st["records"]["works"] == s["totals"]["works"]


# ============================================================================= manual import
class TestManualImport:
    def upload(self, client, officer, name, data, **form):
        return client.post(f"{BASE}/manual-import", files={"file": (name, data, "text/csv")}, data=form, headers=officer)

    def test_file_import_uses_the_same_pipeline_and_history(self, client, officer, setup, db):
        r = self.upload(client, officer, "my_download.csv", fixture("wo_payments_2024_25_198.csv"), fiscal_year="2024-25", ward_regime="198", label="Downloaded by hand")
        assert r.status_code == 200, r.text
        f = r.json()["file"]
        assert f["manual"] and f["status"] == "imported" and f["rows_inserted"] == 15 and f["fiscal_year"] == "2024-25" and f["ward_regime"] == "198"
        assert r.json()["run"]["trigger"] == "manual_import"
        assert all_counts(db)["src"] == 15

    def test_same_file_twice_changes_nothing(self, client, officer, setup, db):
        self.upload(client, officer, "a.csv", fixture("wo_payments_2024_25_198.csv"), fiscal_year="2024-25", ward_regime="198")
        n = all_counts(db)
        r = self.upload(client, officer, "a.csv", fixture("wo_payments_2024_25_198.csv"), fiscal_year="2024-25", ward_regime="198")
        assert r.status_code == 200 and r.json()["run"]["files_unchanged"] == 1 and all_counts(db) == n

    def test_manual_file_overlapping_an_automatic_one_adds_no_duplicates(self, client, officer, setup, db):
        run(client, officer)
        before = all_counts(db)
        r = self.upload(client, officer, "copy.csv", fixture("wo_payments_2024_25_198.csv") + b"\n", fiscal_year="2024-25", ward_regime="198")
        assert r.status_code == 200
        after = all_counts(db)
        assert {k: v for k, v in after.items() if k != "files"} == {k: v for k, v in before.items() if k != "files"}

    @pytest.mark.parametrize("form,fragment", [({"ward_regime": "198"}, "financial year"), ({"fiscal_year": "2024-25"}, "ward regime")])
    def test_missing_year_or_regime_is_asked_for(self, client, officer, setup, form, fragment):
        r = self.upload(client, officer, "x.csv", fixture("wo_payments_2024_25_198.csv"), **form)
        assert r.status_code == 422 and fragment in r.json()["detail"]

    def test_year_and_regime_can_come_from_the_file_name(self, client, officer, setup, db):
        r = self.upload(client, officer, "bbmp-2024-25-198-wards.csv", fixture("wo_payments_2024_25_198.csv"))
        assert r.status_code == 200 and r.json()["file"]["fiscal_year"] == "2024-25" and r.json()["file"]["ward_regime"] == "198"

    def test_excel_file(self, client, officer, setup, db):
        r = client.post(f"{BASE}/manual-import", files={"file": ("a.xlsx", fixture("wo_payments_2024_25_243.xlsx"), "application/octet-stream")},
                        data={"fiscal_year": "2024-25", "ward_regime": "243"}, headers=officer)
        assert r.status_code == 200 and r.json()["file"]["file_format"] == "xlsx" and r.json()["file"]["rows_inserted"] == 10

    def test_not_a_data_file_is_refused(self, client, officer, setup):
        r = self.upload(client, officer, "page.html", fixture("login_page.html"), fiscal_year="2024-25", ward_regime="198")
        assert r.status_code == 422 and "not a CSV or Excel" in r.json()["detail"]

    def test_by_url(self, client, officer, setup, monkeypatch, db):
        site, _ = setup
        site.add("https://example.org/files/bbmp.csv", fixture("wo_payments_2024_25_198.csv"))
        monkeypatch.setattr(col, "FETCHER_FACTORY", lambda: site.fetcher(allowed_hosts=["data.opencity.in"]))
        r = client.post(f"{BASE}/manual-url", json={"url": "https://example.org/files/bbmp.csv", "fiscal_year": "2024-25", "ward_regime": "198"}, headers=officer)
        assert r.status_code == 200 and r.json()["file"]["download_url"] == "https://example.org/files/bbmp.csv" and r.json()["file"]["rows_inserted"] == 15

    @pytest.mark.parametrize("url", ["http://127.0.0.1/a.csv", "http://192.168.1.5/a.csv", "http://localhost:8000/a.csv", "ftp://example.org/a.csv"])
    def test_by_url_refuses_private_addresses_and_other_schemes(self, client, officer, setup, url):
        assert client.post(f"{BASE}/manual-url", json={"url": url, "fiscal_year": "2024-25", "ward_regime": "198"}, headers=officer).status_code == 422


# ============================================================================= scheduler
class TestSchedule:
    def test_off_by_default_and_configurable(self, client, admin, officer):
        try:
            assert client.get(f"{BASE}/schedule", headers=officer).json()["enabled"] is False
            r = client.put(f"{BASE}/schedule", json={"enabled": True, "cron": "30 2 * * 1", "timezone": "Asia/Kolkata"}, headers=admin)
            assert r.status_code == 200 and r.json()["enabled"] and r.json()["next_run"]
            nxt = datetime.fromisoformat(r.json()["next_run"])
            assert nxt.minute == 30 and nxt.hour == 2 and nxt.weekday() == 0
            assert client.get(f"{BASE}/schedule", headers=officer).json()["cron"] == "30 2 * * 1"
            assert sched.next_run_time() is not None
            assert client.put(f"{BASE}/schedule", json={"enabled": False}, headers=admin).json()["next_run"] is None
            assert sched.next_run_time() is None
        finally:
            client.put(f"{BASE}/schedule", json={"enabled": False}, headers=admin)
            sched.shutdown()

    @pytest.mark.parametrize("cron,tz", [("every day", "Asia/Kolkata"), ("61 * * * *", "Asia/Kolkata"), ("0 3 * * 0", "Nowhere/City")])
    def test_bad_schedule_is_refused_and_not_saved(self, client, admin, cron, tz):
        r = client.put(f"{BASE}/schedule", json={"enabled": True, "cron": cron, "timezone": tz}, headers=admin)
        assert r.status_code == 422 and client.get(f"{BASE}/schedule", headers=admin).json()["enabled"] is False

    def test_only_admin_changes_it(self, client, officer):
        assert client.put(f"{BASE}/schedule", json={"enabled": True}, headers=officer).status_code == 403

    @pytest.mark.parametrize("field,expected", [("0", "sun"), ("7", "sun"), ("1", "mon"), ("6", "sat"), ("1-5", "mon,tue,wed,thu,fri"), ("0,3", "sun,wed"), ("*", "*"),
                                                ("mon-fri", "mon-fri"), ("*/2", "sun,tue,thu,sat"), ("5-7", "sun,fri,sat")])
    def test_cron_weekdays_mean_what_cron_users_expect(self, field, expected):
        assert sched._dow_to_names(field) == expected

    def test_default_schedule_is_sunday(self):
        from zoneinfo import ZoneInfo
        t = sched.build_trigger(sched.DEFAULT_CRON, sched.DEFAULT_TZ)
        nxt = t.get_next_fire_time(None, datetime.now(ZoneInfo(sched.DEFAULT_TZ)))
        assert nxt.weekday() == 6 and (nxt.hour, nxt.minute) == (3, 0)

    def test_environment_defaults(self, monkeypatch):
        monkeypatch.setenv("BBMP_SCHEDULE_ENABLED", "1"); monkeypatch.setenv("BBMP_SCHEDULE_CRON", "15 4 * * *")
        s = sched.env_settings()
        assert s["enabled"] and s["cron"] == "15 4 * * *"
        assert sched.build_trigger(s["cron"], s["timezone"]) is not None


# ============================================================================= access, listings
class TestApiAccess:
    paths = ["/status", "/runs", "/files", "/records", "/issues", "/summary", "/payments", "/schedule"]

    @pytest.mark.parametrize("path", paths)
    def test_login_and_role_required(self, client, authority, officer, path):
        assert client.get(BASE + path).status_code == 401
        assert client.get(BASE + path, headers=authority).status_code == 403
        assert client.get(BASE + path, headers=officer).status_code == 200

    def test_starting_a_run_needs_the_officer_role(self, client, authority):
        assert client.post(f"{BASE}/run", json={}, headers=authority).status_code == 403
        assert client.post(f"{BASE}/run", json={}).status_code == 401


class TestListings:
    def test_history_files_records_issues_unmatched(self, client, officer, setup, db):
        run(client, officer)
        runs = client.get(f"{BASE}/runs", headers=officer).json()
        assert runs["total"] == 1 and runs["items"][0]["records_inserted"] == 53
        assert client.get(f"{BASE}/files?fiscal_year=2024-25", headers=officer).json()["total"] == 3
        assert client.get(f"{BASE}/files?status=imported", headers=officer).json()["total"] == 2
        recs = client.get(f"{BASE}/records?fiscal_year=2024-25&page_size=200", headers=officer).json()
        assert recs["total"] == 53 and all(r["raw"] and r["normalised"] and r["external_id"].startswith("bbmp|2024-25|") for r in recs["items"])
        one = next(r for r in recs["items"] if r["payment"])
        assert one["work"]["id"] and one["payment"]["amount"] > 0 and one["payment"]["counted"] is True
        assert client.get(f"{BASE}/records?q=borewells", headers=officer).json()["total"] > 0
        failed = client.get(f"{BASE}/issues?kind=failed", headers=officer).json()
        assert failed["total"] == 2 and {i["code"] for i in failed["items"]} == {"bad_amount", "negative_amount"} and all(i["raw"] for i in failed["items"])
        assert client.get(f"{BASE}/issues?kind=duplicate", headers=officer).json()["total"] == 1
        assert client.get(f"{BASE}/issues?kind=warning&code=amount_mismatch", headers=officer).json()["total"] == 1
        # a row with no work-order code was kept and given its own work; a row with nothing to identify a work would be listed here
        assert client.get(f"{BASE}/records?unmatched=true", headers=officer).json()["total"] == 0
        assert client.get(f"{BASE}/runs/9999", headers=officer).status_code == 404

    def test_unmatched_source_rows_are_listed(self, client, officer, setup, db):
        csv_ = b"slno,id,Ward,wodetails,contractor,brnumber,amount,nett,deduction\n1,1,3,,,BR - 1 / 01-Jan-2024CBR - / Rtgs - 2 / 02-Jan-2024,1000,900,100\n"
        client.post(f"{BASE}/manual-import", files={"file": ("u.csv", csv_, "text/csv")}, data={"fiscal_year": "2024-25", "ward_regime": "243"}, headers=officer)
        u = client.get(f"{BASE}/records?unmatched=true", headers=officer).json()
        assert u["total"] == 1 and u["items"][0]["work"] is None
        s = client.get(f"{BASE}/summary", headers=officer).json()
        assert s["totals"]["unmatched_source_records"] == 1 and s["unmatched"][0]["raw"]["amount"] == "1000"


# ============================================================================= dashboard
@pytest.fixture()
def loaded(client, officer, monkeypatch, db):
    site = Site().standard(only=("24", "23", "25"))
    cfg = only_config(SLUG24, SLUG23, SLUG25)
    monkeypatch.setattr(col, "FETCHER_FACTORY", lambda: site.fetcher())
    monkeypatch.setattr(col, "load_sources", lambda *a, **k: cfg)
    monkeypatch.setattr(colr, "load_sources", lambda *a, **k: cfg)
    run(client, officer)
    return site


class TestDashboardNumbers:
    def db_totals(self, db, **cond):
        R, P = m.Roadwork, m.Payment
        works = db.query(R).filter(R.data_origin == "bbmp_open_data")
        ids = [w.id for w in works]
        pay = db.query(func.sum(P.amount), func.count(P.id)).filter(P.roadwork_id.in_(ids), P.duplicate_of_id.is_(None)).one()
        return len(ids), float(pay[0] or 0), pay[1]

    def test_summary_equals_the_database(self, client, officer, loaded, db):
        s = client.get(f"{BASE}/summary", headers=officer).json()
        works, paid, n_pay = self.db_totals(db)
        assert (s["totals"]["works"], s["totals"]["paid_net"], s["totals"]["payments"]) == (works, pytest.approx(paid), n_pay)
        bills = db.query(func.sum(m.Bill.amount), func.sum(m.Bill.deduction)).filter(m.Bill.external_key.isnot(None)).one()
        assert s["totals"]["billed_gross"] == pytest.approx(bills[0]) and s["totals"]["deductions"] == pytest.approx(bills[1])
        assert sum(x["works"] for x in s["by_ward"]) <= works and s["totals"]["wards"] >= len(s["by_ward"]) - 5
        by_w = client.get(f"{BASE}/summary", headers=officer).json()["by_ward"]
        assert all(x["works"] > 0 for x in by_w)

    def test_group_totals_add_up(self, client, officer, loaded, db):
        s = client.get(f"{BASE}/summary", headers=officer).json()
        assert sum(x["billed"] for x in s["by_fiscal_year"]) == pytest.approx(s["totals"]["billed_gross"])
        assert sum(x["paid"] for x in s["by_fiscal_year"]) == pytest.approx(s["totals"]["paid_net"])
        assert sum(c["paid"] for c in s["contractors"]) <= s["totals"]["paid_net"] + 0.01
        top = s["contractors"][0]
        assert db.query(func.count(m.Roadwork.id)).filter(m.Roadwork.contractor_id == top["id"]).scalar() == top["works"]

    def test_estimated_and_approved_amounts_are_reported_as_not_published(self, client, officer, loaded):
        t = client.get(f"{BASE}/summary", headers=officer).json()["totals"]
        assert t["estimated_amount"] == 0 and t["works_with_estimated_amount"] == 0 and t["approved_amount"] == 0 and t["works_with_approved_amount"] == 0
        notes = " ".join(client.get(f"{BASE}/summary", headers=officer).json()["notes"])
        assert "no estimated or approved amounts" in notes

    def test_missing_information_counts(self, client, officer, loaded, db):
        s = client.get(f"{BASE}/summary", headers=officer).json()
        R = m.Roadwork
        base = db.query(func.count(R.id)).filter(R.data_origin == "bbmp_open_data")
        assert s["missing"]["no_contractor"] == base.filter(R.contractor_id.is_(None)).scalar() >= 1
        assert s["missing"]["no_estimated_amount"] == s["totals"]["works"] and s["missing"]["no_work_order_date"] == s["totals"]["works"]
        assert s["missing"]["non_numeric_ward"] >= 1

    def test_filters_use_the_same_records(self, client, officer, loaded, db):
        R = m.Roadwork
        # financial year: a work belongs to a year when a row of its was published in that year's file
        s = client.get(f"{BASE}/summary?fiscal_year=2025-26", headers=officer).json()
        n = db.query(func.count(func.distinct(m.WorkSourceLink.roadwork_id))).join(m.SourceRecord, m.SourceRecord.id == m.WorkSourceLink.source_record_id) \
            .filter(m.SourceRecord.fiscal_year == "2025-26", m.SourceRecord.external_id.like("bbmp|%")).scalar()
        assert s["totals"]["works"] == n and [x["fiscal_year"] for x in s["by_fiscal_year"]] == ["2025-26"]
        paid = db.query(func.sum(m.Payment.amount)).join(m.SourceRecord, m.SourceRecord.id == m.Payment.source_record_id) \
            .filter(m.SourceRecord.fiscal_year == "2025-26", m.Payment.duplicate_of_id.is_(None)).scalar()
        assert s["totals"]["paid_net"] == pytest.approx(paid)
        # ward + regime
        w = db.query(R.ward_number).filter(R.data_origin == "bbmp_open_data", R.ward_regime == "243").first()[0]
        sw = client.get(f"{BASE}/summary?ward=243:{w}", headers=officer).json()
        assert sw["totals"]["works"] == db.query(func.count(R.id)).filter(R.data_origin == "bbmp_open_data", R.ward_regime == "243", R.ward_number == w).scalar()
        # contractor
        c = client.get(f"{BASE}/summary", headers=officer).json()["contractors"][0]
        sc = client.get(f"{BASE}/summary?contractor={c['name'][:6]}", headers=officer).json()
        assert sc["totals"]["works"] >= c["works"]

    def test_existing_dashboard_shows_the_collected_works_and_agrees(self, client, officer, loaded, db):
        s = client.get(f"{BASE}/summary", headers=officer).json()
        d = client.get("/api/gov/dashboard?origin=collected", headers=officer).json()
        assert d["totals"]["roadworks"] == s["totals"]["works"] == d["totals"]["collected_roadworks"]
        assert d["totals"]["paid_amount_recorded"] == pytest.approx(s["totals"]["paid_net"]) and d["totals"]["payments"] == s["totals"]["payments"]
        assert d["totals"]["sample_roadworks"] == 0
        dy = client.get("/api/gov/dashboard?origin=collected&fiscal_year=2024-25", headers=officer).json()
        sy = client.get(f"{BASE}/summary?fiscal_year=2024-25", headers=officer).json()
        assert dy["totals"]["roadworks"] == sy["totals"]["works"] and dy["totals"]["paid_amount_recorded"] == pytest.approx(sy["totals"]["paid_net"])
        lst = client.get("/api/gov/roadworks?origin=collected&fiscal_year=2024-25&page_size=5", headers=officer).json()
        assert lst["total"] == sy["totals"]["works"] and lst["totals"]["paid_amount_recorded"] == pytest.approx(sy["totals"]["paid_net"])
        assert "2024-25" in client.get("/api/gov/filters", headers=officer).json()["fiscal_years"]
        assert [j["kind"] for j in d["imports"]][0] == "bbmp_open_data"

    def test_sample_data_is_never_counted_as_collected(self, client, officer, admin, seeded, loaded, db):
        d_all = client.get("/api/gov/dashboard", headers=officer).json()
        assert d_all["totals"]["sample_roadworks"] > 0 and d_all["totals"]["collected_roadworks"] > 0
        d_col = client.get("/api/gov/dashboard?origin=collected", headers=officer).json()
        d_sam = client.get("/api/gov/dashboard?origin=sample", headers=officer).json()
        assert d_col["totals"]["sample_roadworks"] == 0 and d_sam["totals"]["collected_roadworks"] == 0
        assert d_col["totals"]["roadworks"] + d_sam["totals"]["roadworks"] <= d_all["totals"]["roadworks"]
        s = client.get(f"{BASE}/summary", headers=officer).json()
        assert s["totals"]["works"] == d_col["totals"]["roadworks"]
        assert all(x["download_url"] for x in s["sources"])

    def test_payment_history_and_work_profile(self, client, officer, loaded, db):
        hist = client.get(f"{BASE}/payments?page_size=200", headers=officer).json()
        s = client.get(f"{BASE}/summary", headers=officer).json()
        assert hist["total"] == s["totals"]["payments"] and all(p["fiscal_year"] and p["reference"].startswith("Rtgs ") for p in hist["items"])
        p0 = hist["items"][0]
        prof = client.get(f"/api/gov/roadworks/{p0['work_id']}", headers=officer).json()
        pay = next(p for p in prof["payments"] if p["id"] == p0["id"])
        assert pay["gross_amount"] >= pay["amount"] and pay["deduction"] is not None
        src = prof["sources"][0]
        assert src["file"]["download_url"].startswith("https://data.opencity.in/") and src["file"]["sha256"] and src["fiscal_year"] and src["raw"]["wodetails"]
        assert prof["values"]["data_origin"] == "bbmp_open_data" and prof["values"]["external_key"].startswith("bbmp:")

    def test_officer_verified_value_survives_the_next_collection(self, client, officer, loaded, db):
        w = db.query(m.Roadwork).filter(m.Roadwork.data_origin == "bbmp_open_data", m.Roadwork.work_order_id.isnot(None)).first()
        r = client.patch(f"/api/gov/roadworks/{w.id}/fields", json={"description": "Checked on site: drain desilting"}, headers=officer)
        assert r.status_code == 200 and r.json()["verified_fields"] == ["description"]
        assert client.patch(f"/api/gov/roadworks/{w.id}/fields", json={"password": "x"}, headers=officer).status_code == 422
        # the publisher changes the text of one of that work's rows; a forced re-collection must not touch the verified description
        row = db.query(m.SourceRecord).join(m.WorkSourceLink, m.WorkSourceLink.source_record_id == m.SourceRecord.id).filter(m.WorkSourceLink.roadwork_id == w.id).first()
        f = db.get(m.SourceFile, row.source_file_id)
        text = open(f.stored_path, encoding="utf-8").read().replace(w.description[:25], "Something else entirely 25c")
        loaded.add(f.download_url, text)
        client.post(f"{BASE}/run", json={}, headers=officer)
        db.expire_all()
        assert db.get(m.Roadwork, w.id).description == "Checked on site: drain desilting"
        assert client.delete(f"/api/gov/roadworks/{w.id}/fields/description", headers=officer).json()["verified_fields"] == []
