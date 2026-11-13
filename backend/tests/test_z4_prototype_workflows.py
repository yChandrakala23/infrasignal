"""End-to-end workflows of the prototype and the collectors added in round 5.

Citizen: submit (GPS + photo + description) -> reference and private code -> track -> public map.
Officer / authority: login -> dashboard -> works search -> tenders and payments -> source records -> match review accept/reject ->
clusters -> alert acknowledge -> complaint status -> audit log (admin only).
Clustering: complaints close together form a cluster, a complaint 2 km away does not.
Collectors: OpenCity CKAN API discovery, IFMS (rows, HTTP 500 failure, regime-aware job-code candidates), tender plausibility checks.
Network is replaced by httpx.MockTransport; nothing here contacts a real server."""
import io
from datetime import datetime, timedelta

import httpx
from PIL import Image

from app import models as m
from app.clustering import cluster
from app.connectors.bbmp_connector import BbmpConnector, Fetcher
from app.connectors.ifms import normalise_ifms_row
from app.db import SessionLocal


def jpg(color=(90, 90, 90)):
    b = io.BytesIO(); Image.new("RGB", (64, 48), color).save(b, "JPEG"); return b.getvalue()


# ----------------------------------------------------------------------------- clustering: near vs far
def _rep(lat, lon, days=0, cat="pothole", text="deep pothole near the bus stop"):
    return {"lat": lat, "lon": lon, "created_at": datetime(2026, 11, 1) - timedelta(days=days), "category": cat, "description": text}


def test_near_complaints_cluster_and_far_ones_do_not():
    base = (12.9716, 77.5946)
    near = [_rep(base[0], base[1]), _rep(base[0] + 0.0001, base[1]), _rep(base[0], base[1] + 0.0001)]  # about 11 m apart
    far = _rep(base[0] + 0.018, base[1])  # about 2 km north
    labels, _ = cluster(near + [far])
    assert labels[0] == labels[1] == labels[2] != -1
    assert labels[3] == -1


def test_same_place_but_months_apart_does_not_cluster():
    labels, _ = cluster([_rep(12.97, 77.59, 0), _rep(12.97, 77.59001, 200)])
    assert labels == [-1, -1]


# ----------------------------------------------------------------------------- citizen portal
def test_citizen_submit_track_and_public_map(client, seeded):
    r = client.post("/api/reports", data={"category": "pothole", "description": "Large pothole at the junction, two wheelers falling",
                                          "lat": 12.9352, "lon": 77.6245, "accuracy_m": 8, "contact": "citizen@example.org"},
                    files=[("photos", ("p.jpg", jpg(), "image/jpeg"))])
    assert r.status_code == 200, r.text
    ref, code = r.json()["ref"], r.json()["token"]
    assert ref and code and ref != code
    t = client.get("/api/reports/track", params={"ref": ref, "token": code})
    assert t.status_code == 200 and t.json()["status"] == "received" and t.json()["history"]
    assert client.get("/api/reports/track", params={"ref": ref, "token": "wrong"}).status_code == 404
    mp = client.get("/api/public/map").json()
    assert "citizen@example.org" not in str(mp)  # contact details are never public
    assert client.get("/api/public/stats").status_code == 200


