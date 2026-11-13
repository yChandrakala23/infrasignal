"""Requirement 5: filters apply the same way to every metric, chart and table; payments are counted once; totals equal the database."""
import io
from datetime import datetime
from app import models as m
from app.db import SessionLocal
from pdfmaker import make_pdf
from test_sources_provenance import post_import


def setup_module(_):
    pass


def jobs(client, officer, qs):
    r = client.get("/api/gov/roadworks?page_size=200&" + qs, headers=officer).json()
    return {x["job_number"] for x in r["items"] if (x["job_number"] or "").startswith("DASH/")}


def dash(client, officer, qs=""):
    r = client.get("/api/gov/dashboard?" + qs, headers=officer)
    assert r.status_code == 200, r.text
    return r.json()


def test_fixture_works_for_filter_tests(client, officer):
    post_import(client, officer, b"Job No,Work Description,Ward,Department,Contractor,Work Order Amount,Work Order Date,Paid Amount,Payment Date\n"
                                 b"DASH/A1,Asphalting of Dash Alpha Road,77,Dash Engineering,M/s Dash Alpha Co,1000000,2023-05-10,400000,2023-06-01\n"
                                 b"DASH/A2,Asphalting of Dash Beta Road,077,DASH ENGINEERING ,M/s Dash Alpha Co,2000000,2024-03-31,,\n", "dash243.csv")
    post_import(client, officer, b"Job No,Work Description,Ward,Department,Contractor,Work Order Amount,Work Order Date,Paid Amount,Payment Date\n"
                                 b"DASH/B1,Asphalting of Dash Gamma Road,77,dash engineering,M/s Dash Beta Co,500000,2024-04-01,100000,2024-05-01\n",
                "dash198.csv", ward_regime="198")
    db = SessionLocal()
    a2 = db.query(m.Roadwork).filter_by(job_number="DASH/A2").one(); a2.fiscal_year = None      # no stored label: the order date decides
    b1 = db.query(m.Roadwork).filter_by(job_number="DASH/B1").one(); b1.fiscal_year = "2024-2025"   # another way of writing the label (the column holds 10 characters)
    db.commit()
    assert {x for x in jobs(client, officer, "id_search=DASH/")} == {"DASH/A1", "DASH/A2", "DASH/B1"}


# ----------------------------------------------------------------------------- the filters
def test_ward_filter_respects_the_ward_map_and_ignores_leading_zeros(client, officer):
    assert jobs(client, officer, "id_search=DASH/&ward=243:77") == {"DASH/A1", "DASH/A2"}
    assert jobs(client, officer, "id_search=DASH/&ward=198:77") == {"DASH/B1"}
    assert jobs(client, officer, "id_search=DASH/&ward=77") == {"DASH/A1", "DASH/A2", "DASH/B1"}
    assert jobs(client, officer, "id_search=DASH/&ward=077") == {"DASH/A1", "DASH/A2", "DASH/B1"}, "ward 077 stored on a work still matches ward 77"
    assert jobs(client, officer, "id_search=DASH/&ward=77&regime=198") == {"DASH/B1"}
    d = dash(client, officer, "id_search=DASH/&ward=243:77")["totals"]
    assert (d["roadworks"], d["awarded_amount"], d["paid_amount_recorded"], d["payments"]) == (2, 3000000, 400000, 1)
    assert dash(client, officer, "id_search=DASH/&ward=198:77")["totals"]["awarded_amount"] == 500000


def test_filter_options_offer_each_ward_map_separately(client, officer):
    o = client.get("/api/gov/filters", headers=officer).json()
    w77 = [w for w in o["wards"] if w["number"] == "77"]
    assert {w["value"] for w in w77} >= {"243:77", "198:77"} and len({w["value"] for w in o["wards"]}) == len(o["wards"])


def test_department_filter_ignores_case_and_spaces(client, officer):
    for spelling in ("Dash Engineering", "DASH ENGINEERING", "dash engineering ", "  Dash   Engineering"):
        assert jobs(client, officer, "id_search=DASH/&department=" + spelling.replace(" ", "%20")) == {"DASH/A1", "DASH/A2", "DASH/B1"}, spelling
    o = client.get("/api/gov/filters", headers=officer).json()
    assert len([d for d in o["departments"] if d.lower() == "dash engineering"]) == 1, "one entry for three spellings"
    d = dash(client, officer, "id_search=DASH/")
    assert [x["count"] for x in d["works_by_department"] if x["key"].lower() == "dash engineering"] == [3]


