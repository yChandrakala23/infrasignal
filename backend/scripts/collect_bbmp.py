"""Collect public BBMP data from every configured source into the database (DATABASE_URL), then print a per-source report.

    python -m scripts.collect_bbmp                      # all sources: OpenCity datasets, IFMS, BBMP/GBA websites
    python -m scripts.collect_bbmp --sources opencity   # one or more of: opencity, ifms, web
    python -m scripts.collect_bbmp --force              # download and import again even if a file is unchanged
    python -m scripts.collect_bbmp --status             # last runs per source

Run from the backend folder. Logs: logs/collection.log (every run) and logs/collection_<time>.json (full report of one run).
Downloaded files: uploads/bbmp/ (OpenCity files by financial year) and uploads/bbmp/web/ (documents from the websites).
A file you downloaded yourself:  python scripts/bbmp_collect.py --file my.csv --fiscal-year 2024-25 --ward-regime 243
Nothing is logged in to, no CAPTCHA is solved, robots.txt is respected. Exit code: 0 all sources fine, 1 some failures, 2 nothing collected.
"""
import argparse, json, logging, os, sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.db import SessionLocal, init_db, is_postgres  # noqa: E402
from app import models as m, collection as col, web_collection as webcol, ifms_collection as ifms  # noqa: E402

LOG_DIR = BACKEND / "logs"


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    lg = logging.getLogger("collect_bbmp")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if not lg.handlers:
        fh = logging.FileHandler(LOG_DIR / "collection.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(fh)
    return lg


def run_one(name, starter, executor, **kw):
    db = SessionLocal()
    try:
        rid = starter(db, "cli", None, {"cli": True, **{k: v for k, v in kw.items() if isinstance(v, (str, bool, list, type(None)))}}).id
    except col.RunInProgress as e:
        return {"source": name, "status": "not_started", "error": str(e)}
    finally:
        db.close()
    executor(rid, **kw)
    db = SessionLocal()
    try:
        r = db.get(m.CollectionRun, rid)
        return {"source": name, "run_id": r.id, "status": r.status, "started_at": str(r.started_at), "finished_at": str(r.finished_at),
                "pages_or_datasets": r.datasets_found, "files_found": r.files_found, "files_downloaded": r.files_downloaded,
                "files_unchanged": r.files_unchanged, "files_failed": r.files_failed, "records_processed": r.records_processed,
                "records_inserted": r.records_inserted, "records_updated": r.records_updated, "records_duplicate": r.records_duplicate,
                "records_failed": r.records_failed, "log": r.log or []}
    finally:
        db.close()


def show_status():
    db = SessionLocal()
    try:
        for conn in ("bbmp", "ifms", "web"):
            last = db.query(m.CollectionRun).filter(m.CollectionRun.connector == conn).order_by(m.CollectionRun.id.desc()).first()
            label = {"bbmp": "opencity", "ifms": "ifms", "web": "web"}[conn]
            print(f"{label:9} " + (f"run {last.id} {last.started_at:%Y-%m-%d %H:%M} {last.status}: rows +{last.records_inserted} dup {last.records_duplicate} "
                                    f"failed {last.records_failed}; files {last.files_downloaded} downloaded, {last.files_failed} failed" if last else "never run"))
        print(f"works from BBMP open data: {db.query(m.Roadwork).filter(m.Roadwork.data_origin == 'bbmp_open_data').count()}, "
              f"payments: {db.query(m.Payment).filter(m.Payment.external_key.like('bbmp|%')).count()}, "
              f"web/IFMS records: {db.query(m.WebRecord).count()}, documents: {db.query(m.Document).count()}")
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", default="opencity,ifms,web", help="comma list: opencity, ifms, web")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max-documents", type=int, help="web documents to download per source this run")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    init_db()
    if a.status:
        show_status()
        return 0
    lg = setup_logging()
    wanted = [s.strip() for s in a.sources.split(",") if s.strip()]
    print(f"Database: {'PostgreSQL' if is_postgres() else 'SQLite'}   Sources: {', '.join(wanted)}")
    results = []
    if "opencity" in wanted:
        results.append(run_one("opencity", col.start_run, col.execute_run, force=a.force))
    if "ifms" in wanted:
        results.append(run_one("ifms", ifms.start_ifms_run, ifms.execute_ifms_run))
    if "web" in wanted:
        results.append(run_one("web", webcol.start_web_run, webcol.execute_web_run, force=a.force, max_docs=a.max_documents))

    print("\nSource     Status             Files ok/unchanged/failed   Records new/updated/duplicate/failed")
    for r in results:
        if r.get("status") == "not_started":
            print(f"{r['source']:10} not started: {r['error']}")
            continue
        print(f"{r['source']:10} {r['status']:18} {r['files_downloaded'] or 0:>4}/{r['files_unchanged'] or 0}/{r['files_failed'] or 0:<18}"
              f" {r['records_inserted'] or 0}/{r['records_updated'] or 0}/{r['records_duplicate'] or 0}/{r['records_failed'] or 0}")
    failures = [(r["source"], x["message"]) for r in results for x in r.get("log", []) if x.get("level") == "error"]
    print(f"\nFailures ({len(failures)}):" if failures else "\nNo failures.")
    for s, msg in failures[:60]:
        print(f"  [{s}] {msg}")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = LOG_DIR / f"collection_{stamp}.json"
    report.write_text(json.dumps({"at": stamp, "results": results}, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    for r in results:
        lg.info("%s run %s: %s; records +%s dup %s failed %s; files %s/%s/%s", r["source"], r.get("run_id"), r.get("status"), r.get("records_inserted"),
                r.get("records_duplicate"), r.get("records_failed"), r.get("files_downloaded"), r.get("files_unchanged"), r.get("files_failed"))
        for x in r.get("log", []):
            lg.log(logging.ERROR if x.get("level") == "error" else logging.WARNING if x.get("level") == "warning" else logging.INFO, "%s: %s", r["source"], x["message"])
    print(f"\nFull report: {report}\nLog: {LOG_DIR / 'collection.log'}")
    ok = [r for r in results if r.get("status") in ("done", "done_with_errors")]
    return 0 if all(r.get("status") == "done" for r in results) else (1 if ok else 2)


if __name__ == "__main__":
    sys.exit(main())