# ----------------------------------------------------------------------------- government side
def test_officer_and_authority_workflow(client, seeded, officer, authority, admin):
    assert client.get("/api/gov/dashboard", headers=officer).status_code == 200
    works = client.get("/api/gov/roadworks", params={"q": "road"}, headers=officer).json()
    assert works["total"] >= 1
    wid = works["items"][0]["id"]
    assert client.get(f"/api/gov/roadworks/{wid}", headers=officer).status_code == 200
    assert client.get(f"/api/gov/roadworks/{wid}/sources", headers=officer).status_code == 200
    assert client.get("/api/gov/contractors", headers=officer).status_code == 200
    assert client.get("/api/gov/collection/payments", headers=officer).status_code == 200
    # match review: accept one suggestion, reject another (tender suggestions, so the document matching tests keep their state),
    # then reopen both so the shared sample data is unchanged for later tests
    links = client.get("/api/gov/links", params={"status": "suggested"}, headers=officer).json()
    items = [x for x in (links["items"] if isinstance(links, dict) else links) if "document" not in (x["a_type"], x["b_type"])]
    assert len(items) >= 2, links
    a, b = items[0]["id"], items[1]["id"]
    assert client.patch(f"/api/gov/links/{a}", data={"status": "verified"}, headers=officer).json()["link"]["status"] == "verified"
    assert client.patch(f"/api/gov/links/{b}", data={"status": "rejected"}, headers=officer).json()["link"]["status"] == "rejected"
    for lid in (a, b):
        assert client.patch(f"/api/gov/links/{lid}", data={"status": "unverified"}, headers=officer).json()["ok"]
    # clusters, alerts, complaint status
    cl = client.get("/api/authority/clusters", headers=authority).json()
    assert cl and client.get(f"/api/authority/clusters/{cl[0]['id']}", headers=authority).json()["reports"]
    al = client.get("/api/authority/alerts", headers=authority).json()
    assert al, 'the seeded complaints should raise at least one alert'
    if al:
        assert client.patch(f"/api/authority/alerts/{al[0]['id']}", data={"status": "acknowledged", "note": "Seen"}, headers=authority).json()["ok"]
        assert client.patch(f"/api/authority/alerts/{al[0]['id']}", data={"status": "acknowledged"}, headers=officer).status_code == 403
    reps = client.get("/api/authority/reports", headers=authority).json()
    rid = (reps["items"] if isinstance(reps, dict) else reps)[0]["id"]
    assert client.patch(f"/api/authority/reports/{rid}/status", data={"status": "under_review", "note_public": "Inspection booked"},
                        headers=authority).json()["ok"]
    # audit: admin only, and it recorded the actions above
    assert client.get("/api/admin/audit", headers=officer).status_code == 403
    actions = {a["action"] for a in client.get("/api/admin/audit", headers=admin).json()}
    assert "report_status" in actions


# ----------------------------------------------------------------------------- OpenCity CKAN API
PKG = {"success": True, "result": {"id": "p1", "name": "bbmp-work-orders-and-payments-2024-25", "title": "BBMP Work Orders and Payments 2024-25",
       "license_title": "Other (Public Domain)", "metadata_modified": "2025-11-27T12:38:11", "organization": {"title": "BBMP"},
       "resources": [{"id": "83d27234-f87c-4a0b-a668-a58bd57b5034", "name": "BBMP Work Orders and Payments for 243 Wards Regime", "format": "CSV",
                      "url": "https://data.opencity.in/dataset/x/resource/83d27234/download/a.csv"},
                     {"id": "r2", "name": "Readme", "format": "PDF", "url": "https://data.opencity.in/dataset/x/resource/r2/download/readme.pdf"}]}}


def _ckan(handler):
    return BbmpConnector(Fetcher(httpx.Client(transport=httpx.MockTransport(handler)), delay=0, retries=0, allowed_hosts=["data.opencity.in"]))


def test_ckan_api_discovery_reads_exact_resources():
    def h(req):
        if req.url.path.endswith("robots.txt"):
            return httpx.Response(404)
        if "package_show" in str(req.url):
            return httpx.Response(200, json=PKG)
        return httpx.Response(404)
    info = _ckan(h).discover_dataset("https://data.opencity.in/dataset/bbmp-work-orders-and-payments-2024-25")
    assert info.discovered_via == "ckan_api" and info.fiscal_year == "2024-25"
    r = info.resources[0]
    assert r.resource_id == "83d27234-f87c-4a0b-a668-a58bd57b5034" and r.ward_regime == "243" and r.file_format == "csv"
    assert info.resources[1].file_format == "pdf" and info.meta["License"].startswith("Other")


