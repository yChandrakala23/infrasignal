"""Public portal API. No login. Never returns contact details, internal notes, photos or unverified cluster-work links."""
import io, json, os, secrets, uuid
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from PIL import Image
from shapely.geometry import shape, mapping
from sqlalchemy import func
from sqlalchemy.orm import Session
from ..db import get_db
from .. import models as m, spatial
from ..deps import BBOX, CATEGORIES, UPLOAD
from ..pipeline import locate_report, recluster, road_gov_status
from ..photohash import dhash
from ..security import hash_secret
from ..textnorm import norm_road_name

router = APIRouter(tags=["public"])
MAX_PHOTOS = 3
WORK_STATUS_TEXT = {"not_stated": "No status stated in the records loaded", "work_order_issued": "Work order on file",
                    "completion_certificate_on_file": "Completion certificate on file"}


@router.get("/api/public/categories")
def categories():
    return [{"key": k, "label": v} for k, v in CATEGORIES.items()]


@router.post("/api/reports")
async def submit_report(request: Request, category: str = Form(...), description: str = Form(""),
                        lat: float = Form(...), lon: float = Form(...), accuracy_m: float = Form(None),
                        road_name: str = Form(""), contact: str = Form(""),
                        photo: UploadFile | None = File(None), photos: list[UploadFile] = File(default=[]),
                        db: Session = Depends(get_db)):
    if category not in CATEGORIES:
        raise HTTPException(422, "Unknown category")
    if not (BBOX[0] <= lat <= BBOX[1] and BBOX[2] <= lon <= BBOX[3]):
        raise HTTPException(422, "Location is outside Bengaluru")
    if len(description) > 1000:
        raise HTTPException(422, "Description too long (1000 characters maximum)")
    if len(road_name) > 200 or len(contact) > 200:
        raise HTTPException(422, "Road name or contact too long")
    if category == "other" and len(description.strip()) < 5:
        raise HTTPException(422, "Please describe the problem when the category is 'Other'")
    client = hash_secret(request.client.host if request.client else "unknown")
    hour_ago = datetime.utcnow() - timedelta(hours=1)
    recent = db.query(m.Report).filter(m.Report.client_hash == client, m.Report.created_at > hour_ago).all()
    if len(recent) >= int(os.getenv("REPORT_RATE_PER_HOUR", 5)):
        raise HTTPException(429, "Too many reports in the last hour")
    dup = any(r.description == description and abs(r.lat - lat) < 1e-4 and abs(r.lon - lon) < 1e-4
              and r.created_at > datetime.utcnow() - timedelta(minutes=10) for r in recent)
    files = [f for f in ([photo] if photo else []) + list(photos) if f and f.filename]
    if len(files) > MAX_PHOTOS:
        raise HTTPException(422, f"At most {MAX_PHOTOS} photos")
    saved = []
    for f in files:
        raw = await f.read()
        if len(raw) > 5 * 1024 * 1024:
            raise HTTPException(422, "Photo larger than 5 MB")
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")  # re-encoding drops EXIF (privacy)
        except Exception:
            raise HTTPException(422, "File is not a valid image")
        h = dhash(img)
        img.thumbnail((1600, 1600))
        path = os.path.join(UPLOAD, f"{uuid.uuid4().hex}.jpg")
        img.save(path, "JPEG", quality=85)
        saved.append((path, h))
    ref = "IS" + secrets.token_hex(4).upper()
    token = secrets.token_urlsafe(12)
    r = m.Report(public_ref=ref, token_hash=hash_secret(token), category=category, description=description.strip(), lat=lat, lon=lon,
                 accuracy_m=accuracy_m, road_name=road_name.strip() or None, contact=contact.strip() or None,
                 photo_path=saved[0][0] if saved else None, photo_hash=saved[0][1] if saved else None,
                 client_hash=client, is_duplicate=dup)
    db.add(r); db.flush()
    for path, h in saved:
        db.add(m.ReportPhoto(report_id=r.id, path=path, phash=h))
    db.add(m.ReportStatus(report_id=r.id, status="received", note_public="Report received"))
    locate_report(db, r)
    db.commit()
    recluster(db)
    db.refresh(r)
    road = db.get(m.Road, r.road_id) if r.road_id else None
    return {"ref": ref, "token": token, "duplicate": dup,
            "location": {"ward": r.ward_label, "road": (road.name or "Unnamed road") if road else None,
                         "road_distance_m": r.road_distance_m, "road_matched": bool(road)},
            "grouped_with_similar": bool(r.cluster_id),
            "message": "Keep the reference and tracking code. The code is shown only once."}


