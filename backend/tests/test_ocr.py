"""Requirement 7: Tesseract language setup (eng / kan / eng+kan), preprocessing, deskew, resolution, page-level confidence, review flags.

IMPORTANT LIMIT: the Kannada pages used here are rendered by tests/scanmaker.py (Lohit Kannada font) and then degraded. They show that the
pipeline is wired correctly and behaves sensibly. They are NOT scans of real government documents, and nothing here validates accuracy on them.
"""
import shutil
import fitz
import pytest
from app import ocr
from app.extract import process_document
from app.textnorm import parse_amount, parse_date
from pdfmaker import make_pdf
import scanmaker as sm

TESS = bool(shutil.which("tesseract")) and {"eng", "kan"} <= set(ocr.installed_languages())
needs_kn = pytest.mark.skipif(not (TESS and sm.can_render_kannada()), reason="needs tesseract with eng+kan data and a Kannada font (apt install tesseract-ocr-kan fonts-lohit-knda)")

LINES = ["ಬೃಹತ್ ಬೆಂಗಳೂರು ಮಹಾನಗರ ಪಾಲಿಕೆ", "ಕಾಮಗಾರಿಯ ಹೆಸರು: ರಸ್ತೆ ಡಾಂಬರೀಕರಣ ಕಾಮಗಾರಿ", "Work Order No: BBMP/WO/2026/777", "BBMP Job No: BBMP/2026/1234",
         "Contractor: M/s Ram Constructions", "ಮೊತ್ತ: 4,50,000  ದಿನಾಂಕ: 12/03/2026"]


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    d = tmp_path_factory.mktemp("scans")
    base = sm.render_lines(LINES)
    out = {}
    for name, kw, dpi in (("clean", {}, 200), ("skew", dict(skew=4), 200), ("poor", dict(skew=-3, noise=0.01, shade=0.5, scale=0.4), 80)):
        out[name] = str(sm.scan_pdf(d / f"{name}.pdf", [sm.degrade(base, **kw) if kw else base], dpi=dpi))
    return out


# ----------------------------------------------------------------------------- language configuration (no Tesseract needed)
def test_language_plan_for_every_setting():
    both, eng_only = {"eng", "kan", "osd"}, {"eng"}
    assert ocr.plan("auto", both) == (["eng", "eng+kan"], [])
    assert ocr.plan("eng", both) == (["eng"], [])
    assert ocr.plan("kan", both) == (["kan"], [])
    assert ocr.plan("eng+kan", both) == (["eng+kan"], [])
    assert ocr.plan("ENG + KAN", both)[0] == ["eng+kan"], "spelling and spaces do not matter"


def test_missing_kannada_data_is_a_warning_not_a_silent_downgrade():
    tries, warns = ocr.plan("eng+kan", {"eng"})
    assert tries == ["eng"] and "not installed" in warns[0] and "Kannada" in warns[0]
    tries, warns = ocr.plan("kan", {"eng"})
    assert tries == ["eng"] and warns
    tries, warns = ocr.plan("auto", {"eng"})
    assert tries == ["eng"] and "not installed" in warns[0]
    assert ocr.plan("auto", set()) == ([], ["Tesseract has no usable language data installed"])


def test_environment_setting_and_unknown_values(monkeypatch):
    for raw, want in (("eng", "eng"), ("kan", "kan"), ("eng+kan", "eng+kan"), ("auto", "auto"), ("", "auto"), ("klingon", "auto")):
        monkeypatch.setenv("OCR_LANG", raw)
        assert ocr.configured_lang() == want
    monkeypatch.delenv("OCR_LANG")
    assert ocr.configured_lang() == "auto"


def test_scan_resolution_rules(monkeypatch):
    monkeypatch.delenv("OCR_DPI", raising=False)
    assert [ocr.target_dpi(n) for n in (None, 72, 150, 300, 350, 600)] == [300, 300, 300, 300, 350, 400]
    monkeypatch.setenv("OCR_DPI", "450")
    assert ocr.target_dpi(150) == 450


