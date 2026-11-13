"""Collection management API: start and monitor BBMP data collection, see what was downloaded and imported, retry failures,
import a file or URL by hand, set the schedule, and the numbers shown on the government dashboard for collected data."""
import os
from datetime import datetime
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, and_, exists, select, true, not_
from sqlalchemy.orm import Session

from ..db import get_db, SessionLocal
from .. import models as m
from .. import collection as col
from .. import scheduler as sched
from .. import scope
from ..connectors.bbmp_connector import FetchError, load_sources
from ..deps import officer, admin, audit
from ..sources import counted_payments
from .gov import Filters, filtered_query

router = APIRouter(prefix="/api/gov/collection", tags=["data collection"])
ORIGIN = "bbmp_open_data"
MAX_UPLOAD = 60 * 1024 * 1024


def _run_json(r: m.CollectionRun, with_log=False):
    d = {"id": r.id, "connector": r.connector, "trigger": r.trigger, "status": r.status, "started_at": r.started_at, "finished_at": r.finished_at,
         "datasets_found": r.datasets_found, "files_found": r.files_found, "files_downloaded": r.files_downloaded, "files_unchanged": r.files_unchanged,
         "files_failed": r.files_failed, "records_processed": r.records_processed, "records_inserted": r.records_inserted, "records_updated": r.records_updated,
         "records_duplicate": r.records_duplicate, "records_failed": r.records_failed, "requested_by": r.requested_by, "params": r.params}
    if with_log:
        d["log"] = r.log or []
    return d


def _file_json(f: m.SourceFile):
    return {"id": f.id, "run_id": f.run_id, "source_website": f.source_website, "dataset": f.dataset_title, "dataset_slug": f.dataset_slug, "dataset_url": f.dataset_url,
            "resource_name": f.resource_name, "resource_id": f.resource_id, "download_url": f.download_url, "original_filename": f.original_filename,
            "file_format": f.file_format, "fiscal_year": f.fiscal_year, "ward_regime": f.ward_regime, "size_bytes": f.size_bytes, "sha256": f.sha256,
            "downloaded_at": f.downloaded_at, "last_checked_at": f.last_checked_at, "status": f.status, "error": f.error, "attempts": f.attempts,
            "import_job_id": f.import_job_id, "imported_at": f.imported_at, "rows_processed": f.rows_processed, "rows_inserted": f.rows_inserted,
            "rows_updated": f.rows_updated, "rows_duplicate": f.rows_duplicate, "rows_failed": f.rows_failed, "stored": bool(f.stored_path and os.path.exists(f.stored_path)),
            "manual": (f.dataset_slug or "") == "manual"}


# ----------------------------------------------------------------------------- status / run / history
@router.get("/status")
def status(u=Depends(officer), db: Session = Depends(get_db)):
    R = m.CollectionRun
    last = db.query(R).order_by(R.id.desc()).first()
    ok = db.query(R).filter(R.status == "done").order_by(R.id.desc()).first()
    running = db.query(R).filter(R.status == "running").order_by(R.id.desc()).first()
    last_import = db.query(m.SourceFile).filter(m.SourceFile.status == "imported").order_by(m.SourceFile.imported_at.desc()).first()
    try:
        cfg = load_sources()
        datasets = [{"slug": d["slug"], "url": d["url"], "fiscal_year": d.get("fiscal_year"), "enabled": d.get("enabled", True)} for d in cfg.get("datasets", [])]
        pages = [{"url": p["url"], "enabled": p.get("enabled", True)} for p in cfg.get("site_pages", [])]
    except Exception as e:
        datasets, pages = [], []
    s = sched.get_settings(db)
    nxt = sched.next_run_time()
    counts = dict(db.query(m.SourceFile.status, func.count(m.SourceFile.id)).group_by(m.SourceFile.status))
    return {"last_run": _run_json(last) if last else None, "last_successful_run": _run_json(ok) if ok else None,
            "last_successful_collection": ok.finished_at if ok else None, "last_import": last_import.imported_at if last_import else None,
            "running": _run_json(running) if running else None, "files_by_status": counts,
            "records": {"works": db.query(func.count(m.Roadwork.id)).filter(m.Roadwork.data_origin == ORIGIN).scalar(),
                        "source_records": db.query(func.count(m.SourceRecord.id)).filter(m.SourceRecord.external_id.like("bbmp|%")).scalar(),
                        "payments": db.query(func.count(m.Payment.id)).filter(m.Payment.external_key.like("bbmp|%")).scalar()},
            "schedule": {**s, "next_run": nxt}, "configured_datasets": datasets, "configured_site_pages": pages,
            "notes": ["Download addresses are read from each dataset page on every run; they are not stored as fixed links.",
                      "A file whose checksum has not changed is not imported again.", "Access restrictions and CAPTCHAs are never bypassed: use the manual import."]}


