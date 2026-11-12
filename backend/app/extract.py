"""Structured extraction from municipal PDFs / scans.

Pipeline: page text (text layer, else OCR) -> classify document -> label-driven field extraction
(plus table key/value rows and a few unlabelled identifier patterns) -> optional LLM pass whose
answers are only kept if the value literally appears on the cited page.

Every value keeps: page number, a text snippet, and (when it is on a text layer) a bounding box.
Nothing is invented: a field that is not found is simply absent.
"""
import re, hashlib, os
import fitz  # PyMuPDF
from .classify import classify_text
from .textnorm import parse_amount, parse_date, clean_id, norm_text

# ----------------------------------------------------------------------------- label table
_ID = r"[A-Za-z0-9][A-Za-z0-9/_\-.]{2,}"
# (field, kind, base_confidence, label regex, separator_required)
LABELS = [
    ("tender_id", "id", 0.9, r"(?:e-?\s*procurement\s*)?(?:tender|nit|indent|bid)\s*(?:id|no\.?|number|ref(?:erence)?(?:\s*no\.?)?|notification\s*no\.?)", False),
    ("job_number", "id", 0.92, r"(?:bbmp\s*)?job\s*(?:no\.?|number|code|id)", False),
    ("work_order_id", "id", 0.9, r"(?:work\s*order\s*(?:no\.?|number|id)|w\.?\s?o\.?\s*(?:no\.?|number))", False),
    ("work_id", "id", 0.9, r"(?:work\s*id|work\s*code|contract\s*(?:no\.?|number|id|ref(?:erence)?)|agreement\s*(?:no\.?|number))", False),
    ("work_description", "text", 0.85, r"(?:name\s*of\s*(?:the\s*)?work|work\s*description|description\s*of\s*(?:the\s*)?work|work\s*name|particulars\s*of\s*work)", False),
    ("work_description", "text", 0.7, r"(?:subject|sub)", True),
    ("road_name", "text", 0.85, r"(?:road\s*name|name\s*of\s*(?:the\s*)?road|name\s*of\s*(?:the\s*)?street)", False),
    ("location", "text", 0.8, r"(?:location|site|place\s*of\s*work|stretch|locality|area)", True),
    ("ward", "ward", 0.88, r"ward(?:\s*(?:no\.?|number|name))?", True),
    ("department", "text", 0.85, r"(?:department|dept\.?|division|executing\s*agency|implementing\s*agency|issuing\s*office)", True),
    ("contractor", "text", 0.88, r"(?:name\s*of\s*(?:the\s*)?(?:contractor|agency|bidder|firm)|contractor(?:'s)?(?:\s*name)?|successful\s*bidder|awarded\s*to|issued\s*to|payee(?:\s*name)?|agency)", True),
    ("estimated_cost", "amount", 0.88, r"(?:estimated\s*(?:project\s*)?(?:cost|value|amount)|estimate\s*(?:cost|amount|value)|sanctioned\s*(?:amount|estimate)|tender\s*value|approximate\s*cost|e\.?c\.?v\.?|approved\s*estimate)", False),
    ("awarded_amount", "amount", 0.9, r"(?:awarded\s*(?:contract\s*)?(?:amount|value|cost)|contract\s*(?:amount|value|price|sum)|accepted\s*(?:tender\s*)?(?:amount|value)|work\s*order\s*(?:amount|value)|agreement\s*(?:amount|value)|tendered\s*amount|accepted\s*bid\s*(?:amount|value))", False),
    ("payment_amount", "amount", 0.9, r"(?:amount\s*paid|payment\s*amount|paid\s*amount|net\s*(?:amount\s*payable|amount|payable)(?:\s*paid)?|amount\s*released|amount\s*of\s*payment|amount\s*sanctioned\s*for\s*payment)", False),
    ("work_order_date", "date", 0.9, r"(?:work\s*order\s*date|date\s*of\s*(?:the\s*)?work\s*order|w\.?o\.?\s*date)", False),
    ("commencement_date", "date", 0.9, r"(?:date\s*of\s*commencement|commencement\s*date|(?:stipulated\s*)?(?:date\s*of\s*)?start(?:\s*date)?|date\s*of\s*start|to\s*commence\s*(?:the\s*work\s*)?on)", False),
    ("completion_deadline", "date", 0.9, r"(?:(?:stipulated\s*)?date\s*of\s*completion|completion\s*(?:date|deadline)|due\s*date|(?:to\s*be\s*)?completed?\s*(?:on\s*or\s*before|by)|scheduled\s*completion)", False),
    ("payment_date", "date", 0.88, r"(?:payment\s*date|date\s*of\s*payment|paid\s*on)", False),
    ("document_date", "date", 0.85, r"(?:date|dated|dt\.?)", True),
]
_COMPILED = [(f, k, c, re.compile(r"^\s*(?:\d{1,2}[.)]\s*)?(?P<label>" + lab + r")(?![A-Za-z])\s*(?P<sep>[:\-–=.]?)\s*(?P<val>.*)$", re.I), strict)
             for f, k, c, lab, strict in LABELS]
