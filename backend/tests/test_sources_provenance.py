"""Requirement 1 (many source records per work) and requirement 2 (document provenance)."""
import io
from pathlib import Path
from app import models as m
from app.db import SessionLocal

HELD = Path(__file__).resolve().parents[1] / "data" / "heldout"


def post_import(client, headers, csv: bytes, name: str, source="unit test fixture", **extra):
    data = {"kind": "works", "source_name": source, "ward_regime": "243", "is_sample": "true", **extra}
    r = client.post("/api/gov/imports", headers=headers, files={"file": (name, io.BytesIO(csv), "text/csv")}, data=data)
    assert r.status_code == 200, r.text
    return r.json()


def work_by_job(db, job):
    return db.query(m.Roadwork).filter_by(job_number=job).one()


# ------------------------------------------------------------------ requirement 1
def test_second_file_row_links_to_same_work_and_keeps_both_originals(client, officer):
    a = b"Job No,Work Description,Ward,Work Order Amount,Contractor\nSRC/J/1,Asphalting of Source Test Road,150,500000,M/s Source Test Co\n"
    b_ = b"Job No,Work Description,Ward,Paid Amount,Payment Date\nSRC/J/1,Asphalting of Source Test Road,150,200000,2025-01-15\n"
    post_import(client, officer, a, "bbmp_work_orders.csv", "BBMP work orders (test)")
    post_import(client, officer, b_, "bbmp_payments.csv", "BBMP payments (test)")
    db = SessionLocal()
    w = work_by_job(db, "SRC/J/1")
    links = db.query(m.WorkSourceLink).filter_by(roadwork_id=w.id).order_by(m.WorkSourceLink.id).all()
    assert len(links) == 2
    first, second = links
    assert first.role == "primary" and first.match_method == "created_from_row" and first.status == "auto"
    assert second.role == "merged_row" and second.source_type == "payment_row"
    assert second.match_method == "exact_job_number" and second.confidence == 0.99 and second.status == "auto"
    assert second.evidence["job_number"] == "SRC/J/1" and second.evidence["file"] == "bbmp_payments.csv"
    # both original rows are intact, in their own import jobs
    recs = [db.get(m.SourceRecord, l.source_record_id) for l in links]
    assert recs[0].import_job_id != recs[1].import_job_id
    assert recs[0].raw["Job No"] == "SRC/J/1" and recs[1].raw["Paid Amount"] == "200000"
    # the legacy pointer is still the first record
    assert w.source_record_id == first.source_record_id


def test_profile_lists_every_source_with_method_confidence_and_original_row(client, officer):
    db = SessionLocal(); w = work_by_job(db, "SRC/J/1")
    p = client.get(f"/api/gov/roadworks/{w.id}", headers=officer).json()
    assert len(p["sources"]) == 2 and p["source_row"]["raw"]["Job No"] == "SRC/J/1"
    s = p["sources"][1]
    assert {"method", "confidence", "status", "evidence", "raw", "import"} <= set(s) and s["import"]["file"] == "bbmp_payments.csv"
    assert client.get(f"/api/gov/roadworks/{w.id}/sources", headers=officer).json()["sources"][0]["role"] == "primary"
    assert client.get("/api/gov/roadworks/999999/sources", headers=officer).status_code == 404


def test_row_without_identifiers_is_a_suggested_link_not_an_assured_one(client, officer):
    row = b"Work Description,Ward,Work Order Amount\nResurfacing of Fingerprint Test Cross Road,77,123000\n"
    post_import(client, officer, row, "fp1.csv"); post_import(client, officer, row.replace(b"123000", b"123000"), "fp2.csv")
    db = SessionLocal()
    w = db.query(m.Roadwork).filter(m.Roadwork.description.like("Resurfacing of Fingerprint Test%")).one()
    links = db.query(m.WorkSourceLink).filter_by(roadwork_id=w.id).all()
    # the second identical row is dropped as a byte-for-byte duplicate row, so a single source link exists
    assert len(links) == 1 and links[0].role == "primary"
    row2 = b"Work Description,Ward,Work Order Amount,Contractor\nResurfacing of Fingerprint Test Cross Road,77,123000,Other Co\n"
    post_import(client, officer, row2, "fp3.csv")
    links = db.query(m.WorkSourceLink).filter_by(roadwork_id=w.id).order_by(m.WorkSourceLink.id).all()
    assert len(links) == 2 and links[1].match_method == "fingerprint_description_ward_amount" and links[1].status == "suggested"


