"""Fill an empty database with REAL public data captured from the live portals, so the whole application can be demonstrated offline.

    python -m scripts.setup_demo                         # real data only
    python -m scripts.setup_demo --with-sample-complaints   # also 85 SYNTHETIC citizen complaints (labelled SAMPLE) to show clustering

What is loaded (all real, with source URLs and checksums; see data/captures/*/manifest.json):
  1. Ward maps: 198 wards (2012) and 243 wards (2022), DataMeet / KGIS, CC BY-SA 2.5 India
  2. BBMP work orders and payments 2024-25 and 2025-26 (OpenCity, 7 files, 4 ward regimes), through the real collector (python -m scripts.replay_capture)
  3. BBMP tenders 2017-18 (OpenCity, 6,842 rows)
  4. Two PDFs from bbmp.gov.in (a 123-page tender document and a scanned notification), through the document pipeline
  5. Tender <-> work matching (suggestions only; an officer accepts or rejects them)
Takes about 5-10 minutes. Safe to run again: nothing is imported twice.
"""
import argparse, json, os, sys, time
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)
from app.db import init_db, SessionLocal  # noqa: E402
from app import models as m, spatial  # noqa: E402
from app.seed import load_ward_file, load_complaints, DATA  # noqa: E402
from app.ingest import import_tenders  # noqa: E402
from app.pipeline import ingest_document, recluster  # noqa: E402
from app.matching import match_tenders  # noqa: E402

CAP = BACKEND / "data" / "captures"


def step(msg):
    print(f"\n[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--with-sample-complaints", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    init_db()
    db = SessionLocal()
    step("1/5 Ward maps (real, DataMeet)")
    for r in ("198", "243"):
        if not db.query(m.Ward).filter_by(regime=r).first():
            print("  ", r, load_ward_file(db, DATA / "wards" / f"wards_{r}.geojson", r, f"data/wards/SOURCES.md ({r} regime)"))
        else:
            print(f"   {r}: already loaded")
    db.commit(); spatial.clear_cache()
    try:
        spatial.sync_postgis(db); db.commit()
    except Exception as e:
        print("   (PostGIS geometry sync skipped:", e, ")")
    db.close()

    step("2/5 BBMP work orders and payments (real OpenCity files, through the collector)")
    from scripts import replay_capture
    sys.argv = ["replay_capture"]
    replay_capture.main()

    step("3/5 BBMP tenders 2017-18 (real OpenCity file)")
    db = SessionLocal()
    man = json.loads((CAP / "opencity-2026-11-13" / "manifest.json").read_text(encoding="utf-8"))["tenders"]
    if db.query(m.ImportJob).filter_by(kind="tenders", source_url=man["url"]).first():
        print("   already imported")
    else:
        job = import_tenders(db, (CAP / "opencity-2026-11-13" / man["file"]).read_bytes(), man["file"], "OpenCity: BBMP Tenders (2017-2018)", man["url"], None)
        db.commit()
        print(f"   {job.rows_ok} imported, {job.rows_duplicate} duplicate, {job.rows_failed} failed")

    step("4/5 PDFs from bbmp.gov.in (real; text + OCR extraction)")
    gm = json.loads((CAP / "gba-site-2026-11-12" / "manifest.json").read_text(encoding="utf-8"))
    for d in gm["documents"]:
        doc, new = ingest_document(db, str(CAP / "gba-site-2026-11-12" / d["file"]), d["file"], source_name="GBA", match=False,
                                   source_page_url=gm["source_page"], download_url=d["url"], downloaded_at=datetime.fromisoformat(gm["captured_at"]).replace(tzinfo=None))
        print(f"   {d['file']}: {'new' if new else 'already stored'}, type {doc.doc_type}, {doc.pages} pages, {doc.ocr_pages or 0} read by OCR, status {doc.status}")

    step("5/5 Tender <-> work matching (suggestions for review)")
    print(f"   {match_tenders(db, None)} suggestion(s)")
    if a.with_sample_complaints:
        step("Extra: 85 SYNTHETIC sample complaints (labelled SAMPLE) and clustering")
        print(f"   complaints added: {load_complaints(db)}; clusters: {recluster(db)}")
    print(f"\nDone in {time.time() - t0:.0f}s. Works: {db.query(m.Roadwork).count()}, payments: {db.query(m.Payment).count()}, "
          f"tenders: {db.query(m.Tender).count()}, documents: {db.query(m.Document).count()}, wards: {db.query(m.Ward).count()}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