def test_fiscal_year_uses_the_label_or_else_the_order_date(client, officer):
    assert jobs(client, officer, "id_search=DASH/&fiscal_year=2023-24") == {"DASH/A1", "DASH/A2"}, "A2 has no label; 31 Mar 2024 is still FY 2023-24"
    assert jobs(client, officer, "id_search=DASH/&fiscal_year=2024-25") == {"DASH/B1"}, "stored as '2024-2025'"
    assert jobs(client, officer, "id_search=DASH/&fiscal_year=2024-2025") == {"DASH/B1"}
    assert jobs(client, officer, "id_search=DASH/&fiscal_year=2019-20") == set()
    assert dash(client, officer, "id_search=DASH/&fiscal_year=2023-24")["totals"]["awarded_amount"] == 3000000
    fy = client.get("/api/gov/filters", headers=officer).json()["fiscal_years"]
    assert "2023-24" in fy and "2024-25" in fy and len(fy) == len(set(fy))


def test_date_range_includes_both_end_days(client, officer):
    assert jobs(client, officer, "id_search=DASH/&date_from=2023-05-10&date_to=2023-05-10") == {"DASH/A1"}
    assert jobs(client, officer, "id_search=DASH/&date_from=2024-03-31&date_to=2024-04-01") == {"DASH/A2", "DASH/B1"}


def test_contractor_and_amount_filters_reach_the_dashboard_too(client, officer):
    assert jobs(client, officer, "id_search=DASH/&contractor=Dash Alpha") == {"DASH/A1", "DASH/A2"}
    d = dash(client, officer, "id_search=DASH/&contractor=Dash%20Alpha")["totals"]
    assert (d["roadworks"], d["contractors"], d["awarded_amount"]) == (2, 1, 3000000)
    assert jobs(client, officer, "id_search=DASH/&min_amount=1500000") == {"DASH/A2"}
    assert dash(client, officer, "id_search=DASH/&min_amount=1500000")["totals"]["roadworks"] == 1, "the dashboard applies the amount range like the table"
    assert dash(client, officer, "id_search=DASH/&max_amount=600000")["totals"]["awarded_amount"] == 500000


# ----------------------------------------------------------------------------- totals equal the database
def reference(db, keep):
    """Independent recomputation in plain Python from the raw tables."""
    works = [w for w in db.query(m.Roadwork) if w.verification_status in (None, "official", "confirmed") and keep(w)]
    ids = {w.id for w in works}
    pays = [p for p in db.query(m.Payment) if p.roadwork_id in ids and p.duplicate_of_id is None]
    return {"roadworks": len(works), "awarded": sum((w.awarded_amount or w.sanctioned_amount or 0) for w in works),
            "paid": sum(p.amount for p in pays), "payments": len(pays),
            "documents": sum(1 for d in db.query(m.Document) if d.roadwork_id in ids),
            "contractors": len({w.contractor_id for w in works if w.contractor_id})}


def test_dashboard_numbers_equal_a_recount_of_the_raw_records(client, seeded, officer):
    db = SessionLocal()
    wards = sorted({w.ward_number for w in db.query(m.Roadwork) if w.ward_number})[:2]
    cases = [("", lambda w: True),
             (f"ward={wards[0]}", lambda w: (w.ward_number or "").lstrip("0") == wards[0].lstrip("0")),
             ("regime=243", lambda w: w.ward_regime == "243"),
             ("contractor=Dash", lambda w: w.contractor_id and (db.get(m.Contractor, w.contractor_id).name or "").lower().find("dash") >= 0),
             ("fiscal_year=2023-24", lambda w: (w.fiscal_year == "2023-24") or (not w.fiscal_year and w.work_order_date and datetime(2023, 4, 1) <= w.work_order_date < datetime(2024, 4, 1))),
             ("department=dash engineering", lambda w: (w.department or "").strip().lower() == "dash engineering")]
    for qs, keep in cases:
        t = dash(client, officer, qs)["totals"]
        ref = reference(db, keep)
        assert (t["roadworks"], t["awarded_amount"], t["paid_amount_recorded"], t["payments"], t["documents"], t["contractors"]) == \
               (ref["roadworks"], ref["awarded"], ref["paid"], ref["payments"], ref["documents"], ref["contractors"]), qs