def test_source_link_review_and_validation(client, officer):
    db = SessionLocal(); w = work_by_job(db, "SRC/J/1")
    link = db.query(m.WorkSourceLink).filter_by(roadwork_id=w.id, role="merged_row").one()
    assert client.patch(f"/api/gov/source-links/{link.id}", headers=officer, data={"status": "bogus"}).status_code == 422
    assert client.patch("/api/gov/source-links/999999", headers=officer, data={"status": "verified"}).status_code == 404
    assert client.patch(f"/api/gov/source-links/{link.id}", headers=officer, data={"status": "verified"}).json()["status"] == "verified"
    assert client.patch(f"/api/gov/source-links/{link.id}", headers=officer, data={"status": "rejected"}).status_code == 200
    db.expire_all()
    l2 = db.get(m.WorkSourceLink, link.id)
    assert l2.status == "rejected" and l2.reviewer_id and db.get(m.SourceRecord, l2.source_record_id) is not None  # record kept
    client.patch(f"/api/gov/source-links/{link.id}", headers=officer, data={"status": "verified"})


def test_tender_rows_back_the_work_they_are_linked_to(client, seeded, officer):
    db = SessionLocal()
    n = db.query(m.WorkSourceLink).filter_by(source_type="tender_row").count()
    assert n >= 5
    l = db.query(m.WorkSourceLink).filter_by(source_type="tender_row", status="auto").first()
    t = db.query(m.Tender).filter_by(source_record_id=l.source_record_id).one()
    assert l.match_method.startswith("exact_") and l.evidence["tender_id"] == t.tender_id
    rl = db.query(m.RecordLink).filter_by(a_type="roadwork", a_id=l.roadwork_id, b_type="tender", b_id=t.id).one()
    client.patch(f"/api/gov/links/{rl.id}", headers=officer, data={"status": "rejected"})
    db.expire_all()
    assert db.get(m.WorkSourceLink, l.id).status == "rejected"  # follows the review decision; the row is kept
    client.patch(f"/api/gov/links/{rl.id}", headers=officer, data={"status": "verified"})
    db.expire_all()
    assert db.get(m.WorkSourceLink, l.id).status == "verified"


def test_one_source_record_cannot_be_linked_twice_to_the_same_work(client, officer):
    from sqlalchemy.exc import IntegrityError
    db = SessionLocal(); l = db.query(m.WorkSourceLink).first()
    db.add(m.WorkSourceLink(roadwork_id=l.roadwork_id, source_record_id=l.source_record_id))
    try:
        db.commit(); raise AssertionError("duplicate link accepted")
    except IntegrityError:
        db.rollback()


# ------------------------------------------------------------------ requirement 2
def pdf_bytes(name="H03_payment_release_prose.pdf"):
    p = HELD / name
    return p.name, p.read_bytes()


def test_upload_stores_urls_download_date_filename_and_checksum(client, officer):
    name, raw = pdf_bytes("H01_work_order_letter.pdf")
    r = client.post("/api/gov/documents", headers=officer, files=[("files", (name, raw, "application/pdf"))],
                    data={"source_name": "BBMP (test)", "source_page_url": "https://example.gov.in/tenders/list", "download_url": "https://example.gov.in/files/a.pdf",
                          "downloaded_at": "2026-11-10"}).json()["results"][0]
    assert "error" not in r and r["source_page_url"] == "https://example.gov.in/tenders/list"
    d = client.get(f"/api/gov/documents/{r['document_id']}", headers=officer).json()
    pv = d["provenance"]
    assert pv["source_page_url"] == "https://example.gov.in/tenders/list" and pv["download_url"] == "https://example.gov.in/files/a.pdf"
    assert pv["downloaded_at"].startswith("2026-11-10") and pv["original_filename"] == name and len(pv["sha256"]) == 64 and pv["source_name"] == "BBMP (test)"
    assert pv["file_available"] and pv["note"] is None
    assert client.get(f"/api/gov/documents/{r['document_id']}/file", headers=officer).content[:4] == b"%PDF"  # the original can be opened