def _background_run(run_id, only, force):
    col.execute_run(run_id, only=only or None, force=force)


@router.post("/run")
def run_collection(background: BackgroundTasks, body: dict | None = None, u=Depends(officer), db: Session = Depends(get_db)):
    """Start a collection. body: {"datasets": ["bbmp-work-orders-and-payments-2024-25"], "force": false}. Runs in the background; poll /runs/{id}."""
    body = body or {}
    only = body.get("datasets") or None
    if only is not None and not (isinstance(only, list) and all(isinstance(x, str) for x in only)):
        raise HTTPException(422, "datasets must be a list of dataset slugs")
    try:
        run = col.start_run(db, "manual", u.id, {"datasets": only, "force": bool(body.get("force"))})
    except col.RunInProgress as e:
        raise HTTPException(409, str(e))
    audit(db, u, "collection_start", "collection_run", run.id, {"datasets": only}); db.commit()
    background.add_task(_background_run, run.id, only, bool(body.get("force")))
    return {"run_id": run.id, "status": "running"}


@router.get("/runs")
def runs(page: int = 1, page_size: int = Query(25, le=100), u=Depends(officer), db: Session = Depends(get_db)):
    q = db.query(m.CollectionRun).order_by(m.CollectionRun.id.desc())
    return {"total": q.count(), "items": [_run_json(r) for r in q.offset((page - 1) * page_size).limit(page_size)]}


@router.get("/runs/{rid}")
def run_detail(rid: int, u=Depends(officer), db: Session = Depends(get_db)):
    r = db.get(m.CollectionRun, rid)
    if not r: raise HTTPException(404, "Run not found")
    files = db.query(m.SourceFile).filter(m.SourceFile.run_id == rid).order_by(m.SourceFile.id).all()
    return {**_run_json(r, True), "files": [_file_json(f) for f in files]}


@router.get("/files")
def files(status: str = "", fiscal_year: str = "", run_id: int | None = None, page: int = 1, page_size: int = Query(50, le=200),
          u=Depends(officer), db: Session = Depends(get_db)):
    q = db.query(m.SourceFile)
    if status: q = q.filter(m.SourceFile.status == status)
    if fiscal_year: q = q.filter(m.SourceFile.fiscal_year == scope.fy_canonical(scope.fy_start(fiscal_year)) if scope.fy_start(fiscal_year) else m.SourceFile.fiscal_year == fiscal_year)
    if run_id: q = q.filter(m.SourceFile.run_id == run_id)
    q = q.order_by(m.SourceFile.id.desc())
    return {"total": q.count(), "items": [_file_json(f) for f in q.offset((page - 1) * page_size).limit(page_size)]}


@router.get("/files/{fid}")
def file_detail(fid: int, u=Depends(officer), db: Session = Depends(get_db)):
    f = db.get(m.SourceFile, fid)
    if not f: raise HTTPException(404, "File not found")
    kinds = dict(db.query(m.ImportIssue.kind, func.count(m.ImportIssue.id)).filter(m.ImportIssue.source_file_id == fid).group_by(m.ImportIssue.kind))
    return {**_file_json(f), "dataset_meta": f.dataset_meta, "issues_by_kind": kinds}


@router.get("/files/{fid}/download")
def file_download(fid: int, u=Depends(officer), db: Session = Depends(get_db)):
    """The stored copy of the file exactly as it was downloaded."""
    f = db.get(m.SourceFile, fid)
    if not f or not f.stored_path or not os.path.exists(f.stored_path): raise HTTPException(404, "No stored copy of this file")
    return FileResponse(f.stored_path, filename=f.original_filename or os.path.basename(f.stored_path))


