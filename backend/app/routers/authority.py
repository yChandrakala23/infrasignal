"""Authority dashboard API: complaints, clusters, alerts and their status management.
Read access for officer / authority / admin, write access for authority / admin. Contact details only for admin."""
import csv, io, os
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from ..db import get_db
from .. import models as m
from .. import alerts as alert_engine
from ..deps import CATEGORIES, REPORT_STATUSES, audit, authority, staff
from ..pipeline import recluster, road_gov_status
from ..matching import review_link, upsert_link, log_event
from .gov import link_json

router = APIRouter(prefix="/api/authority", tags=["authority dashboard"])
PUBLIC_CLUSTER_STATUS = ["received", "under_review", "forwarded", "work_started", "repair_reported", "closed"]


def _road_name(db, rid):
    r = db.get(m.Road, rid) if rid else None
    return (r.name or "Unnamed road") if r else None


@router.get("/overview")
def overview(days: int = Query(60, ge=7, le=365), u=Depends(staff), db: Session = Depends(get_db)):
    R = m.Report
    live = db.query(m.Cluster).filter(m.Cluster.status.notin_(["rejected", "dissolved"]))
    since = datetime.utcnow() - timedelta(days=days)
    reps = db.query(R).filter(R.created_at >= since, R.is_duplicate.is_(False)).all()
    per_day = {}
    for r in reps:
        per_day[r.created_at.date().isoformat()] = per_day.get(r.created_at.date().isoformat(), 0) + 1
    by_road = {}
    for r in db.query(R).filter(R.is_duplicate.is_(False), R.road_id.isnot(None)):
        by_road[r.road_id] = by_road.get(r.road_id, 0) + 1
    top_roads = sorted(by_road.items(), key=lambda x: -x[1])[:8]
    by_ward = dict(db.query(R.ward_label, func.count(R.id)).filter(R.is_duplicate.is_(False), R.ward_label.isnot(None)).group_by(R.ward_label).order_by(func.count(R.id).desc()).limit(8))
    unmatched_clusters = live.filter(m.Cluster.gov_match_status == "unmatched").count()
    return {"totals": {"reports": db.query(func.count(R.id)).filter(R.is_duplicate.is_(False)).scalar(), "last_period": len(reps),
                       "clusters": live.count(), "high_severity_clusters": live.filter(m.Cluster.severity == "high").count(),
                       "open_alerts": db.query(func.count(m.Alert.id)).filter(m.Alert.status == "open").scalar(),
                       "reports_without_road": db.query(func.count(R.id)).filter(R.road_id.is_(None), R.is_duplicate.is_(False)).scalar(),
                       "clusters_without_government_record": unmatched_clusters,
                       "sample_reports": db.query(func.count(R.id)).filter(R.is_demo.is_(True)).scalar()},
            "by_status": dict(db.query(R.status, func.count(R.id)).filter(R.is_duplicate.is_(False)).group_by(R.status)),
            "by_category": dict(db.query(R.category, func.count(R.id)).filter(R.is_duplicate.is_(False)).group_by(R.category)),
            "per_day": [{"day": d, "count": n} for d, n in sorted(per_day.items())],
            "top_roads": [{"road_id": i, "name": _road_name(db, i), "reports": n} for i, n in top_roads],
            "top_wards": [{"ward": k, "reports": n} for k, n in by_ward.items()],
            "alerts_by_severity": dict(db.query(m.Alert.severity, func.count(m.Alert.id)).filter(m.Alert.status == "open").group_by(m.Alert.severity))}


def _cluster_row(db, c):
    return {"id": c.id, "category": c.category, "category_label": CATEGORIES.get(c.category, c.category), "lat": c.lat, "lon": c.lon, "count": c.report_count,
            "first": c.first_at, "last": c.last_at, "status": c.status, "public_status": c.public_status, "severity": c.severity, "summary": c.summary,
            "road_id": c.road_id, "road": _road_name(db, c.road_id), "ward": c.ward_label, "gov_match_status": c.gov_match_status,
            "open_alerts": db.query(func.count(m.Alert.id)).filter(m.Alert.cluster_id == c.id, m.Alert.status == "open").scalar()}