_ANY_LABEL_LINE = re.compile(r"^\s*(?:\d{1,2}[.)]\s*)?[A-Za-z][A-Za-z .()/&'\-]{1,40}\s*:\s*\S")
CONTINUABLE = {"work_description", "location", "contractor", "department"}
FUNCTION_WORDS = {"is", "are", "shall", "has", "have", "will", "must", "to", "the", "and", "may", "should", "can", "of",
                  "was", "were", "be", "for", "in", "on", "at", "by", "with", "that", "this", "as", "or", "not", "no"}
NO_SEP_OK = {"ward", "department", "contractor", "document_date"}  # these may appear as 'Ward No 181' without a colon


def _no_sep_value_ok(field, val):
    val = val.strip()
    if field == "ward":
        return bool(re.match(r"\d{1,3}\b", val))
    if field == "document_date":
        return parse_date(val) is not None
    first = val.split()[0].lower().strip(".,;:") if val.split() else ""
    return field in NO_SEP_OK and first not in FUNCTION_WORDS and len(val.split()) <= 12


def _lines(text):
    out = []
    for l in text.splitlines():
        l = re.sub(r"^[\s|\"'`~*\u2022]+", "", l)          # OCR table borders / bullets at line start
        l = re.sub(r"\s+\|\s+", ": ", l, count=1)              # 'Label | value' -> 'Label: value'
        l = re.sub(r"\s+(?:oe|ee|\||_)+\s*$", "", l)          # OCR noise at line end
        out.append(re.sub(r"[ \t]+", " ", l).rstrip())
    return out


def _looks_like_label(line):
    if _ANY_LABEL_LINE.match(line):
        return True
    for f, k, c, rx, strict in _COMPILED:
        mm = rx.match(line)
        if mm and (mm.group("sep") or not strict or not mm.group("val").strip() or _no_sep_value_ok(f, mm.group("val"))):
            return True
    return False


def _clean_value(kind, field, val):
    """-> (raw, norm) or None if the value is not valid for the field kind."""
    val = val.strip().strip("|").strip()
    if not val:
        return None
    if kind == "id":
        val = re.sub(r"^(\S*[/\-][A-Za-z]):?\s+(\d{2,}\S*)", r"\1\2", val)  # OCR split 'T 003' -> 'T003'
        tok = re.search(_ID, val)
        if not tok or not re.search(r"\d", tok.group(0)):
            return None
        return tok.group(0), clean_id(tok.group(0))
    if kind == "amount":
        a = parse_amount(val)
        if a is None or a <= 0:
            return None
        return val[:80], f"{a:.2f}".rstrip("0").rstrip(".")
    if kind == "date":
        d = parse_date(val)
        return (val[:60], d.date().isoformat()) if d else None
    if kind == "ward":
        n = re.search(r"\b(\d{1,3})\b", val)
        if n:
            return val[:100], n.group(1)
        return (val[:100], norm_text(val)) if len(val) > 2 else None
    v = re.sub(r"\s+", " ", val)
    if len(v) < 3:
        return None
    norm = v[:500]
    if field == "work_description":  # 'Sub: Administrative approval for <work>' -> '<work>'
        norm = re.sub(r"^(?:administrative\s+(?:approval|sanction)|technical\s+sanction|work\s+order|letter\s+of\s+acceptance|"
                      r"tender|completion\s+certificate|payment|running\s+(?:account\s+)?bill)\s+(?:for|to|of|regarding)\s+", "", norm, flags=re.I)
    if field == "contractor":
        norm = re.sub(r"^(?:m\s*/\s*s\.?|messrs\.?)\s*", "", norm, flags=re.I)
    return v[:500], norm


