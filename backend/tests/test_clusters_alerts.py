from datetime import datetime, timedelta
from app import models as m
from app.db import SessionLocal
from app.clustering import cluster


def test_clusters_exist_with_road_and_severity(client, seeded, authority):
    cl = client.get("/api/authority/clusters", headers=authority).json()
    assert len(cl) >= 15 and all(c["severity"] in ("low", "medium", "high") for c in cl)
    assert any(c["road"] for c in cl)


def test_cluster_ids_are_stable_across_reclustering(client, seeded, authority):
    before = {c["id"]: c["count"] for c in client.get("/api/authority/clusters", headers=authority).json()}
    assert client.post("/api/authority/recluster", headers=authority).status_code == 200
    after = {c["id"]: c["count"] for c in client.get("/api/authority/clusters", headers=authority).json()}
    assert before.keys() <= after.keys() or len(before.keys() & after.keys()) >= 0.9 * len(before)
    assert {k: v for k, v in before.items() if k in after and before[k] == after[k]}


def test_semantic_text_pulls_borderline_pair_together():
    t = datetime.utcnow()
    near = [dict(lat=12.9352, lon=77.6245, created_at=t, category="pothole", description="huge pothole near the school gate"),
            dict(lat=12.93535, lon=77.62475, created_at=t, category="damaged_road", description="big hole in road near school gate")]
    assert cluster(near)[0][0] == cluster(near)[0][1] != -1


def test_unrelated_categories_do_not_cluster():
    t = datetime.utcnow()
    reps = [dict(lat=12.9352, lon=77.6245, created_at=t, category="pothole", description="pothole"),
            dict(lat=12.93521, lon=77.62451, created_at=t, category="waterlogging", description="water collects here")]
    assert cluster(reps)[0] == [-1, -1]


def test_photo_similarity_helps():
    from app.photohash import similarity
    assert similarity("f" * 16, "f" * 16) == 1.0 and similarity("0" * 16, "f" * 16) == 0.0


def test_alert_rules_present_and_explain_without_accusing(client, seeded, authority):
    alerts = client.get("/api/authority/alerts?status=open", headers=authority).json()
    rules = {a["rule"] for a in alerts}
    assert {"multiple_reports", "repeat_days", "unmatched_recurring"} <= rules
    text = " ".join(a["title"] + a["explanation"] for a in alerts).lower()
    for banned in ("negligen", "corrupt", "fraud", "delayed by", "guilty"):
        assert banned not in text


def test_threshold_is_configurable_and_validated(client, seeded, authority):
    def n(rule): return len([a for a in client.get("/api/authority/alerts?status=open&rule=" + rule, headers=authority).json()])
    base = n("multiple_reports")
    r = client.put("/api/authority/alerts/settings/multiple_reports", headers=authority, data={"params_json": '{"min_reports": 6}'})
    assert r.status_code == 200 and n("multiple_reports") < base
    closed = client.get("/api/authority/alerts?status=auto_closed&rule=multiple_reports", headers=authority).json()
    assert closed, "alerts that stop applying are closed, not deleted"
    assert client.put("/api/authority/alerts/settings/multiple_reports", headers=authority, data={"params_json": '{"min_reports": 5000}'}).status_code == 422
    assert client.put("/api/authority/alerts/settings/multiple_reports", headers=authority, data={"params_json": '{"bogus": 1}'}).status_code == 422
    assert client.put("/api/authority/alerts/settings/nope", headers=authority, data={"params_json": "{}"}).status_code == 422
    client.put("/api/authority/alerts/settings/multiple_reports", headers=authority, data={"params_json": '{"min_reports": 3}'})
    assert n("multiple_reports") == base, "re-opened when the threshold is restored"


def test_disabled_rule_raises_nothing(client, seeded, authority):
    client.put("/api/authority/alerts/settings/surge", headers=authority, data={"enabled": "false"})
    assert client.get("/api/authority/alerts?status=open&rule=surge", headers=authority).json() == []
    client.put("/api/authority/alerts/settings/surge", headers=authority, data={"enabled": "true"})


def test_status_management_and_public_tracking(client, seeded, authority):
    cl = client.get("/api/authority/clusters", headers=authority).json()[0]
    r = client.post(f"/api/authority/clusters/{cl['id']}/status", headers=authority, data={"status": "under_review", "note_public": "Inspection booked"})
    assert r.status_code == 200 and r.json()["reports_updated"] >= 1
    d = client.get(f"/api/authority/clusters/{cl['id']}", headers=authority).json()
    ref = d["reports"][0]["ref"]
    assert d["public_status"] == "under_review" and ref
    assert client.post(f"/api/authority/clusters/{cl['id']}/status", headers=authority, data={"status": "nonsense"}).status_code == 422


def test_unmatched_issues_are_kept_visible(client, seeded, authority):
    u = client.get("/api/authority/unmatched", headers=authority).json()
    assert u["clusters"] and "does not show that no work exists" in u["note"]


def test_rejected_cluster_is_not_rebuilt(client, seeded, authority):
    """Rejected clusters stay listed for staff (so the decision can be reversed) but are frozen and hidden from the public."""
    cl = client.get("/api/authority/clusters?status=auto", headers=authority).json()[-1]
    assert client.post(f"/api/authority/clusters/{cl['id']}/review", headers=authority, data={"decision": "rejected"}).status_code == 200
    client.post("/api/authority/recluster", headers=authority)
    rows = {c["id"]: c for c in client.get("/api/authority/clusters", headers=authority).json()}
    assert rows[cl["id"]]["status"] == "rejected"
    assert all(c["id"] != cl["id"] for c in client.get("/api/public/map").json()["clusters"])
    assert client.get(f"/api/authority/clusters/{cl['id']}", headers=authority).json()["reports"], "its reports are kept"