def test_charts_and_tables_add_up_to_the_same_works(client, seeded, officer):
    for qs in ("", "regime=243", "department=dash engineering"):
        d = dash(client, officer, qs)
        n = d["totals"]["roadworks"]
        assert sum(x["count"] for x in d["works_by_status"]) == n and sum(x["count"] for x in d["works_by_category"]) == n
        assert sum(x["count"] for x in d["works_by_department"]) == n
        assert sum(x["count"] for x in d["works_by_ward"]) <= n and sum(x["count"] for x in d["works_by_month"]) <= n
        lst = client.get("/api/gov/roadworks?page_size=1&" + qs, headers=officer).json()
        assert lst["total"] == n and lst["totals"]["awarded_amount"] == d["totals"]["awarded_amount"] and lst["totals"]["paid_amount_recorded"] == d["totals"]["paid_amount_recorded"]


def test_documents_and_tenders_follow_the_filters(client, seeded, officer):
    db = SessionLocal()
    d_all = dash(client, officer)
    assert d_all["totals"]["documents"] == sum(x["count"] for x in d_all["documents"]["by_type"]) == sum(d_all["documents"]["by_match_status"].values())
    assert d_all["unscoped"]["all_documents"] == db.query(m.Document).count()
    assert d_all["totals"]["documents"] + d_all["unscoped"]["documents_without_a_work"] <= d_all["unscoped"]["all_documents"], "linked + unlinked never exceeds the total"
    none = dash(client, officer, "ward=243:9999")
    assert none["totals"]["roadworks"] == none["totals"]["documents"] == none["totals"]["tenders"] == none["totals"]["payments"] == 0
    assert none["totals"]["paid_amount_recorded"] == 0 and none["documents"]["by_type"] == []
    assert none["unscoped"]["all_documents"] == d_all["unscoped"]["all_documents"], "unattached records are reported, labelled, and not filtered"
    one = dash(client, officer, "id_search=BBMP")  # whatever matches: documents counted are only those linked to those works
    ids = {w.id for w in db.query(m.Roadwork).filter(m.Roadwork.job_number.ilike("%BBMP%"), m.Roadwork.verification_status.in_(["official", "confirmed"]))}
    assert one["totals"]["documents"] == db.query(m.Document).filter(m.Document.roadwork_id.in_(ids)).count()


def test_provisional_works_are_counted_beside_not_inside_the_totals(client, officer):
    d = dash(client, officer)
    prov = client.get("/api/gov/roadworks?verification=provisional", headers=officer).json()["total"]
    assert d["totals"]["provisional_works_not_counted"] == prov
    assert dash(client, officer, "verification=all")["totals"]["roadworks"] == d["totals"]["roadworks"] + prov


def test_contractor_list_counts_only_official_works(client, officer):
    r = client.get("/api/gov/contractors?q=Dash Alpha", headers=officer).json()
    assert [(c["name"], c["works"], c["awarded"]) for c in r] == [("Dash Alpha Co", 2, 3000000.0)]


# ----------------------------------------------------------------------------- payments counted once
def test_same_payment_in_a_spreadsheet_and_a_payment_pdf_is_counted_once(client, officer, tmp_path):
    post_import(client, officer, b"Job No,Work Description,Ward,Contractor,Work Order Amount,Paid Amount,Payment Date\n"
                                 b"DASH/P1,Asphalting of Dash Payment Road,77,M/s Dash Pay Co,900000,250000,2025-02-10\n", "dashpay.csv")
    pdf = make_pdf(tmp_path / "advice.pdf", "PAYMENT ADVICE", ["BBMP Job No: DASH/P1", "Contractor: M/s Dash Pay Co", "Amount Paid: Rs. 2,50,000", "Payment Date: 10/02/2025"])
    r = client.post("/api/gov/documents", headers=officer, files=[("files", ("advice.pdf", pdf.read_bytes(), "application/pdf"))]).json()["results"][0]
    assert r["match_status"] == "auto"
    db = SessionLocal()
    w = db.query(m.Roadwork).filter_by(job_number="DASH/P1").one()
    pays = db.query(m.Payment).filter_by(roadwork_id=w.id).order_by(m.Payment.id).all()
    assert [(p.amount, p.document_id is not None, p.duplicate_of_id) for p in pays] == [(250000, False, None), (250000, True, pays[0].id)], "both records kept, the second marked as a repeat"
    lst = client.get("/api/gov/roadworks?id_search=DASH/P1", headers=officer).json()
    assert lst["items"][0]["paid"] == 250000 and lst["totals"]["paid_amount_recorded"] == 250000
    t = dash(client, officer, "id_search=DASH/P1")["totals"]
    assert (t["paid_amount_recorded"], t["payments"], t["repeat_payment_records_excluded"], t["repeat_payment_amount_excluded"]) == (250000, 1, 1, 250000)
    p = client.get(f"/api/gov/roadworks/{w.id}", headers=officer).json()
    assert p["payment_summary"]["repeat_records_excluded"] == 1 and sum(x["amount"] for x in p["payments"] if x["counted"]) == 250000
    # the document is also kept as a source on the work; uploading the same bytes again changes nothing
    again = client.post("/api/gov/documents", headers=officer, files=[("files", ("advice.pdf", pdf.read_bytes(), "application/pdf"))]).json()["results"][0]
    assert again["duplicate_of_existing"] is True
    assert dash(client, officer, "id_search=DASH/P1")["totals"]["paid_amount_recorded"] == 250000
    # an officer can say it was a genuine second payment: then it counts
    pid = pays[1].id
    assert client.patch(f"/api/gov/payments/{pid}", headers=officer, data={"duplicate_of": ""}).status_code == 200
    assert dash(client, officer, "id_search=DASH/P1")["totals"]["paid_amount_recorded"] == 500000
    assert client.patch(f"/api/gov/payments/{pid}", headers=officer, data={"duplicate_of": str(pays[0].id)}).status_code == 200
    assert dash(client, officer, "id_search=DASH/P1")["totals"]["paid_amount_recorded"] == 250000