@router.get("/clusters")
def clusters(status: str = "", category: str = "", severity: str = "", gov: str = "", ward: str = "", road_id: int | None = None, q: str = "",
             sort: str = "count", u=Depends(staff), db: Session = Depends(get_db)):
    qs = db.query(m.Cluster)
    qs = qs.filter(m.Cluster.status == status) if status else qs.filter(m.Cluster.status.notin_(["dissolved"]))
    if category: qs = qs.filter(m.Cluster.category == category)
    if severity: qs = qs.filter(m.Cluster.severity == severity)
    if gov: qs = qs.filter(m.Cluster.gov_match_status == gov)
    if ward: qs = qs.filter(m.Cluster.ward_label.ilike(f"%{ward}%"))
    if road_id: qs = qs.filter(m.Cluster.road_id == road_id)
    if q: qs = qs.filter(m.Cluster.summary.ilike(f"%{q}%"))
    order = {"count": m.Cluster.report_count.desc(), "recent": m.Cluster.last_at.desc(), "oldest": m.Cluster.first_at.asc()}.get(sort, m.Cluster.report_count.desc())
    return [_cluster_row(db, c) for c in qs.order_by(order).limit(300)]


@router.get("/clusters/{cid}")
def cluster_detail(cid: int, u=Depends(staff), db: Session = Depends(get_db)):
    c = db.get(m.Cluster, cid)
    if not c: raise HTTPException(404)
    reps = db.query(m.Report).filter_by(cluster_id=cid).order_by(m.Report.created_at).all()
    photos = {}
    for p in db.query(m.ReportPhoto).filter(m.ReportPhoto.report_id.in_([r.id for r in reps] or [0])):
        photos.setdefault(p.report_id, []).append(p.id)
    links = db.query(m.RecordLink).filter_by(a_type="cluster", a_id=cid).order_by(m.RecordLink.confidence.desc()).all()
    alerts = db.query(m.Alert).filter_by(cluster_id=cid).order_by(m.Alert.id.desc()).all()
    return {**_cluster_row(db, c), "note_internal": c.note_internal, "evidence": c.evidence,
            "reports": [{"id": r.id, "ref": r.public_ref, "category": r.category, "description": r.description, "lat": r.lat, "lon": r.lon, "road_name_typed": r.road_name,
                         "status": r.status, "created_at": r.created_at, "photos": photos.get(r.id, []) or ([0] if r.photo_path else []), "sample": r.is_demo,
                         "road_distance_m": r.road_distance_m} for r in reps],
            "related_works": [link_json(db, l) for l in links],
            "related_works_note": "Links are suggestions from the road and ward match. Only an officer-verified link is shown to the public.",
            "alerts": [{"id": a.id, "rule": a.rule, "severity": a.severity, "title": a.title, "status": a.status, "created_at": a.created_at, "updated_at": a.updated_at} for a in alerts]}


@router.post("/clusters/{cid}/review")
def review_cluster(cid: int, decision: str = Form(...), public_status: str = Form(""), note: str = Form(""), u=Depends(authority), db: Session = Depends(get_db)):
    c = db.get(m.Cluster, cid)
    if not c or decision not in ("verified", "rejected", "auto"):
        raise HTTPException(422, "Decision must be verified, rejected or auto (reopen)")
    if public_status and public_status not in PUBLIC_CLUSTER_STATUS: raise HTTPException(422, "Unknown public status")
    c.status = decision
    if public_status: c.public_status = public_status
    if note: c.note_internal = note[:2000]
    audit(db, u, "cluster_review", "cluster", cid, {"decision": decision, "public_status": public_status}); db.commit()
    return {"ok": True}


@router.post("/clusters/{cid}/status")
def set_cluster_status(cid: int, status: str = Form(...), note_public: str = Form(""), u=Depends(authority), db: Session = Depends(get_db)):
    """Move the cluster and every report in it to a new status, with a public note."""
    if status not in REPORT_STATUSES: raise HTTPException(422, "Unknown status")
    c = db.get(m.Cluster, cid)
    if not c: raise HTTPException(404)
    if status in PUBLIC_CLUSTER_STATUS: c.public_status = status
    n = 0
    for r in db.query(m.Report).filter_by(cluster_id=cid):
        if r.status != status:
            r.status = status
            db.add(m.ReportStatus(report_id=r.id, status=status, note_public=note_public[:500], by_user=u.id)); n += 1
    audit(db, u, "cluster_status", "cluster", cid, {"status": status, "reports": n}); db.commit()
    return {"ok": True, "reports_updated": n}


@router.post("/clusters/{cid}/link")
def link_cluster_to_work(cid: int, roadwork_id: int = Form(...), u=Depends(authority), db: Session = Depends(get_db)):
    c, w = db.get(m.Cluster, cid), db.get(m.Roadwork, roadwork_id)
    if not c or not w: raise HTTPException(404, "Cluster or roadwork not found")
    link, _ = upsert_link(db, "cluster", cid, "roadwork", roadwork_id, "manual", 1.0, {"note": "linked by an authority user"}, "verified")
    review_link(db, link, "verified", u)
    c.gov_match_status = "matched"
    audit(db, u, "cluster_link", "cluster", cid, {"roadwork_id": roadwork_id}); db.commit()
    return {"ok": True}


