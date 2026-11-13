"""Requirement 4: BOQ tables -> boq_items (item no, description, unit, quantity, rate, amount, document, page) with arithmetic checks."""
import fitz
from pathlib import Path
from app import boq, models as m
from app.db import SessionLocal
from pdfmaker import make_boq_pdf

SAMPLE = Path(__file__).resolve().parents[1] / "data/sample/documents/A02_boq_hosur_road.pdf"
ROWS6 = [[str(i), f"Item {i}", "nos", str(i * 10), "100", f"{i * 1000:,}"] for i in range(1, 7)]


def read(path):
    res = boq.extract_boq(str(path))
    return res, boq.validate(res["items"])


def items(res):
    return [i for i in res["items"] if i["row_kind"] == "item"]


def upload(client, officer, path):
    r = client.post("/api/gov/documents", headers=officer, files=[("files", (Path(path).name, Path(path).read_bytes(), "application/pdf"))])
    assert r.status_code == 200, r.text
    return r.json()["results"][0]


# ----------------------------------------------------------------------------- parsing
def test_strict_numbers_never_glue_or_guess():
    assert boq.number("1,25,000.50") == 125000.5 and boq.number("Rs. 4,500") == 4500 and boq.number("(500)") == -500
    for bad in ("12,000 12,000", "1.2.3", "LS", "NIL", "-", "", "10 nos"):
        assert boq.number(bad) is None, bad


def test_header_synonyms_are_recognised():
    h = boq.header_map(["S.No", "Particulars", "UOM", "Quantity", "Unit Rate", "Total Amount"])
    assert list(h.values()) == ["item_no", "description", "unit", "quantity", "unit_rate", "total"]
    assert boq.header_map(["Name", "Date", "Place"]) is None


# ----------------------------------------------------------------------------- reading tables
def test_sample_boq_items_page_and_total(tmp_path):
    res, s = read(SAMPLE)
    its = items(res)
    assert [(i["item_no"], i["description"], i["unit"], i["quantity"], i["unit_rate"], i["total_amount"]) for i in its] == [
        ("1", "Bituminous concrete 40mm", "sqm", 12000, 1450, 17400000), ("2", "Dense bituminous macadam 50mm", "sqm", 12000, 1900, 22800000),
        ("3", "Tack coat", "sqm", 24000, 38, 912000), ("4", "Road marking", "m", 3500, 95, 332500)]
    assert all(i["page"] == 1 and i["check_status"] == "ok" for i in its)
    assert s["sum_of_item_amounts"] == 41444500 == s["printed_total"] and s["mismatches"] == 0
    assert [i for i in res["items"] if i["row_kind"] == "total"][0]["check_status"] == "ok"


def test_table_continued_over_pages_without_repeating_the_header(tmp_path):
    p = make_boq_pdf(tmp_path / "multi.pdf", [[ROWS6[0], ROWS6[1]], [ROWS6[2], ROWS6[3]], [ROWS6[4], ROWS6[5], ["", "Grand Total", "", "", "", "21,000"]]],
                     repeat_header=False)
    res, s = read(p)
    assert [(i["item_no"], i["page"]) for i in items(res)] == [("1", 1), ("2", 1), ("3", 2), ("4", 2), ("5", 3), ("6", 3)]
    assert s["mismatches"] == 0 and s["printed_total"] == 21000 == s["sum_of_item_amounts"]
    assert any("continued" in (i["method"] or "") for i in res["items"] if i["page"] > 1) or all(i["check_status"] == "ok" for i in items(res))


def test_two_blocks_side_by_side_are_both_read(tmp_path):
    p = make_boq_pdf(tmp_path / "two.pdf", [ROWS6], widths=(22, 100, 25, 38, 38, 50), two_blocks=True, font=7)
    res, s = read(p)
    assert [i["item_no"] for i in items(res)] == ["1", "2", "3", "4", "5", "6"] and s["mismatches"] == 0 and s["incomplete"] == 0