def test_two_equal_lines_in_one_file_and_undated_payments_are_not_merged(client, officer):
    post_import(client, officer, b"Job No,Work Description,Ward,Contractor,Work Order Amount,Paid Amount,Payment Date\n"
                                 b"DASH/P2,Asphalting of Dash Twice Road,77,M/s Dash Pay Co,900000,100000,2025-03-01\n"
                                 b"DASH/P2,Asphalting of Dash Twice Road,77,M/s Dash Pay Co,900000,100000,2025-03-05\n", "dashtwice.csv")
    t = dash(client, officer, "id_search=DASH/P2")["totals"]
    assert (t["payments"], t["paid_amount_recorded"], t["repeat_payment_records_excluded"]) == (2, 200000, 0), "different dates are different payments"


def test_documents_and_payments_do_not_multiply_each_other(client, officer, tmp_path):
    post_import(client, officer, b"Job No,Work Description,Ward,Contractor,Work Order Amount,Paid Amount,Payment Date\n"
                                 b"DASH/M1,Asphalting of Dash Multiply Road,77,M/s Dash Multi Co,800000,100000,2025-04-01\n"
                                 b"DASH/M1,Asphalting of Dash Multiply Road,77,M/s Dash Multi Co,800000,200000,2025-05-01\n", "dashmulti.csv")
    for i, (title, extra) in enumerate((("WORK ORDER", "Work Order No: DASH/WO/M1"), ("WORK ORDER", "Work Order Amount: Rs. 8,00,000"), ("COMPLETION CERTIFICATE", "Certified complete"))):
        pdf = make_pdf(tmp_path / f"m{i}.pdf", title, ["BBMP Job No: DASH/M1", extra, f"Reference copy {i}"])
        client.post("/api/gov/documents", headers=officer, files=[("files", (f"m{i}.pdf", pdf.read_bytes(), "application/pdf"))])
    db = SessionLocal()
    w = db.query(m.Roadwork).filter_by(job_number="DASH/M1").one()
    ndocs = db.query(m.Document).filter_by(roadwork_id=w.id).count()
    assert ndocs >= 2
    row = client.get("/api/gov/roadworks?id_search=DASH/M1", headers=officer).json()["items"][0]
    assert row["paid"] == 300000 and row["documents"] == ndocs
    t = dash(client, officer, "id_search=DASH/M1")["totals"]
    assert (t["roadworks"], t["awarded_amount"], t["paid_amount_recorded"], t["documents"]) == (1, 800000, 300000, ndocs)
    c = [x for x in client.get("/api/gov/contractors?q=Dash Multi", headers=officer).json()]
    assert c[0]["works"] == 1 and c[0]["awarded"] == 800000


def test_payment_date_column_is_not_mistaken_for_the_work_order_date():
    from app.ingest import guess_mapping
    mp = guess_mapping(["Job No", "Work Description", "Ward Name", "Paid Amount", "Payment Date"])
    assert mp["payment_date"] == "Payment Date" and "date" not in mp, "without a work-order date column, 'date' stays unmapped"
    assert mp["ward_name"] == "Ward Name" and "ward_number" not in mp
    mp = guess_mapping(["Job No", "Ward", "Ward Name", "Work Order Date", "Payment Date", "Bill Date"])
    assert (mp["date"], mp["payment_date"], mp["bill_date"], mp["ward_number"], mp["ward_name"]) == ("Work Order Date", "Payment Date", "Bill Date", "Ward", "Ward Name")
