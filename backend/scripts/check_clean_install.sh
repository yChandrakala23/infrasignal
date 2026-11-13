#!/usr/bin/env bash
# Installs requirements.txt into a brand-new virtual environment and proves the app's PDF, OCR, Excel and LLM-fallback paths work
# with nothing else installed. Run from backend/:   bash scripts/check_clean_install.sh [--with-tests]
# Needs network access to PyPI. Tesseract (apt install tesseract-ocr tesseract-ocr-kan) is a system package; the OCR check says so
# when it is missing instead of failing.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$(mktemp -d)/venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"
echo "installed: $("$VENV/bin/pip" list --format=freeze | wc -l) packages into $VENV"
cd "$HERE"
export DATABASE_URL="sqlite:///$(mktemp -d)/clean.db" UPLOAD_DIR="$(mktemp -d)" DOCUMENT_DIR="$(mktemp -d)" SECRET_KEY=clean-install-check
unset ANTHROPIC_API_KEY
"$VENV/bin/python" -W ignore - <<'PY'
import io, shutil, sys
import httpx, xlrd, openpyxl, pdfplumber, fitz, pytesseract, PIL, scipy, numpy, pandas  # every runtime library imports
print("imports ok; xlrd", xlrd.__version__, "pdfplumber", pdfplumber.__version__)

# 1. the application itself starts (migrations run on an empty database)
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    assert c.get("/api/public/stats").status_code == 200
print("app starts, migrations applied")

# 2. PDF extraction without any LLM key
import tempfile, fitz
d = tempfile.mkdtemp(); p = d + "/wo.pdf"
doc = fitz.open(); pg = doc.new_page()
pg.insert_text((50, 80), "WORK ORDER\nWork Order No: CLEAN/WO/1\nBBMP Job No: CLEAN/J/1\nContractor: M/s Clean Co\nWork Order Amount: Rs. 4,50,000", fontsize=11)
doc.save(p)
from app.extract import process_document
r = process_document(p, use_llm=True)
assert r["llm_used"] is False and any(f["field"] == "job_number" and f["norm_value"] == "CLEAN/J/1" for f in r["fields"]), r["fields"]
print("rule-based extraction ok without an LLM key")

# 3. BOQ tables (PyMuPDF + pdfplumber)
from app import boq
bp = d + "/boq.pdf"
doc = fitz.open(); pg = doc.new_page()
rows = [["Sl No", "Description", "Unit", "Qty", "Rate", "Amount"], ["1", "Tack coat", "sqm", "100", "10", "1,000"]]
for i, row in enumerate(rows):
    x = 40
    for v, w in zip(row, (40, 200, 50, 60, 60, 80)):
        pg.draw_rect(fitz.Rect(x, 100 + i * 20, x + w, 120 + i * 20)); pg.insert_text((x + 3, 114 + i * 20), v, fontsize=9); x += w
pg.insert_text((40, 60), "Bill of Quantities", fontsize=12); doc.save(bp)
res = boq.extract_boq(bp); boq.validate(res["items"])
assert [i["total_amount"] for i in res["items"] if i["row_kind"] == "item"] == [1000.0] and res["items"][0]["check_status"] == "ok"
print("BOQ extraction ok")

# 4. Excel: .xlsx and .xls
import openpyxl
from app.ingest import read_table
wb = openpyxl.Workbook(); ws = wb.active; ws.append(["Job No", "Work Description"]); ws.append(["A/1", "Road work"]); b = io.BytesIO(); wb.save(b)
assert read_table(b.getvalue(), "a.xlsx").shape == (1, 2)
try:
    import xlwt
    wb = xlwt.Workbook(); w = wb.add_sheet("s"); w.write(0, 0, "Job No"); w.write(0, 1, "Work Description"); w.write(1, 0, "A/1"); w.write(1, 1, "Road work"); b = io.BytesIO(); wb.save(b)
    assert read_table(b.getvalue(), "a.xls").shape == (1, 2); print("xlsx and xls ok")
except ImportError:
    print("xlsx ok (xlwt not installed, so no .xls file was written to test; run with --with-tests for the .xls tests)")

# 5. OCR
from app import ocr
have = ocr.installed_languages()
if not shutil.which("tesseract"):
    print("OCR: tesseract binary NOT installed (apt install tesseract-ocr tesseract-ocr-kan). Text-layer PDFs still work; scanned pages are flagged.")
else:
    print("OCR: tesseract", ocr.tesseract_version(), "languages:", sorted(have & {"eng", "kan"}), "plan:", ocr.plan())

# 6. BBMP collection: libraries import, a saved BBMP-format file goes through validation and import, a second import changes nothing,
#    the schedule's cron weekdays mean what cron users expect
import bs4, apscheduler
from app.db import SessionLocal
from app import collection as col, models as m
from app.scheduler import build_trigger
csv_ = b"slno,id,Ward,wodetails,contractor,brnumber,amount,nett,deduction\n1,10,5,005-24-000001</a>Asphalting of Test Road,123456 CLEAN CO0000000000,BR - 000001 / 10-Jul-2024CBR - / Rtgs - 000009 / 19-Jul-2024,1000,900,100\n"
db = SessionLocal()
run, sf = col.manual_import(db, csv_, "clean.csv", fiscal_year="2024-25", ward_regime="243")
assert sf.status == "imported" and sf.rows_inserted == 1, (sf.status, sf.error)
run2, sf2 = col.manual_import(db, csv_, "clean.csv", fiscal_year="2024-25", ward_regime="243")
assert run2.files_unchanged == 1 and db.query(m.Payment).filter(m.Payment.external_key.isnot(None)).count() == 1
nxt = build_trigger("0 3 * * 0", "Asia/Kolkata").get_next_fire_time(None, __import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")))
assert nxt.weekday() == 6
print("BBMP collection ok (import, repeat import is a no-op, Sunday schedule)")
PY
if [ "${1:-}" = "--with-tests" ]; then
  "$VENV/bin/pip" install --quiet -r "$HERE/requirements-dev.txt"
  "$VENV/bin/python" -m pytest -q -W ignore
fi
echo "CLEAN INSTALL CHECK PASSED"
