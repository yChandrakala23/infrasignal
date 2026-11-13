"""Runs a BBMP collection: discover -> download (only what changed) -> import -> record what happened.

Every file keeps a `source_files` row (where it came from, checksum, size, download date, import counts, errors) and every execution a
`collection_runs` row (counts and a short log), so the history can be listed and a failed file retried. Nothing here parses rows:
that is app/bbmp_ingest.py. Nothing here talks HTTP directly: that is app/connectors/bbmp_connector.py.

"Unchanged" means the server answered 304 to a conditional request, or the downloaded bytes have the same SHA-256 as the file already
imported for that resource. An unchanged file is neither stored nor imported again.
"""
import hashlib, os, re, socket
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session

from . import models as m
from .bbmp_ingest import FileContext, ImportAborted, import_rows, SOURCE_NAME
from .bbmp_schema import fiscal_label, regime_from_text
from .connectors.bbmp_connector import BbmpConnector, Fetcher, FetchError, DATA_EXT, detect_format, looks_restricted, load_sources
from .db import SessionLocal

RUN_LOCK_MINUTES = int(os.getenv("BBMP_RUN_LOCK_MINUTES", "120"))
FETCHER_FACTORY = Fetcher  # replaced in tests (and could be replaced to route requests through a proxy)
LOG_CAP = 300
SUPPORTED = ("csv", "xlsx", "xls")


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def download_dir():
    base = os.getenv("BBMP_DOWNLOAD_DIR") or os.path.join(os.getenv("UPLOAD_DIR", "uploads"), "bbmp")
    os.makedirs(base, exist_ok=True)
    return base


class RunInProgress(Exception):
    pass


class RunLog:
    def __init__(self, run: m.CollectionRun):
        self.run, self.items = run, list(run.log or [])

    def add(self, level, message):
        self.items.append({"at": now().isoformat(timespec="seconds"), "level": level, "message": str(message)[:500]})
        if len(self.items) > LOG_CAP:
            self.items = self.items[:20] + self.items[-(LOG_CAP - 20):]
        self.run.log = list(self.items)


# ----------------------------------------------------------------------------- run lifecycle
def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _worker_gone(r: m.CollectionRun) -> bool:
    """True when the process that started this run (same machine) has exited, e.g. the server was restarted or the CLI was closed."""
    w = (r.params or {}).get("_worker") or {}
    if not w.get("pid") or w.get("host") != socket.gethostname():
        return False
    return not _pid_alive(w["pid"])


def _mark_interrupted(r: m.CollectionRun, why: str):
    r.status, r.finished_at = "failed", now()
    r.log = (r.log or []) + [{"at": now().isoformat(timespec="seconds"), "level": "error", "message": f"Interrupted: {why}"}]


def clear_stale_runs(db: Session, at_startup=False) -> int:
    """Close 'running' records whose process is gone. At server start-up, a run from an older version (no process recorded) cannot
    still be running inside this new server process either."""
    n = 0
    for r in db.query(m.CollectionRun).filter(m.CollectionRun.status == "running"):
        w = (r.params or {}).get("_worker")
        if _worker_gone(r) or (at_startup and not w):
            _mark_interrupted(r, "the process running it stopped before it finished.")
            n += 1
    db.commit()
    return n


def start_run(db: Session, trigger: str, user_id: int | None = None, params: dict | None = None) -> m.CollectionRun:
    """Create the run record. Only one collection runs at a time; a 'running' record whose process has exited, or that is older than
    the lock time, is treated as interrupted."""
    cutoff = now() - timedelta(minutes=RUN_LOCK_MINUTES)
    for r in db.query(m.CollectionRun).filter(m.CollectionRun.status == "running"):
        if r.started_at and r.started_at < cutoff:
            _mark_interrupted(r, "the run never finished.")
        elif _worker_gone(r):
            _mark_interrupted(r, "the process running it stopped before it finished.")
        else:
            db.rollback()
            raise RunInProgress(f"Collection run {r.id} ({r.connector}) is still running (started {r.started_at:%H:%M} UTC).")
    params = dict(params or {})
    params["_worker"] = {"pid": os.getpid(), "host": socket.gethostname()}
    run = m.CollectionRun(connector="bbmp", trigger=trigger, status="running", requested_by=user_id, params=params, log=[])
    db.add(run)
    db.commit()
    return run


