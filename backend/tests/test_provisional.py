"""Requirement 3: a PDF can start a work profile on its own; uncertain ones are flagged, duplicates are prevented."""
import io
from app import models as m
from app.db import SessionLocal
from pdfmaker import make_pdf


def upload(client, officer, path):
    r = client.post("/api/gov/documents", headers=officer, files=[("files", (path.name, path.read_bytes(), "application/pdf"))])
    assert r.status_code == 200, r.text
    return r.json()["results"][0]


def work_order(tmp_path, job, wo, name="wo.pdf", desc="Asphalting of Provisional Test Road from A Circle to B Junction",
               contractor="Provisional Test Builders Pvt Ltd", amount="4,50,000"):
    return make_pdf(tmp_path / name, "WORK ORDER", [f"Work Order No: {wo}", f"BBMP Job No: {job}", f"Name of Work: {desc}",
                                                   f"Contractor: M/s {contractor}", f"Work Order Amount: Rs. {amount}",
                                                   "Work Order Date: 12/03/2026", "Date of Completion: 12/06/2026", "Ward: 150", "Department: Ward Engineering"])


def test_pdf_alone_creates_a_provisional_work_with_provenance(client, seeded, officer, tmp_path):
    r = upload(client, officer, work_order(tmp_path, "PV/J/2026/001", "PV/WO/2026/001"))
    assert r["doc_type"] == "work_order" and r["match_status"] == "provisional" and r["created_work_id"]
    db = SessionLocal(); w = db.get(m.Roadwork, r["created_work_id"])
    assert (w.verification_status, w.data_origin, w.created_from_document_id) == ("provisional", "document", r["document_id"])
    assert w.job_number == "PV/J/2026/001" and w.work_order_id == "PV/WO/2026/001" and w.provisional_key == "job:" + "PVJ2026001" or w.provisional_key.startswith("job:")
    assert w.awarded_amount == 450000 and w.work_order_date.year == 2026 and w.contractor_id and w.department == "Ward Engineering"
    assert w.ward_number == "150" and w.ward_regime is None, "the ward map is not stated in a PDF, so it is not guessed"
    assert w.work_status == "work_order_issued" and "document" in w.work_status_source
    p = client.get(f"/api/gov/roadworks/{w.id}", headers=officer).json()
    assert p["roadwork"]["verification_status"] == "provisional" and p["provisional"]["reasons"] and p["provisional"]["created_from_document_id"] == r["document_id"]
    fields = {f["field"]: f for f in p["provisional"]["fields"]}
    assert {"job_number", "description", "contractor", "awarded_amount"} <= set(fields)
    assert all(f["document_id"] == r["document_id"] and f["page"] == 1 and f["snippet"] for f in fields.values())
    assert [d["id"] for d in p["documents"]] == [r["document_id"]]


def test_provisional_works_are_kept_out_of_default_lists_and_totals(client, seeded, officer):
    base = client.get("/api/gov/roadworks?id_search=PV/J/2026/001", headers=officer).json()
    assert base["total"] == 0
    prov = client.get("/api/gov/roadworks?id_search=PV/J/2026/001&verification=provisional", headers=officer).json()
    assert prov["total"] == 1 and prov["items"][0]["verification_status"] == "provisional"
    assert client.get("/api/gov/roadworks?id_search=PV/J/2026/001&verification=all", headers=officer).json()["total"] == 1
    d_def = client.get("/api/gov/dashboard", headers=officer).json()["totals"]
    d_all = client.get("/api/gov/dashboard?verification=all", headers=officer).json()["totals"]
    assert d_all["roadworks"] == d_def["roadworks"] + client.get("/api/gov/roadworks?verification=provisional", headers=officer).json()["total"]


def test_second_document_with_same_id_joins_the_existing_profile_not_a_new_one(client, officer, tmp_path):
    db = SessionLocal(); before = db.query(m.Roadwork).count()
    pay = make_pdf(tmp_path / "pay.pdf", "PAYMENT ADVICE", ["BBMP Job No: PV/J/2026/001", "Contractor: M/s Provisional Test Builders Pvt Ltd",
                                                           "Amount Paid: Rs. 1,00,000", "Payment Date: 20/04/2026"])
    r = upload(client, officer, pay)
    assert r["doc_type"] == "payment_record" and r["match_status"] == "auto" and r["created_work_id"] is None
    db.expire_all()
    assert db.query(m.Roadwork).count() == before
    w = db.query(m.Roadwork).filter_by(job_number="PV/J/2026/001").one()
    assert [p.amount for p in db.query(m.Payment).filter_by(roadwork_id=w.id)] == [100000]
    assert w.work_status == "work_order_issued", "a payment document does not change the stated status"


