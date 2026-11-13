"""Process one or more PDFs (tender notices, work orders, bills, BOQs...) into the database, with their source recorded.

    python -m scripts.process_pdf path\\to\\file.pdf
    python -m scripts.process_pdf file.pdf --source-page https://bbmp.gov.in/ --download-url https://bbmp.gov.in/csr/file.pdf
    python -m scripts.process_pdf folder\\with\\pdfs              # every PDF in the folder

Text pages are read with PyMuPDF; scanned pages with Tesseract OCR (install it first; see README). For every field the page number, the
raw text, the normalised value, the method and a confidence are stored, and the value stays "unverified" until an officer verifies it on
the Documents page. A file already stored (same SHA-256) is not processed again.
"""
import argparse, os, sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)
from app.db import init_db, SessionLocal  # noqa: E402
from app import models as m  # noqa: E402
from app.pipeline import ingest_document  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--source-page", help="web page that lists the document")
    ap.add_argument("--download-url", help="direct link to the file (only with a single file)")
    ap.add_argument("--source-name", default="manual upload")
    a = ap.parse_args()
    files = []
    for p in a.paths:
        p = Path(p)
        files += sorted(p.glob("*.pdf")) + sorted(p.glob("*.PDF")) if p.is_dir() else [p]
    if a.download_url and len(files) != 1:
        print("--download-url can only be used with a single file"); return 2
    init_db()
    db = SessionLocal()
    bad = 0
    for f in files:
        try:
            doc, new = ingest_document(db, str(f), f.name, source_name=a.source_name, source_page_url=a.source_page,
                                       download_url=a.download_url, downloaded_at=datetime.now() if a.download_url else None)
        except Exception as e:
            bad += 1
            print(f"FAILED  {f.name}: {type(e).__name__}: {e}")
            db.rollback()
            continue
        fields = db.query(m.Extraction).filter_by(document_id=doc.id).count()
        flagged = (doc.ocr_summary or {}).get("flagged_pages") or []
        print(f"{'NEW    ' if new else 'EXISTS '} {f.name}: type {doc.doc_type} ({(doc.doc_type_confidence or 0):.0%}), {doc.pages} page(s), "
              f"{doc.ocr_pages or 0} by OCR{', pages to check: ' + ', '.join(map(str, flagged)) if flagged else ''}; {fields} field value(s); status {doc.status}")
    db.close()
    print("\nReview the values on the Government Monitoring page > Documents.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