def _finish(db, run: m.CollectionRun, log: RunLog):
    run.finished_at = now()
    if run.status == "running":
        if run.files_failed or run.records_failed:
            run.status = "done_with_errors"
        else:
            run.status = "done"
        if run.files_failed and not (run.files_downloaded or run.files_unchanged or run.records_processed):
            run.status = "failed"
    db.commit()


def _fy_for(res, info, ds_cfg, filename=""):
    return (fiscal_label((ds_cfg or {}).get("fiscal_year")) or info.fiscal_year or fiscal_label(res.name) or fiscal_label(filename))


def _safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s or "file")[:120]


def _store_bytes(sf: m.SourceFile, content: bytes):
    d = os.path.join(download_dir(), sf.fiscal_year or "unknown")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{_safe_name(sf.resource_id)[:40]}_{sf.sha256[:10]}_{_safe_name(sf.original_filename)}")
    with open(path, "wb") as f:
        f.write(content)
    sf.stored_path = path


def _overrides_for(slug):
    try:
        for d in load_sources().get("datasets", []):
            if d.get("slug") == slug:
                return d.get("column_overrides") or {}
    except Exception:
        pass
    return {}


def _ctx_from_file(sf: m.SourceFile) -> FileContext:
    return FileContext(fiscal_year=sf.fiscal_year or "", ward_regime=sf.ward_regime or "", filename=sf.original_filename or "", file_format=sf.file_format,
                       dataset_slug=sf.dataset_slug or "", dataset_title=sf.dataset_title or "", dataset_url=sf.dataset_url or "",
                       download_url=sf.download_url or "", source_name=SOURCE_NAME if (sf.dataset_slug or "").startswith("bbmp") else (sf.dataset_title or SOURCE_NAME),
                       overrides=(sf.dataset_meta or {}).get("column_overrides") or _overrides_for(sf.dataset_slug), source_file_id=sf.id)


def import_source_file(db: Session, sf: m.SourceFile, content: bytes, run: m.CollectionRun | None, log: RunLog | None, user_id=None) -> bool:
    """Import the bytes of one SourceFile. True on success. On failure nothing from this file stays in the database and the file is marked."""
    sf_id = sf.id
    ctx = _ctx_from_file(sf)
    try:
        job, counts = import_rows(db, content, ctx, user_id=user_id, run_id=run.id if run else None)
    except Exception as e:  # ImportAborted, SchemaError, or an unexpected database / parsing error: all rolled back
        sf = db.get(m.SourceFile, sf_id)
        sf.status = "import_failed"
        sf.error = (f"{type(e).__name__}: {e}" if not isinstance(e, ImportAborted) else str(e))[:2000]
        sf.attempts = (sf.attempts or 0) + 1
        sf.last_checked_at = now()
        if run:
            run.files_failed = (run.files_failed or 0) + 1
        if log:
            log.add("error", f"Import of {sf.original_filename} failed: {sf.error[:300]}")
        db.commit()
        return False
    sf = db.get(m.SourceFile, sf_id)
    sf.status, sf.error, sf.import_job_id, sf.imported_at = "imported", None, job.id, now()
    sf.rows_processed, sf.rows_inserted, sf.rows_updated = counts.processed, counts.inserted, counts.updated
    sf.rows_duplicate, sf.rows_failed = counts.duplicate, counts.failed
    sf.attempts = (sf.attempts or 0) + 1
    if run:
        run.records_processed = (run.records_processed or 0) + counts.processed
        run.records_inserted = (run.records_inserted or 0) + counts.inserted
        run.records_updated = (run.records_updated or 0) + counts.updated
        run.records_duplicate = (run.records_duplicate or 0) + counts.duplicate
        run.records_failed = (run.records_failed or 0) + counts.failed
        job.stats = {**(job.stats or {}), "collection_run_id": run.id}
    if log:
        log.add("info", f"{sf.original_filename} ({sf.fiscal_year}, {sf.ward_regime}): {counts.processed} rows, {counts.inserted} new, {counts.updated} updated, "
                        f"{counts.duplicate} duplicate, {counts.failed} failed")
    db.commit()
    return True


