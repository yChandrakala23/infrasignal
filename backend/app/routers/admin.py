"""Administrator API: users, audit trail, geography loading, sample data, and the original /api/admin/* paths kept for compatibility."""
import json, os
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from ..db import get_db
from .. import models as m, spatial
from ..deps import admin, audit
from ..security import hash_pw
from ..roads import import_roads, overpass_to_features
from ..wards import load_wards, parse_kml
from .. import seed as seedmod
from . import gov

router = APIRouter(prefix="/api/admin", tags=["admin"])
ROLES = ("officer", "authority", "admin")


@router.get("/audit")
def audit_list(u=Depends(admin), db: Session = Depends(get_db)):
    return [{"id": a.id, "user": a.user_id, "action": a.action, "entity": a.entity, "entity_id": a.entity_id, "detail": a.detail, "at": a.at}
            for a in db.query(m.AuditLog).order_by(m.AuditLog.id.desc()).limit(300)]


@router.get("/users")
def users(u=Depends(admin), db: Session = Depends(get_db)):
    return [{"id": x.id, "email": x.email, "role": x.role, "active": x.active} for x in db.query(m.User)]


@router.post("/users")
def create_user(email: str = Form(...), password: str = Form(...), role: str = Form(...), u=Depends(admin), db: Session = Depends(get_db)):
    email = email.strip().lower()
    if role not in ROLES: raise HTTPException(422, f"Role must be one of {', '.join(ROLES)}")
    if "@" not in email or len(password) < 10: raise HTTPException(422, "Valid email and a password of at least 10 characters required")
    if db.query(m.User).filter_by(email=email).first(): raise HTTPException(409, "A user with this email already exists")
    x = m.User(email=email, password_hash=hash_pw(password), role=role)
    db.add(x); db.flush(); audit(db, u, "user_create", "user", x.id, {"role": role}); db.commit()
    return {"id": x.id}


@router.patch("/users/{uid}")
def update_user(uid: int, active: bool = Form(...), u=Depends(admin), db: Session = Depends(get_db)):
    x = db.get(m.User, uid)
    if not x: raise HTTPException(404)
    if x.id == u.id and not active: raise HTTPException(422, "You cannot disable your own account")
    x.active = active; audit(db, u, "user_update", "user", uid, {"active": active}); db.commit()
    return {"ok": True}


@router.post("/geography/wards")
async def upload_wards(file: UploadFile = File(...), regime: str = Form(...), source: str = Form(...), number_field: str = Form(""), name_field: str = Form(""),
                       replace: bool = Form(False), u=Depends(admin), db: Session = Depends(get_db)):
    """GeoJSON or KML ward boundaries, e.g. the GBA 369-ward map (regime 'gba369')."""
    raw = (await file.read()).decode("utf-8", "replace")
    try:
        feats = parse_kml(raw) if file.filename.lower().endswith(".kml") else json.loads(raw)["features"]
    except Exception:
        raise HTTPException(422, "Could not parse the boundary file (GeoJSON FeatureCollection or KML expected)")
    stats = load_wards(db, feats, regime, source, number_field or None, name_field or None, replace)
    db.commit(); spatial.clear_cache()
    audit(db, u, "ward_load", "ward", 0, {"regime": regime, **stats}); db.commit()
    return stats


@router.post("/geography/roads")
async def upload_roads(file: UploadFile = File(...), source: str = Form(...), approximate: bool = Form(False), u=Depends(admin), db: Session = Depends(get_db)):
    """Road centrelines as GeoJSON LineStrings, or an Overpass API JSON export."""
    try:
        data = json.loads((await file.read()).decode("utf-8", "replace"))
        feats = overpass_to_features(data) if "elements" in data else data["features"]
    except Exception:
        raise HTTPException(422, "Could not parse the road file (GeoJSON or Overpass JSON expected)")
    stats = import_roads(db, feats, source, is_approx=approximate)
    db.commit(); spatial.sync_postgis(db); db.commit()
    from ..pipeline import relocate_all_reports
    relocate_all_reports(db)
    audit(db, u, "road_load", "road", 0, stats); db.commit()
    return stats


@router.post("/seed-sample")
def seed_sample(documents: bool = Form(True), complaints: bool = Form(True), u=Depends(admin), db: Session = Depends(get_db)):
    """Loads the bundled SAMPLE data. Every record is marked sample/demo; none of it is a real government record or complaint."""
    out = seedmod.seed_all(db, u, documents=documents, complaints=complaints)
    audit(db, u, "seed_sample", "system", 0, {}); db.commit()
    return out


# ------- original paths, kept so existing integrations and notes keep working -------
@router.post("/import")
async def admin_import(file: UploadFile = File(...), source_name: str = Form(...), source_url: str = Form(""), ward_regime: str = Form(...),
                       mapping_json: str = Form(""), roads_only: bool = Form(True), u=Depends(admin), db: Session = Depends(get_db)):
    return await gov.import_confirm(file, "works", source_name, source_url, ward_regime, mapping_json, roads_only, False, "", u, db)


@router.get("/imports")
def list_imports(u=Depends(admin), db: Session = Depends(get_db)):
    return gov.list_imports(u, db)


@router.post("/documents")
async def upload_doc(file: UploadFile = File(...), u=Depends(admin), db: Session = Depends(get_db)):
    r = await gov.upload_documents([file], "", False, u, db)
    return r["results"][0]


@router.patch("/extractions/{eid}")
def correct_extraction(eid: int, status: str = Form(...), norm_value: str = Form(None), u=Depends(admin), db: Session = Depends(get_db)):
    return gov.review_extraction(eid, status, norm_value, u, db)


@router.post("/match")
def run_match(u=Depends(admin), db: Session = Depends(get_db)):
    return gov.run_matching(True, True, True, False, u, db)


@router.post("/tenders")
def add_tender(tender_id: str = Form(...), title: str = Form(""), estimated_value: float = Form(None), awarded_value: float = Form(None),
               status: str = Form(""), url: str = Form(""), u=Depends(admin), db: Session = Depends(get_db)):
    if db.query(m.Tender).filter_by(tender_id=tender_id.strip()).first(): raise HTTPException(409, "Tender ID already exists")
    t = m.Tender(tender_id=tender_id.strip(), title=title, estimated_value=estimated_value, awarded_value=awarded_value, status=status, url=url)
    db.add(t); db.flush(); audit(db, u, "tender_add", "tender", t.id); db.commit()
    return {"id": t.id}
