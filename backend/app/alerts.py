"""Alert engine with configurable thresholds (stored in alert_settings, defaults below).

Rules
  multiple_reports        a cluster reaches N reports
  repeat_days             a cluster has reports on N or more different days  (repeated complaints over several days)
  long_running            a cluster's first and latest report are N+ days apart
  surge                   N+ reports within R metres in the last D days, and >= ratio x the weekly baseline
  road_hotspot            N+ reports on one road segment in D days (does not need a DBSCAN cluster)
  unmatched_recurring     a recurring cluster on a road with no government work record
  roadwork_multi_clusters several clusters are linked to one roadwork (links are suggestions, not findings)
  incomplete_information  a roadwork record is missing several public fields (off by default: it is a data-quality note)

Alerts describe patterns in reports and records. They never state negligence, delay or misconduct.
Alert history is kept: an alert whose condition stops being true is set to 'auto_closed', never deleted.
"""
import os
from datetime import datetime, timedelta
import numpy as np
from sqlalchemy.orm import Session
from . import models as m
from .spatial import haversine_m

DEFAULTS = {
    "multiple_reports": {"enabled": True, "params": {"min_reports": int(os.getenv("ALERT_MIN_CLUSTER", 3))}},
    "repeat_days": {"enabled": True, "params": {"min_reports": 3, "min_distinct_days": 3}},
    "long_running": {"enabled": True, "params": {"min_span_days": 30}},
    "surge": {"enabled": True, "params": {"min_reports": 4, "ratio": 3.0, "radius_m": 200, "window_days": 7, "baseline_days": 28}},
    "road_hotspot": {"enabled": True, "params": {"min_reports": 4, "window_days": 30}},
    "unmatched_recurring": {"enabled": True, "params": {"min_reports": 3}},
    "roadwork_multi_clusters": {"enabled": True, "params": {"min_clusters": 2}},
    "incomplete_information": {"enabled": False, "params": {"min_missing": 3}},
}
LIMITS = {"min_reports": (2, 100), "min_distinct_days": (2, 60), "min_span_days": (2, 365), "ratio": (1.0, 20.0),
          "radius_m": (20, 2000), "window_days": (1, 180), "baseline_days": (7, 180), "min_clusters": (2, 20), "min_missing": (1, 10)}
RULE_TITLES = {
    "multiple_reports": "Multiple reports at one location", "repeat_days": "Repeated reports over several days",
    "long_running": "Long-running reported issue", "surge": "Sudden increase in reports", "road_hotspot": "Road segment hotspot",
    "unmatched_recurring": "Recurring issue with no government work record", "roadwork_multi_clusters": "Several issue clusters near one roadwork",
    "incomplete_information": "Roadwork with incomplete public information"}


def get_settings(db: Session):
    out = {k: {"enabled": v["enabled"], "params": dict(v["params"]), "default_params": dict(v["params"])} for k, v in DEFAULTS.items()}
    for s in db.query(m.AlertSetting).all():
        if s.rule in out:
            out[s.rule]["enabled"] = bool(s.enabled)
            out[s.rule]["params"].update(s.params or {})
    return out


def set_setting(db: Session, rule: str, enabled: bool | None, params: dict | None, user):
    if rule not in DEFAULTS:
        raise ValueError("Unknown alert rule")
    clean = {}
    for k, v in (params or {}).items():
        if k not in DEFAULTS[rule]["params"]:
            raise ValueError(f"Unknown parameter '{k}' for {rule}")
        lo, hi = LIMITS[k]
        v = float(v)
        if not (lo <= v <= hi):
            raise ValueError(f"{k} must be between {lo} and {hi}")
        clean[k] = v if k == "ratio" else int(v)
    row = db.query(m.AlertSetting).filter_by(rule=rule).first() or m.AlertSetting(rule=rule, params={})
    if row.id is None:
        db.add(row)
    if enabled is not None:
        row.enabled = bool(enabled)
    row.params = {**(row.params or {}), **clean}
    row.updated_by = user.id if user else None
    row.updated_at = datetime.utcnow()
    db.commit()
    return get_settings(db)[rule]