# ----------------------------------------------------------------------------- one resource
def _process_resource(db, run, log, fetcher: Fetcher, info, res, ds_cfg, force, source_website):
    fmt_hint = (res.file_format or "").lower()
    regime = res.ward_regime or (ds_cfg or {}).get("default_ward_regime") or regime_from_text(info.title) or None
    fy = _fy_for(res, info, ds_cfg, res.filename or "")
    run.files_found = (run.files_found or 0) + 1
    base = dict(source_website=source_website, dataset_slug=info.slug, dataset_title=info.title[:300], dataset_url=info.url[:500], dataset_meta={**info.meta, "description": info.description},
                resource_id=res.resource_id[:80], resource_name=res.name[:300], download_url=res.download_url[:700], original_filename=(res.filename or "")[:300],
                fiscal_year=fy, ward_regime=regime)

    if fmt_hint not in SUPPORTED:
        sf = db.query(m.SourceFile).filter_by(resource_id=base["resource_id"], status="skipped").first()
        if not sf:
            db.add(m.SourceFile(run_id=run.id, file_format=fmt_hint[:10] or None, status="skipped", error=f"Format {fmt_hint or 'unknown'!r} is not a supported data file (CSV, XLSX, XLS).",
                                last_checked_at=now(), **base))
        else:
            sf.last_checked_at = now()
        log.add("info", f"Skipped {res.name}: not a CSV or Excel file")
        db.commit()
        return

    prev = db.query(m.SourceFile).filter(m.SourceFile.resource_id == base["resource_id"], m.SourceFile.sha256.isnot(None)).order_by(m.SourceFile.id.desc()).first()
    try:
        dl = fetcher.download(res.download_url, None if force or not prev else prev.http_etag, None if force or not prev else prev.http_last_modified)
    except FetchError as e:
        failed = db.query(m.SourceFile).filter_by(resource_id=base["resource_id"], status="download_failed").first()
        if failed:
            failed.error, failed.attempts, failed.last_checked_at, failed.run_id = f"{e.kind}: {e}"[:2000], (failed.attempts or 0) + 1, now(), run.id
            failed.download_url = base["download_url"]
        else:
            db.add(m.SourceFile(run_id=run.id, file_format=fmt_hint[:10], status="download_failed", error=f"{e.kind}: {e}"[:2000], attempts=1, last_checked_at=now(), **base))
        run.files_failed = (run.files_failed or 0) + 1
        log.add("error", f"Download of {res.name} failed ({e.kind}): {e}")
        db.commit()
        return

    if dl.status == "not_modified" and prev is not None:
        prev.last_checked_at = now()
        if prev.status == "imported":
            run.files_unchanged = (run.files_unchanged or 0) + 1
            log.add("info", f"{res.name}: unchanged (server reports not modified)")
            db.commit()
            return
        if prev.stored_path and os.path.exists(prev.stored_path):  # downloaded earlier but never imported successfully
            with open(prev.stored_path, "rb") as f:
                content = f.read()
            run.files_downloaded = (run.files_downloaded or 0) + 1
            import_source_file(db, prev, content, run, log)
            return
        dl = fetcher.download(res.download_url)  # nothing usable on disk: fetch again without the condition

    content = dl.content
    sha = hashlib.sha256(content).hexdigest()
    fmt = detect_format(content, res.filename or "", dl.content_type or "")
    if fmt == "html":
        msg = ("The server returned a web page instead of a data file" + (" that looks like a login or CAPTCHA page. Access restrictions are not bypassed; use the manual import."
                                                                          if looks_restricted(content[:20000].decode("utf-8", "replace")) else "."))
        failed = db.query(m.SourceFile).filter_by(resource_id=base["resource_id"], status="download_failed").first()
        if failed:
            failed.error, failed.attempts, failed.last_checked_at, failed.run_id = msg, (failed.attempts or 0) + 1, now(), run.id
        else:
            db.add(m.SourceFile(run_id=run.id, file_format=fmt_hint[:10], status="download_failed", error=msg, attempts=1, last_checked_at=now(), **base))
        run.files_failed = (run.files_failed or 0) + 1
        log.add("error", f"{res.name}: {msg}")
        db.commit()
        return
    if fmt not in SUPPORTED:
        db.add(m.SourceFile(run_id=run.id, file_format=(fmt or fmt_hint)[:10], status="skipped", sha256=sha, size_bytes=len(content), last_checked_at=now(),
                            error=f"The downloaded file is not a CSV or Excel file (detected: {fmt or 'unreadable'}).", **base))
        log.add("warning", f"{res.name}: downloaded content is {fmt or 'unreadable'}, not a data file")
        db.commit()
        return

    same = db.query(m.SourceFile).filter_by(resource_id=base["resource_id"], sha256=sha).first()
    if same is not None:
        same.last_checked_at, same.http_etag, same.http_last_modified = now(), dl.etag or same.http_etag, dl.last_modified or same.http_last_modified
        if same.status == "imported" and not force:
            run.files_unchanged = (run.files_unchanged or 0) + 1
            log.add("info", f"{res.name}: unchanged (same checksum as the file imported on {same.imported_at:%Y-%m-%d})")
            db.commit()
            return
        sf = same
        sf.run_id = run.id
    else:
        sf = m.SourceFile(run_id=run.id, file_format=fmt, sha256=sha, size_bytes=len(content), http_etag=dl.etag, http_last_modified=dl.last_modified,
                          downloaded_at=now(), last_checked_at=now(), status="downloaded", attempts=0, **base)
        # a failed earlier attempt for this resource is superseded by this download
        db.add(sf)
        db.flush()
        _store_bytes(sf, content)
        db.query(m.SourceFile).filter(m.SourceFile.resource_id == base["resource_id"], m.SourceFile.status == "download_failed", m.SourceFile.id != sf.id).delete()
    run.files_downloaded = (run.files_downloaded or 0) + 1
    log.add("info", f"Downloaded {res.name} ({len(content):,} bytes, {fmt})")
    db.commit()
    import_source_file(db, sf, content, run, log)