@router.patch("/links/{lid}")
def set_cluster_link(lid: int, status: str = Form(...), u=Depends(authority), db: Session = Depends(get_db)):
    l = db.get(m.RecordLink, lid)
    if not l or l.a_type != "cluster" or status not in ("verified", "rejected", "unverified"): raise HTTPException(422)
    review_link(db, l, status, u)
    audit(db, u, "link_review", "record_link", lid, {"status": status}); db.commit()
    return {"ok": True}


@router.get("/roadworks")
def search_roadworks(q: str = Query("", max_length=80), u=Depends(staff), db: Session = Depends(get_db)):
    like = f"%{q}%"
    qs = db.query(m.Roadwork).filter(m.Roadwork.description.ilike(like) | m.Roadwork.job_number.ilike(like) | m.Roadwork.work_order_id.ilike(like),
                                     m.Roadwork.verification_status != "merged").limit(20)
    return [{"id": w.id, "job_number": w.job_number, "description": w.description, "sample": w.is_demo} for w in qs]


# ----------------------------------------------------------------------------- reports
def _photo_ids(db, r):
    ids = [p.id for p in db.query(m.ReportPhoto).filter_by(report_id=r.id)]
    return ids or ([0] if r.photo_path else [])


@router.get("/reports")
def reports(status: str = "", category: str = "", cluster: str = "", road_id: int | None = None, no_road: bool = False, q: str = "",
            page: int = 1, u=Depends(staff), db: Session = Depends(get_db)):
    R = m.Report
    qs = db.query(R)
    if status: qs = qs.filter(R.status == status)
    if category: qs = qs.filter(R.category == category)
    if cluster == "none": qs = qs.filter(R.cluster_id.is_(None))
    elif cluster == "any": qs = qs.filter(R.cluster_id.isnot(None))
    if road_id: qs = qs.filter(R.road_id == road_id)
    if no_road: qs = qs.filter(R.road_id.is_(None))
    if q: qs = qs.filter(R.description.ilike(f"%{q}%") | R.public_ref.ilike(f"%{q}%"))
    total = qs.count()
    items = qs.order_by(R.created_at.desc()).offset((page - 1) * 50).limit(50).all()
    return {"total": total, "items": [{"id": r.id, "ref": r.public_ref, "category": r.category, "description": r.description, "lat": r.lat, "lon": r.lon,
                                       "road": _road_name(db, r.road_id), "road_name_typed": r.road_name, "ward": r.ward_label, "status": r.status, "cluster_id": r.cluster_id,
                                       "photos": len(_photo_ids(db, r)), "photo_ids": _photo_ids(db, r),
                                       "contact": (r.contact if u.role == "admin" else ("provided" if r.contact else None)), "sample": r.is_demo,
                                       "duplicate": r.is_duplicate, "gov_match_status": r.gov_match_status, "created_at": r.created_at} for r in items]}


@router.get("/photos/{photo_id}")
def photo(photo_id: int, report_id: int | None = None, u=Depends(staff), db: Session = Depends(get_db)):
    """photo_id 0 + report_id = legacy single photo stored on the report row."""
    if photo_id == 0 and report_id:
        r = db.get(m.Report, report_id)
        path = r.photo_path if r else None
    else:
        p = db.get(m.ReportPhoto, photo_id)
        path = p.path if p else None
    if not path or not os.path.exists(path): raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@router.patch("/reports/{rid}/status")
def set_status(rid: int, status: str = Form(...), note_public: str = Form(""), note_internal: str = Form(""), u=Depends(authority), db: Session = Depends(get_db)):
    if status not in REPORT_STATUSES: raise HTTPException(422, "Unknown status")
    r = db.get(m.Report, rid)
    if not r: raise HTTPException(404)
    r.status = status
    db.add(m.ReportStatus(report_id=rid, status=status, note_public=note_public[:500], note_internal=note_internal[:2000], by_user=u.id))
    audit(db, u, "report_status", "report", rid, {"status": status}); db.commit()
    return {"ok": True}


@router.post("/reports/{rid}/move")
def move_report(rid: int, cluster_id: int | None = Form(None), u=Depends(authority), db: Session = Depends(get_db)):
    """A manual correction freezes the clusters involved so automatic re-clustering will not undo it."""
    r = db.get(m.Report, rid)
    if not r: raise HTTPException(404)
    if cluster_id is not None and not db.get(m.Cluster, cluster_id): raise HTTPException(404, "Target cluster not found")
    old = r.cluster_id
    r.cluster_id = cluster_id
    for cid in {old, cluster_id} - {None}:
        c = db.get(m.Cluster, cid)
        c.status = "verified"
        c.report_count = db.query(m.Report).filter(m.Report.cluster_id == cid).count()
    audit(db, u, "move_report", "report", rid, {"from": old, "to": cluster_id}); db.commit()
    return {"ok": True}


