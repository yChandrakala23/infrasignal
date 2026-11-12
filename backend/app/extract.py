import re, hashlib
import fitz  # PyMuPDF

PATTERNS = {
    "job_number": (r"\bJob\s*(?:No|Number|Code)\.?\s*[:.]?\s*([A-Z0-9/_\-]{5,})", 0.9),
    "work_order_id": (r"\bW\.?\s?O\.?\s*(?:No|Number)\.?\s*[:.]?\s*([A-Z0-9/_\-]{4,})", 0.85),
    "tender_id": (r"\b(?:Tender|NIT|Indent)\s*(?:ID|No|Number)\.?\s*[:.]?\s*([A-Z0-9/_\-]{4,})", 0.85),
    "amount": (r"(?:Rs\.?|INR|\u20b9)\s*([\d,]+(?:\.\d{1,2})?)", 0.6),
    "date": (r"\b(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})\b", 0.5),
    "contractor": (r"(?:Contractor|M/s\.?)\s*[:.]?\s*([A-Z][A-Za-z&.,' ]{3,60})", 0.6),
}


def page_texts(path: str, ocr: bool = True):
    """Yield (page_number, text, method). Uses OCR only when a page has no text layer."""
    doc = fitz.open(path)
    for i, page in enumerate(doc, start=1):
        text = page.get_text()
        method = "text_regex"
        if len(text.strip()) < 30 and ocr:
            try:
                import pytesseract
                from PIL import Image
                pix = page.get_pixmap(dpi=250)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                text = pytesseract.image_to_string(img, lang="eng")
                method = "ocr_regex"
            except Exception:
                method = "ocr_unavailable"
        yield i, text, method


def extract_fields(text: str, page: int, method: str):
    out = []
    factor = 0.8 if method == "ocr_regex" else 1.0
    for field, (pat, conf) in PATTERNS.items():
        for mt in re.finditer(pat, text, re.I):
            raw = mt.group(1).strip()
            norm = raw.replace(",", "") if field == "amount" else raw.upper() if field.endswith(("_id", "_number")) else raw
            out.append({"field": field, "raw_value": raw, "norm_value": norm, "page": page,
                        "confidence": round(conf * factor, 2), "method": method})
    return out


def process_pdf(path: str):
    with open(path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    results, pages = [], 0
    for p, text, method in page_texts(path):
        pages = p
        results.extend(extract_fields(text, p, method))
    return sha, pages, results