# ----------------------------------------------------------------------------- a whole run
def execute_run(run_id: int, fetcher: Fetcher | None = None, config: dict | None = None, only: list[str] | None = None, force: bool = False,
                session_factory=None) -> int:
    """Do the collection for an existing run record. Owns its database session. Returns the run id."""
    db = (session_factory or SessionLocal)()
    try:
        run = db.get(m.CollectionRun, run_id)
        log = RunLog(run)
        try:
            cfg = config or load_sources()
            conn = BbmpConnector(fetcher or FETCHER_FACTORY())
            f = conn.fetcher
            website = cfg.get("source_website") or "https://data.opencity.in"
            datasets = [d for d in cfg.get("datasets", []) if d.get("enabled", True) and (not only or d.get("slug") in only or d.get("url") in only)]
            if not datasets and not only:
                log.add("warning", "No datasets are enabled in the sources file.")
            for ds in datasets:
                try:
                    info = conn.discover_dataset(ds["url"], ds.get("fiscal_year"))
                except FetchError as e:
                    run.files_failed = (run.files_failed or 0) + 1
                    log.add("error", f"Dataset page {ds['url']} could not be read ({e.kind}): {e}")
                    db.commit()
                    continue
                run.datasets_found = (run.datasets_found or 0) + 1
                log.add("info", f"{info.title or info.slug}: {len(info.resources)} file(s) listed (via {info.discovered_via})")
                db.commit()
                for res in info.resources:
                    try:
                        _process_resource(db, run, log, f, info, res, ds, force, website)
                    except Exception as e:  # one bad file must not stop the others
                        db.rollback()
                        run = db.get(m.CollectionRun, run_id)
                        log.run = run
                        run.files_failed = (run.files_failed or 0) + 1
                        log.add("error", f"{res.name}: unexpected {type(e).__name__}: {str(e)[:200]}")
                        db.commit()
            if not only and cfg.get("discover_query"):
                try:  # report BBMP datasets on the portal that are not configured yet (they are not imported automatically)
                    known = {d.get("slug") for d in cfg.get("datasets", [])}
                    found = conn.search_datasets(website, cfg["discover_query"])
                    new = [d for d in found if d["slug"] not in known and any(k in (d["slug"] or "") for k in cfg.get("discover_slug_keywords", ["work-order"]))]
                    log.add("info", f"Portal search '{cfg['discover_query']}': {len(found)} dataset(s); not configured yet: "
                                    + (", ".join(d["slug"] for d in new[:15]) or "none"))
                except FetchError as e:
                    log.add("warning", f"Dataset search on {website} failed ({e.kind}): {e}")
                db.commit()
            if not only:
                for sp in cfg.get("site_pages", []):
                    if not sp.get("enabled", True):
                        continue
                    try:
                        info = conn.discover_site(sp["url"])
                    except FetchError as e:
                        log.add("warning", f"Official site {sp['url']} could not be scanned ({e.kind}): {e}")
                        db.commit()
                        continue
                    log.add("info", f"Official site {sp['url']}: {len(info.resources)} data file link(s) found")
                    for res in info.resources:
                        try:
                            _process_resource(db, run, log, f, info, res, sp, force, sp["url"])
                        except Exception as e:
                            db.rollback()
                            run = db.get(m.CollectionRun, run_id)
                            log.run = run
                            log.add("error", f"{res.name}: unexpected {type(e).__name__}: {str(e)[:200]}")
                            db.commit()
        except Exception as e:
            db.rollback()
            run = db.get(m.CollectionRun, run_id)
            log.run = run
            run.status = "failed"
            log.add("error", f"Run stopped: {type(e).__name__}: {str(e)[:300]}")
        _finish(db, run, log)
        return run_id
    finally:
        db.close()


