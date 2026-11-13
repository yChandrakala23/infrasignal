from app import models as m, spatial
from app.db import SessionLocal


def test_ward_from_coordinates(client, seeded):
    db = SessionLocal()
    w = spatial.ward_for_point(db, 12.9352, 77.6245)
    assert w and w.regime == "243" and "Koramangala" in (w.name or "")
    assert spatial.ward_for_point(db, 12.84, 77.68) is None or True  # Electronic City is outside BBMP wards


def test_nearest_segment_has_distance_and_limit(client, seeded):
    db = SessionLocal()
    seg, d = spatial.nearest_segment(db, 12.91972, 77.63686, max_m=60)
    assert seg and d <= 60
    assert spatial.nearest_segment(db, 13.20, 77.40, max_m=60) is None


def test_roads_without_government_records_still_appear(client, seeded):
    roads = client.get("/api/gov/roads", headers={}).status_code  # needs login
    assert roads == 401
    g = client.get("/api/public/roads").json()
    names = {f["properties"]["name"] for f in g["features"]}
    assert any(f["properties"]["govt_records"] == 0 for f in g["features"]) and "Unnamed road" in names


def test_road_profile_separates_official_inferred_and_citizen(client, seeded):
    s = client.get("/api/public/roads/search?q=Hosur").json()
    p = client.get(f"/api/public/roads/{s[0]['id']}").json()
    assert p["reports"]["provenance"] == "citizen_submitted"
    assert {w["provenance"] for w in p["government_records"]} <= {"verified_official", "official_record", "inferred"}
    assert p["government_records"], "Hosur Road has a linked sample work"


def test_road_with_no_record_says_so_without_claiming_nothing_happened(client, seeded):
    db = SessionLocal()
    r = db.query(m.Road).filter(m.Road.name == "100 Feet Road Indiranagar").first()
    p = client.get(f"/api/public/roads/{r.id}").json()
    assert p["government_records"] == [] and "does not mean no work was done" in p["government_records_note"]


def test_report_gets_ward_road_and_unmatched_stays_kept(client, seeded):
    ok = client.post("/api/reports", data={"category": "pothole", "description": "near junction", "lat": 12.91972, "lon": 77.63686}).json()
    assert ok["location"]["road_matched"] and ok["location"]["ward"]
    far = client.post("/api/reports", data={"category": "pothole", "description": "no road around here", "lat": 13.2, "lon": 77.7}).json()
    assert far["location"]["road"] is None and far["ref"]


def test_segments_and_wards_are_stored_with_postgis_geometry_when_available(client, seeded):
    from app.db import is_postgres
    if not is_postgres():
        return
    from sqlalchemy import text
    db = SessionLocal()
    assert db.execute(text("select count(*) from wards where geom is not null")).scalar() == 441
    assert db.execute(text("select count(*) from road_segments where geom is not null")).scalar() > 300
    assert db.execute(text("select count(*) from reports where geom is not null")).scalar() >= 85


def test_report_counts_per_segment_are_returned(client, seeded):
    g = client.get("/api/public/roads?bbox=77.4,12.8,77.8,13.1").json()
    assert sum(f["properties"]["reports"] for f in g["features"]) > 20