def _verified_links(db, cluster_id):
    out = []
    if not cluster_id:
        return out
    for l in db.query(m.RecordLink).filter_by(a_type="cluster", a_id=cluster_id, status="verified"):
        w = db.get(m.Roadwork, l.b_id)
        if w:
            out.append({"roadwork_id": w.id, "description": w.description, "relationship": "verified"})
    return out


@router.get("/api/reports/track")
def track(ref: str, token: str, db: Session = Depends(get_db)):
    r = db.query(m.Report).filter_by(public_ref=ref.strip()).first()
    if not r or r.token_hash != hash_secret(token.strip()):
        raise HTTPException(404, "No report found for this reference and code")
    hist = db.query(m.ReportStatus).filter_by(report_id=r.id).order_by(m.ReportStatus.at).all()
    c = db.get(m.Cluster, r.cluster_id) if r.cluster_id else None
    road = db.get(m.Road, r.road_id) if r.road_id else None
    return {"ref": r.public_ref, "category": r.category, "category_label": CATEGORIES.get(r.category, r.category),
            "status": r.status, "created_at": r.created_at, "ward": r.ward_label,
            "road": (road.name or "Unnamed road") if road else None,
            "history": [{"status": h.status, "note": h.note_public, "at": h.at} for h in hist],
            "cluster": {"id": c.id, "report_count": c.report_count, "status": c.public_status} if c and c.status != "rejected" else None,
            "related_roadworks": _verified_links(db, r.cluster_id)}


@router.get("/api/public/map")
def public_map(db: Session = Depends(get_db)):
    clusters = db.query(m.Cluster).filter(m.Cluster.status.notin_(["rejected", "dissolved"])).all()
    rejected = {c.id for c in db.query(m.Cluster).filter_by(status="rejected")}
    singles = [r for r in db.query(m.Report).filter(m.Report.is_duplicate.is_(False)) if r.cluster_id is None or r.cluster_id in rejected]
    roads = {r.id: r.name for r in db.query(m.Road)}
    return {"clusters": [{"id": c.id, "lat": round(c.lat, 5), "lon": round(c.lon, 5), "category": c.category, "count": c.report_count,
                          "status": c.public_status, "first": c.first_at, "last": c.last_at,
                          "road": roads.get(c.road_id) if c.road_id else None, "ward": c.ward_label} for c in clusters],
            "reports": [{"lat": round(r.lat, 5), "lon": round(r.lon, 5), "category": r.category, "date": r.created_at.date(),
                         "demo": r.is_demo, "status": r.status} for r in singles]}


@router.get("/api/public/clusters/{cid}")
def public_cluster(cid: int, db: Session = Depends(get_db)):
    c = db.get(m.Cluster, cid)
    if not c or c.status in ("rejected", "dissolved"):
        raise HTTPException(404)
    road = db.get(m.Road, c.road_id) if c.road_id else None
    return {"id": c.id, "category": c.category, "count": c.report_count, "status": c.public_status, "first": c.first_at,
            "last": c.last_at, "road": road.name if road else None, "ward": c.ward_label,
            "related_roadworks": _verified_links(db, c.id)}


_ward_geo_cache = {}


@router.get("/api/public/wards")
def ward_layer(regime: str = "", db: Session = Depends(get_db)):
    """Simplified ward polygons (about 40 m tolerance) for map display, with report counts."""
    regime = regime or spatial.primary_regime(db)
    if not regime:
        return {"type": "FeatureCollection", "features": [], "regime": None}
    key = (regime, db.query(func.count(m.Ward.id)).filter_by(regime=regime).scalar())
    if key not in _ward_geo_cache:
        feats = []
        for w in db.query(m.Ward).filter_by(regime=regime):
            if not w.geojson:
                continue
            g = shape(json.loads(w.geojson)).simplify(0.0004, preserve_topology=True)
            feats.append({"type": "Feature", "geometry": mapping(g), "properties": {"id": w.id, "number": w.number, "name": w.name}})
        _ward_geo_cache[key] = feats
    counts = dict(db.query(m.Report.ward_id, func.count(m.Report.id)).filter(m.Report.is_duplicate.is_(False)).group_by(m.Report.ward_id))
    feats = [{**f, "properties": {**f["properties"], "reports": counts.get(f["properties"]["id"], 0)}} for f in _ward_geo_cache[key]]
    return {"type": "FeatureCollection", "features": feats, "regime": regime}