def test_legacy_kannada_font_text_layer_is_recognised():
    legacy = "ÀÄ¸ï ¥ÀæªÀiÁt ¥ÀvÀæ PÁªÀÄUÁj ªÉÆvÀÄÛ ÀªÀÄ§Âj PÁAiÀiÁð¢ñ ªÉÆvÀÛ ¥ÁªÀw ¢£ÁAPÀ"
    assert ocr.looks_like_legacy_kannada_font(legacy)
    assert not ocr.looks_like_legacy_kannada_font("Work Order No: BBMP/WO/2026/777 issued to M/s Ram Constructions for asphalting of the road.")
    assert not ocr.looks_like_legacy_kannada_font("ಬೃಹತ್ ಬೆಂಗಳೂರು ಮಹಾನಗರ ಪಾಲಿಕೆ ಕಾಮಗಾರಿಯ ಹೆಸರು ರಸ್ತೆ ಡಾಂಬರೀಕರಣ")
    assert not ocr.looks_like_legacy_kannada_font("café résumé")  # short text with accents is not a font problem


def test_kannada_numerals_are_read_as_numbers():
    assert parse_amount("₹ ೪,೫೦,೦೦೦") == 450000
    d = parse_date("೧೨/೦೩/೨೦೨೬")
    assert (d.year, d.month, d.day) == (2026, 3, 12)


# ----------------------------------------------------------------------------- reading scanned Kannada + English pages
@needs_kn
def test_mixed_page_is_read_with_both_languages_and_identifiers_survive(pages):
    r = process_document(pages["clean"])
    pi = r["page_info"][0]
    assert pi["method"] == "ocr" and pi["ocr_lang"] == "eng+kan" and pi["kannada_chars"] > 40
    assert pi["confidence"] >= 70 and pi["needs_review"] is False and pi["dpi"] >= 300
    ids = {f["field"]: f["norm_value"] for f in r["fields"]}
    assert ids.get("job_number") == "BBMP/2026/1234" and r["ocr_pages"] == 1
    assert r["ocr_summary"]["languages_used"] == ["eng+kan"] and r["ocr_summary"]["pages_flagged"] == []


@needs_kn
def test_forced_languages_behave_as_configured(pages, monkeypatch):
    fitz_page = fitz.open(pages["clean"])[0]
    monkeypatch.setenv("OCR_LANG", "eng")
    e = ocr.ocr_page(fitz_page)
    assert e["lang"] == "eng" and e["kannada_chars"] == 0 and e["needs_review"], "English-only reading of a Kannada page is poor, and flagged"
    monkeypatch.setenv("OCR_LANG", "kan")
    k = ocr.ocr_page(fitz_page)
    assert k["lang"] == "kan" and k["kannada_chars"] > 40
    monkeypatch.setenv("OCR_LANG", "eng+kan")
    b = ocr.ocr_page(fitz_page)
    assert b["lang"] == "eng+kan" and "BBMP/2026/1234" in b["text"] and b["kannada_chars"] > 40


@needs_kn
def test_kannada_requested_but_not_installed_flags_the_page(pages, monkeypatch):
    monkeypatch.setattr(ocr, "installed_languages", lambda: frozenset({"eng"}))
    monkeypatch.setenv("OCR_LANG", "eng+kan")
    r = ocr.ocr_page(fitz.open(pages["clean"])[0])
    assert r["lang"] == "eng" and r["needs_review"] and "not installed" in r["review_reason"]


@needs_kn
def test_tilted_scan_is_straightened_and_still_read(pages):
    pg = fitz.open(pages["skew"])[0]
    img = ocr.render(pg, 300)
    assert abs(ocr.estimate_skew(img) - (-4.0)) <= 1.0, "a page tilted by +4 degrees is measured as about -4 (the correction)"
    r = ocr.ocr_page(pg)
    assert abs(r["deskew_deg"] - (-4.0)) <= 1.0 and "deskew" in r["preprocess"]
    assert r["kannada_chars"] > 40 and "BBMP/2026/1234" in r["text"]


@needs_kn
def test_low_resolution_scan_is_rendered_larger_and_a_poor_page_is_flagged(pages):
    r = ocr.ocr_page(fitz.open(pages["poor"])[0])
    assert r["native_dpi"] and r["native_dpi"] < 150 and r["dpi"] == 300
    assert r["needs_review"] is True and "low scan resolution" in r["review_reason"]
    assert r["confidence"] is not None


@needs_kn
def test_extracted_values_from_a_weaker_page_carry_less_confidence(pages, tmp_path):
    clean = process_document(pages["clean"]); poor = process_document(pages["poor"])
    c = {f["field"]: f["confidence"] for f in clean["fields"] if f["field"] in ("job_number",)}
    p = {f["field"]: f["confidence"] for f in poor["fields"] if f["field"] in ("job_number",)}
    if c and p:
        assert p["job_number"] <= c["job_number"]
    assert poor["ocr_summary"]["pages_flagged"] == [1]