def retry_failed(db: Session, user_id: int | None = None, file_ids: list[int] | None = None, fetcher: Fetcher | None = None,
                 run: m.CollectionRun | None = None) -> m.CollectionRun:
    """Try again every file whose download or import failed (or just `file_ids`). Imports use the stored copy when there is one."""
    run = run or start_run(db, "retry", user_id, {"file_ids": file_ids})
    log = RunLog(run)
    q = db.query(m.SourceFile).filter(m.SourceFile.status.in_(["download_failed", "import_failed"]))
    if file_ids:
        q = q.filter(m.SourceFile.id.in_(file_ids))
    files = q.order_by(m.SourceFile.id).all()
    log.add("info", f"Retrying {len(files)} file(s)")
    run.files_found = len(files)
    f = fetcher or FETCHER_FACTORY()
    for sf in files:
        sf_id = sf.id
        try:
            if sf.status == "import_failed" and sf.stored_path and os.path.exists(sf.stored_path):
                with open(sf.stored_path, "rb") as fh:
                    content = fh.read()
                sf.run_id = run.id
                db.commit()
                import_source_file(db, db.get(m.SourceFile, sf_id), content, run, log, user_id)
                continue
            dl = f.download(sf.download_url, strict_host=not (sf.dataset_slug or "").startswith("manual"))
            content = dl.content
            fmt = detect_format(content, sf.original_filename or "", dl.content_type or "")
            if fmt not in SUPPORTED:
                raise FetchError("not_data", f"the response is {fmt or 'unreadable'}, not a CSV or Excel file")
            sha = hashlib.sha256(content).hexdigest()
            other = db.query(m.SourceFile).filter(m.SourceFile.resource_id == sf.resource_id, m.SourceFile.sha256 == sha, m.SourceFile.id != sf.id).first()
            if other is not None:
                db.delete(sf)
                sf = other
                sf.run_id = run.id
                if sf.status == "imported":
                    run.files_unchanged = (run.files_unchanged or 0) + 1
                    db.commit()
                    continue
            else:
                sf.sha256, sf.size_bytes, sf.file_format, sf.http_etag, sf.http_last_modified = sha, len(content), fmt, dl.etag, dl.last_modified
                sf.downloaded_at, sf.run_id, sf.status, sf.error = now(), run.id, "downloaded", None
                _store_bytes(sf, content)
            run.files_downloaded = (run.files_downloaded or 0) + 1
            db.commit()
            import_source_file(db, sf, content, run, log, user_id)
        except FetchError as e:
            db.rollback()
            sf = db.get(m.SourceFile, sf_id)
            run = db.get(m.CollectionRun, run.id)
            log.run = run
            sf.error, sf.attempts, sf.last_checked_at = f"{e.kind}: {e}"[:2000], (sf.attempts or 0) + 1, now()
            run.files_failed = (run.files_failed or 0) + 1
            log.add("error", f"Retry of {sf.original_filename or sf.resource_name} failed ({e.kind}): {e}")
            db.commit()
    run = db.get(m.CollectionRun, run.id)
    log.run = run
    _finish(db, run, log)
    return run