def test_ckan_api_failure_falls_back_to_the_web_page():
    page = '<html><h1>BBMP Work Orders 2024-25</h1><a href="/dataset/x/resource/abcdef12-3456/download/f.csv">243 Wards Regime</a></html>'

    def h(req):
        if "package_show" in str(req.url):
            return httpx.Response(500)
        if req.url.path.endswith("robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})
    info = _ckan(h).discover_dataset("https://data.opencity.in/dataset/bbmp-work-orders-and-payments-2024-25")
    assert info.discovered_via == "html" and info.resources[0].download_url.endswith("f.csv")


# ----------------------------------------------------------------------------- IFMS
ROWS = [{"wbid": 101, "slno": "1", "wcname": "Yelahanka Division", "jobcode": "001-23-000012", "nameofwork": "Drilling of borewells in ward 01",
         "brdetails": "BR - 000042 / 10-Jul-2023CBR - / Rtgs - 004097 / 19-Jun-2024", "amount": 1167109, "remarks": ""}]


def test_ifms_row_normalisation():
    n = normalise_ifms_row(ROWS[0])
    assert n["job_number"] == "001-23-000012" and n["amount"] == 1167109 and n["bill_number"] == "000042"
    assert n["payment_reference"] == "004097" and n["ward_or_division"] == "Yelahanka Division" and "ward_number" not in n


def _ifms_run(handler):
    from app import ifms_collection as ic
    db = SessionLocal()
    for r in db.query(m.CollectionRun).filter_by(status="running"):
        r.status = "failed"
    db.commit()
    rid = ic.start_ifms_run(db, "test").id
    db.close()
    f = Fetcher(httpx.Client(transport=httpx.MockTransport(handler)), delay=0, retries=0, allowed_hosts=["accounts.bbmp.gov.in"])
    ic.execute_ifms_run(rid, fetcher=f, config={"ifms": {"search_terms": ["road", "drain"]}})
    db = SessionLocal()
    return db, db.get(m.CollectionRun, rid)


def test_ifms_collects_rows_once_and_flags_code_candidates(client):
    db = SessionLocal()
    for regime in ("198", "243"):  # the same published code in two regimes
        db.add(m.Roadwork(description="test work", work_order_id="001-23-000012", ward_regime=regime, external_key=f"test:ifms:{regime}"))
    db.commit(); db.close()

    def h(req):
        if req.url.path.endswith("robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, json=ROWS)
    db, run = _ifms_run(h)
    assert run.connector == "ifms" and run.records_inserted == 1 and run.records_duplicate == 1  # 2 terms, same row
    rec = db.query(m.WebRecord).filter_by(origin="ifms").one()
    assert rec.raw["jobcode"] == "001-23-000012"
    assert {c["ward_regime"] for c in rec.normalised["candidate_works"]} == {"198", "243"} and "verify" in rec.normalised["candidate_note"]
    db.delete(rec)  # leave the shared test database as it was for later tests
    db.query(m.Roadwork).filter(m.Roadwork.external_key.like("test:ifms:%")).delete(synchronize_session=False)
    db.commit(); db.close()


def test_ifms_server_error_is_reported_not_faked(client):
    def h(req):
        if req.url.path.endswith("robots.txt"):
            return httpx.Response(404)
        return httpx.Response(500, text="")
    db, run = _ifms_run(h)
    assert run.status == "failed" and run.records_inserted == 0
    assert any("HTTP 500 from the IFMS data service" in x["message"] for x in run.log)
    db.close()


# ----------------------------------------------------------------------------- tender suggestions: plausibility
def test_generic_wording_is_not_suggested_but_a_specific_match_is():
    from types import SimpleNamespace as N
    from app.matching import _plausibility, word_rarity
    works = [N(description=f"Improvements to roads and drains in ward no {i}", ward_regime="243", work_order_id=f"{i:03d}-18-000001") for i in range(1, 60)]
    lake = N(description="Development of Mangammanapalya Lake in ward no 190 Mangammanapalya", ward_regime="243", work_order_id="190-17-000026")
    rarity = word_rarity(works + [lake])
    t = N(title="Development of Mangammanapalya Lake in ward no 190", published_date=datetime(2017, 6, 1))
    ok, ev = _plausibility(t, lake, rarity)
    assert ok and "mangammanapalya" in ev["shared_rare_words"]
    generic = N(title="Improvements to roads and drains at KEB layout in ward no 12", published_date=datetime(2017, 6, 1))
    assert not _plausibility(generic, works[5], rarity)[0]
    late = N(title="Development of Mangammanapalya Lake in ward no 190", published_date=datetime(2011, 6, 1))
    ok, ev = _plausibility(late, lake, rarity)
    assert not ok and "within 2 years" in ev["rejected"]