def _upsert(db, key, seen, **kw):
    seen.add(key)
    a = db.query(m.Alert).filter_by(dedupe_key=key).first()
    if a:
        a.evidence = kw.get("evidence", a.evidence)
        a.explanation = kw.get("explanation", a.explanation)
        a.title = kw.get("title", a.title)
        if a.status == "auto_closed":  # the condition is true again
            a.status, a.updated_at = "open", datetime.utcnow()
        return a
    a = m.Alert(dedupe_key=key, **kw)
    db.add(a)
    db.flush()  # session has autoflush off; later look-ups in the same run must see this row
    return a


def run_alerts(db: Session, now: datetime | None = None) -> int:
    """Evaluate every enabled rule. Returns the number of alert conditions currently true."""
    now = now or datetime.utcnow()
    cfg = get_settings(db)
    seen: set[str] = set()
    on = lambda r: cfg[r]["enabled"]
    P = lambda r: cfg[r]["params"]
    clusters = db.query(m.Cluster).filter(m.Cluster.status.notin_(["rejected", "dissolved"])).all()
    road_names = {r.id: r.name for r in db.query(m.Road)}
    for c in clusters:
        reps = db.query(m.Report).filter_by(cluster_id=c.id).all()
        if not reps:
            continue
        ids = [r.public_ref for r in reps]
        where = road_names.get(c.road_id) or "an unnamed or unmapped road"
        common = dict(cluster_id=c.id, road_id=c.road_id, ward_label=c.ward_label)
        days = {r.created_at.date() for r in reps}
        if on("multiple_reports") and c.report_count >= P("multiple_reports")["min_reports"]:
            _upsert(db, f"multi:{c.id}", seen, rule="multiple_reports", severity=c.severity or "medium", **common,
                    title=f"{c.report_count} reports at one location on {where}",
                    explanation=f"{c.report_count} reports within the clustering radius. This shows repeated reporting, not the cause of the problem.",
                    evidence={"reports": ids, "threshold": P("multiple_reports")["min_reports"]})
        p = P("repeat_days")
        if on("repeat_days") and len(reps) >= p["min_reports"] and len(days) >= p["min_distinct_days"]:
            _upsert(db, f"repeat:{c.id}", seen, rule="repeat_days", severity="medium", **common,
                    title=f"Reported on {len(days)} different days on {where}",
                    explanation=f"{len(reps)} reports on {len(days)} separate days at the same spot.",
                    evidence={"reports": ids, "days": sorted(str(d) for d in days)})
        if on("long_running") and c.first_at and c.last_at and (c.last_at - c.first_at).days >= P("long_running")["min_span_days"]:
            _upsert(db, f"recur:{c.id}", seen, rule="long_running", severity="medium", **common,
                    title=f"Issue reported over more than {P('long_running')['min_span_days']} days on {where}",
                    explanation=f"First report {c.first_at:%d %b %Y}, latest {c.last_at:%d %b %Y}.", evidence={"reports": ids})
        if on("unmatched_recurring") and c.gov_match_status == "unmatched" and c.report_count >= P("unmatched_recurring")["min_reports"]:
            _upsert(db, f"unmatched:{c.id}", seen, rule="unmatched_recurring", severity="medium", **common,
                    title=f"{c.report_count} reports, no government work record found",
                    explanation="No tender or work order could be linked to this road. A missing record does not mean no work was done; "
                                "the records imported so far may simply not cover it.",
                    evidence={"reports": ids})
    # surge
    if on("surge"):
        p = P("surge")
        recent = db.query(m.Report).filter(m.Report.created_at >= now - timedelta(days=p["window_days"]), m.Report.is_duplicate.is_(False)).all()
        older = db.query(m.Report).filter(m.Report.created_at < now - timedelta(days=p["window_days"]),
                                          m.Report.created_at >= now - timedelta(days=p["window_days"] + p["baseline_days"]),
                                          m.Report.is_duplicate.is_(False)).all()
        if recent:
            covered = set()
            for r in sorted(recent, key=lambda x: x.created_at, reverse=True):
                if r.id in covered:
                    continue
                near_recent = [x for x in recent if haversine_m(r.lat, r.lon, x.lat, x.lon) <= p["radius_m"]]
                if len(near_recent) < p["min_reports"]:
                    continue
                n_old = sum(1 for x in older if haversine_m(r.lat, r.lon, x.lat, x.lon) <= p["radius_m"])
                weekly = n_old / max(p["baseline_days"] / 7, 1)
                if len(near_recent) >= p["ratio"] * max(weekly, 1):
                    covered |= {x.id for x in near_recent}
                    key = f"surge:{round(r.lat, 3)}:{round(r.lon, 3)}:{now:%Y%W}"
                    _upsert(db, key, seen, rule="surge", severity="high", ward_label=r.ward_label, road_id=r.road_id,
                            title=f"Surge: {len(near_recent)} reports in {p['window_days']} days within {p['radius_m']} m",
                            explanation=f"The previous {p['baseline_days']} days averaged {weekly:.1f} reports per week nearby.",
                            evidence={"reports": [x.public_ref for x in near_recent], "weekly_baseline": round(weekly, 2)})
    # per-segment hotspot
    if on("road_hotspot"):
        p = P("road_hotspot")
        since = now - timedelta(days=p["window_days"])
        per = {}
        for r in db.query(m.Report).filter(m.Report.created_at >= since, m.Report.is_duplicate.is_(False), m.Report.segment_id.isnot(None)):
            per.setdefault(r.segment_id, []).append(r)
        for sid, reps in per.items():
            if len(reps) >= p["min_reports"]:
                seg = db.get(m.RoadSegment, sid)
                where = road_names.get(seg.road_id) or "an unnamed road"
                _upsert(db, f"hotspot:{sid}", seen, rule="road_hotspot", severity="medium", road_id=seg.road_id,
                        ward_label=f"Ward {seg.ward_number} {seg.ward_name or ''}".strip() if seg.ward_number else None,
                        title=f"{len(reps)} reports on one segment of {where} in {p['window_days']} days",
                        explanation="Reports are spread along the same road segment even if they are not close enough to form one cluster.",
                        evidence={"reports": [r.public_ref for r in reps], "segment_id": sid})
    # government-record alerts
    if on("roadwork_multi_clusters"):
        per_rw = {}
        for l in db.query(m.RecordLink).filter(m.RecordLink.a_type == "cluster", m.RecordLink.b_type == "roadwork",
                                               m.RecordLink.status.in_(["suggested", "verified"])):
            per_rw.setdefault(l.b_id, set()).add(l.a_id)
        live = {c.id for c in clusters}
        for rw_id, cl in per_rw.items():
            cl = cl & live
            if len(cl) >= P("roadwork_multi_clusters")["min_clusters"]:
                _upsert(db, f"rwmulti:{rw_id}", seen, rule="roadwork_multi_clusters", severity="medium", roadwork_id=rw_id,
                        title="Several issue clusters suggested for one roadwork",
                        explanation="These are suggested links that need review. Not evidence of fault.",
                        evidence={"clusters": sorted(cl)})
    if on("incomplete_information"):
        for w in db.query(m.Roadwork).all():
            missing = [f for f in ("tender_id", "contractor_id", "ward_number", "sanctioned_amount", "work_order_date") if getattr(w, f) in (None, "")]
            if len(missing) >= P("incomplete_information")["min_missing"]:
                _upsert(db, f"gap:{w.id}", seen, rule="incomplete_information", severity="low", roadwork_id=w.id,
                        title="Roadwork has incomplete public information", explanation="Missing fields: " + ", ".join(missing),
                        evidence={"missing": missing})
    # history is kept: open alerts whose condition no longer holds are closed, not deleted
    managed = {"multi", "repeat", "recur", "unmatched", "surge", "hotspot", "rwmulti", "gap"}
    for a in db.query(m.Alert).filter(m.Alert.status == "open"):
        if a.dedupe_key not in seen and a.dedupe_key.split(":")[0] in managed:
            a.status, a.updated_at = "auto_closed", now
            a.resolution_note = "Condition no longer true after re-evaluation (cluster changed, threshold changed or rule disabled)."
    db.commit()
    return len(seen)
