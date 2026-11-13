"""Collect BBMP work-order and payment data and load it into the configured database (DATABASE_URL).

    python scripts/bbmp_collect.py                          # all enabled datasets in data/bbmp_sources.json (needs internet access)
    python scripts/bbmp_collect.py --dataset bbmp-work-orders-and-payments-2024-25   # one dataset (the 2024-25 workflow)
    python scripts/bbmp_collect.py --force                  # download and import again even if nothing changed
    python scripts/bbmp_collect.py --retry                  # try again the files that failed last time
    python scripts/bbmp_collect.py --file my.csv --fiscal-year 2024-25 --ward-regime 243     # a file you downloaded yourself
    python scripts/bbmp_collect.py --url https://.../file.csv --fiscal-year 2024-25 --ward-regime 243
    python scripts/bbmp_collect.py --status                 # last runs and the last successful collection

Same code as the API and the schedule, so the history (collection_runs / source_files) is identical. Nothing is logged in to, no CAPTCHA is
solved and robots.txt is respected: a page that needs any of that is reported and left alone; use --file or --url instead.
Exit code: 0 all good, 1 some files or rows failed, 2 the run could not start.
"""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import SessionLocal, init_db
from app import collection as col
from app import models as m


def show_run(db, run_id):
    db.expire_all()
    r = db.get(m.CollectionRun, run_id)
    print(f"\nRun {r.id} ({r.trigger}): {r.status}")
    print(f"  datasets {r.datasets_found}, files listed {r.files_found}, downloaded {r.files_downloaded}, unchanged {r.files_unchanged}, failed {r.files_failed}")
    print(f"  rows processed {r.records_processed}, inserted {r.records_inserted}, updated {r.records_updated}, duplicate {r.records_duplicate}, failed {r.records_failed}")
    for f in db.query(m.SourceFile).filter(m.SourceFile.run_id == r.id).order_by(m.SourceFile.id):
        err = f"  -> {f.error}" if f.error else ""
        print(f"  [{f.status}] {f.fiscal_year or '?'} {f.ward_regime or '?'}  {f.original_filename or f.download_url}  rows {f.rows_processed}/{f.rows_inserted}/{f.rows_duplicate}/{f.rows_failed}{err}")
    return 0 if r.status == "done" else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", action="append", help="dataset slug or URL from data/bbmp_sources.json (repeatable)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--retry", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--url")
    ap.add_argument("--fiscal-year")
    ap.add_argument("--ward-regime", help="198, 225, 243 or common")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    init_db()
    db = SessionLocal()
    try:
        if a.status:
            for r in db.query(m.CollectionRun).order_by(m.CollectionRun.id.desc()).limit(10):
                print(f"{r.id:>4} {r.started_at:%Y-%m-%d %H:%M} {r.trigger:<13} {r.status:<17} files {r.files_downloaded}/{r.files_found}  rows +{r.records_inserted} ~{r.records_updated} !{r.records_failed}")
            ok = db.query(m.CollectionRun).filter(m.CollectionRun.status == "done").order_by(m.CollectionRun.id.desc()).first()
            print("Last successful collection:", f"run {ok.id} at {ok.finished_at}" if ok else "none yet")
            return 0
        if a.file or a.url:
            if a.file:
                p = Path(a.file)
                content, name, src = p.read_bytes(), p.name, ""
            else:
                dl = col.FETCHER_FACTORY().download(a.url, strict_host=False)
                content, name, src = dl.content, a.url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "download", a.url
            run, sf = col.manual_import(db, content, name, fiscal_year=a.fiscal_year, ward_regime=a.ward_regime, source_url=src)
            return show_run(db, run.id)
        if a.retry:
            run = col.retry_failed(db)
            return show_run(db, run.id)
        try:
            run = col.start_run(db, "manual", None, {"cli": True, "only": a.dataset, "force": a.force})
        except col.RunInProgress as e:
            print(e)
            return 2
        col.execute_run(run.id, only=a.dataset, force=a.force)
        return show_run(db, run.id)
    except (ValueError, OSError, col.FetchError) as e:
        print("Error:", e)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