def _segment_counts(db):
    rows = db.query(m.Report.segment_id, func.count(m.Report.id)).filter(m.Report.is_duplicate.is_(False), m.Report.segment_id.isnot(None)) \
        .group_by(m.Report.segment_id).all()
    return dict(rows)


@router.get("/api/public/roads")
def road_layer(bbox: str = Query("", description="west,south,east,north"), db: Session = Depends(get_db)):
    try:
        w, s, e, n = [float(x) for x in bbox.split(",")] if bbox else (BBOX[2], BBOX[0], BBOX[3], BBOX[1])
    except ValueError:
        raise HTTPException(422, "bbox must be west,south,east,north")
    segs = spatial.segments_in_bbox(db, w, s, e, n)
    counts = _segment_counts(db)
    roads = {r.id: r for r in db.query(m.Road).filter(m.Road.id.in_({x.road_id for x in segs}))} if segs else {}
    works_by_road = {}
    for l in db.query(m.RecordLink).filter(m.RecordLink.a_type == "road", m.RecordLink.b_type == "roadwork", m.RecordLink.status != "rejected"):
        works_by_road[l.a_id] = works_by_road.get(l.a_id, 0) + 1
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": json.loads(x.geojson),
         "properties": {"segment_id": x.id, "road_id": x.road_id, "name": roads[x.road_id].name or "Unnamed road",
                        "unnamed": not roads[x.road_id].name, "ward": f"{x.ward_number} {x.ward_name or ''}".strip() if x.ward_number else None,
                        "length_m": round(x.length_m or 0), "reports": counts.get(x.id, 0),
                        "govt_records": works_by_road.get(x.road_id, 0), "approx": bool(roads[x.road_id].is_approx),
                        "sample": bool(roads[x.road_id].is_demo)}} for x in segs]}


@router.get("/api/public/roads/search")
def road_search(q: str = Query(..., min_length=2, max_length=80), db: Session = Depends(get_db)):
    nq = norm_road_name(q) or q.lower()
    out = db.query(m.Road).filter((m.Road.name_norm.like(f"%{nq}%")) | (m.Road.name.ilike(f"%{q}%"))).limit(15).all()
    return [{"id": r.id, "name": r.name, "lat": r.centroid_lat, "lon": r.centroid_lon, "approx": r.is_approx} for r in out]


@router.get("/api/public/locate")
def locate(lat: float, lon: float, db: Session = Depends(get_db)):
    """What the portal shows after the user drops a pin: ward, nearest road, and whether records exist."""
    if not (BBOX[0] <= lat <= BBOX[1] and BBOX[2] <= lon <= BBOX[3]):
        raise HTTPException(422, "Location is outside Bengaluru")
    ward = spatial.ward_for_point(db, lat, lon)
    hit = spatial.nearest_segment(db, lat, lon, 60)
    out = {"ward": spatial.ward_label(ward) if ward else None, "road": None}
    if hit:
        seg, dist = hit
        road = db.get(m.Road, seg.road_id)
        out["road"] = {"id": road.id, "name": road.name or "Unnamed road", "distance_m": round(dist), "gov_status": road_gov_status(db, road.id)}
    return out


def _work_public(w, contractor, link):
    prov = {"verified": "verified_official", "auto": "official_record", "suggested": "inferred"}.get(link.status, "inferred")
    return {"roadwork_id": w.id, "description": w.description, "ward": f"{w.ward_number} {w.ward_name or ''}".strip() if w.ward_number else None,
            "contractor": contractor, "work_order_date": w.work_order_date, "completion_deadline": w.completion_deadline,
            "declared_status": WORK_STATUS_TEXT.get(w.work_status, w.work_status), "sample": bool(w.is_demo),
            "provenance": prov, "link_confidence": link.confidence,
            "provenance_text": {"verified_official": "Government record, link confirmed by an officer",
                                "official_record": "Government record, linked to this road by name and ward",
                                "inferred": "Possibly related. Inferred from the road name, not confirmed"}[prov]}


