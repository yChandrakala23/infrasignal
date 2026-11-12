from app import models as m
from app.db import SessionLocal


def docs(db):
    return {d.filename: d for d in db.query(m.Document)}


def test_exact_identifier_links_automatically(client, seeded):
    db = SessionLocal()
    d = docs(db)["A06_work_order_hosur_road.pdf"]
    assert d.match_status == "auto" and db.get(m.Roadwork, d.roadwork_id).job_number == "SAMPLE/J/2425/001"


def test_conflicting_identifiers_are_never_auto_merged(client, seeded):
    db = SessionLocal()
    d = docs(db)["D01_loa_conflict.pdf"]
    assert d.match_status == "needs_review" and d.roadwork_id is None


def test_fuzzy_only_document_is_suggested_not_merged(client, seeded):
    db = SessionLocal()
    d = docs(db)["F01_work_order_no_ids.pdf"]
    assert d.roadwork_id is None and d.match_status == "needs_review"
    links = db.query(m.RecordLink).filter_by(a_type="document", a_id=d.id).all()
    assert links and all(l.status == "suggested" and l.method.startswith("fuzzy") and l.confidence <= 0.88 for l in links)


def test_orphan_and_unclassifiable_documents_stay_visible(client, seeded):
    db = SessionLocal(); d = docs(db)
    assert d["X01_nit_orphan_hennur.pdf"].match_status == "unmatched" and d["Z01_meeting_minutes_other.pdf"].doc_type == "other"


def test_no_fuzzy_link_is_ever_automatic(client, seeded):
    db = SessionLocal()
    bad = db.query(m.RecordLink).filter(m.RecordLink.method.like("fuzzy%"), m.RecordLink.status == "auto").count()
    assert bad == 0


def test_payment_never_sets_completion(client, seeded):
    db = SessionLocal()
    w = db.query(m.Roadwork).filter_by(job_number="SAMPLE/J/2425/003").one()  # has a payment document but no completion certificate
    assert db.query(m.Payment).filter_by(roadwork_id=w.id).count() >= 1
    assert w.work_status != "completion_certificate_on_file"
    w1 = db.query(m.Roadwork).filter_by(job_number="SAMPLE/J/2425/001").one()  # has a completion certificate
    assert w1.work_status == "completion_certificate_on_file" and "document" in w1.work_status_source


def test_documents_do_not_overwrite_official_fields(client, seeded):
    db = SessionLocal()
    w = db.query(m.Roadwork).filter_by(job_number="SAMPLE/J/2425/001").one()
    assert w.awarded_amount is not None and w.description.startswith("Asphalting of Hosur Road")


def test_human_decision_survives_a_rerun(client, seeded, officer):
    db = SessionLocal()
    d = docs(db)["F01_work_order_no_ids.pdf"]
    link = db.query(m.RecordLink).filter_by(a_type="document", a_id=d.id, status="suggested").order_by(m.RecordLink.confidence.desc()).first()
    r = client.patch(f"/api/gov/links/{link.id}", headers=officer, data={"status": "verified"})
    assert r.status_code == 200
    client.post("/api/gov/matching/run", headers=officer, data={"reprocess_all": "true"})
    db.expire_all()
    assert db.get(m.RecordLink, link.id).status == "verified" and db.get(m.Document, d.id).roadwork_id == link.b_id
    events = db.query(m.MatchEvent).filter_by(subject_type="document", subject_id=d.id).count()
    assert events >= 2  # history is kept


def test_rejected_link_stays_rejected(client, seeded, officer):
    db = SessionLocal()
    link = db.query(m.RecordLink).filter_by(status="suggested", a_type="document").first()
    if not link:
        return
    client.patch(f"/api/gov/links/{link.id}", headers=officer, data={"status": "rejected"})
    client.post("/api/gov/matching/run", headers=officer, data={"reprocess_all": "true"})
    db.expire_all()
    assert db.get(m.RecordLink, link.id).status == "rejected"


def test_bad_review_status_is_rejected(client, seeded, officer):
    assert client.patch("/api/gov/links/1", headers=officer, data={"status": "bogus"}).status_code == 422
    assert client.patch("/api/gov/links/999999", headers=officer, data={"status": "verified"}).status_code == 404


def test_matching_metrics_floor(client, seeded):
    from app.evaluation import eval_matching
    r = eval_matching(SessionLocal())
    assert r["document_to_work"]["precision"] == 1.0 and r["tender_to_work"]["precision"] == 1.0 and r["work_to_road"]["precision"] == 1.0
    assert r["document_to_work"]["fuzzy_only_auto_links"] == 0