def test_table_without_ruling_lines(tmp_path):
    res, s = read(make_boq_pdf(tmp_path / "plain.pdf", [ROWS6], ruled=False))
    assert len(items(res)) == 6 and s["mismatches"] == 0
    res, s = read(make_boq_pdf(tmp_path / "plain2.pdf", [ROWS6, ROWS6[:4]], widths=(22, 100, 25, 38, 38, 50), two_blocks=True, font=7, ruled=False))
    assert len(items(res)) == 10, "two blocks per page, no lines, two pages"


def test_wrapped_description_is_joined_to_its_item(tmp_path):
    p = make_boq_pdf(tmp_path / "wrap.pdf", [[["1", "Providing and laying", "sqm", "10", "100", "1,000"], ["", "wet mix macadam in layers", "", "", "", ""],
                                              ["2", "Kerb stone", "m", "50", "200", "10,000"]]])
    res, _ = read(p)
    assert [i["description"] for i in items(res)] == ["Providing and laying wet mix macadam in layers", "Kerb stone"]


def test_section_headings_and_subtotals(tmp_path):
    p = make_boq_pdf(tmp_path / "sub.pdf", [[["A", "EARTHWORK", "", "", "", ""], ["1", "Excavation", "cum", "10", "100", "1,000"], ["2", "Filling", "cum", "5", "200", "1,000"],
                                             ["", "Sub total", "", "", "", "2,000"], ["B", "PAVEMENT", "", "", "", ""], ["3", "Surfacing", "sqm", "4", "500", "2,000"],
                                             ["", "Sub total", "", "", "", "2,000"], ["", "Grand Total", "", "", "", "4,000"]]])
    res, s = read(p)
    assert [i["item_no"] for i in items(res)] == ["1", "2", "3"], "section headings are not items"
    assert [i["row_kind"] for i in res["items"] if i["row_kind"] != "item"] == ["subtotal", "subtotal", "total"]
    assert s["mismatches"] == 0 and s["printed_total"] == 4000


# ----------------------------------------------------------------------------- validation
def test_amount_mismatch_is_flagged_not_corrected(tmp_path):
    p = make_boq_pdf(tmp_path / "bad.pdf", [[["1", "Kerb stone", "m", "50", "200", "11,000"], ["2", "Tack coat", "sqm", "10", "20", "200"], ["", "Grand Total", "", "", "", "11,200"]]])
    res, s = read(p)
    a, b = items(res)
    assert a["check_status"] == "mismatch" and a["total_amount"] == 11000 and a["calc_total"] == 10000
    assert "11,000.00" in a["check_note"] and "10,000.00" in a["check_note"] and "1,000.00" in a["check_note"]
    assert b["check_status"] == "ok" and s["mismatches"] == 1 and s["items_with_issues"] == 1


def test_rounding_to_the_rupee_is_not_a_mismatch():
    its = [{"row_kind": "item", "quantity": 3.333, "unit_rate": 100.0, "total_amount": 333.0}]
    boq.validate(its)
    assert its[0]["check_status"] == "ok"


def test_total_that_disagrees_with_the_items_is_flagged(tmp_path):
    p = make_boq_pdf(tmp_path / "tot.pdf", [[["1", "Kerb stone", "m", "50", "200", "10,000"], ["", "Grand Total", "", "", "", "12,000"]]])
    res, s = read(p)
    t = [i for i in res["items"] if i["row_kind"] == "total"][0]
    assert t["check_status"] == "mismatch" and "10,000.00" in t["check_note"] and s["total_mismatches"] == 1


def test_unreadable_numbers_make_the_item_incomplete(tmp_path):
    p = make_boq_pdf(tmp_path / "inc.pdf", [[["1", "Lump sum item", "LS", "1", "LS", "5,000"], ["2", "Kerb stone", "m", "5", "200", "1,000"]]])
    res, s = read(p)
    a, b = items(res)
    assert a["check_status"] == "incomplete" and "rate" in a["check_note"] and a["total_amount"] == 5000
    assert b["check_status"] == "ok" and s["incomplete"] == 1