def extract_labelled(lines, page, method, doc_type=None):
    """Walk lines, find 'Label: value' (value may continue on the next line)."""
    out = []
    factor = 0.85 if method == "ocr_regex" else 1.0
    n = len(lines)
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        for field, kind, conf, rx, strict in _COMPILED:
            mm = rx.match(line)
            if not mm:
                continue
            sep, val = mm.group("sep"), mm.group("val")
            if strict and not sep and val.strip() and not _no_sep_value_ok(field, val):
                continue  # 'Ward development works ...' is not a ward label
            snippet_lines = [line]
            if not val.strip():  # value on the following line(s)
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and not _looks_like_label(lines[j]):
                    val = lines[j]; snippet_lines.append(lines[j]); i_end = j
                else:
                    continue
            else:
                i_end = i
            if field in CONTINUABLE:
                k = i_end + 1
                extra = 0
                while k < n and lines[k].strip() and not _looks_like_label(lines[k]) and extra < 3 and len(val) < 350:
                    val += " " + lines[k].strip(); snippet_lines.append(lines[k]); k += 1; extra += 1
            cv = _clean_value(kind, field, val)
            if not cv:
                continue
            out.append({"field": field, "raw_value": cv[0], "norm_value": cv[1], "page": page,
                        "confidence": round(conf * factor, 2), "method": method,
                        "snippet": " / ".join(s.strip() for s in snippet_lines)[:300]})
            break  # one label per line
    return out


# ----------------------------------------------------------------------------- unlabelled patterns
LEGACY_PATTERNS = {
    "job_number": (r"\bJob\s*(?:No|Number|Code)\b\.?\s*[:.]?\s*([A-Z0-9/_\-]{5,})", 0.9),
    "work_order_id": (r"\bW\.?\s?O\.?\s*(?:No|Number)\b\.?\s*[:.]?\s*([A-Z0-9/_\-]{4,})", 0.85),
    "tender_id": (r"\b(?:Tender|NIT|Indent)\s*(?:ID|No|Number)\b\.?\s*[:.]?\s*([A-Z0-9/_\-]{4,})", 0.85),
    "amount": (r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)", 0.4),
    "date": (r"\b(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})\b", 0.4),
    "contractor": (r"(?:Contractor|M/s\.?)\s*[:.]?\s*([A-Z][A-Za-z&.,' ]{3,60})", 0.6),
}


def extract_fields(text: str, page: int, method: str):
    """Original generic regex layer, kept for compatibility. Produces low-confidence 'mentions'."""
    out = []
    factor = 0.8 if method == "ocr_regex" else 1.0
    for field, (pat, conf) in LEGACY_PATTERNS.items():
        for mt in re.finditer(pat, text, re.I):
            raw = mt.group(1).strip()
            norm = raw.replace(",", "") if field == "amount" else raw.upper() if field.endswith(("_id", "_number")) else raw
            out.append({"field": field, "raw_value": raw, "norm_value": norm, "page": page,
                        "confidence": round(conf * factor, 2), "method": method,
                        "snippet": text[max(0, mt.start() - 40): mt.end() + 40].replace("\n", " ")})
    return out


def extract_contractor_ms(text, page, method):
    from .narrative import MS
    out = []
    for mm in MS.finditer(text.replace("\n", " ")):
        v = re.sub(r"\s+", " ", mm.group(1)).strip(" .,")
        out.append({"field": "contractor", "raw_value": "M/s " + v, "norm_value": v, "page": page,
                    "confidence": 0.7 if method == "text_regex" else 0.6, "method": method,
                    "snippet": text[max(0, mm.start() - 30): mm.end() + 30].replace("\n", " ")})
    return out


# ----------------------------------------------------------------------------- pages
def page_texts(path: str, ocr: bool = True):
    """Yield (page_number, text, method, page_object). OCR only when a page has no usable text layer."""
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
        yield i, text, method, page


def _table_lines(page):
    """Key/value rows from ruled tables become 'Label: value' lines."""
    rows = []
    try:
        for t in page.find_tables().tables:
            for r in t.extract():
                cells = [re.sub(r"\s+", " ", (c or "")).strip() for c in r]
                cells = [c for c in cells if c]
                if len(cells) == 2 and len(cells[0]) <= 45:
                    rows.append(f"{cells[0]}: {cells[1]}")
    except Exception:
        pass
    return rows


def _locate(page, raw):
    """Bounding box of a value on a text-layer page, or None."""
    try:
        for probe in (raw[:60], raw.split("  ")[0][:40], raw.split()[0] if raw.split() else ""):
            if probe and len(probe) >= 3:
                rects = page.search_for(probe)
                if rects:
                    r = rects[0]
                    return [round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1)]
    except Exception:
        pass
    return None


def _dedupe(rows):
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: (-r["confidence"], r["page"])):
        key = (r["field"], r["norm_value"].lower() if isinstance(r["norm_value"], str) else r["norm_value"], r["page"])
        if key in seen:
            continue
        seen.add(key); out.append(r)
    return sorted(out, key=lambda r: (r["page"], r["field"]))


