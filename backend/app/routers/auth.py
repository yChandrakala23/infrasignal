from fastapi import APIRouter, Depends, Form, HTTPException
from sqlalchemy.orm import Session
from ..db import get_db
from .. import models as m
from ..deps import audit, current_user
from ..security import check_pw, make_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    u = db.query(m.User).filter_by(email=email.strip().lower()).first() or db.query(m.User).filter_by(email=email).first()
    if not u or not u.active or not check_pw(password, u.password_hash):
        raise HTTPException(401, "Wrong email or password")
    audit(db, u, "login", "user", u.id); db.commit()
    return {"token": make_token(u.id, u.role), "role": u.role, "email": u.email}


@router.get("/me")
def me(u: m.User = Depends(current_user)):
    return {"id": u.id, "email": u.email, "role": u.role}
