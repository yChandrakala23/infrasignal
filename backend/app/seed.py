"""Loads the bundled, clearly labelled SAMPLE data set (wards, approximate roads, synthetic work orders / tenders /
documents / complaints). Nothing here is a real government record or a real citizen complaint."""
import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from sqlalchemy.orm import Session
from . import models as m, spatial
from .wards import load_ward_file
from .roads import import_road_file
from .ingest import import_table, import_tenders
from .matching import link_roads_to_works, match_tenders, run_document_matching
from .pipeline import ingest_document, locate_report, recluster
from .security import hash_secret

DATA = Path(__file__).resolve().parents[1] / "data"
SAMPLE_SRC = "SAMPLE (synthetic, not a government record)"


def load_geography(db: Session, regimes=("243", "198")):
    out = {}
    for r in regimes:
        f = DATA / "wards" / f"wards_{r}.geojson"
        if f.exists() and not db.query(m.Ward).filter_by(regime=r).first():
            out[f"wards_{r}"] = load_ward_file(db, f, r, f"data/wards/SOURCES.md ({r} regime)")
    db.commit(); spatial.clear_cache()
    if not db.query(m.Road).first():
        out["roads"] = import_road_file(db, DATA / "roads" / "sample_major_roads.geojson", "sample_major_roads (approximate)",
                                        is_approx=True, is_demo=True)
    db.commit()
    return out


def load_works(db: Session, user_id=None):
    out = {}
    if not db.query(m.ImportJob).filter_by(filename="sample_work_orders_243.csv").first():
        p = DATA / "sample" / "sample_work_orders_243.csv"
        job = import_table(db, p.read_bytes(), p.name, SAMPLE_SRC, "data/sample/", "243", user_id, roads_only=False, is_demo=True)
        out["works"] = {"ok": job.rows_ok, "dup": job.rows_duplicate, "errors": len(job.errors or [])}
    if not db.query(m.ImportJob).filter_by(filename="sample_tenders_kppp.csv").first():
        p = DATA / "sample" / "sample_tenders_kppp.csv"
        job = import_tenders(db, p.read_bytes(), p.name, SAMPLE_SRC, "data/sample/", user_id, is_demo=True)
        out["tenders"] = {"ok": job.rows_ok, "errors": len(job.errors or [])}
    db.commit()
    return out


def load_documents(db: Session, user=None, use_llm=False):
    n = 0
    for p in sorted((DATA / "sample" / "documents").glob("*.pdf")):
        _, created = ingest_document(db, str(p), p.name, user=user, source_name=SAMPLE_SRC, is_demo=True, use_llm=use_llm, match=False)
        n += created
    return n


def load_complaints(db: Session, now=None, only_if_empty=True):
    if only_if_empty and db.query(m.Report).filter_by(is_demo=True).first():
        return 0
    now = now or datetime.utcnow()
    data = json.loads((DATA / "eval" / "complaints_labelled.json").read_text())
    for c in data["complaints"]:
        r = m.Report(public_ref="DEMO" + secrets.token_hex(4).upper(), token_hash=hash_secret("demo"), category=c["category"],
                     description=c["description"], lat=c["lat"], lon=c["lon"], road_name=c.get("road_name"), is_demo=True,
                     client_hash="demo", created_at=now - timedelta(days=c["days_ago"], hours=secrets.randbelow(10)))
        db.add(r); db.flush()
        locate_report(db, r)
        db.add(m.ReportStatus(report_id=r.id, status="received", note_public="Sample complaint (synthetic)"))
    db.commit()
    return len(data["complaints"])


def seed_all(db: Session, user=None, documents=True, complaints=True, use_llm=False):
    s = {"geography": load_geography(db)}
    s["records"] = load_works(db, user.id if user else None)
    if documents:
        s["documents"] = load_documents(db, user, use_llm)
    db.commit()
    s["document_matching"] = run_document_matching(db, user=user, only_unmatched=True)
    s["tender_links"] = match_tenders(db, user)
    s["road_links"] = link_roads_to_works(db, user)
    db.commit()
    if complaints:
        s["complaints"] = load_complaints(db)
        for r in db.query(m.Report):  # roads may have been linked after the reports were located
            locate_report(db, r)
        db.commit()
        s["clusters"] = recluster(db)
    return s
