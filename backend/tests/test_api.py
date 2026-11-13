import io
from PIL import Image


def img(color=(120, 90, 60)):
    b = io.BytesIO(); Image.new("RGB", (80, 60), color).save(b, "JPEG"); return b.getvalue()


def test_roles_are_separated(client, officer, authority, admin):
    assert client.get("/api/authority/overview").status_code == 401
    assert client.get("/api/authority/overview", headers=officer).status_code == 200  # read-only
    assert client.post("/api/authority/recluster", headers=officer).status_code == 403
    assert client.get("/api/gov/dashboard", headers=authority).status_code == 403
    assert client.post("/api/admin/seed-sample", headers=authority).status_code == 403
    assert client.get("/api/admin/users", headers=admin).status_code == 200


def test_login_errors(client):
    assert client.post("/api/auth/login", data={"email": "x@example.test", "password": "nope"}).status_code == 401
    assert client.get("/api/gov/dashboard", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_report_validation(client):
    ok = dict(category="pothole", lat=12.97, lon=77.59)
    assert client.post("/api/reports", data={**ok, "category": "nope"}).status_code == 422
    assert client.post("/api/reports", data={**ok, "lat": 0, "lon": 0}).status_code == 422
    assert client.post("/api/reports", data={**ok, "description": "x" * 1001}).status_code == 422
    assert client.post("/api/reports", data={**ok, "category": "other"}).status_code == 422  # needs a description
    assert client.post("/api/reports", data=ok, files=[("photos", ("a.jpg", b"not an image", "image/jpeg"))]).status_code == 422
    assert client.post("/api/reports", data=ok, files=[("photos", (f"{i}.jpg", img(), "image/jpeg")) for i in range(4)]).status_code == 422
    assert client.post("/api/reports", data={"category": "pothole"}).status_code == 422


def test_photos_are_private_and_exif_free(client, authority):
    r = client.post("/api/reports", data=dict(category="pothole", lat=12.97, lon=77.59, description="photo test"),
                    files=[("photos", ("a.jpg", img(), "image/jpeg")), ("photos", ("b.jpg", img((10, 10, 10)), "image/jpeg"))])
    assert r.status_code == 200
    reps = client.get("/api/authority/reports?q=photo test", headers=authority).json()["items"]
    assert reps and reps[0]["photos"] == 2
    pid = reps[0]["photo_ids"][0]
    assert client.get(f"/api/authority/photos/{pid}").status_code == 401
    body = client.get(f"/api/authority/photos/{pid}", headers=authority)
    assert body.status_code == 200 and b"Exif" not in body.content[:200]


def test_rate_limit(client, monkeypatch):
    monkeypatch.setenv("REPORT_RATE_PER_HOUR", "2")
    codes = [client.post("/api/reports", data=dict(category="cracks", lat=13.0 + i * 0.001, lon=77.6, description=f"rate {i}")).status_code for i in range(5)]
    assert 429 in codes


def test_contact_only_visible_to_admin(client, authority, admin):
    client.post("/api/reports", data=dict(category="cracks", lat=12.95, lon=77.62, description="contact check", contact="secret@example.test"))
    a = client.get("/api/authority/reports?q=contact check", headers=authority).text
    assert "secret@example.test" not in a and "provided" in a
    assert "secret@example.test" in client.get("/api/authority/reports?q=contact check", headers=admin).text


def test_document_upload_dedupes_and_rejects_non_pdf(client, officer):
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "data" / "heldout" / "H02_tender_notice_prose.pdf"
    r1 = client.post("/api/gov/documents", headers=officer, files=[("files", (p.name, p.read_bytes(), "application/pdf"))]).json()["results"][0]
    r2 = client.post("/api/gov/documents", headers=officer, files=[("files", (p.name, p.read_bytes(), "application/pdf"))]).json()["results"][0]
    assert r1["doc_type"] == "nit" and r2["duplicate_of_existing"] and r1["document_id"] == r2["document_id"]
    bad = client.post("/api/gov/documents", headers=officer, files=[("files", ("x.pdf", b"hello", "application/pdf")), ("files", ("x.txt", b"hello", "text/plain"))]).json()["results"]
    assert all("error" in b for b in bad)
    d = client.get(f"/api/gov/documents/{r1['document_id']}", headers=officer).json()
    assert d["extractions"] and all(e["page"] for e in d["extractions"])
    assert client.get(f"/api/gov/documents/{r1['document_id']}/file", headers=officer).headers["content-type"] == "application/pdf"


def test_extraction_review_and_validation(client, officer):
    docs = client.get("/api/gov/documents", headers=officer).json()["items"]
    eid = client.get(f"/api/gov/documents/{docs[0]['id']}", headers=officer).json()["extractions"][0]["id"]
    assert client.patch(f"/api/gov/extractions/{eid}", headers=officer, data={"status": "bogus"}).status_code == 422
    assert client.patch(f"/api/gov/extractions/{eid}", headers=officer, data={"status": "corrected"}).status_code == 422
    assert client.patch(f"/api/gov/extractions/{eid}", headers=officer, data={"status": "verified"}).status_code == 200


def test_import_preview_mapping_and_duplicate_skipping(client, officer):
    csv = b"Ward,Work Description,Contractor,Job No\n12,Asphalting of Test Road,Test Contractor,TESTJOB/1\n"
    f = lambda: {"file": ("t.csv", io.BytesIO(csv), "text/csv")}
    pv = client.post("/api/gov/imports/preview", headers=officer, files=f(), data={"kind": "works"}).json()
    assert pv["mapping"]["description"] == "Work Description" and pv["rows"] == 1
    d = {"kind": "works", "source_name": "unit test", "ward_regime": "243", "is_sample": "true"}
    a = client.post("/api/gov/imports", headers=officer, files=f(), data=d).json()
    b = client.post("/api/gov/imports", headers=officer, files=f(), data=d).json()
    assert a["ok"] == 1 and b["ok"] == 0 and b["duplicate_rows"] == 1
    assert client.post("/api/gov/imports/preview", headers=officer, files={"file": ("x.txt", b"a", "text/plain")}, data={"kind": "works"}).status_code == 422
    assert client.post("/api/gov/imports/preview", headers=officer, files=f(), data={"kind": "nope"}).status_code == 422


def test_dashboard_filters_and_provenance_fields(client, seeded, officer):
    all_ = client.get("/api/gov/dashboard", headers=officer).json()
    one = client.get("/api/gov/dashboard?ward=233", headers=officer).json()
    assert 0 < one["totals"]["roadworks"] < all_["totals"]["roadworks"]
    assert any("not" in n.lower() for n in all_["notes"])
    w = client.get("/api/gov/roadworks?id_search=SAMPLE/J/2425/001", headers=officer).json()["items"][0]
    p = client.get(f"/api/gov/roadworks/{w['id']}", headers=officer).json()
    assert p["documents"] and p["lifecycle"] and p["payment_summary"]["note"] and p["source_row"]["raw"]
    assert client.get("/api/gov/roadworks/999999", headers=officer).status_code == 404


def test_filters_work_individually(client, seeded, officer):
    for q in ("contractor=Sample", "category=resurfacing", "work_status=completion_certificate_on_file", "fiscal_year=2024-25", "date_from=2024-06-01&date_to=2024-07-01",
              "tender_status=awarded", "flag=no_documents", "flag=past_deadline_no_completion", "department=Major Roads (sample)"):
        r = client.get("/api/gov/roadworks?" + q, headers=officer)
        assert r.status_code == 200, q
        assert client.get("/api/gov/dashboard?" + q, headers=officer).status_code == 200


def test_evaluation_endpoint(client, seeded, officer):
    r = client.get("/api/gov/evaluation", headers=officer).json()
    assert r["extraction"]["sets"] and r["clustering"]["ablation"] and r["alerts"]["by_rule"] and r["limitations"]