def test_unsafe_or_malformed_links_are_refused(client, officer):
    name, raw = pdf_bytes()
    for bad in ("javascript:alert(1)", "ftp://x/y.pdf", "not a url", "https://a b.com", "data:text/html,x"):
        r = client.post("/api/gov/documents", headers=officer, files=[("files", (name, raw, "application/pdf"))], data={"source_page_url": bad})
        assert r.status_code == 422, bad
    assert client.post("/api/gov/documents", headers=officer, files=[("files", (name, raw, "application/pdf"))], data={"downloaded_at": "someday"}).status_code == 422


def test_repeat_upload_fills_missing_links_but_never_overwrites(client, officer):
    name, raw = pdf_bytes("H02_tender_notice_prose.pdf")
    first = client.post("/api/gov/documents", headers=officer, files=[("files", (name, raw, "application/pdf"))]).json()["results"][0]
    did = first["document_id"]
    assert client.get(f"/api/gov/documents/{did}", headers=officer).json()["provenance"]["note"] == "No web address was recorded for this file."
    client.post("/api/gov/documents", headers=officer, files=[("files", (name, raw, "application/pdf"))], data={"download_url": "https://example.gov.in/one.pdf"})
    client.post("/api/gov/documents", headers=officer, files=[("files", (name, raw, "application/pdf"))], data={"download_url": "https://example.gov.in/two.pdf"})
    assert client.get(f"/api/gov/documents/{did}", headers=officer).json()["provenance"]["download_url"] == "https://example.gov.in/one.pdf"


def test_source_details_can_be_added_corrected_and_cleared_with_an_audit_trail(client, officer, admin):
    docs = client.get("/api/gov/documents", headers=officer).json()["items"]; did = docs[0]["id"]
    r = client.patch(f"/api/gov/documents/{did}/source", headers=officer, data={"source_page_url": "https://example.gov.in/p", "downloaded_at": "2026-01-02"})
    assert r.status_code == 200 and r.json()["source_page_url"] == "https://example.gov.in/p"
    assert client.patch(f"/api/gov/documents/{did}/source", headers=officer, data={"download_url": "javascript:x"}).status_code == 422
    cleared = client.patch(f"/api/gov/documents/{did}/source", headers=officer, data={"clear": "source_page_url"}).json()
    assert cleared["source_page_url"] is None
    assert any(a["action"] == "document_source_edit" and a["entity_id"] == did for a in client.get("/api/admin/audit", headers=admin).json())
    assert client.patch(f"/api/gov/documents/{did}/source", headers=officer, data={"clear": "path"}).status_code == 422
    assert client.patch("/api/gov/documents/999999/source", headers=officer, data={"source_name": "x"}).status_code == 404


def test_work_profile_exposes_document_source_links(client, seeded, officer):
    w = client.get("/api/gov/roadworks?id_search=SAMPLE/J/2425/001", headers=officer).json()["items"][0]
    docs = client.get(f"/api/gov/roadworks/{w['id']}", headers=officer).json()["documents"]
    assert docs and all({"source_page_url", "download_url", "downloaded_at"} <= set(d) for d in docs)


def test_every_extracted_field_has_document_page_and_supporting_text(client, seeded, officer):
    docs = client.get("/api/gov/documents", headers=officer).json()["items"]
    checked = 0
    for d in docs[:40]:
        det = client.get(f"/api/gov/documents/{d['id']}", headers=officer).json()
        for e in det["extractions"]:
            checked += 1
            assert e["page"] and 1 <= e["page"] <= det["pages"], (d["filename"], e["field"])
            assert (e["snippet"] or "").strip() and e["method"] and e["confidence"] is not None
    assert checked > 100
