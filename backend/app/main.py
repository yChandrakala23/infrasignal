import os, secrets, uuid, io, json
from datetime import datetime
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Header, Request, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session
from PIL import Image
from .db import Base, engine, get_db, SessionLocal
from . import models as m
from .security import hash_pw, check_pw, make_token, read_token, hash_secret
from .ingest import import_table
from .extract import process_pdf
from .matching import match_tenders, suggest_roadworks_for_cluster
from .clustering import cluster as run_cluster
from .alerts import run_alerts

UPLOAD = os.getenv("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD, exist_ok=True)
BBOX = (12.73, 13.27, 77.33, 77.85)
CATEGORIES = ["pothole", "damaged_road", "cracks", "waterlogging", "incomplete_work", "recurring", "other"]
app = FastAPI(title="InfraSignal API (prototype)")
BASE = os.path.dirname(os.path.dirname(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


@app.on_event("startup")
def startup():
    configured_secret = os.getenv("SECRET_KEY", "")
    if configured_secret.startswith("CHANGE_ME"):
        raise RuntimeError("Replace SECRET_KEY in .env before starting InfraSignal")
    for variable in ("ADMIN_PASSWORD", "OFFICER_PASSWORD"):
        configured_password = os.getenv(variable, "")
        if configured_password.startswith("CHANGE_ME"):
            raise RuntimeError(f"Replace {variable} in .env before starting InfraSignal")
    Base.metadata.create_all(engine)
    db = SessionLocal()
    # Never create well-known default credentials. Seed accounts only when both
    # email and password are explicitly configured in the environment.
    accounts = [
        (os.getenv("ADMIN_EMAIL"), os.getenv("ADMIN_PASSWORD"), "admin"),
        (os.getenv("OFFICER_EMAIL"), os.getenv("OFFICER_PASSWORD"), "officer"),
    ]
    for email, pw, role in accounts:
        if not email or not pw:
            continue
        if not db.query(m.User).filter_by(email=email).first():
            db.add(m.User(email=email, password_hash=hash_pw(pw), role=role))
    db.commit(); db.close()


@app.get("/")
def root():
    return RedirectResponse("/citizen")


@app.get("/citizen")
def citizen_page():
    return FileResponse(os.path.join(BASE, "static", "citizen.html"))


@app.get("/gov")
def gov_page():
    return FileResponse(os.path.join(BASE, "static", "gov.html"))


# ---------- auth ----------
def current_user(authorization: str = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Login required")
    try:
        data = read_token(authorization[7:])
    except Exception:
        raise HTTPException(401, "Invalid or expired session")
    u = db.get(m.User, data["uid"])
    if not u or not u.active:
        raise HTTPException(401, "User disabled")
    return u


def officer(u: m.User = Depends(current_user)):
    if u.role not in ("officer", "admin"):
        raise HTTPException(403, "Monitoring officer role required")
    return u


def admin(u: m.User = Depends(current_user)):
    if u.role != "admin":
        raise HTTPException(403, "Administrator role required")
    return u


def audit(db, user, action, entity, entity_id, detail=None):
    db.add(m.AuditLog(user_id=user.id if user else None, action=action, entity=entity,
                      entity_id=entity_id, detail=detail or {}))


@app.post("/api/auth/login")
def login(email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    u = db.query(m.User).filter_by(email=email).first()
    if not u or not check_pw(password, u.password_hash):
        raise HTTPException(401, "Wrong email or password")
    audit(db, u, "login", "user", u.id); db.commit()
    return {"token": make_token(u.id, u.role), "role": u.role, "email": u.email}


# ---------- clustering pipeline ----------
def recluster(db: Session):
    frozen = {c.id for c in db.query(m.Cluster).filter(m.Cluster.status == "verified")}
    reps = db.query(m.Report).filter(m.Report.is_duplicate.is_(False)).all()
    free = [r for r in reps if r.cluster_id not in frozen]
    for r in free:
        r.cluster_id = None
    db.flush()
    old = db.query(m.Cluster).filter(m.Cluster.status == "auto").all()
    old_ids = [c.id for c in old]
    if old_ids:
        db.query(m.RecordLink).filter(m.RecordLink.a_type == "cluster", m.RecordLink.a_id.in_(old_ids),
                                      m.RecordLink.status == "suggested").delete(synchronize_session=False)
        db.query(m.Alert).filter(m.Alert.cluster_id.in_(old_ids), m.Alert.status == "open").delete(synchronize_session=False)
    for c in old:
        db.delete(c)
    db.flush()
    data = [{"lat": r.lat, "lon": r.lon, "created_at": r.created_at, "category": r.category,
             "description": r.description} for r in free]
    labels, pairs = run_cluster(data)
    groups = {}
    for idx, lab in enumerate(labels):
        if lab >= 0:
            groups.setdefault(lab, []).append(idx)
    for lab, idxs in groups.items():
        members = [free[i] for i in idxs]
        cats = [r.category for r in members]
        c = m.Cluster(category=max(set(cats), key=cats.count),
                      lat=sum(r.lat for r in members) / len(members),
                      lon=sum(r.lon for r in members) / len(members),
                      report_count=len(members),
                      first_at=min(r.created_at for r in members), last_at=max(r.created_at for r in members),
                      evidence={"pairs": [{"a": free[i].public_ref, "b": free[j].public_ref, **v}
                                          for (i, j), v in pairs.items() if i in idxs and j in idxs]})
        db.add(c); db.flush()
        for r in members:
            r.cluster_id = c.id
        suggest_roadworks_for_cluster(db, c, members)
    db.commit()
    run_alerts(db)


# ---------- citizen API ----------
@app.post("/api/reports")
async def submit_report(request: Request, category: str = Form(...), description: str = Form(""),
                        lat: float = Form(...), lon: float = Form(...), accuracy_m: float = Form(None),
                        road_name: str = Form(""), contact: str = Form(""),
                        photo: UploadFile | None = File(None), db: Session = Depends(get_db)):
    if category not in CATEGORIES:
        raise HTTPException(422, "Unknown category")
    if not (BBOX[0] <= lat <= BBOX[1] and BBOX[2] <= lon <= BBOX[3]):
        raise HTTPException(422, "Location is outside Bengaluru")
    if len(description) > 1000:
        raise HTTPException(422, "Description too long")
    client = hash_secret(request.client.host if request.client else "unknown")
    hour_ago = datetime.utcnow().timestamp() - 3600
    recent = db.query(m.Report).filter(m.Report.client_hash == client).all()
    if sum(1 for r in recent if r.created_at.timestamp() > hour_ago) >= 5:
        raise HTTPException(429, "Too many reports in the last hour")
    dup = any(r.description == description and abs(r.lat - lat) < 1e-4 and abs(r.lon - lon) < 1e-4
              and r.created_at.timestamp() > datetime.utcnow().timestamp() - 600 for r in recent)
    photo_path = None
    if photo and photo.filename:
        raw = await photo.read()
        if len(raw) > 5 * 1024 * 1024:
            raise HTTPException(422, "Photo larger than 5 MB")
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")  # re-encoding drops EXIF (privacy)
        except Exception:
            raise HTTPException(422, "Photo is not a valid image")
        img.thumbnail((1600, 1600))
        photo_path = os.path.join(UPLOAD, f"{uuid.uuid4().hex}.jpg")
        img.save(photo_path, "JPEG", quality=85)
    ref = "IS" + secrets.token_hex(4).upper()
    token = secrets.token_urlsafe(12)
    r = m.Report(public_ref=ref, token_hash=hash_secret(token), category=category, description=description,
                 lat=lat, lon=lon, accuracy_m=accuracy_m, road_name=road_name or None,
                 contact=contact or None, photo_path=photo_path, client_hash=client, is_duplicate=dup)
    db.add(r); db.flush()
    db.add(m.ReportStatus(report_id=r.id, status="received", note_public="Report received"))
    db.commit()
    recluster(db)
    return {"ref": ref, "token": token, "duplicate": dup,
            "message": "Keep the reference and tracking code. The code is shown only once."}


def _public_links(db, cluster_id):
    if not cluster_id:
        return []
    links = db.query(m.RecordLink).filter_by(a_type="cluster", a_id=cluster_id, status="verified").all()
    out = []
    for l in links:
        w = db.get(m.Roadwork, l.b_id)
        if w:
            out.append({"roadwork_id": w.id, "description": w.description, "relationship": "verified"})
    return out


@app.get("/api/reports/track")
def track(ref: str, token: str, db: Session = Depends(get_db)):
    r = db.query(m.Report).filter_by(public_ref=ref).first()
    if not r or r.token_hash != hash_secret(token):
        raise HTTPException(404, "No report found for this reference and code")
    hist = db.query(m.ReportStatus).filter_by(report_id=r.id).order_by(m.ReportStatus.at).all()
    c = db.get(m.Cluster, r.cluster_id) if r.cluster_id else None
    return {"ref": r.public_ref, "category": r.category, "status": r.status, "created_at": r.created_at,
            "history": [{"status": h.status, "note": h.note_public, "at": h.at} for h in hist],
            "cluster": {"id": c.id, "report_count": c.report_count, "status": c.public_status} if c else None,
            "related_roadworks": _public_links(db, r.cluster_id)}


@app.get("/api/public/map")
def public_map(db: Session = Depends(get_db)):
    clusters = db.query(m.Cluster).filter(m.Cluster.status != "rejected").all()
    singles = db.query(m.Report).filter(m.Report.cluster_id.is_(None), m.Report.is_duplicate.is_(False)).all()
    return {"clusters": [{"id": c.id, "lat": round(c.lat, 4), "lon": round(c.lon, 4), "category": c.category,
                          "count": c.report_count, "status": c.public_status,
                          "first": c.first_at, "last": c.last_at} for c in clusters],
            "reports": [{"lat": round(r.lat, 4), "lon": round(r.lon, 4), "category": r.category,
                         "date": r.created_at.date(), "demo": r.is_demo} for r in singles]}


@app.get("/api/public/clusters/{cid}")
def public_cluster(cid: int, db: Session = Depends(get_db)):
    c = db.get(m.Cluster, cid)
    if not c or c.status == "rejected":
        raise HTTPException(404)
    return {"id": c.id, "category": c.category, "count": c.report_count, "status": c.public_status,
            "first": c.first_at, "last": c.last_at, "related_roadworks": _public_links(db, c.id)}


# ---------- government API ----------
@app.get("/api/gov/dashboard")
def dashboard(u=Depends(officer), db: Session = Depends(get_db)):
    q = lambda model: db.query(func.count(model.id)).scalar()
    by_ward = db.query(m.Roadwork.ward_regime, m.Roadwork.ward_number, func.count(m.Roadwork.id)) \
        .group_by(m.Roadwork.ward_regime, m.Roadwork.ward_number).order_by(func.count(m.Roadwork.id).desc()).limit(10).all()
    return {"roadworks": q(m.Roadwork),
            "with_tender_id": db.query(func.count(m.Roadwork.id)).filter(m.Roadwork.tender_id.isnot(None)).scalar(),
            "with_work_order": db.query(func.count(m.Roadwork.id)).filter(m.Roadwork.work_order_id.isnot(None)).scalar(),
            "contractors": q(m.Contractor), "bills": q(m.Bill), "payments": q(m.Payment),
            "paid_total": db.query(func.coalesce(func.sum(m.Payment.amount), 0)).scalar(),
            "reports": q(m.Report),
            "demo_reports": db.query(func.count(m.Report.id)).filter(m.Report.is_demo.is_(True)).scalar(),
            "clusters": db.query(func.count(m.Cluster.id)).filter(m.Cluster.status != "rejected").scalar(),
            "open_alerts": db.query(func.count(m.Alert.id)).filter(m.Alert.status == "open").scalar(),
            "top_wards": [{"regime": a, "ward": b, "count": c} for a, b, c in by_ward],
            "imports": [{"file": j.filename, "ok": j.rows_ok, "at": j.created_at}
                        for j in db.query(m.ImportJob).order_by(m.ImportJob.id.desc()).limit(5)]}


@app.get("/api/gov/roadworks")
def list_roadworks(q: str = "", ward: str = "", regime: str = "", contractor: str = "",
                   id_search: str = "", min_amount: float | None = None, max_amount: float | None = None,
                   page: int = 1, u=Depends(officer), db: Session = Depends(get_db)):
    qs = db.query(m.Roadwork, m.Contractor).outerjoin(m.Contractor, m.Roadwork.contractor_id == m.Contractor.id)
    if q: qs = qs.filter(m.Roadwork.description.ilike(f"%{q}%"))
    if ward: qs = qs.filter(m.Roadwork.ward_number == ward)
    if regime: qs = qs.filter(m.Roadwork.ward_regime == regime)
    if contractor: qs = qs.filter(m.Contractor.name.ilike(f"%{contractor}%"))
    if id_search:
        like = f"%{id_search}%"
        qs = qs.filter((m.Roadwork.job_number.ilike(like)) | (m.Roadwork.work_order_id.ilike(like)) | (m.Roadwork.tender_id.ilike(like)))
    if min_amount is not None: qs = qs.filter(m.Roadwork.sanctioned_amount >= min_amount)
    if max_amount is not None: qs = qs.filter(m.Roadwork.sanctioned_amount <= max_amount)
    total = qs.count()
    rows = qs.order_by(m.Roadwork.id.desc()).offset((page - 1) * 50).limit(50).all()
    return {"total": total, "items": [{"id": w.id, "job_number": w.job_number, "work_order_id": w.work_order_id,
                                       "tender_id": w.tender_id, "description": w.description, "ward": w.ward_number,
                                       "regime": w.ward_regime, "contractor": c.name if c else None,
                                       "amount": w.sanctioned_amount, "date": w.work_order_date, "demo": w.is_demo}
                                      for w, c in rows]}


LIFECYCLE = ["Proposal", "Estimate", "Sanction", "Tender publication", "BOQ and tender documents", "Bid evaluation",
             "Contract award", "Contractor identified", "Work order", "Execution and progress",
             "Measurement and running bills", "Bill approval", "Payment", "Completion documentation"]


def lifecycle(db, w):
    docs = db.query(m.Document).filter_by(roadwork_id=w.id).all()
    dtypes = {d.doc_type for d in docs}
    tlinks = db.query(m.RecordLink).filter_by(a_type="roadwork", a_id=w.id, b_type="tender").all()
    bills = db.query(m.Bill).filter_by(roadwork_id=w.id).count()
    pays = db.query(m.Payment).filter_by(roadwork_id=w.id).count()
    ev = {
        "Estimate": "Job number in source data" if w.job_number else None,
        "Tender publication": f"Tender ID {w.tender_id}" if w.tender_id else ("Suggested tender link" if tlinks else None),
        "BOQ and tender documents": "BOQ document uploaded" if "boq" in dtypes else ("Tender document uploaded" if "tender" in dtypes else None),
        "Contract award": "Award document uploaded" if "award" in dtypes else None,
        "Contractor identified": "Contractor named in work order data" if w.contractor_id else None,
        "Work order": f"Work order {w.work_order_id or ''} {w.work_order_date or ''}".strip() if (w.work_order_id or w.work_order_date) else None,
        "Measurement and running bills": f"{bills} bill record(s)" if bills else None,
        "Bill approval": "Bill records present (approval stage not always published)" if bills else None,
        "Payment": f"{pays} payment record(s). Payment does not prove physical completion." if pays else None,
        "Completion documentation": "Completion document uploaded" if "completion" in dtypes else None,
    }
    return [{"stage": s, "evidence": ev.get(s), "available": bool(ev.get(s))} for s in LIFECYCLE]


@app.get("/api/gov/roadworks/{rid}")
def roadwork_detail(rid: int, u=Depends(officer), db: Session = Depends(get_db)):
    w = db.get(m.Roadwork, rid)
    if not w:
        raise HTTPException(404)
    c = db.get(m.Contractor, w.contractor_id) if w.contractor_id else None
    src = db.get(m.SourceRecord, w.source_record_id) if w.source_record_id else None
    links = db.query(m.RecordLink).filter(((m.RecordLink.a_type == "roadwork") & (m.RecordLink.a_id == rid)) |
                                          ((m.RecordLink.b_type == "roadwork") & (m.RecordLink.b_id == rid))).all()
    missing = [f for f in ("job_number", "work_order_id", "tender_id", "ward_number", "sanctioned_amount", "work_order_date", "contractor_id")
               if getattr(w, f) in (None, "")]
    return {"roadwork": {k: getattr(w, k) for k in ("id", "job_number", "work_order_id", "tender_id", "description", "ward_regime",
                                                     "ward_number", "ward_name", "sanctioned_amount", "work_order_date",
                                                     "source_name", "source_url", "is_demo")},
            "contractor": {"id": c.id, "name": c.name} if c else None,
            "bills": [{"id": b.id, "number": b.bill_number, "amount": b.amount, "date": b.bill_date}
                      for b in db.query(m.Bill).filter_by(roadwork_id=rid)],
            "payments": [{"id": p.id, "amount": p.amount, "paid_on": p.paid_on, "reference": p.reference}
                         for p in db.query(m.Payment).filter_by(roadwork_id=rid)],
            "documents": [{"id": d.id, "filename": d.filename, "type": d.doc_type, "pages": d.pages}
                          for d in db.query(m.Document).filter_by(roadwork_id=rid)],
            "links": [{"id": l.id, "a": f"{l.a_type}:{l.a_id}", "b": f"{l.b_type}:{l.b_id}", "method": l.method,
                       "confidence": l.confidence, "status": l.status, "evidence": l.evidence} for l in links],
            "lifecycle": lifecycle(db, w), "missing_fields": missing,
            "source_row": {"import_job": src.import_job_id, "row": src.row_number, "raw": src.raw} if src else None}


@app.get("/api/gov/reports")
def gov_reports(category: str = "", status: str = "", u=Depends(officer), db: Session = Depends(get_db)):
    qs = db.query(m.Report)
    if category: qs = qs.filter_by(category=category)
    if status: qs = qs.filter_by(status=status)
    return [{"id": r.id, "ref": r.public_ref, "category": r.category, "description": r.description, "lat": r.lat,
             "lon": r.lon, "road_name": r.road_name, "status": r.status, "cluster_id": r.cluster_id,
             "has_photo": bool(r.photo_path), "contact": r.contact if u.role == "admin" else ("provided" if r.contact else None),
             "demo": r.is_demo, "duplicate": r.is_duplicate, "created_at": r.created_at}
            for r in qs.order_by(m.Report.created_at.desc()).limit(500)]


@app.get("/api/gov/photos/{rid}")
def photo(rid: int, u=Depends(officer), db: Session = Depends(get_db)):
    r = db.get(m.Report, rid)
    if not r or not r.photo_path:
        raise HTTPException(404)
    return FileResponse(r.photo_path)


@app.patch("/api/gov/reports/{rid}/status")
def set_status(rid: int, status: str = Form(...), note_public: str = Form(""), note_internal: str = Form(""),
               u=Depends(officer), db: Session = Depends(get_db)):
    if status not in ("received", "under_review", "forwarded", "work_started", "repair_reported", "closed", "rejected"):
        raise HTTPException(422, "Unknown status")
    r = db.get(m.Report, rid)
    if not r:
        raise HTTPException(404)
    r.status = status
    db.add(m.ReportStatus(report_id=rid, status=status, note_public=note_public, note_internal=note_internal, by_user=u.id))
    audit(db, u, "report_status", "report", rid, {"status": status}); db.commit()
    return {"ok": True}


@app.post("/api/gov/reports/{rid}/move")
def move_report(rid: int, cluster_id: int | None = Form(None), u=Depends(officer), db: Session = Depends(get_db)):
    r = db.get(m.Report, rid)
    if not r:
        raise HTTPException(404)
    old = r.cluster_id
    r.cluster_id = cluster_id
    for cid in {old, cluster_id} - {None}:
        c = db.get(m.Cluster, cid)
        c.status = "verified"  # manual correction freezes the cluster
        c.report_count = db.query(m.Report).filter_by(cluster_id=cid).count()
    audit(db, u, "move_report", "report", rid, {"from": old, "to": cluster_id}); db.commit()
    return {"ok": True}


@app.get("/api/gov/clusters")
def gov_clusters(u=Depends(officer), db: Session = Depends(get_db)):
    out = []
    for c in db.query(m.Cluster).order_by(m.Cluster.report_count.desc()):
        links = db.query(m.RecordLink).filter_by(a_type="cluster", a_id=c.id).all()
        out.append({"id": c.id, "category": c.category, "lat": c.lat, "lon": c.lon, "count": c.report_count,
                    "first": c.first_at, "last": c.last_at, "status": c.status, "public_status": c.public_status,
                    "evidence": c.evidence,
                    "reports": [x.public_ref for x in db.query(m.Report).filter_by(cluster_id=c.id)],
                    "roadwork_links": [{"id": l.id, "roadwork_id": l.b_id, "method": l.method,
                                        "confidence": l.confidence, "status": l.status, "evidence": l.evidence} for l in links]})
    return out


@app.post("/api/gov/clusters/{cid}/review")
def review_cluster(cid: int, decision: str = Form(...), public_status: str = Form(""), note: str = Form(""),
                   u=Depends(officer), db: Session = Depends(get_db)):
    c = db.get(m.Cluster, cid)
    if not c or decision not in ("verified", "rejected"):
        raise HTTPException(422)
    c.status = decision
    if public_status: c.public_status = public_status
    c.note_internal = note
    audit(db, u, "cluster_review", "cluster", cid, {"decision": decision}); db.commit()
    return {"ok": True}


@app.get("/api/gov/links")
def gov_links(status: str = "suggested", u=Depends(officer), db: Session = Depends(get_db)):
    return [{"id": l.id, "a": f"{l.a_type}:{l.a_id}", "b": f"{l.b_type}:{l.b_id}", "method": l.method,
             "confidence": l.confidence, "status": l.status, "evidence": l.evidence}
            for l in db.query(m.RecordLink).filter_by(status=status).order_by(m.RecordLink.confidence.desc())]


@app.patch("/api/gov/links/{lid}")
def review_link(lid: int, status: str = Form(...), u=Depends(officer), db: Session = Depends(get_db)):
    if status not in ("verified", "rejected", "unverified"):
        raise HTTPException(422)
    l = db.get(m.RecordLink, lid)
    if not l:
        raise HTTPException(404)
    l.status, l.reviewer_id, l.reviewed_at = status, u.id, datetime.utcnow()
    audit(db, u, "link_review", "record_link", lid, {"status": status}); db.commit()
    return {"ok": True}


@app.get("/api/gov/alerts")
def gov_alerts(status: str = "open", u=Depends(officer), db: Session = Depends(get_db)):
    order = {"high": 0, "medium": 1, "low": 2}
    items = db.query(m.Alert).filter_by(status=status).all()
    items.sort(key=lambda a: (order.get(a.severity, 3), -a.id))
    return [{"id": a.id, "rule": a.rule, "severity": a.severity, "title": a.title, "explanation": a.explanation,
             "evidence": a.evidence, "cluster_id": a.cluster_id, "roadwork_id": a.roadwork_id, "created_at": a.created_at}
            for a in items]


@app.patch("/api/gov/alerts/{aid}")
def update_alert(aid: int, status: str = Form(...), u=Depends(officer), db: Session = Depends(get_db)):
    a = db.get(m.Alert, aid)
    if not a or status not in ("open", "acknowledged", "dismissed", "resolved"):
        raise HTTPException(422)
    a.status = status
    audit(db, u, "alert_status", "alert", aid, {"status": status}); db.commit()
    return {"ok": True}


@app.post("/api/gov/recluster")
def force_recluster(u=Depends(officer), db: Session = Depends(get_db)):
    recluster(db); audit(db, u, "recluster", "system", 0); db.commit()
    return {"ok": True}


# ---------- admin API ----------
@app.post("/api/admin/import")
async def admin_import(file: UploadFile = File(...), source_name: str = Form(...), source_url: str = Form(""),
                       ward_regime: str = Form(...), mapping_json: str = Form(""), roads_only: bool = Form(True),
                       u=Depends(admin), db: Session = Depends(get_db)):
    data = await file.read()
    mapping = json.loads(mapping_json) if mapping_json else None
    job = import_table(db, data, file.filename, source_name, source_url, ward_regime, u.id, mapping, roads_only)
    audit(db, u, "import", "import_job", job.id, {"file": file.filename}); db.commit()
    return {"job_id": job.id, "status": job.status, "total": job.rows_total, "ok": job.rows_ok,
            "skipped_non_road": job.rows_skipped, "failed": job.rows_failed, "column_map": job.column_map,
            "errors": job.errors[:20]}


@app.get("/api/admin/imports")
def list_imports(u=Depends(admin), db: Session = Depends(get_db)):
    return [{"id": j.id, "file": j.filename, "source": j.source_name, "regime": j.ward_regime, "status": j.status,
             "total": j.rows_total, "ok": j.rows_ok, "skipped": j.rows_skipped, "failed": j.rows_failed,
             "map": j.column_map, "at": j.created_at} for j in db.query(m.ImportJob).order_by(m.ImportJob.id.desc())]


@app.post("/api/admin/documents")
async def upload_doc(file: UploadFile = File(...), roadwork_id: int = Form(None), doc_type: str = Form("other"),
                     u=Depends(admin), db: Session = Depends(get_db)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(422, "PDF only")
    path = os.path.join(UPLOAD, f"{uuid.uuid4().hex}.pdf")
    with open(path, "wb") as f:
        f.write(await file.read())
    sha, pages, fields = process_pdf(path)
    d = m.Document(roadwork_id=roadwork_id, filename=file.filename, path=path, doc_type=doc_type, pages=pages,
                   sha256=sha, uploaded_by=u.id)
    db.add(d); db.flush()
    for fdict in fields:
        db.add(m.Extraction(document_id=d.id, **fdict))
    audit(db, u, "document_upload", "document", d.id, {"fields": len(fields)}); db.commit()
    return {"document_id": d.id, "pages": pages, "fields_extracted": len(fields)}


@app.get("/api/gov/documents/{did}")
def get_doc(did: int, u=Depends(officer), db: Session = Depends(get_db)):
    d = db.get(m.Document, did)
    if not d:
        raise HTTPException(404)
    return FileResponse(d.path, media_type="application/pdf")


@app.get("/api/gov/documents/{did}/extractions")
def get_extractions(did: int, u=Depends(officer), db: Session = Depends(get_db)):
    return [{"id": e.id, "field": e.field, "raw": e.raw_value, "norm": e.norm_value, "page": e.page,
             "confidence": e.confidence, "method": e.method, "status": e.status}
            for e in db.query(m.Extraction).filter_by(document_id=did)]


@app.patch("/api/admin/extractions/{eid}")
def correct_extraction(eid: int, status: str = Form(...), norm_value: str = Form(None),
                       u=Depends(admin), db: Session = Depends(get_db)):
    e = db.get(m.Extraction, eid)
    if not e or status not in ("verified", "corrected", "rejected"):
        raise HTTPException(422)
    e.status, e.corrected_by = status, u.id
    if norm_value is not None: e.norm_value = norm_value
    audit(db, u, "extraction_review", "extraction", eid, {"status": status}); db.commit()
    return {"ok": True}


@app.post("/api/admin/match")
def run_match(u=Depends(admin), db: Session = Depends(get_db)):
    n = match_tenders(db); audit(db, u, "match_run", "system", 0, {"links": n}); db.commit()
    return {"links_created_or_found": n}


@app.post("/api/admin/tenders")
def add_tender(tender_id: str = Form(...), title: str = Form(""), estimated_value: float = Form(None),
               awarded_value: float = Form(None), status: str = Form(""), url: str = Form(""),
               u=Depends(admin), db: Session = Depends(get_db)):
    t = m.Tender(tender_id=tender_id, title=title, estimated_value=estimated_value, awarded_value=awarded_value,
                 status=status, url=url)
    db.add(t); db.flush(); audit(db, u, "tender_add", "tender", t.id); db.commit()
    return {"id": t.id}


@app.get("/api/admin/audit")
def audit_list(u=Depends(admin), db: Session = Depends(get_db)):
    return [{"id": a.id, "user": a.user_id, "action": a.action, "entity": a.entity, "entity_id": a.entity_id,
             "detail": a.detail, "at": a.at} for a in db.query(m.AuditLog).order_by(m.AuditLog.id.desc()).limit(300)]


@app.get("/api/admin/users")
def users(u=Depends(admin), db: Session = Depends(get_db)):
    return [{"id": x.id, "email": x.email, "role": x.role, "active": x.active} for x in db.query(m.User)]


@app.post("/api/admin/users")
def create_user(email: str = Form(...), password: str = Form(...), role: str = Form(...),
                u=Depends(admin), db: Session = Depends(get_db)):
    if role not in ("officer", "admin"):
        raise HTTPException(422)
    x = m.User(email=email, password_hash=hash_pw(password), role=role)
    db.add(x); db.flush(); audit(db, u, "user_create", "user", x.id, {"role": role}); db.commit()
    return {"id": x.id}
