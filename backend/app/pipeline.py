"""Glue between the engines and the database.

  ingest_document    file -> Document + Extractions (+ tender from an NIT) -> matching
  locate_report      citizen report -> ward, nearest road segment, government-record status
  recluster          DBSCAN over live reports with stable cluster ids, then road/work links, severity, alerts
"""
import hashlib
import os
import shutil
from datetime import datetime
from sqlalchemy.orm import Session
from . import models as m
from . import spatial
from .alerts import run_alerts
from .clustering import cluster as run_cluster, severity_for, summarise
from .extract import process_document
from .matching import (doc_values, run_document_matching, match_tenders, suggest_roadworks_for_cluster, _first)
from .textnorm import parse_amount, parse_date, work_category, clean_id

DOC_DIR = os.getenv("DOCUMENT_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "uploads", "documents"))


# --------------------------------------------------------------------------- documents
def ingest_document(db: Session, path: str, filename: str, user=None, source_name: str | None = None,
                    is_demo: bool = False, use_llm: bool = False, match: bool = True, copy: bool = True):
    """Process one PDF. A byte-identical file that is already stored returns the existing record (no duplicates)."""
    with open(path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    existing = db.query(m.Document).filter_by(sha256=sha).first()
    if existing:
        return existing, False
    res = process_document(path, use_llm=use_llm)
    stored = path
    if copy:
        os.makedirs(DOC_DIR, exist_ok=True)
        stored = os.path.join(DOC_DIR, f"{sha[:16]}_{os.path.basename(filename)}")
        if os.path.abspath(stored) != os.path.abspath(path):
            shutil.copyfile(path, stored)
    doc = m.Document(filename=filename, path=stored, doc_type=res["doc_type"], doc_type_confidence=res["type_confidence"],
                     classification=res["classification"], pages=res["pages"], sha256=sha, uploaded_by=user.id if user else None,
                     source_name=source_name, text_chars=res["text_chars"], ocr_pages=res["ocr_pages"], is_demo=is_demo,
                     status="needs_review" if res["type_confidence"] < 0.5 or res["doc_type"] == "other" else "processed",
                     match_status="unmatched")
    db.add(doc); db.flush()
    for r in res["fields"]:
        db.add(m.Extraction(document_id=doc.id, field=r["field"], raw_value=r["raw_value"], norm_value=r.get("norm_value"),
                            page=r.get("page"), confidence=r.get("confidence"), method=r.get("method"),
                            snippet=r.get("snippet"), bbox=r.get("bbox")))
    db.flush()
    vals = doc_values(db, doc)
    dd = _first(vals, "document_date", 0.0)
    doc.doc_date = parse_date(dd["norm"]) if dd else None
    if doc.doc_type == "nit":
        upsert_tender_from_nit(db, doc, vals, is_demo)
    db.commit()
    if match:
        run_document_matching(db, user=user, doc_ids=[doc.id])
        if doc.doc_type == "nit":
            match_tenders(db, user)
    return doc, True


def upsert_tender_from_nit(db, doc, vals, is_demo=False):
    """A Notice Inviting Tender is itself the source for a tender record. Only values read from the document are stored."""
    g = lambda f: (_first(vals, f, 0.5) or {}).get("norm")
    tid, job = g("tender_id"), g("job_number")
    if not (tid or job):
        return None
    t = None
    if tid:
        t = db.query(m.Tender).filter(m.Tender.tender_id == tid).first()
    if not t and job:
        t = db.query(m.Tender).filter(m.Tender.job_number == job).first()
    if not t:
        t = m.Tender(tender_id=tid, job_number=job, is_demo=is_demo, source_name=f"Document {doc.filename}")
        db.add(t)
    t.document_id = doc.id
    t.title = t.title or (_first(vals, "description", 0.5) or {}).get("value")
    t.estimated_value = t.estimated_value or parse_amount(g("estimated_cost"))
    t.department = t.department or (_first(vals, "department", 0.5) or {}).get("value")
    t.location_text = t.location_text or (_first(vals, "location", 0.5) or {}).get("value")
    t.category = t.category or work_category(t.title or "")
    t.status = t.status or "notice_published"
    if doc.doc_date and not t.published_date:
        t.published_date = doc.doc_date
    db.flush()
    return t


# --------------------------------------------------------------------------- reports
def road_gov_status(db, road_id) -> str:
    """matched = a verified/auto road<->work link exists; possible = only suggested links; unmatched = none."""
    if not road_id:
        return "unmatched"
    sts = {l.status for l in db.query(m.RecordLink).filter_by(a_type="road", a_id=road_id, b_type="roadwork")}
    if sts & {"verified", "auto"}:
        return "matched"
    return "possible" if "suggested" in sts else "unmatched"


def locate_report(db: Session, r: m.Report):
    """Ward from coordinates and nearest road segment within 60 m. A report with no road stays valid (unmatched)."""
    w = spatial.ward_for_point(db, r.lat, r.lon)
    r.ward_id = w.id if w else None
    r.ward_label = spatial.ward_label(w) if w else None
    hit = spatial.nearest_segment(db, r.lat, r.lon, max_m=float(os.getenv("ROAD_SNAP_M", 60)))
    if hit:
        seg, dist = hit
        r.segment_id, r.road_id, r.road_distance_m, r.road_match_method = seg.id, seg.road_id, round(dist, 1), "nearest_segment"
    else:
        r.segment_id = r.road_id = r.road_distance_m = None
        r.road_match_method = "none"
    r.gov_match_status = road_gov_status(db, r.road_id)
    return r


def relocate_all_reports(db: Session):
    for r in db.query(m.Report):
        locate_report(db, r)
    db.commit()


# --------------------------------------------------------------------------- clustering with stable ids
def _majority(vals):
    vals = [v for v in vals if v is not None]
    return max(set(vals), key=vals.count) if vals else None


def recluster(db: Session, run_alert_rules: bool = True):
    """Re-run DBSCAN over live reports. Cluster ids are kept stable: a new group inherits the id of the old
    automatic cluster it overlaps most. Old clusters that no longer exist become 'dissolved' (kept for history).
    Verified and rejected clusters are an authority decision and are never rebuilt."""
    fixed = {c.id for c in db.query(m.Cluster).filter(m.Cluster.status.in_(["verified", "rejected"]))}
    reps = db.query(m.Report).filter(m.Report.is_duplicate.is_(False)).all()
    free = [r for r in reps if r.cluster_id not in fixed]
    photos = {}
    for p in db.query(m.ReportPhoto):
        if p.phash:
            photos.setdefault(p.report_id, []).append(p.phash)
    data = [{"lat": r.lat, "lon": r.lon, "created_at": r.created_at, "category": r.category, "description": r.description,
             "photo_hashes": photos.get(r.id) or ([r.photo_hash] if r.photo_hash else [])} for r in free]
    labels, pairs = run_cluster(data) if data else ([], {})
    groups = {}
    for idx, lab in enumerate(labels):
        if lab >= 0:
            groups.setdefault(lab, []).append(idx)
    old = {c.id: c for c in db.query(m.Cluster).filter(m.Cluster.status.in_(["auto", "dissolved"]))}
    old_members = {}
    for r in free:
        if r.cluster_id in old:
            old_members.setdefault(r.cluster_id, set()).add(r.id)
    # greedy assignment by overlap size
    cand = sorted(((len(old_members[cid] & {free[i].id for i in idxs}), cid, lab)
                   for lab, idxs in groups.items() for cid in old_members if old_members[cid] & {free[i].id for i in idxs}),
                  reverse=True)
    assigned_lab, used_old = {}, set()
    for ov, cid, lab in cand:
        if lab not in assigned_lab and cid not in used_old:
            assigned_lab[lab] = cid; used_old.add(cid)
    for r in free:
        r.cluster_id = None
    db.flush()
    # stale suggested cluster links are rebuilt; human decisions stay
    db.query(m.RecordLink).filter(m.RecordLink.a_type == "cluster", m.RecordLink.status == "suggested",
                                  m.RecordLink.a_id.in_([c for c in old])).delete(synchronize_session=False) if old else None
    road_names = {x.id: x.name for x in db.query(m.Road)}
    touched = set()
    for lab, idxs in groups.items():
        members = [free[i] for i in idxs]
        c = old.get(assigned_lab.get(lab))
        if c is None:
            c = m.Cluster(); db.add(c)
        cats = [r.category for r in members]
        c.category = max(set(cats), key=cats.count)
        c.lat = sum(r.lat for r in members) / len(members); c.lon = sum(r.lon for r in members) / len(members)
        c.report_count = len(members)
        c.first_at = min(r.created_at for r in members); c.last_at = max(r.created_at for r in members)
        c.status = "auto"
        c.road_id = _majority([r.road_id for r in members]); c.segment_id = _majority([r.segment_id for r in members])
        c.ward_label = _majority([r.ward_label for r in members])
        c.severity = severity_for(len(members), cats)
        c.summary = summarise(members, road_names.get(c.road_id), c.ward_label)
        ids = {free[i].id for i in idxs}
        c.evidence = {"pairs": [{"a": free[i].public_ref, "b": free[j].public_ref, **v} for (i, j), v in pairs.items() if i in idxs and j in idxs],
                      "signals": "distance, text similarity, photo similarity and time gap"}
        db.flush()
        for r in members:
            r.cluster_id = c.id
        touched.add(c.id)
        suggest_roadworks_for_cluster(db, c, members)
        # government status: same tiers as for roads, then fall back to cluster-level suggestions
        st = road_gov_status(db, c.road_id)
        if st == "unmatched" and db.query(m.RecordLink).filter_by(a_type="cluster", a_id=c.id, b_type="roadwork", status="suggested").first():
            st = "possible"
        c.gov_match_status = st
    for cid, c in old.items():
        if cid not in touched:
            c.status, c.report_count, c.public_status = "dissolved", 0, c.public_status
    for c in db.query(m.Cluster).filter(m.Cluster.status.in_(["verified", "rejected"])):
        members = db.query(m.Report).filter_by(cluster_id=c.id).all()
        c.report_count = len(members)
        if members and c.status == "verified":
            c.last_at = max(r.created_at for r in members)
    db.commit()
    if run_alert_rules:
        run_alerts(db)
    return {"clusters": len(touched), "clustered_reports": sum(len(v) for v in groups.values()), "reports": len(free)}
