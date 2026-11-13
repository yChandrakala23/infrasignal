"""One IFMS collection run: search the Works Bill Public View for each configured term, store every row once (web_records, origin
'ifms', original row kept), link rows to existing works by exact job number, and record counts and failures in collection_runs."""
import hashlib, json
from sqlalchemy import or_

from . import models as m
from .collection import start_run, RunLog, _finish, now
from .connectors.bbmp_connector import Fetcher, FetchError, load_sources
from .connectors.ifms import IfmsClient, normalise_ifms_row
from .db import SessionLocal

FETCHER_FACTORY = Fetcher
SOURCE = "BBMP IFMS Works Bill Public View"


def start_ifms_run(db, trigger="manual", user_id=None, params=None):
    run = start_run(db, trigger, user_id, params)
    run.connector = "ifms"
    db.commit()
    return run


def execute_ifms_run(run_id, fetcher=None, config=None, terms=None, session_factory=None):
    db = (session_factory or SessionLocal)()
    try:
        run = db.get(m.CollectionRun, run_id)
        log = RunLog(run)
        cfg = (config or load_sources()).get("ifms") or {}
        f = fetcher or FETCHER_FACTORY(allowed_hosts=["accounts.bbmp.gov.in"])
        client = IfmsClient(f, cfg.get("url", "https://accounts.bbmp.gov.in/PublicView/"))
        terms = terms or cfg.get("search_terms") or ["road"]
        cap = int(cfg.get("max_rows_per_term", 5000))
        seen = set()
        for term in terms:
            run.datasets_found = (run.datasets_found or 0) + 1
            try:
                rows, url = client.search(term)
            except FetchError as e:
                run.files_failed = (run.files_failed or 0) + 1
                log.add("error", f"IFMS search '{term}' failed ({e.kind}): {e}")
                db.commit()
                if e.kind in ("http", "network", "timeout") and run.files_failed >= 2 and not run.records_processed:
                    log.add("error", "IFMS is not answering; the remaining search terms were not tried.")
                    break
                continue
            ins = dup = 0
            for row in rows[:cap]:
                body = json.dumps({k: v for k, v in row.items() if k != "slno"}, sort_keys=True, ensure_ascii=False, default=str)
                h = hashlib.sha256(("ifms|" + body).encode()).hexdigest()
                run.records_processed = (run.records_processed or 0) + 1
                if h in seen or db.query(m.WebRecord.id).filter_by(row_hash=h).first():
                    dup += 1
                    continue
                seen.add(h)
                norm = normalise_ifms_row(row)
                if norm.get("job_number"):
                    code = norm["job_number"]
                    w = db.query(m.Roadwork.id, m.Roadwork.ward_regime).filter(or_(m.Roadwork.job_number == code, m.Roadwork.work_order_id == code)).all()
                    if w:  # exact code only. IFMS does not state the ward regime and a code can repeat across regimes: candidates, not links
                        norm["candidate_works"] = [{"roadwork_id": x[0], "ward_regime": x[1]} for x in w]
                        norm["candidate_note"] = "same published code; IFMS does not state the ward regime: verify before linking"
                db.add(m.WebRecord(source=SOURCE, origin="ifms", page_url=url[:1000], row_number=row.get("slno"), raw=row, normalised=norm,
                                   category="payment", row_hash=h, last_seen_at=now(), run_id=run.id))
                ins += 1
            run.records_inserted = (run.records_inserted or 0) + ins
            run.records_duplicate = (run.records_duplicate or 0) + dup
            log.add("info", f"IFMS search '{term}': {len(rows)} row(s), {ins} new, {dup} already stored")
            db.commit()
        if run.status == "running" and not run.records_processed:
            run.status = "failed"
        _finish(db, run, log)
        return run_id
    finally:
        db.close()