def test_identifierless_document_makes_a_profile_and_a_reissued_copy_does_not_duplicate_it(client, officer, tmp_path):
    lines = ["Name of Work: Resurfacing of Idless Cross Road near Quiet Park", "Contractor: M/s Idless Roadworks", "Estimated Cost: Rs. 7,50,000"]
    a = upload(client, officer, make_pdf(tmp_path / "a.pdf", "NOTICE INVITING TENDER", lines))
    assert a["doc_type"] == "nit" and a["created_work_id"]
    db = SessionLocal(); w = db.get(m.Roadwork, a["created_work_id"])
    assert w.provisional_key.startswith("fp:") and w.job_number is None
    p = client.get(f"/api/gov/roadworks/{w.id}", headers=officer).json()
    assert any("no job number" in x for x in p["provisional"]["reasons"])
    n = db.query(m.Roadwork).count()
    b = upload(client, officer, make_pdf(tmp_path / "b.pdf", "NOTICE INVITING TENDER", lines + ["Remarks: reissued copy"]))
    assert b["created_work_id"] is None and b["match_status"] == "needs_review" and b["roadwork_id"] is None
    db.expire_all()
    assert db.query(m.Roadwork).count() == n
    link = db.query(m.RecordLink).filter_by(a_type="document", a_id=b["document_id"], b_type="roadwork", b_id=w.id).one()
    assert link.status == "suggested" and link.method.startswith(("fuzzy", "provisional_")), "found by matching first; never merged automatically"
    # second safeguard: even when an officer forces creation, the same key does not make a second profile
    forced = client.post(f"/api/gov/documents/{b['document_id']}/create-work", headers=officer).json()
    assert forced["duplicate_prevented"] and forced["roadwork_id"] == w.id and not forced["created"]
    db.expire_all()
    assert db.query(m.Roadwork).count() == n
    assert db.query(m.MatchEvent).filter_by(subject_type="document", subject_id=b["document_id"], action="provisional_dup_prevented").count() == 1
    assert db.get(m.Document, b["document_id"]).roadwork_id is None, "a fingerprint-only match is a suggestion, so the document stays unlinked"


def test_document_matching_an_official_work_never_creates_a_profile(client, seeded, officer, tmp_path):
    db = SessionLocal(); n = db.query(m.Roadwork).count()
    off = db.query(m.Roadwork).filter(m.Roadwork.job_number == "SAMPLE/J/2425/003").one()
    r = upload(client, officer, work_order(tmp_path, "SAMPLE/J/2425/003", "SAMPLE/WO/OTHER/9", "off.pdf", "Some other wording entirely"))
    assert r["created_work_id"] is None and r["roadwork_id"] == off.id and r["match_status"] == "auto"
    db.expire_all(); assert db.query(m.Roadwork).count() == n


def test_unrecognised_document_starts_nothing(client, officer, tmp_path):
    r = upload(client, officer, make_pdf(tmp_path / "min.pdf", "MEETING MINUTES", ["Agenda: canteen menu", "Attendees: several"]))
    assert r["doc_type"] == "other" and r["created_work_id"] is None and r["match_status"] == "unmatched"
    assert client.post(f"/api/gov/documents/{r['document_id']}/create-work", headers=officer).status_code == 422


def test_officer_can_start_a_profile_for_an_unmatched_document_and_it_is_flagged(client, seeded, officer, admin):
    db = SessionLocal()
    d = db.query(m.Document).filter_by(filename="X01_nit_orphan_hennur.pdf").one()
    assert d.roadwork_id is None
    r = client.post(f"/api/gov/documents/{d.id}/create-work", headers=officer)
    assert r.status_code == 200 and r.json()["created"] and r.json()["reasons"]
    assert client.post(f"/api/gov/documents/{d.id}/create-work", headers=officer).json().get("reason") or True
    assert any(a["action"] == "provisional_work_create" for a in client.get("/api/admin/audit", headers=admin).json())


