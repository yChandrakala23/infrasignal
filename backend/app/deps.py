"""Shared FastAPI dependencies: auth, roles, audit, constants."""
import os
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session
from .db import get_db
from . import models as m
from .security import read_token

UPLOAD = os.getenv("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD, exist_ok=True)
BBOX = (12.73, 13.27, 77.33, 77.85)  # lat_min, lat_max, lon_min, lon_max (Bengaluru metropolitan area)
CATEGORIES = {
    "pothole": "Pothole", "damaged_road": "Damaged road surface", "cracks": "Cracks", "waterlogging": "Waterlogging / drainage",
    "incomplete_work": "Incomplete or abandoned work", "recurring": "Repeatedly repaired, failing again",
    "footpath": "Damaged footpath", "service_lane": "Service lane issue", "other": "Other",
}
REPORT_STATUSES = ["received", "under_review", "forwarded", "work_started", "repair_reported", "closed", "rejected"]
ROLES = ("officer", "authority", "admin")


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


def _role(*roles, message):
    def dep(u: m.User = Depends(current_user)):
        if u.role not in roles:
            raise HTTPException(403, message)
        return u
    return dep


officer = _role("officer", "admin", message="Monitoring officer role required")      # government monitoring module
authority = _role("authority", "admin", message="Authority role required")          # complaint handling (write)
staff = _role("officer", "authority", "admin", message="Staff login required")      # read access to either side
admin = _role("admin", message="Administrator role required")


def audit(db, user, action, entity, entity_id, detail=None):
    db.add(m.AuditLog(user_id=user.id if user else None, action=action, entity=entity, entity_id=entity_id, detail=detail or {}))
