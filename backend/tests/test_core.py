import os, io, tempfile
os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(suffix=".db")
os.environ["UPLOAD_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_EMAIL", "admin@infrasignal.demo")
os.environ.setdefault("ADMIN_PASSWORD", "Admin@123")
os.environ.setdefault("OFFICER_EMAIL", "officer@infrasignal.demo")
os.environ.setdefault("OFFICER_PASSWORD", "Officer@123")
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-deployment")
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from app.main import app
from app.clustering import cluster, haversine_m
from app.extract import extract_fields
from app.ingest import guess_mapping, norm_amount, norm_contractor

client = TestClient(app)
client.__enter__()  # runs startup


def tok(email, pw):
    return client.post("/api/auth/login", data={"email": email, "password": pw}).json()["token"]


def test_haversine():
    assert 100 < haversine_m(12.9716, 77.5946, 12.9725, 77.5946) < 101


def test_cluster_groups_bus_stop_example():
    t = datetime.utcnow()
    reps = [dict(lat=12.93520, lon=77.62450, created_at=t, category="pothole", description="Large pothole near the bus stop"),
            dict(lat=12.93525, lon=77.62455, created_at=t, category="damaged_road", description="Road is damaged in front of the bus stop"),
            dict(lat=12.93518, lon=77.62448, created_at=t, category="pothole", description="Deep pothole on the same stretch"),
            dict(lat=12.94500, lon=77.62450, created_at=t, category="pothole", description="Pothole far away")]
    labels, _ = cluster(reps)
    assert labels[0] == labels[1] == labels[2] != -1 and labels[3] == -1


def test_cluster_respects_time_window():
    t = datetime.utcnow()
    reps = [dict(lat=12.9352, lon=77.6245, created_at=t, category="pothole", description="pothole"),
            dict(lat=12.9352, lon=77.6245, created_at=t - timedelta(days=200), category="pothole", description="pothole")]
    assert cluster(reps)[0] == [-1, -1]


def test_extraction_regex():
    f = extract_fields("Job No: 123-45-678901 W.O. No: WO/2024/77 Amount Rs. 12,50,000", 1, "text_regex")
    got = {x["field"]: x["norm_value"] for x in f}
    assert got["job_number"] == "123-45-678901" and got["amount"] == "1250000"


def test_normalisers():
    assert norm_amount("Rs 1,23,456.50") == 123456.5
    assert norm_contractor("M/s Sri Ganesh Constructions Pvt Ltd") == "ganesh constructions"
    assert guess_mapping(["Ward No", "Name of Work", "Contractor Name"])["description"] == "Name of Work"


def test_permissions():
    assert client.get("/api/gov/dashboard").status_code == 401
    t = tok("officer@infrasignal.demo", "Officer@123")
    assert client.get("/api/gov/dashboard", headers={"Authorization": "Bearer " + t}).status_code == 200
    assert client.get("/api/admin/audit", headers={"Authorization": "Bearer " + t}).status_code == 403


def test_report_submit_track_and_privacy():
    r = client.post("/api/reports", data={"category": "pothole", "description": "test", "lat": 12.97,
                                          "lon": 77.59, "contact": "9999999999"})
    assert r.status_code == 200
    j = r.json()
    tr = client.get(f"/api/reports/track?ref={j['ref']}&token={j['token']}").json()
    assert tr["status"] == "received"
    assert client.get(f"/api/reports/track?ref={j['ref']}&token=wrong").status_code == 404
    assert "9999999999" not in client.get("/api/public/map").text


def test_rejects_outside_bengaluru():
    r = client.post("/api/reports", data={"category": "pothole", "lat": 19.07, "lon": 72.87})
    assert r.status_code == 422


def test_csv_import():
    t = tok("admin@infrasignal.demo", "Admin@123")
    csv = b"Ward No,Name of Work,Contractor Name,Paid Amount\n150,Asphalting of 5th Cross Road,M/s ABC Works,100000\n150,Purchase of chairs,XYZ,500\n"
    r = client.post("/api/admin/import", headers={"Authorization": "Bearer " + t},
                    files={"file": ("t.csv", io.BytesIO(csv), "text/csv")},
                    data={"source_name": "unit test fixture", "ward_regime": "225"}).json()
    assert r["ok"] == 1 and r["skipped_non_road"] == 1
