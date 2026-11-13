"""Live crawl of the BBMP / GBA website and the other configured portals, straight into the database (DATABASE_URL).

    python scripts/crawl_live.py                       # every enabled source in data/bbmp_sources.json -> crawl_sources
    python scripts/crawl_live.py --source "GBA / BBMP official website"
    python scripts/crawl_live.py --no-download         # discover and list only
    python scripts/crawl_live.py --max-documents 10    # cap downloads this run
    python scripts/crawl_live.py --report evidence.json   # write what was visited, found, downloaded and stored

Run it twice: the second run reports pages as unchanged / modified and only downloads new or changed files.
Nothing is logged in to, no CAPTCHA is solved, robots.txt is respected. Exit code: 0 done, 1 done with errors, 2 failed.
"""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import SessionLocal, init_db
from app import models as m, web_collection as wc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max-documents", type=int)
    ap.add_argument("--report")
    a = ap.parse_args()
    init_db()
    db = SessionLocal()
    try:
        run = wc.start_web_run(db, "cli", None, {"sources": a.source})
    except wc.RunInProgress as e:
        print(e); return 2
    rid = run.id
    db.close()
    wc.execute_web_run(rid, only=a.source, download=not a.no_download, force=a.force, max_docs=a.max_documents)
    db = SessionLocal()
    r = db.get(m.CollectionRun, rid)
    print(f"Run {r.id}: {r.status} | pages {r.datasets_found} | document links {r.files_found} | downloaded {r.files_downloaded} "
          f"unchanged {r.files_unchanged} failed {r.files_failed} | records +{r.records_inserted} (dup {r.records_duplicate})")
    for x in r.log or []:
        print(f"  [{x['level']}] {x['message']}")
    if a.report:
        pages = db.query(m.WebPage).filter(m.WebPage.last_run_id == rid).all()
        items = db.query(m.WebItem).filter(m.WebItem.last_run_id == rid, m.WebItem.kind == "document").all()
        recs = db.query(m.WebRecord).filter(m.WebRecord.run_id == rid).limit(200).all()
        docs = {d.id: d for d in db.query(m.Document).filter(m.Document.id.in_([i.document_id for i in items if i.document_id]))}
        out = {"run": {"id": r.id, "status": r.status, "started_at": str(r.started_at), "finished_at": str(r.finished_at), "log": r.log},
               "pages": [{"url": p.url, "final_url": p.final_url, "status": p.status, "error": p.error, "via": p.via, "title": p.title, "links": p.links_found,
                          "tables": p.tables_found, "sha256": p.raw_sha256, "fetched": str(p.last_fetched_at), "changed": str(p.last_changed_at)} for p in pages],
               "documents": [{"id": i.id, "label": i.label, "url": i.url, "found_on": i.found_on, "category": i.category, "via": i.discovered_via,
                              "status": i.status, "error": i.error, "sha256": i.sha256, "bytes": i.size_bytes, "downloaded_at": str(i.downloaded_at),
                              "versions": i.versions, "document_id": i.document_id,
                              "doc_type": docs[i.document_id].doc_type if i.document_id in docs else None,
                              "records": i.records_extracted, "excerpt": (i.text_excerpt or "")[:300]} for i in items],
               "records_sample": [{"id": x.id, "origin": x.origin, "page_url": x.page_url, "raw": x.raw, "normalised": x.normalised} for x in recs]}
        Path(a.report).write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        print("Evidence report:", a.report)
    return {"done": 0, "done_with_errors": 1}.get(r.status, 2)


if __name__ == "__main__":
    sys.exit(main())
