"""Sentence-level extraction for letters and prose ("...awarded at a contract value of Rs. 84,50,000/- ... complete it on or before 11.12.2025").
Complements the label-driven pass. Everything it returns is lower confidence (0.6 to 0.75) than a labelled value and is only
added; a value is never invented, it must be literally present in the page text."""
import re
from .textnorm import parse_amount, parse_date

ID = r"([A-Za-z0-9][A-Za-z0-9/_\-]*[/\-][A-Za-z0-9/_\-]*\d[A-Za-z0-9/_\-]*|[A-Za-z0-9/_\-]*\d[A-Za-z0-9/_\-]*[/\-][A-Za-z0-9/_\-]+)"
DATE = r"(\d{1,2}(?:st|nd|rd|th)?[./\- ]\s?(?:\d{1,2}|[A-Za-z]{3,9})[./\- ,]*\s?\d{2,4})"
MONEY = r"(?:rs\.?|₹|inr)\s*([\d,]+(?:\.\d+)?(?:\s*(?:crores?|cr\b\.?|lakhs?|lacs?))?)"
MS = re.compile(r"\bM/s\.?\s+((?:[A-Z][\w&'\-]*\.?(?:\s+|(?=[,)]))|\([A-Za-z ]+\)\s*){1,8})")


def _flat(text):
    return re.sub(r"\s+", " ", text)


def _row(field, raw, norm, page, method, text, mt, conf=0.7):
    s = max(0, mt.start() - 40)
    return {"field": field, "raw_value": raw.strip(), "norm_value": norm, "page": page, "confidence": round(conf * (0.85 if method == "ocr_regex" else 1), 2),
            "method": method.replace("regex", "narrative"), "snippet": text[s: mt.end() + 40]}


def extract_narrative(text, page, method, doc_type=None):
    t = _flat(text)
    out = []

    def add(field, rx, group=1, norm=None, flags=re.I, conf=0.7, first_only=True, prefix=None):
        for mt in re.finditer(rx, t, flags):
            raw = mt.group(group).strip(" .,;:")
            n = norm(raw) if norm else raw
            if n in (None, ""):
                continue
            out.append(_row(field, raw, n, page, method, t, mt, conf))
            if first_only:
                break

    iso = lambda s: (parse_date(s).date().isoformat() if parse_date(s) else None)
    add("tender_id", r"(?:tender|nit|e-?procurement)\s*(?:notification\s*)?(?:id|no\.?|number|ref\.?)\s*[:.]?\s*" + ID, norm=lambda s: s.upper())
    add("job_number", r"\bjob\s*(?:no\.?|number|code)\s*[:.]?\s*" + ID, norm=lambda s: s.upper())
    add("work_order_id", r"work\s*order\s*(?:no\.?|number)\s*[:.]?\s*" + ID, norm=lambda s: s.upper())
    if not any(r["field"] == "work_order_id" for r in out):
        mt = re.match(r"^(?:.{0,160}?)\bno\.?\s*" + ID + r"\s*(?:,|\s)\s*date[d:]?", t, re.I)
        if mt and doc_type == "work_order":
            out.append(_row("work_order_id", mt.group(1), mt.group(1).upper(), page, method, t, mt, 0.6))
    add("ward", r"\bward\s*(?:no\.?|number)\s*[:.]?\s*(\d{1,3})\b", conf=0.75)
    # amounts: classify by the words just before the figure
    for mt in re.finditer(MONEY, t, re.I):
        ctx = t[max(0, mt.start() - 110): mt.start()].lower()
        val = parse_amount(mt.group(0))
        if not val:
            continue
        if re.search(r"sum of|released|paid|payment of|remitted|disbursed", ctx) and doc_type in ("payment_record", "measurement_book", None):
            f = "payment_amount"
        elif re.search(r"contract value|award|accepted|bid|tendered|agreement (?:value|amount)|work order (?:value|amount)", ctx) and not re.search(r"estimat", ctx):
            f = "awarded_amount"
        elif re.search(r"estimat|put to tender|approval|sanction|accorded|amount of", ctx):
            f = "estimated_cost"
        else:
            continue
        if not any(r["field"] == f for r in out):
            out.append(_row(f, mt.group(0), str(int(val)) if float(val).is_integer() else str(val), page, method, t, mt, 0.72))
    add("commencement_date", r"commence\w*[^.]{0,70}?(?:on or before|on|by|from)\s*" + DATE, norm=iso, conf=0.72)
    add("completion_deadline", r"complet\w*[^.]{0,70}?(?:on or before|by|before)\s*" + DATE, norm=iso, conf=0.72)
    add("payment_date", r"(?:released|paid|credited|remitted|disbursed)[^.]{0,40}?(?:on|dated)\s*" + DATE, norm=iso, conf=0.72)
    if doc_type == "completion_certificate":
        add("document_date", r"(?:taken over|handed over|completed)[^.]{0,60}?\bon\s*" + DATE, norm=iso, conf=0.7)
    if not any(r["field"] == "document_date" for r in out):
        add("document_date", r"\b(?:dated|date\s*:)\s*" + DATE, norm=iso, conf=0.6)
    mt = MS.search(t)
    if mt:
        name = re.sub(r"\s+", " ", mt.group(1)).strip(" .,")
        out.append(_row("contractor", "M/s " + name, name, page, method, t, mt, 0.72))
    return out