@router.get("/unmatched")
def unmatched(u=Depends(staff), db: Session = Depends(get_db)):
    """Issues for which no government record could be linked. Kept visible on purpose."""
    cl = db.query(m.Cluster).filter(m.Cluster.status.notin_(["rejected", "dissolved"]), m.Cluster.gov_match_status == "unmatched").order_by(m.Cluster.report_count.desc()).all()
    singles = db.query(m.Report).filter(m.Report.cluster_id.is_(None), m.Report.is_duplicate.is_(False), m.Report.gov_match_status == "unmatched").order_by(m.Report.created_at.desc()).limit(200).all()
    return {"clusters": [_cluster_row(db, c) for c in cl],
            "reports": [{"id": r.id, "ref": r.public_ref, "category": r.category, "road": _road_name(db, r.road_id), "ward": r.ward_label, "created_at": r.created_at, "sample": r.is_demo} for r in singles],
            "note": "No linked tender or work order was found in the records loaded. That does not show that no work exists."}


# ----------------------------------------------------------------------------- alerts
@router.get("/alerts")
def alerts(status: str = "open", rule: str = "", severity: str = "", u=Depends(staff), db: Session = Depends(get_db)):
    qs = db.query(m.Alert)
    if status != "all": qs = qs.filter(m.Alert.status == status)
    if rule: qs = qs.filter(m.Alert.rule == rule)
    if severity: qs = qs.filter(m.Alert.severity == severity)
    order = {"high": 0, "medium": 1, "low": 2}
    items = sorted(qs.limit(500).all(), key=lambda a: (order.get(a.severity, 3), -a.id))
    return [{"id": a.id, "rule": a.rule, "rule_title": alert_engine.RULE_TITLES.get(a.rule, a.rule), "severity": a.severity, "title": a.title, "explanation": a.explanation,
             "evidence": a.evidence, "cluster_id": a.cluster_id, "roadwork_id": a.roadwork_id, "road": _road_name(db, a.road_id), "ward": a.ward_label,
             "status": a.status, "created_at": a.created_at, "updated_at": a.updated_at, "resolution_note": a.resolution_note} for a in items]


@router.patch("/alerts/{aid}")
def update_alert(aid: int, status: str = Form(...), note: str = Form(""), u=Depends(authority), db: Session = Depends(get_db)):
    a = db.get(m.Alert, aid)
    if not a or status not in ("open", "acknowledged", "dismissed", "resolved"): raise HTTPException(422, "Unknown alert or status")
    a.status, a.updated_at, a.updated_by = status, datetime.utcnow(), u.id
    if note: a.resolution_note = note[:1000]
    audit(db, u, "alert_status", "alert", aid, {"status": status}); db.commit()
    return {"ok": True}


@router.get("/alerts/settings")
def get_alert_settings(u=Depends(staff), db: Session = Depends(get_db)):
    s = alert_engine.get_settings(db)
    return [{"rule": k, "title": alert_engine.RULE_TITLES[k], **v} for k, v in s.items()]


@router.put("/alerts/settings/{rule}")
def put_alert_setting(rule: str, enabled: bool | None = Form(None), params_json: str = Form(""), u=Depends(authority), db: Session = Depends(get_db)):
    import json
    try:
        params = json.loads(params_json) if params_json else None
        out = alert_engine.set_setting(db, rule, enabled, params, u)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e))
    audit(db, u, "alert_setting", "alert_setting", 0, {"rule": rule, "enabled": enabled, "params": params})
    alert_engine.run_alerts(db)
    db.commit()
    return out


@router.post("/recluster")
def force_recluster(u=Depends(authority), db: Session = Depends(get_db)):
    out = recluster(db); audit(db, u, "recluster", "system", 0, out); db.commit()
    return out


@router.get("/export/clusters.csv")
def export_clusters(u=Depends(staff), db: Session = Depends(get_db)):
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["cluster_id", "category", "reports", "first", "last", "road", "ward", "severity", "status", "government_record_status"])
    for c in db.query(m.Cluster).filter(m.Cluster.status.notin_(["dissolved"])).order_by(m.Cluster.id):
        w.writerow([c.id, c.category, c.report_count, c.first_at, c.last_at, _road_name(db, c.road_id), c.ward_label, c.severity, c.status, c.gov_match_status])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=clusters.csv"})