# ----------------------------------------------------------------------------- manual file / URL import
def manual_import(db: Session, content: bytes, filename: str, *, fiscal_year: str | None, ward_regime: str | None, label: str = "Manual import",
                  source_url: str = "", user_id: int | None = None, column_overrides: dict | None = None) -> tuple[m.CollectionRun, m.SourceFile]:
    """Import a file the officer downloaded (or a URL's content). Same validation, storage and history as the automatic path."""
    fy = fiscal_label(fiscal_year) or fiscal_label(filename)
    regime = (ward_regime or regime_from_text(filename) or "").strip()
    fmt = detect_format(content, filename)
    if fmt not in SUPPORTED:
        raise ValueError(f"This is not a CSV or Excel file (detected: {fmt or 'unreadable'}).")
    if not fy:
        raise ValueError("State the financial year of the file (for example 2024-25); it cannot be read from the file name.")
    if not regime:
        raise ValueError("State the ward regime of the file (198, 225, 243 or common).")
    run = start_run(db, "manual_import", user_id, {"filename": filename})
    log = RunLog(run)
    sha = hashlib.sha256(content).hexdigest()
    rid = f"manual-{sha[:16]}"
    sf = db.query(m.SourceFile).filter_by(resource_id=rid, sha256=sha).first()
    run.files_found = 1
    if sf is not None and sf.status == "imported":
        run.files_unchanged = 1
        log.add("info", f"{filename}: this exact file was already imported on {sf.imported_at:%Y-%m-%d}; nothing to do.")
        _finish(db, run, log)
        return run, sf
    if sf is None:
        sf = m.SourceFile(run_id=run.id, source_website="manual", dataset_slug="manual", dataset_title=label[:300], dataset_url=source_url[:500], resource_id=rid,
                          resource_name=filename[:300], download_url=source_url[:700], original_filename=filename[:300], file_format=fmt, fiscal_year=fy,
                          ward_regime=regime[:10], size_bytes=len(content), sha256=sha, downloaded_at=now(), last_checked_at=now(), status="downloaded", attempts=0,
                          dataset_meta={"manual": True})
        db.add(sf); db.flush()
        _store_bytes(sf, content)
    run.files_downloaded = 1
    db.commit()
    if column_overrides:
        sf.dataset_meta = {**(sf.dataset_meta or {}), "column_overrides": column_overrides}
        db.commit()
    import_source_file(db, sf, content, run, log, user_id)
    run = db.get(m.CollectionRun, run.id)
    log.run = run
    _finish(db, run, log)
    return run, db.get(m.SourceFile, sf.id)