@router.get("/api/public/roads/{road_id}")
def road_profile(road_id: int, db: Session = Depends(get_db)):
    road = db.get(m.Road, road_id)
    if not road:
        raise HTTPException(404)
    segs = db.query(m.RoadSegment).filter_by(road_id=road_id).order_by(m.RoadSegment.seq).all()
    seg_ids = [s.id for s in segs]
    reps = db.query(m.Report).filter(m.Report.road_id == road_id, m.Report.is_duplicate.is_(False)).all()
    since = datetime.utcnow() - timedelta(days=30)
    by_cat = {}
    for r in reps:
        by_cat[r.category] = by_cat.get(r.category, 0) + 1
    seg_counts = {}
    for r in reps:
        seg_counts[r.segment_id] = seg_counts.get(r.segment_id, 0) + 1
    wards = {}
    for s in segs:
        if s.ward_number:
            wards[s.ward_number] = f"{s.ward_number} {s.ward_name or ''}".strip()
    works = []
    for l in db.query(m.RecordLink).filter_by(a_type="road", a_id=road_id, b_type="roadwork").filter(m.RecordLink.status != "rejected"):
        w = db.get(m.Roadwork, l.b_id)
        c = db.get(m.Contractor, w.contractor_id) if w and w.contractor_id else None
        if w:
            works.append(_work_public(w, c.name if c else None, l))
    works.sort(key=lambda x: {"verified_official": 0, "official_record": 1, "inferred": 2}[x["provenance"]])
    clusters = db.query(m.Cluster).filter(m.Cluster.road_id == road_id, m.Cluster.status.notin_(["rejected", "dissolved"])).all()
    return {"id": road.id, "name": road.name or "Unnamed road", "unnamed": not road.name, "road_class": road.road_class,
            "length_m": round(road.length_m or 0), "centroid": [road.centroid_lat, road.centroid_lon],
            "geometry_source": road.source, "geometry_approximate": bool(road.is_approx), "sample_geometry": bool(road.is_demo),
            "wards": sorted(wards.values()),
            "reports": {"total": len(reps), "last_30_days": sum(1 for r in reps if r.created_at >= since), "by_category": by_cat,
                        "by_segment": [{"segment_id": s.id, "seq": s.seq, "ward": s.ward_number, "count": seg_counts.get(s.id, 0)} for s in segs],
                        "provenance": "citizen_submitted"},
            "clusters": [{"id": c.id, "category": c.category, "count": c.report_count, "status": c.public_status, "first": c.first_at, "last": c.last_at} for c in clusters],
            "government_records": works,
            "government_records_note": ("No government work record is linked to this road in the data loaded so far. "
                                       "This does not mean no work was done." if not works else
                                       "Records marked 'inferred' were matched by road name only and have not been confirmed."),
            "bbox": [min(s.minx for s in segs), min(s.miny for s in segs), max(s.maxx for s in segs), max(s.maxy for s in segs)] if segs else None}


@router.get("/api/public/stats")
def stats(db: Session = Depends(get_db)):
    q = db.query
    return {"reports": q(func.count(m.Report.id)).filter(m.Report.is_duplicate.is_(False)).scalar(),
            "sample_reports": q(func.count(m.Report.id)).filter(m.Report.is_demo.is_(True)).scalar(),
            "clusters": q(func.count(m.Cluster.id)).filter(m.Cluster.status.notin_(["rejected", "dissolved"])).scalar(),
            "roads": q(func.count(m.Road.id)).scalar(), "wards": q(func.count(m.Ward.id)).filter(m.Ward.regime == spatial.primary_regime(db)).scalar(),
            "roadworks_on_record": q(func.count(m.Roadwork.id)).scalar(),
            "by_category": dict(q(m.Report.category, func.count(m.Report.id)).filter(m.Report.is_duplicate.is_(False)).group_by(m.Report.category)),
            "ward_regime": spatial.primary_regime(db)}
