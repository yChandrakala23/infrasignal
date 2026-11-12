import json
from pathlib import Path
import pytest
from app.extract import process_document
from app.evaluation import eval_extraction, DATA
from app.classify import classify_text


def test_extraction_accuracy_floors():
    """Regression floors, not accuracy claims. Narrative set 2 is the only independent set; the floor is deliberately low."""
    r = eval_extraction()
    sets = {s["key"]: s for s in r["sets"]}
    assert sets["sample_pack"]["accuracy"] >= 0.97
    assert sets["narrative_1"]["accuracy"] >= 0.9
    assert sets["narrative_2"]["accuracy"] >= 0.55
    assert r["type_accuracy"] >= 0.9


def test_every_value_has_page_and_provenance():
    r = process_document(str(DATA / "sample" / "documents" / "A06_work_order_hosur_road.pdf"))
    assert r["doc_type"] == "work_order"
    for f in r["fields"]:
        assert f["page"] >= 1 and f["method"] and f["confidence"] is not None


def test_nothing_is_invented_for_unrelated_document():
    r = process_document(str(DATA / "sample" / "documents" / "Z01_meeting_minutes_other.pdf"))
    assert r["doc_type"] == "other"
    assert not {f["field"] for f in r["fields"]} & {"job_number", "tender_id", "work_order_id", "awarded_amount"}


def test_values_literally_appear_in_the_document():
    import fitz
    p = DATA / "sample" / "documents" / "A06_work_order_hosur_road.pdf"
    text = " ".join(pg.get_text() for pg in fitz.open(p)).replace("\n", " ")
    for f in process_document(str(p))["fields"]:
        if f["field"] in ("job_number", "tender_id", "work_order_id"):
            assert f["raw_value"].strip() in text


def test_scanned_pdf_uses_ocr():
    pytest.importorskip("pytesseract")
    import shutil
    if not shutil.which("tesseract"):
        pytest.skip("tesseract not installed")
    r = process_document(str(DATA / "sample" / "documents" / "C01_work_order_mg_road_SCANNED.pdf"))
    assert r["ocr_pages"] == 1 and r["doc_type"] == "work_order"
    assert any(f["field"] == "job_number" for f in r["fields"])


def test_classifier_survives_line_wrapped_phrases():
    assert classify_text(["Payment Advice\nThis is the net amount\npayable to the contractor for the running\naccount bill"])["type"] == "payment_record"


def test_llm_hallucination_guard():
    from app.llm_extract import occurs_on_page
    assert occurs_on_page("SAMPLE/J/1", "Job No: SAMPLE/J/1") and not occurs_on_page("SAMPLE/J/9", "Job No: SAMPLE/J/1")