def find_road_in_description(rows):
    """Derive a road name from the description when no explicit road label exists (low confidence)."""
    from .textnorm import road_names_in_text
    if any(r["field"] == "road_name" for r in rows):
        return []
    out = []
    for r in rows:
        if r["field"] == "work_description":
            for nm in road_names_in_text(r["raw_value"], limit=1):
                out.append({"field": "road_name", "raw_value": nm, "norm_value": nm, "page": r["page"],
                            "confidence": round(min(0.65, r["confidence"] - 0.2), 2), "method": r["method"] + "_derived",
                            "snippet": r["snippet"]})
    return out


def _narr(text, p, method, found):
    """Sentence-level fallback. Adds a value only when the labelled pass found nothing for that field on this page."""
    from .narrative import extract_narrative
    have = {r["field"] for r in found}
    return [r for r in extract_narrative(text, p, method, classify_text([text])["type"]) if r["field"] not in have]


def process_document(path: str, use_llm: bool = False, llm=None):
    """Full pipeline for one file -> dict(sha256, pages, text_chars, ocr_pages, doc_type, type_confidence,
    classification, fields[list of extraction dicts], llm_used)."""
    with open(path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    texts, pages_meta, ocr_pages = [], [], 0
    rows = []
    for p, text, method, page in page_texts(path):
        texts.append(text)
        ocr_pages += method.startswith("ocr")
        lines = _lines(text)
        found = extract_labelled(lines, p, method)
        if method == "text_regex":
            have = {r["field"] for r in found}
            for r in extract_labelled(_table_lines(page), p, method):
                if r["field"] not in have:  # table cell text can lose spaces, so it is only a fallback
                    r["confidence"] = round(r["confidence"] * 0.95, 2); r["method"] = "table_regex"; found.append(r)
        found += extract_contractor_ms(text, p, method)
        found += _narr(text, p, method, found)
        found += [r for r in extract_fields(text, p, method) if r["field"] in ("job_number", "work_order_id", "tender_id")]
        if method == "text_regex":
            for r in found:
                r["bbox"] = _locate(page, r["raw_value"])
        pages_meta.append((p, text, method, page))
        rows += found
    rows += find_road_in_description(rows)
    # keep legacy generic amount/date mentions only as low-confidence extras
    for p, text, method, page in pages_meta:
        for r in extract_fields(text, p, method):
            if r["field"] in ("amount", "date"):
                r["bbox"] = _locate(page, r["raw_value"]) if method == "text_regex" else None
                rows.append(r)
    cls = classify_text(texts)
    # a work order's own date is its document date unless an explicit work-order date exists
    if cls["type"] == "work_order" and not any(r["field"] == "work_order_date" for r in rows):
        for r in rows:
            if r["field"] == "document_date":
                rows.append({**r, "field": "work_order_date", "confidence": round(r["confidence"] - 0.15, 2),
                             "method": r["method"] + "_derived"})
                break
    if not any(r["field"] == "document_date" for r in rows):
        for p, text, method, page in pages_meta[:1]:
            d = parse_date(text)
            if d:
                mm = re.search(r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}", text)
                raw = mm.group(0) if mm else d.date().isoformat()
                rows.append({"field": "document_date", "raw_value": raw, "norm_value": d.date().isoformat(), "page": p,
                             "confidence": 0.45, "method": method + "_fallback", "snippet": "first date on page 1"})
    llm_used = False
    if use_llm:
        from . import llm_extract
        added, llm_used = llm_extract.augment(rows, [(p, t) for p, t, _, _ in pages_meta], llm)
        rows = added
    rows = _dedupe(rows)
    rows.append({"field": "document_type", "raw_value": cls["type"], "norm_value": cls["type"], "page": 1,
                 "confidence": cls["confidence"], "method": "classifier",
                 "snippet": ", ".join(c["cue"] for c in cls["cues"][:6]) or "no strong cue", "bbox": None})
    for r in rows:
        r.setdefault("bbox", None); r.setdefault("snippet", None)
    return {"sha256": sha, "pages": len(texts), "text_chars": sum(len(t) for t in texts), "ocr_pages": ocr_pages,
            "doc_type": cls["type"], "type_confidence": cls["confidence"], "classification": cls,
            "fields": rows, "llm_used": llm_used}


def process_pdf(path: str):
    """Backward-compatible wrapper: (sha256, pages, [extraction dicts])."""
    r = process_document(path)
    return r["sha256"], r["pages"], [x for x in r["fields"]]