def test_text_layer_pages_are_not_ocr_pages(tmp_path):
    r = process_document(str(make_pdf(tmp_path / "plain.pdf", "WORK ORDER", ["BBMP Job No: TL/J/1", "Work Order No: TL/WO/1"])))
    assert r["ocr_pages"] == 0 and r["page_info"][0]["method"] == "text_layer" and r["page_info"][0]["needs_review"] is False
    assert r["ocr_summary"]["pages_ocr"] == 0 and r["ocr_summary"]["mean_confidence"] is None


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
def test_legacy_font_text_layer_is_sent_to_ocr(tmp_path):
    junk = ["ÀÄ¸ï ¥ÀæªÀiÁt ¥ÀvÀæ PÁªÀÄUÁj ªÉÆvÀÄÛ ÀªÀÄ§Âj PÁAiÀiÁð¢ñ ªÉÆvÀÛ ¥ÁªÀw ¢£ÁAPÀ", "ÀÄ¸ï ¥ÀæªÀiÁt ¥ÀvÀæ PÁªÀÄUÁj ªÉÆvÀÄÛ ÀªÀÄ§Âj PÁAiÀiÁð¢ñ ªÉÆvÀÛ ¥ÁªÀw"]
    r = process_document(str(make_pdf(tmp_path / "legacy.pdf", "ÀÄ¸ï ¥ÀæªÀiÁt", junk)))
    pi = r["page_info"][0]
    assert pi["method"] == "ocr" and pi["needs_review"] and "legacy Kannada font" in pi["review_reason"]


# ----------------------------------------------------------------------------- stored per page, API, manual correction
@needs_kn
def test_upload_stores_page_confidence_and_review_flags_and_officer_can_clear_them(client, officer, pages):
    from pathlib import Path
    from app import models as m
    from app.db import SessionLocal
    for name in ("clean", "poor"):
        r = client.post("/api/gov/documents", headers=officer, files=[("files", (f"{name}_kn.pdf", Path(pages[name]).read_bytes(), "application/pdf"))]).json()["results"][0]
        assert "error" not in r, r
        db = SessionLocal()
        pg = db.query(m.DocumentPage).filter_by(document_id=r["document_id"]).one()
        assert (pg.method, pg.ocr_lang) == ("ocr", "eng+kan") and pg.ocr_confidence and pg.dpi and pg.kannada_chars > 0
        d = client.get(f"/api/gov/documents/{r['document_id']}", headers=officer).json()
        assert d["ocr"]["pages"][0]["confidence"] == pg.ocr_confidence and d["ocr"]["summary"]["pages_ocr"] == 1
        if name == "poor":
            assert pg.needs_review and d["ocr"]["summary"]["pages_flagged"] == [1]
            row = [x for x in client.get("/api/gov/documents?q=poor_kn", headers=officer).json()["items"]][0]
            assert row["ocr_flagged_pages"] == 1
            ok = client.patch(f"/api/gov/documents/{r['document_id']}/pages/1", headers=officer, data={"note": "compared with the original"})
            assert ok.status_code == 200
            db.expire_all()
            pg = db.query(m.DocumentPage).filter_by(document_id=r["document_id"]).one()
            assert pg.needs_review is False and "checked by an officer" in pg.review_reason
            assert client.patch(f"/api/gov/documents/{r['document_id']}/pages/9", headers=officer, data={}).status_code == 404
        else:
            assert not pg.needs_review


def test_ocr_status_endpoint(client, officer):
    s = client.get("/api/gov/ocr", headers=officer).json()
    assert s["configured_language"] in ocr.LANGS and "min_confidence" in s and isinstance(s["installed"], list)
    assert client.get("/api/gov/ocr").status_code in (401, 403)


def test_ocr_border_noise_does_not_hide_labels():
    from app.extract import _strip_ocr_border_noise, _lines, extract_labelled
    text = "WORK ORDER\n_Tender ID | SAMPLE/KPPP/2425/T003\n| Ward No | 181\n"
    clean = _strip_ocr_border_noise(text)
    assert "_Tender" not in clean and "| Ward No" in clean  # '|' separators are kept
    got = {r["field"]: r["norm_value"] for r in extract_labelled(_lines(clean), 1, "ocr_regex")}
    raw = {r["field"] for r in extract_labelled(_lines(text), 1, "ocr_regex")}
    assert "tender_id" not in raw  # without the clean-up the label is missed
    assert got.get("tender_id") == "SAMPLE/KPPP/2425/T003"