def test_scanned_pdf_without_text_is_reported_not_guessed(tmp_path):
    d = fitz.open(); d.new_page(); d.save(str(tmp_path / "scan.pdf"))
    res, _ = read(tmp_path / "scan.pdf")
    assert res["items"] == [] and "no text layer" in res["warnings"][0]


# ----------------------------------------------------------------------------- stored items + API
def test_upload_stores_items_with_document_and_page(client, seeded, officer, tmp_path):
    p = make_boq_pdf(tmp_path / "up.pdf", [[["1", "Earthwork excavation", "cum", "100", "250", "25,000"], ["2", "PCC 1:4:8", "cum", "40", "5,500", "2,20,000"]],
                                           [["3", "Brick masonry", "cum", "10", "6,000", "59,000"], ["", "Grand Total", "", "", "", "3,04,000"]]],
                     repeat_header=False, title="Bill of Quantities - Upload Road")
    r = upload(client, officer, p)
    assert r["doc_type"] == "boq" and r["boq"]["items"] == 3 and r["boq"]["mismatches"] == 1
    db = SessionLocal()
    rows = db.query(m.BoqItem).filter_by(document_id=r["document_id"]).order_by(m.BoqItem.page, m.BoqItem.row_index).all()
    assert [(b.row_kind, b.page, b.item_no) for b in rows] == [("item", 1, "1"), ("item", 1, "2"), ("item", 2, "3"), ("total", 2, None)]
    assert rows[2].check_status == "mismatch" and rows[2].calc_total == 60000 and rows[2].total_amount == 59000
    assert rows[3].check_status == "ok", "the printed total 3,04,000 is the sum of the printed amounts, even though one printed amount is wrong"
    d = client.get(f"/api/gov/documents/{r['document_id']}", headers=officer).json()
    assert d["boq"]["items"] == 3
    g = client.get(f"/api/gov/documents/{r['document_id']}/boq", headers=officer).json()
    assert [i["page"] for i in g["items"]] == [1, 1, 2, 2] and g["summary"]["mismatches"] == 1


def test_officer_correction_recomputes_the_checks_and_protects_reviewed_rows(client, officer, tmp_path):
    db = SessionLocal()
    doc = db.query(m.Document).filter(m.Document.filename == "up.pdf").one()
    bad = db.query(m.BoqItem).filter_by(document_id=doc.id, item_no="3").one()
    r = client.patch(f"/api/gov/boq-items/{bad.id}", headers=officer, data={"status": "corrected", "total_amount": "60000"})
    assert r.status_code == 200, r.text
    it = r.json()["item"]
    assert it["status"] == "corrected" and it["check_status"] == "ok" and it["total_amount"] == 60000
    g = client.get(f"/api/gov/documents/{doc.id}/boq", headers=officer).json()
    assert g["summary"]["mismatches"] == 1, "the printed grand total (3,04,000) still disagrees with the corrected items (3,05,000)"
    assert client.patch(f"/api/gov/boq-items/{bad.id}", headers=officer, data={"status": "corrected"}).status_code == 422
    assert client.patch(f"/api/gov/boq-items/{bad.id}", headers=officer, data={"status": "corrected", "quantity": "-1"}).status_code == 422
    assert client.patch(f"/api/gov/boq-items/999999", headers=officer, data={"status": "verified"}).status_code == 422
    # re-reading must not silently throw away the correction
    assert client.post(f"/api/gov/documents/{doc.id}/boq/extract", headers=officer).status_code == 409
    ok = client.post(f"/api/gov/documents/{doc.id}/boq/extract", headers=officer, data={"replace": "true"})
    assert ok.status_code == 200 and ok.json()["items"] == 3
    db.expire_all()
    assert db.query(m.BoqItem).filter_by(document_id=doc.id, status="corrected").count() == 0


def test_boq_endpoints_need_an_officer(client, seeded):
    assert client.get("/api/gov/documents/1/boq").status_code in (401, 403)
    assert client.patch("/api/gov/boq-items/1", data={"status": "verified"}).status_code in (401, 403)