# ----------------------------------------------------------------------------- imported records, issues
@router.get("/records")
def records(file_id: int | None = None, fiscal_year: str = "", q: str = "", unmatched: bool = False, page: int = 1, page_size: int = Query(50, le=200),
            u=Depends(officer), db: Session = Depends(get_db)):
    """Collected source rows with the work they are linked to. unmatched=true lists rows linked to no work."""
    S, L, R = m.SourceRecord, m.WorkSourceLink, m.Roadwork
    qs = db.query(S, R, m.Contractor).outerjoin(L, and_(L.source_record_id == S.id, L.status != "rejected")).outerjoin(R, R.id == L.roadwork_id) \
        .outerjoin(m.Contractor, m.Contractor.id == R.contractor_id).filter(S.external_id.like("bbmp|%"))
    if file_id: qs = qs.filter(S.source_file_id == file_id)
    if fiscal_year:
        y = scope.fy_start(fiscal_year)
        qs = qs.filter(S.fiscal_year == (scope.fy_canonical(y) if y is not None else fiscal_year))
    if unmatched: qs = qs.filter(L.id.is_(None))
    if q: qs = qs.filter(or_(R.description.ilike(f"%{q}%"), R.work_order_id.ilike(f"%{q}%"), m.Contractor.name.ilike(f"%{q}%")))
    total = qs.count()
    rows = qs.order_by(S.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    pay = {}
    ids = [s.id for s, _, _ in rows]
    if ids:
        pay = {p.source_record_id: p for p in db.query(m.Payment).filter(m.Payment.source_record_id.in_(ids))}
    return {"total": total, "items": [{"source_record_id": s.id, "row": s.row_number, "file_id": s.source_file_id, "fiscal_year": s.fiscal_year, "external_id": s.external_id,
                                       "flags": (s.normalised or {}).get("flags", []), "raw": s.raw, "normalised": s.normalised,
                                       "work": ({"id": w.id, "work_order_id": w.work_order_id, "description": w.description, "ward": w.ward_number, "regime": w.ward_regime,
                                                 "contractor": c.name if c else None} if w else None),
                                       "payment": ({"id": p.id, "amount": p.amount, "paid_on": p.paid_on, "reference": p.reference, "counted": p.duplicate_of_id is None} if (p := pay.get(s.id)) else None)}
                                      for s, w, c in rows]}


@router.get("/issues")
def issues(kind: str = "", file_id: int | None = None, job_id: int | None = None, code: str = "", page: int = 1, page_size: int = Query(50, le=200),
           u=Depends(officer), db: Session = Depends(get_db)):
    """Rows that failed, duplicates that were skipped, rows that were updated, conflicts with existing or verified values, and warnings."""
    q = db.query(m.ImportIssue)
    if kind: q = q.filter(m.ImportIssue.kind == kind)
    if file_id: q = q.filter(m.ImportIssue.source_file_id == file_id)
    if job_id: q = q.filter(m.ImportIssue.import_job_id == job_id)
    if code: q = q.filter(m.ImportIssue.code == code)
    by_kind = dict(db.query(m.ImportIssue.kind, func.count(m.ImportIssue.id)).group_by(m.ImportIssue.kind))
    q = q.order_by(m.ImportIssue.id.desc())
    return {"total": q.count(), "by_kind": by_kind, "items": [{"id": i.id, "file_id": i.source_file_id, "job_id": i.import_job_id, "row": i.row_number, "kind": i.kind, "code": i.code,
                                                              "message": i.message, "raw": i.raw, "source_record_id": i.source_record_id, "at": i.created_at}
                                                             for i in q.offset((page - 1) * page_size).limit(page_size)]}


# ----------------------------------------------------------------------------- retry, manual import
def _background_retry(run_id, ids, user_id):
    db = SessionLocal()
    try:
        run = db.get(m.CollectionRun, run_id)
        col.retry_failed(db, user_id, ids, run=run)
    except Exception as e:  # keep the run record honest if something unexpected happens
        db.rollback()
        run = db.get(m.CollectionRun, run_id)
        run.status, run.finished_at = "failed", datetime.utcnow()
        run.log = (run.log or []) + [{"at": datetime.utcnow().isoformat(timespec="seconds"), "level": "error", "message": f"Retry stopped: {type(e).__name__}: {str(e)[:300]}"}]
        db.commit()
    finally:
        db.close()


@router.post("/retry")
def retry(background: BackgroundTasks, body: dict | None = None, u=Depends(officer), db: Session = Depends(get_db)):
    """Retry files whose download or import failed. body: {"file_ids": [3, 4]} or {} for all failed files."""
    ids = (body or {}).get("file_ids") or None
    n = db.query(m.SourceFile).filter(m.SourceFile.status.in_(["download_failed", "import_failed"]))
    if ids: n = n.filter(m.SourceFile.id.in_(ids))
    if n.count() == 0: raise HTTPException(404, "There are no failed files to retry")
    try:
        run = col.start_run(db, "retry", u.id, {"file_ids": ids})
    except col.RunInProgress as e:
        raise HTTPException(409, str(e))
    audit(db, u, "collection_retry", "collection_run", run.id, {"file_ids": ids}); db.commit()
    background.add_task(_background_retry, run.id, ids, u.id)
    return {"run_id": run.id, "status": "running", "files": n.count()}


@router.post("/manual-import")
async def manual_import(file: UploadFile = File(...), fiscal_year: str = Form(""), ward_regime: str = Form(""), label: str = Form("Manual import"),
                        source_url: str = Form(""), u=Depends(officer), db: Session = Depends(get_db)):
    """Import a CSV / Excel file the officer downloaded. The financial year and ward regime must be stated (or be readable from the file name)."""
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD: raise HTTPException(413, "File is larger than 60 MB")
    try:
        run, sf = col.manual_import(db, data, file.filename or "upload", fiscal_year=fiscal_year, ward_regime=ward_regime, label=label or "Manual import",
                                    source_url=source_url, user_id=u.id)
    except col.RunInProgress as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    audit(db, u, "collection_manual_import", "source_file", sf.id, {"filename": file.filename}); db.commit()
    return {"run": _run_json(run, True), "file": _file_json(sf)}


@router.post("/manual-url")
def manual_url(body: dict, u=Depends(officer), db: Session = Depends(get_db)):
    """Download one public file by URL and import it. Private addresses are refused; logins and CAPTCHAs are not bypassed."""
    url = str(body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")): raise HTTPException(422, "Give a full http(s) URL")
    fetcher = col.FETCHER_FACTORY()
    try:
        dl = fetcher.download(url, strict_host=False)
    except FetchError as e:
        raise HTTPException(422 if e.kind in ("host", "blocked", "robots", "not_data") else 502, f"{e.kind}: {e}")
    fname = (url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]) or "download"
    try:
        run, sf = col.manual_import(db, dl.content, fname, fiscal_year=body.get("fiscal_year"), ward_regime=body.get("ward_regime"),
                                    label=str(body.get("label") or "Manual URL import"), source_url=url, user_id=u.id)
    except col.RunInProgress as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    audit(db, u, "collection_manual_url", "source_file", sf.id, {"url": url[:300]}); db.commit()
    return {"run": _run_json(run, True), "file": _file_json(sf)}


# ----------------------------------------------------------------------------- schedule
@router.get("/schedule")
def get_schedule(u=Depends(officer), db: Session = Depends(get_db)):
    return {**sched.get_settings(db), "next_run": sched.next_run_time()}


@router.put("/schedule")
def put_schedule(body: dict, u=Depends(admin), db: Session = Depends(get_db)):
    """body: {"enabled": true, "cron": "0 3 * * 0", "timezone": "Asia/Kolkata"}"""
    cur = sched.get_settings(db)
    enabled = bool(body.get("enabled", cur["enabled"]))
    cron, tz = str(body.get("cron") or cur["cron"]).strip(), str(body.get("timezone") or cur["timezone"]).strip()
    try:
        nxt = sched.save(db, enabled, cron, tz)
    except ValueError as e:
        raise HTTPException(422, str(e))
    audit(db, u, "collection_schedule", "app_setting", 0, {"enabled": enabled, "cron": cron, "timezone": tz}); db.commit()
    return {**sched.get_settings(db), "next_run": nxt}


# ----------------------------------------------------------------------------- numbers for the dashboard
@router.get("/summary")
def summary(f: Filters = Depends(Filters), u=Depends(officer), db: Session = Depends(get_db)):
    """What the collected BBMP data holds, from the database and nothing else. Same filters as the dashboard; only records collected from
    open data (never sample data) are counted. Payments are net amounts, each counted once; bills are the gross amounts before deductions."""
    R, P, B, S, L = m.Roadwork, m.Payment, m.Bill, m.SourceRecord, m.WorkSourceLink
    f.origin = "collected"
    ids = filtered_query(db, f).with_entities(R.id).subquery()
    idsel = select(ids.c.id)
    y = scope.fy_start(f.fiscal_year) if f.fiscal_year else None
    fy = scope.fy_canonical(y) if y is not None else None
    pay_fy = scope.payment_fiscal_year_condition(f.fiscal_year) if f.fiscal_year else true()
    scalar = lambda q: q.scalar() or 0

    n_works = scalar(db.query(func.count()).select_from(ids))
    bill_q = lambda *c: db.query(*c).select_from(B).join(S, S.id == B.source_record_id).filter(B.roadwork_id.in_(idsel), *( [S.fiscal_year == fy] if fy else []))
    billed, deducted, netpayable, n_bills = bill_q(func.sum(B.amount), func.sum(B.deduction), func.sum(B.net_amount), func.count(B.id)).one()
    pay_q = lambda *c: counted_payments(db, *c).filter(P.roadwork_id.in_(idsel), pay_fy)
    paid, n_pay = pay_q(func.sum(P.amount), func.count(P.id)).one()
    dup_n, dup_amt = db.query(func.count(P.id), func.sum(P.amount)).filter(P.roadwork_id.in_(idsel), P.duplicate_of_id.isnot(None), pay_fy).one()

    # financial year of the file each row was published in
    src_fy = [S.external_id.like("bbmp|%"), L.roadwork_id.in_(idsel), L.status != "rejected"] + ([S.fiscal_year == fy] if fy else [])
    fy_rows = {r[0]: {"fiscal_year": r[0], "works": r[1], "source_rows": r[2]} for r in
               db.query(S.fiscal_year, func.count(func.distinct(L.roadwork_id)), func.count(func.distinct(S.id))).join(L, L.source_record_id == S.id).filter(*src_fy).group_by(S.fiscal_year)}
    for k, a, b, c in db.query(S.fiscal_year, func.sum(B.amount), func.sum(B.deduction), func.sum(B.net_amount)).join(B, B.source_record_id == S.id).filter(B.roadwork_id.in_(idsel), S.external_id.like("bbmp|%"), *([S.fiscal_year == fy] if fy else [])).group_by(S.fiscal_year):
        fy_rows.setdefault(k, {"fiscal_year": k, "works": 0, "source_rows": 0}).update(billed=float(a or 0), deductions=float(b or 0), net_payable=float(c or 0))
    for k, a, n in counted_payments(db, S.fiscal_year, func.sum(P.amount), func.count(P.id)).join(S, S.id == P.source_record_id).filter(P.roadwork_id.in_(idsel), S.external_id.like("bbmp|%"), *([S.fiscal_year == fy] if fy else [])).group_by(S.fiscal_year):
        fy_rows.setdefault(k, {"fiscal_year": k, "works": 0, "source_rows": 0}).update(paid=float(a or 0), payments=n)
    by_fy = [{"billed": 0.0, "deductions": 0.0, "net_payable": 0.0, "paid": 0.0, "payments": 0, **v} for k, v in sorted(fy_rows.items(), key=lambda kv: str(kv[0]))]

    paid_sub = counted_payments(db, P.roadwork_id.label("rid"), func.sum(P.amount).label("paid")).filter(P.roadwork_id.in_(idsel), pay_fy).group_by(P.roadwork_id).subquery()
    by_ward = [{"regime": a, "ward": b, "works": n, "paid": float(t or 0)} for a, b, n, t in
               db.query(R.ward_regime, R.ward_number, func.count(R.id), func.sum(paid_sub.c.paid)).outerjoin(paid_sub, paid_sub.c.rid == R.id)
               .filter(R.id.in_(idsel)).group_by(R.ward_regime, R.ward_number).order_by(func.count(R.id).desc()).limit(40)]
    n_wards = scalar(db.query(func.count()).select_from(db.query(R.ward_regime, R.ward_number).filter(R.id.in_(idsel), R.ward_number.isnot(None)).distinct().subquery()))
    by_contractor = [{"id": cid, "name": n, "vendor_code": vc, "works": k, "paid": float(t or 0)} for cid, n, vc, k, t in
                     db.query(m.Contractor.id, m.Contractor.name, m.Contractor.vendor_code, func.count(R.id), func.sum(paid_sub.c.paid))
                     .join(R, R.contractor_id == m.Contractor.id).outerjoin(paid_sub, paid_sub.c.rid == R.id).filter(R.id.in_(idsel))
                     .group_by(m.Contractor.id, m.Contractor.name, m.Contractor.vendor_code).order_by(func.count(R.id).desc()).limit(25)]
    n_contractors = scalar(db.query(func.count(func.distinct(R.contractor_id))).filter(R.id.in_(idsel)))

    cnt = lambda cond: scalar(db.query(func.count(R.id)).filter(R.id.in_(idsel), cond))
    blank = lambda col: or_(col.is_(None), col == "")
    missing = {"no_contractor": cnt(R.contractor_id.is_(None)), "no_ward": cnt(blank(R.ward_number)), "no_description": cnt(blank(R.description)),
               "no_road_name": cnt(blank(R.road_name_extracted)), "no_department": cnt(blank(R.department)), "no_estimated_amount": cnt(R.estimated_cost.is_(None)),
               "no_approved_amount": cnt(and_(R.awarded_amount.is_(None), R.sanctioned_amount.is_(None))), "no_work_order_date": cnt(R.work_order_date.is_(None)),
               "no_payment_recorded": cnt(~exists().where(and_(P.roadwork_id == R.id, P.duplicate_of_id.is_(None), pay_fy))),
               "non_numeric_ward": cnt(and_(R.ward_number.isnot(None), R.ward_number != "", not_(R.ward_number.op("GLOB", is_comparison=True)("[0-9]*")) if db.bind.dialect.name == "sqlite" else not_(R.ward_number.op("~", is_comparison=True)("^[0-9]+$"))))}
    est_n = cnt(R.estimated_cost.isnot(None))
    appr_n = cnt(or_(R.awarded_amount.isnot(None), R.sanctioned_amount.isnot(None)))
    estimated = float(scalar(db.query(func.sum(R.estimated_cost)).filter(R.id.in_(idsel))))
    approved = float(scalar(db.query(func.sum(func.coalesce(R.awarded_amount, R.sanctioned_amount))).filter(R.id.in_(idsel))))

    unmatched_q = db.query(S).outerjoin(L, and_(L.source_record_id == S.id, L.status != "rejected")).filter(S.external_id.like("bbmp|%"), L.id.is_(None))
    n_unmatched = unmatched_q.count()
    unmatched = [{"source_record_id": s.id, "row": s.row_number, "fiscal_year": s.fiscal_year, "file_id": s.source_file_id,
                  "raw": {k: v for k, v in list((s.raw or {}).items())[:9]}} for s in unmatched_q.order_by(S.id.desc()).limit(15)]
    files = db.query(m.SourceFile).filter(m.SourceFile.status == "imported").order_by(m.SourceFile.fiscal_year, m.SourceFile.id).all()
    ok = db.query(m.CollectionRun).filter(m.CollectionRun.status == "done").order_by(m.CollectionRun.id.desc()).first()
    warn_codes = dict(db.query(m.ImportIssue.code, func.count(m.ImportIssue.id)).filter(m.ImportIssue.kind == "warning").group_by(m.ImportIssue.code))
    failed_rows = scalar(db.query(func.count(m.ImportIssue.id)).filter(m.ImportIssue.kind == "failed"))
    return {
        "origin": {"label": "Records collected from BBMP open data (data.opencity.in). Sample and manually uploaded records are not included here.",
                   "filters_active": f.active, "last_successful_collection": ok.finished_at if ok else None},
        "totals": {"works": n_works, "contractors": n_contractors, "wards": n_wards, "bills": n_bills or 0, "billed_gross": float(billed or 0), "deductions": float(deducted or 0),
                   "net_payable": float(netpayable or 0), "payments": n_pay or 0, "paid_net": float(paid or 0), "repeat_payments_excluded": dup_n or 0,
                   "repeat_payment_amount_excluded": float(dup_amt or 0), "estimated_amount": estimated, "works_with_estimated_amount": est_n,
                   "approved_amount": approved, "works_with_approved_amount": appr_n, "unmatched_source_records": n_unmatched, "failed_rows_reported": failed_rows},
        "by_fiscal_year": by_fy, "by_ward": by_ward, "contractors": by_contractor, "missing": missing, "unmatched": unmatched, "warnings_by_code": warn_codes,
        "sources": [{"file_id": x.id, "filename": x.original_filename, "dataset": x.dataset_title, "fiscal_year": x.fiscal_year, "ward_regime": x.ward_regime,
                     "download_url": x.download_url, "dataset_url": x.dataset_url, "downloaded_at": x.downloaded_at, "imported_at": x.imported_at,
                     "rows": x.rows_processed, "inserted": x.rows_inserted, "duplicates": x.rows_duplicate, "failed": x.rows_failed, "manual": (x.dataset_slug or "") == "manual"} for x in files],
        "notes": ["Billed amounts are the gross bill amounts before deductions; paid amounts are the net amounts with an RTGS payment recorded. Neither is an estimate or an approved cost.",
                  "The published files contain no estimated or approved amounts and no work-order dates; those stay empty rather than being derived.",
                  "A work belongs to a financial year when one of its rows was published in that year's file; payments follow the year of their file.",
                  "Payment records are never treated as proof that a work was completed."]}


@router.get("/payments")
def payment_history(f: Filters = Depends(Filters), include_repeats: bool = False, page: int = 1, page_size: int = Query(50, le=200),
                    u=Depends(officer), db: Session = Depends(get_db)):
    """Payment history of collected works, newest first, each with its source file and financial year."""
    R, P, S = m.Roadwork, m.Payment, m.SourceRecord
    f.origin = "collected"
    idsel = select(filtered_query(db, f).with_entities(R.id).subquery().c.id)
    pay_fy = scope.payment_fiscal_year_condition(f.fiscal_year) if f.fiscal_year else true()
    q = db.query(P, R, S).join(R, R.id == P.roadwork_id).outerjoin(S, S.id == P.source_record_id).filter(P.roadwork_id.in_(idsel), pay_fy)
    if not include_repeats: q = q.filter(P.duplicate_of_id.is_(None))
    total = q.count()
    rows = q.order_by(P.paid_on.desc(), P.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [{"id": p.id, "work_id": w.id, "work_order_id": w.work_order_id, "description": w.description, "ward": w.ward_number, "regime": w.ward_regime,
                                       "amount": p.amount, "gross_amount": p.gross_amount, "deduction": p.deduction, "paid_on": p.paid_on, "reference": p.reference,
                                       "fiscal_year": s.fiscal_year if s else None, "source_file_id": s.source_file_id if s else None,
                                       "counted": p.duplicate_of_id is None, "duplicate_of_id": p.duplicate_of_id} for p, w, s in rows]}


# ----------------------------------------------------------------------------- live web crawling (primary collection path)
from .. import web_collection as webcol


def _item_json(i: m.WebItem):
    return {"id": i.id, "source": i.source, "url": i.url, "label": i.label, "category": i.category, "kind": i.kind, "file_ext": i.file_ext,
            "found_on": i.found_on, "discovered_via": i.discovered_via, "first_seen_at": i.first_seen_at, "last_seen_at": i.last_seen_at,
            "first_run_id": i.first_run_id, "still_listed": i.still_listed, "status": i.status, "error": i.error, "sha256": i.sha256,
            "size_bytes": i.size_bytes, "downloaded_at": i.downloaded_at, "versions": i.versions, "document_id": i.document_id,
            "records_extracted": i.records_extracted, "stored": bool(i.stored_path and os.path.exists(i.stored_path)),
            "text_excerpt": (i.text_excerpt or "")[:400]}


def _background_web(run_id, only, download, force, max_docs):
    webcol.execute_web_run(run_id, only=only, download=download, force=force, max_docs=max_docs)


@router.post("/web/run")
def web_run(background: BackgroundTasks, body: dict | None = None, u=Depends(officer), db: Session = Depends(get_db)):
    """Start a live crawl of the configured government websites. body: {"sources": [names], "download": true, "force": false, "max_documents": 40}"""
    body = body or {}
    only = body.get("sources") or None
    if only is not None and not (isinstance(only, list) and all(isinstance(x, str) for x in only)):
        raise HTTPException(422, "sources must be a list of source names")
    md = body.get("max_documents")
    if md is not None and (not isinstance(md, int) or not 0 <= md <= 500):
        raise HTTPException(422, "max_documents must be 0-500")
    try:
        run = webcol.start_web_run(db, "manual", u.id, {"sources": only, "download": body.get("download", True), "force": bool(body.get("force"))})
    except col.RunInProgress as e:
        raise HTTPException(409, str(e))
    audit(db, u, "web_crawl_start", "collection_run", run.id, {"sources": only}); db.commit()
    background.add_task(_background_web, run.id, only, bool(body.get("download", True)), bool(body.get("force")), md)
    return {"run_id": run.id, "status": "running"}


@router.get("/web/summary")
def web_summary(u=Depends(officer), db: Session = Depends(get_db)):
    last = db.query(m.CollectionRun).filter(m.CollectionRun.connector == "web").order_by(m.CollectionRun.id.desc()).first()
    ok = db.query(m.CollectionRun).filter(m.CollectionRun.connector == "web", m.CollectionRun.status.in_(["done", "done_with_errors"])) \
        .order_by(m.CollectionRun.id.desc()).first()
    by_cat = dict(db.query(m.WebItem.category, func.count(m.WebItem.id)).filter(m.WebItem.kind == "document").group_by(m.WebItem.category))
    by_status = dict(db.query(m.WebItem.status, func.count(m.WebItem.id)).filter(m.WebItem.kind == "document").group_by(m.WebItem.status))
    pages = db.query(m.WebPage.source, m.WebPage.status, func.count(m.WebPage.id)).group_by(m.WebPage.source, m.WebPage.status).all()
    cfg = load_sources().get("crawl_sources", [])
    return {"last_run": _run_json(last, True) if last else None, "last_successful_run": _run_json(ok) if ok else None,
            "documents_by_category": by_cat, "documents_by_status": by_status,
            "pages": [{"source": s, "status": st, "count": n} for s, st, n in pages],
            "records": db.query(func.count(m.WebRecord.id)).scalar(), "documents_processed": db.query(func.count(m.WebItem.document_id)).scalar(),
            "sources": [{"name": s.get("name"), "seeds": s.get("seeds"), "enabled": s.get("enabled", True), "note": s.get("note", "")} for s in cfg]}


@router.get("/web/items")
def web_items(category: str = "", status: str = "", source: str = "", q: str = "", run_id: int | None = None, new_in_run: int | None = None,
              kind: str = "document", page: int = 1, page_size: int = Query(50, le=200), u=Depends(officer), db: Session = Depends(get_db)):
    qq = db.query(m.WebItem)
    if kind: qq = qq.filter(m.WebItem.kind == kind)
    if category: qq = qq.filter(m.WebItem.category == category)
    if status: qq = qq.filter(m.WebItem.status == status)
    if source: qq = qq.filter(m.WebItem.source == source)
    if run_id: qq = qq.filter(m.WebItem.last_run_id == run_id)
    if new_in_run: qq = qq.filter(m.WebItem.first_run_id == new_in_run)
    if q: qq = qq.filter(or_(m.WebItem.label.ilike(f"%{q}%"), m.WebItem.url.ilike(f"%{q}%"), m.WebItem.text_excerpt.ilike(f"%{q}%")))
    qq = qq.order_by(m.WebItem.first_seen_at.desc(), m.WebItem.id.desc())
    return {"total": qq.count(), "items": [_item_json(i) for i in qq.offset((page - 1) * page_size).limit(page_size)]}


@router.get("/web/items/{iid}/download")
def web_item_download(iid: int, u=Depends(officer), db: Session = Depends(get_db)):
    i = db.get(m.WebItem, iid)
    if not i or not i.stored_path or not os.path.exists(i.stored_path): raise HTTPException(404, "No stored copy of this document")
    return FileResponse(i.stored_path, filename=os.path.basename(i.stored_path).split("_", 1)[-1])


@router.get("/web/pages")
def web_pages(source: str = "", u=Depends(officer), db: Session = Depends(get_db)):
    qq = db.query(m.WebPage)
    if source: qq = qq.filter(m.WebPage.source == source)
    return {"items": [{"id": p.id, "source": p.source, "url": p.url, "final_url": p.final_url, "title": p.title, "status": p.status, "error": p.error,
                       "via": p.via, "links_found": p.links_found, "tables_found": p.tables_found, "first_seen_at": p.first_seen_at,
                       "last_fetched_at": p.last_fetched_at, "last_changed_at": p.last_changed_at, "times_changed": p.times_changed}
                      for p in qq.order_by(m.WebPage.id).limit(500)]}


@router.get("/web/records")
def web_records(category: str = "", origin: str = "", item_id: int | None = None, q: str = "", page: int = 1, page_size: int = Query(50, le=200),
                u=Depends(officer), db: Session = Depends(get_db)):
    qq = db.query(m.WebRecord)
    if category: qq = qq.filter(m.WebRecord.category == category)
    if origin: qq = qq.filter(m.WebRecord.origin == origin)
    if item_id: qq = qq.filter(m.WebRecord.item_id == item_id)
    if q: qq = qq.filter(m.WebRecord.page_url.ilike(f"%{q}%"))
    qq = qq.order_by(m.WebRecord.id.desc())
    return {"total": qq.count(), "items": [{"id": r.id, "source": r.source, "origin": r.origin, "page_url": r.page_url, "item_id": r.item_id,
                                            "row": r.row_number, "raw": r.raw, "normalised": r.normalised, "category": r.category,
                                            "first_seen_at": r.first_seen_at, "last_seen_at": r.last_seen_at}
                                           for r in qq.offset((page - 1) * page_size).limit(page_size)]}
