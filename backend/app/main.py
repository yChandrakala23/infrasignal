"""InfraSignal API entry point. Routers hold the endpoints; this file wires them together."""
import os
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from .db import SessionLocal, init_db, is_postgres
from . import models as m
from .routers import auth, public, gov, authority, admin, collection
from .security import hash_pw
from . import spatial

BASE = os.path.dirname(os.path.dirname(__file__))
app = FastAPI(title="InfraSignal API", version="0.6",
              description="Civic infrastructure transparency: government work records, mapped roads, citizen reports.")
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
for r in (auth.router, public.router, gov.router, collection.router, authority.router, admin.router):
    app.include_router(r)


def seed_accounts(db):
    """Accounts are created only when both email and password are set in the environment. No default credentials exist."""
    for prefix, role in (("ADMIN", "admin"), ("OFFICER", "officer"), ("AUTHORITY", "authority")):
        email, pw = os.getenv(f"{prefix}_EMAIL"), os.getenv(f"{prefix}_PASSWORD")
        if email and pw and not db.query(m.User).filter_by(email=email.strip().lower()).first():
            db.add(m.User(email=email.strip().lower(), password_hash=hash_pw(pw), role=role))
    db.commit()


@app.on_event("startup")
def startup():
    if os.getenv("SECRET_KEY", "").startswith("CHANGE_ME"):
        raise RuntimeError("Replace SECRET_KEY in .env before starting InfraSignal")
    for variable in ("ADMIN_PASSWORD", "OFFICER_PASSWORD", "AUTHORITY_PASSWORD"):
        if os.getenv(variable, "").startswith("CHANGE_ME"):
            raise RuntimeError(f"Replace {variable} in .env before starting InfraSignal")
    init_db()
    db = SessionLocal()
    try:
        seed_accounts(db)
        from .collection import clear_stale_runs
        clear_stale_runs(db, at_startup=True)  # a collection left "running" by a stopped server would block every new run
        if os.getenv("AUTO_SEED_SAMPLE", "").lower() in ("1", "true", "yes") and not db.query(m.Ward).first():
            from .seed import seed_all
            seed_all(db)
    finally:
        db.close()
    try:  # scheduled BBMP collection: off unless enabled in the environment or saved through the API
        from . import scheduler
        scheduler.apply()
    except Exception as e:  # a bad schedule setting must not stop the application
        import logging
        logging.getLogger("infrasignal").warning("BBMP schedule not started: %s", e)


@app.on_event("shutdown")
def shutdown():
    from . import scheduler
    scheduler.shutdown()


@app.get("/api/health")
def health():
    db = SessionLocal()
    try:
        db.execute(text("select 1"))
        return {"status": "ok", "database": "postgresql+postgis" if is_postgres() else "sqlite", "spatial_backend": spatial.spatial_backend(),
                "wards": db.query(m.Ward).count(), "roads": db.query(m.Road).count()}
    finally:
        db.close()


def _page(name):
    return FileResponse(os.path.join(BASE, "static", name))


@app.get("/")
def root():
    return RedirectResponse("/citizen")


@app.get("/citizen")
def citizen_page():
    return _page("citizen.html")


@app.get("/gov")
def gov_page():
    return _page("gov.html")


@app.get("/authority")
def authority_page():
    return _page("authority.html")
