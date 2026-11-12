import os
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from . import models as m
from .clustering import haversine_m

MIN_CLUSTER = int(os.getenv("ALERT_MIN_CLUSTER", 3))


def _upsert(db, key, **kw):
    a = db.query(m.Alert).filter_by(dedupe_key=key).first()
    if a:
        a.evidence = kw.get("evidence", a.evidence)
        a.explanation = kw.get("explanation", a.explanation)
        return a
    a = m.Alert(dedupe_key=key, **kw)
    db.add(a)
    return a


def run_alerts(db: Session, now: datetime | None = None) -> int:
    now = now or datetime.utcnow()
    count = 0
    clusters = db.query(m.Cluster).filter(m.Cluster.status != "rejected").all()
    for c in clusters:
        reps = db.query(m.Report).filter_by(cluster_id=c.id).all()
        ids = [r.public_ref for r in reps]
        if c.report_count >= MIN_CLUSTER:
            _upsert(db, f"multi:{c.id}", rule="multiple_reports", severity="medium", cluster_id=c.id,
                    title=f"{c.report_count} reports at one location",
                    explanation=f"{c.report_count} reports within the clustering radius. "
                                "This shows repeated reporting, not the cause of the problem.",
                    evidence={"reports": ids}); count += 1
        if c.first_at and c.last_at and (c.last_at - c.first_at).days >= 30:
            _upsert(db, f"recur:{c.id}", rule="recurring", severity="medium", cluster_id=c.id,
                    title="Issue reported repeatedly over more than 30 days",
                    explanation=f"First report {c.first_at:%d %b %Y}, latest {c.last_at:%d %b %Y}.",
                    evidence={"reports": ids}); count += 1
    # Surge: last 7 days versus the previous 28 days, within 200 m
    recent = db.query(m.Report).filter(m.Report.created_at >= now - timedelta(days=7),
                                       m.Report.is_duplicate.is_(False)).all()
    older = db.query(m.Report).filter(m.Report.created_at < now - timedelta(days=7),
                                      m.Report.created_at >= now - timedelta(days=35)).all()
    for r in recent:
        near_recent = [x for x in recent if haversine_m(r.lat, r.lon, x.lat, x.lon) <= 200]
        near_old = [x for x in older if haversine_m(r.lat, r.lon, x.lat, x.lon) <= 200]
        weekly_base = len(near_old) / 4
        if len(near_recent) >= 4 and len(near_recent) >= 3 * max(weekly_base, 1):
            key = f"surge:{round(r.lat, 3)}:{round(r.lon, 3)}:{now:%Y%W}"
            _upsert(db, key, rule="surge", severity="high",
                    title=f"Surge: {len(near_recent)} reports in 7 days within 200 m",
                    explanation=f"Previous 4 weeks averaged {weekly_base:.1f} reports per week nearby.",
                    evidence={"reports": [x.public_ref for x in near_recent]}); count += 1
    # Roadworks linked to two or more clusters
    links = db.query(m.RecordLink).filter(m.RecordLink.b_type == "roadwork",
                                          m.RecordLink.status.in_(["suggested", "verified"])).all()
    per_rw = {}
    for l in links:
        per_rw.setdefault(l.b_id, []).append(l.a_id)
    for rw_id, cl in per_rw.items():
        if len(set(cl)) >= 2:
            _upsert(db, f"rwmulti:{rw_id}", rule="roadwork_multi_clusters", severity="medium",
                    roadwork_id=rw_id, title="Several issue clusters suggested for one roadwork",
                    explanation="These are suggested links that need review. Not evidence of fault.",
                    evidence={"clusters": sorted(set(cl))}); count += 1
    # Incomplete public information
    for w in db.query(m.Roadwork).all():
        missing = [f for f in ("tender_id", "contractor_id", "ward_number", "sanctioned_amount", "work_order_date")
                   if getattr(w, f) in (None, "")]
        if len(missing) >= 3:
            _upsert(db, f"gap:{w.id}", rule="incomplete_information", severity="low", roadwork_id=w.id,
                    title="Roadwork has incomplete public information",
                    explanation="Missing fields: " + ", ".join(missing),
                    evidence={"missing": missing}); count += 1
    db.commit()
    return count