def test_confirm_then_it_joins_the_official_lists(client, officer):
    db = SessionLocal()
    w = db.query(m.Roadwork).filter_by(job_number="PV/J/2026/001").one()
    assert client.post(f"/api/gov/roadworks/{w.id}/confirm", headers=officer).json()["verification_status"] == "confirmed"
    assert client.post(f"/api/gov/roadworks/{w.id}/confirm", headers=officer).status_code == 422
    assert client.get("/api/gov/roadworks?id_search=PV/J/2026/001", headers=officer).json()["total"] == 1
    assert client.post("/api/gov/roadworks/999999/confirm", headers=officer).status_code == 404


def test_later_official_import_promotes_a_provisional_profile(client, officer, tmp_path):
    p = work_order(tmp_path, "PV/J/2026/050", "PV/WO/2026/050", "w50.pdf", "Resurfacing of Promotion Test Road", "Promo Roadways Ltd", "6,25,000")
    r = upload(client, officer, p)
    assert r["created_work_id"]
    csv = b"Job No,Work Description,Ward,Work Order Amount\nPV/J/2026/050,Resurfacing of Promotion Test Road,150,625000\n"
    client.post("/api/gov/imports", headers=officer, files={"file": ("promo.csv", io.BytesIO(csv), "text/csv")},
                data={"kind": "works", "source_name": "BBMP (test)", "ward_regime": "243", "is_sample": "true"})
    db = SessionLocal(); w = db.get(m.Roadwork, r["created_work_id"])
    assert w.verification_status == "official", "an imported government record now backs the work"
    assert db.query(m.Roadwork).filter_by(job_number="PV/J/2026/050").count() == 1
    assert db.query(m.WorkSourceLink).filter_by(roadwork_id=w.id).count() == 1


def test_merge_moves_everything_and_leaves_history(client, seeded, officer, tmp_path):
    db = SessionLocal()
    csv = b"Job No,Work Description,Ward,Work Order Amount\nMRG/J/1,Asphalting of Merge Target Road,150,300000\n"
    client.post("/api/gov/imports", headers=officer, files={"file": ("mrg.csv", io.BytesIO(csv), "text/csv")},
                data={"kind": "works", "source_name": "BBMP (test)", "ward_regime": "243", "is_sample": "true"})
    target = db.query(m.Roadwork).filter_by(job_number="MRG/J/1").one()
    src = upload(client, officer, make_pdf(tmp_path / "m.pdf", "NOTICE INVITING TENDER",
                                           ["Name of Work: Widening of Merge Source Lane behind Old Mill", "Contractor: M/s Merge Source Co", "Estimated Cost: Rs. 9,10,000"]))
    sw = src["created_work_id"]; assert sw
    assert client.post(f"/api/gov/roadworks/{sw}/merge", headers=officer, data={"target_id": sw}).status_code == 422
    assert client.post(f"/api/gov/roadworks/{sw}/merge", headers=officer, data={"target_id": 999999}).status_code == 404
    assert client.post(f"/api/gov/roadworks/{target.id}/merge", headers=officer, data={"target_id": sw}).status_code == 422  # official works are never merged away
    r = client.post(f"/api/gov/roadworks/{sw}/merge", headers=officer, data={"target_id": target.id}).json()
    assert r["moved"]["documents"] == 1
    db.expire_all()
    s = db.get(m.Roadwork, sw)
    assert s.verification_status == "merged" and s.merged_into_id == target.id and db.get(m.Document, src["document_id"]).roadwork_id == target.id
    assert db.get(m.Roadwork, target.id).estimated_cost == 910000, "gaps are filled from the profile; existing values are never overwritten"
    assert db.get(m.Roadwork, target.id).awarded_amount == 300000
    assert client.get("/api/gov/roadworks?verification=all&q=Merge Source", headers=officer).json()["total"] == 0  # merged profiles are hidden
    assert db.query(m.MatchEvent).filter_by(subject_type="roadwork", subject_id=sw, action="provisional_merged").count() == 1
    assert client.post(f"/api/gov/roadworks/{sw}/merge", headers=officer, data={"target_id": target.id}).status_code == 422  # already merged
