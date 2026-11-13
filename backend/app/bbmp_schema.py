"""BBMP work-order / payment files: column mapping, parsing of the packed fields, validation and normalisation.

Pure functions, no database access. What the published files look like (checked on the OpenCity 2023-24, 2024-25 and 2025-26 files):

    slno,id,Ward,wodetails,contractor,brnumber,amount,nett,deduction

    one row = one payment (bill) against one work order
    wodetails   "001-23-000012</a>Drilling of borewells in ..."      work-order code, a stray HTML tag, then the description
    contractor  "023258 RRENGINEERINGWORKS8880709398"                 optional 6-digit code, name (20 characters wide, often cut off), phone
    brnumber    "BR - 000042 / 10-Jul-2023CBR - / Rtgs - 004097 / 19-Jun-2024"   bill no + date, cheque ref, RTGS no + date, no separators
    amount, nett, deduction     whole rupees; nett is what is payable after the deduction
    header spelling varies by file (Ward / Ward No / ward)

The files have no work-order date, no estimate and no approved amount. Those fields stay empty: nothing is invented, and nothing is
derived from a bill amount (a bill is not an estimate or a sanction).

Column mappings are configurable: `COLUMN_SYNONYMS` below, plus per-dataset overrides in the sources file (see connectors/bbmp_connector.py).
"""
import hashlib, html, json, re
from datetime import datetime, timezone

# canonical field -> accepted header spellings (compared after lower-casing and removing punctuation)
COLUMN_SYNONYMS = {
    "serial": ["slno", "sl no", "s no", "sno", "serial", "serial no"],
    "source_id": ["id", "record id", "payment id"],
    "ward": ["ward", "ward no", "ward number", "ward_no"],
    "wo_details": ["wodetails", "wo details", "work order details", "work order", "workorder details"],
    "contractor": ["contractor", "contractor name", "name of contractor", "vendor", "vendor name"],
    "contractor_id": ["contractor id", "contractor code", "vendor code", "vendor id"],
    "bill_payment_ref": ["brnumber", "br number", "br no", "bill number and payment", "bill payment reference"],
    "gross_amount": ["amount", "bill amount", "gross amount", "gross"],
    "net_amount": ["nett", "net", "net amount", "net payment", "net paid"],
    "deduction": ["deduction", "deductions", "total deduction"],
    # fields later or other financial years may carry; used when present, never required
    "work_id": ["work id", "workid", "work code"],
    "job_number": ["job number", "job no", "bbmp job number", "job code"],
    "description": ["work description", "description", "name of work", "work name", "particulars"],
    "road_name": ["road name", "road"],
    "location": ["location", "place of work", "site", "area"],
    "department": ["department", "dept", "division"],
    "financial_year": ["financial year", "fiscal year", "fy", "year"],
    "bill_number": ["bill number", "bill no", "bill reference"],
    "bill_date": ["bill date"],
    "estimated_amount": ["estimated amount", "estimate", "estimated cost", "estimate amount"],
    "approved_amount": ["approved amount", "sanctioned amount", "work order amount", "approved cost"],
    "payment_amount": ["payment amount", "paid amount", "amount paid"],
    "payment_date": ["payment date", "paid date", "paid on", "rtgs date"],
    "work_order_date": ["work order date", "wo date", "date of work order"],
}
REQUIRED_ANY = (("wo_details", "description"), ("gross_amount", "net_amount", "payment_amount"))  # at least one of each group

_MON = {m_: i for i, m_ in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
MAX_AMOUNT = 5_000_000_000.0  # Rs 500 crore on one bill is far beyond a ward-level work order: treated as a data error


def norm_header(h) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(h or "").lower()).strip()


def map_columns(columns, overrides: dict | None = None):
    """-> (mapping canonical field -> original header, unmapped headers). `overrides` {field: exact header} wins over synonyms."""
    normed = {norm_header(c): c for c in columns}
    mapping = {}
    for field, header in (overrides or {}).items():
        hit = next((c for c in columns if c == header or norm_header(c) == norm_header(header)), None)
        if hit is not None:
            mapping[field] = hit
    for field, syns in COLUMN_SYNONYMS.items():
        if field in mapping:
            continue
        for s in syns:
            c = normed.get(norm_header(s))
            if c is not None and c not in mapping.values():
                mapping[field] = c
                break
    unmapped = [c for c in columns if c not in mapping.values()]
    return mapping, unmapped


def missing_required(mapping):
    return [" or ".join(g) for g in REQUIRED_ANY if not any(f in mapping for f in g)]


# ----------------------------------------------------------------------------- packed fields
_TAG = re.compile(r"<[^>]*>")
_CODE = re.compile(r"^\s*(?P<code>\d{3}-\d{2}-\d{6}|\d{2,4}-\d{2}-\d{3,8})(?P<rest>.*)$", re.S)


def clean_text(s) -> str:
    """Strip stray HTML tags, decode entities, collapse whitespace."""
    if s is None:
        return ""
    t = html.unescape(_TAG.sub(" ", str(s)))
    return re.sub(r"\s+", " ", t.replace(" ", " ")).strip()


def parse_wo_details(s):
    """'001-23-000012</a>Drilling of borewells' -> ('001-23-000012', 'Drilling of borewells'). Code is None when there is none."""
    raw = "" if s is None else str(s)
    mm = _CODE.match(raw)
    if mm:
        return mm.group("code"), clean_text(mm.group("rest"))
    return None, clean_text(raw)


_CONTRACTOR = re.compile(r"^(?:(?P<code>\d{4,8})[ \t]+)?(?P<name>.*?)[ \t]*(?P<phone>\d{10})?$", re.S)


def parse_contractor(s):
    """'023258 RRENGINEERINGWORKS8880709398' -> {'vendor_code': '023258', 'name': 'RRENGINEERINGWORKS', 'has_phone': True, 'truncated': False}.
    The phone number is read only to separate it from the name; it is not copied into the normalised record (it stays in the original row)."""
    raw = "" if s is None else str(s).strip("\r\n")
    if not raw.strip():
        return {"vendor_code": None, "name": None, "has_phone": False, "truncated": False}
    mm = _CONTRACTOR.match(raw)
    name_raw = mm.group("name") or ""
    phone = mm.group("phone")
    name = re.sub(r"\s+", " ", clean_text(name_raw)).strip(" ,;-")
    code = mm.group("code")
    if (not name and code) or name.replace(" ", "").isdigit():  # the whole value was digits: it is not a name
        return {"vendor_code": None, "name": None, "has_phone": bool(phone), "truncated": False}
    return {"vendor_code": code, "name": name or None, "has_phone": bool(phone) and set(phone) != {"0"},
            "truncated": len(name_raw.rstrip()) >= 19}


_DATE = r"(?P<{n}>\d{{1,2}}-[A-Za-z]{{3}}-\d{{4}})"
_BR = re.compile(r"(?<![A-Za-z])BR\s*-\s*(?P<no>[A-Za-z0-9]*)\s*/?\s*" + "(?:" + _DATE.format(n="d") + ")?", re.I)
_CBR = re.compile(r"(?<![A-Za-z])CBR\s*-\s*(?P<no>[A-Za-z0-9]*)\s*/?\s*" + "(?:" + _DATE.format(n="d") + ")?", re.I)
_RTGS = re.compile(r"(?<![A-Za-z])Rtgs\s*-\s*(?P<no>[A-Za-z0-9]*)\s*/?\s*" + "(?:" + _DATE.format(n="d") + ")?", re.I)


def parse_dmy(text):
    """'10-Jul-2023' -> datetime; None when absent or not a real date (31-Feb-2024)."""
    if not text:
        return None
    mm = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", str(text).strip())
    if not mm or mm.group(2).lower() not in _MON:
        return None
    try:
        return datetime(int(mm.group(3)), _MON[mm.group(2).lower()], int(mm.group(1)))
    except ValueError:
        return None


def parse_bill_payment(s):
    """The packed brnumber field -> bill, cheque and RTGS references with their dates (each None when absent)."""
    out = {"br_no": None, "br_date_text": None, "cbr_no": None, "cbr_date_text": None, "rtgs_no": None, "rtgs_date_text": None, "parsed": False}
    raw = "" if s is None else str(s)
    for key, rx in (("br", _BR), ("cbr", _CBR), ("rtgs", _RTGS)):
        mm = rx.search(raw)
        if mm:
            out["parsed"] = True
            out[f"{key}_no"] = (mm.group("no") or "").strip() or None
            out[f"{key}_date_text"] = mm.group("d")
    return out


def parse_amount_strict(v):
    """Whole rupees or decimal, optional thousands commas / 'Rs'. Raises ValueError with a plain message for anything else."""
    if v is None:
        raise ValueError("missing")
    t = str(v).strip()
    if t == "" or t.lower() in ("nan", "none", "null", "-"):
        raise ValueError("missing")
    t2 = re.sub(r"^(?:rs\.?|inr|₹)\s*", "", t, flags=re.I).replace(",", "").strip().rstrip("/-").strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", t2):
        raise ValueError(f"not a number: {t[:40]!r}")
    return float(t2)


def row_hash(raw: dict) -> str:
    """Identity of a published row's content (every column, empty values ignored)."""
    body = json.dumps({str(k): ("" if v is None else str(v).strip()) for k, v in sorted(raw.items(), key=lambda kv: str(kv[0])) if v not in (None, "")},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


# ----------------------------------------------------------------------------- one row
class RowResult:
    """Outcome of normalising one published row. `errors` -> the row cannot be stored; `flags` -> stored, with a note."""
    def __init__(self):
        self.values: dict = {}
        self.errors: list[tuple[str, str]] = []
        self.flags: list[tuple[str, str]] = []
        self.missing: list[str] = []

    @property
    def ok(self):
        return not self.errors


def _get(raw, mapping, field):
    h = mapping.get(field)
    if h is None:
        return None
    v = raw.get(h)
    if v is None:
        return None
    v = str(v)
    return v if v.strip() else None


def normalise_row(raw: dict, mapping: dict, *, fiscal_year: str, today: datetime | None = None) -> RowResult:
    """Validate and normalise one row. `raw` is {original header: original text}. `fiscal_year` is the dataset's year ('2024-25')."""
    today = today or datetime.now(timezone.utc).replace(tzinfo=None)
    res = RowResult()
    v = res.values

    # ---- identifiers, description
    wo = _get(raw, mapping, "wo_details")
    code, desc = parse_wo_details(wo) if wo else (None, "")
    if _get(raw, mapping, "description"):
        desc = clean_text(_get(raw, mapping, "description")) or desc
    v["work_order_code"] = code or clean_text(_get(raw, mapping, "work_id")) or None
    v["description"] = desc or None
    v["job_number"] = clean_text(_get(raw, mapping, "job_number")) or None
    v["source_id"] = clean_text(_get(raw, mapping, "source_id")) or None
    v["serial"] = clean_text(_get(raw, mapping, "serial")) or None
    for f in ("road_name", "location", "department"):
        v[f] = clean_text(_get(raw, mapping, f)) or None
    if not v["description"]:
        res.missing.append("description")
    if not v["work_order_code"] and not v["job_number"]:
        res.flags.append(("no_work_code", "No work-order code in the row; the work cannot be matched to other years' records by code."))

    # ---- ward
    w = (clean_text(_get(raw, mapping, "ward")) or "").strip()
    w = re.sub(r"\.0+$", "", w)
    v["ward"] = w or None
    if not w:
        res.missing.append("ward")
    elif not w.isdigit():
        res.flags.append(("ward_not_numeric", f"Ward value {w!r} is not a number; kept as published."))
    elif int(w) == 0:
        res.flags.append(("ward_zero", "Ward is 0."))

    # ---- contractor
    c = parse_contractor(_get(raw, mapping, "contractor"))
    explicit_id = clean_text(_get(raw, mapping, "contractor_id")) or None
    v["contractor_name"], v["contractor_id"] = c["name"], explicit_id or c["vendor_code"]
    v["contractor_name_truncated"] = c["truncated"]
    if not c["name"]:
        res.missing.append("contractor")
    elif c["truncated"]:
        res.flags.append(("contractor_name_cut", "The published contractor name fills (nearly) all of its 20-character field and may be cut off."))
    if not v["contractor_id"] and c["name"]:
        res.missing.append("contractor_id")

    # ---- amounts
    amounts = {}
    for f in ("gross_amount", "net_amount", "deduction", "estimated_amount", "approved_amount", "payment_amount"):
        t = _get(raw, mapping, f)
        if t is None:
            continue
        try:
            a = parse_amount_strict(t)
        except ValueError as e:
            if str(e) != "missing":
                res.errors.append(("bad_amount", f"{f}: {e}"))
            continue
        if a < 0:
            res.errors.append(("negative_amount", f"{f} is negative ({a:g}); not imported, check the source."))
        elif a > MAX_AMOUNT:
            res.errors.append(("implausible_amount", f"{f} is {a:,.0f}, above the {MAX_AMOUNT:,.0f} sanity limit."))
        else:
            amounts[f] = a
    if not amounts and not res.errors:  # a work-order row may carry only an estimate / approved amount; a row with no amount of any kind says nothing
        res.errors.append(("no_amount", "The row has no usable amount."))
    v.update({k: amounts.get(k) for k in ("gross_amount", "net_amount", "deduction", "estimated_amount", "approved_amount", "payment_amount")})
    g, n, d = amounts.get("gross_amount"), amounts.get("net_amount"), amounts.get("deduction")
    if g is not None and n is not None and d is not None and abs(g - (n + d)) > 1.0:
        res.flags.append(("amount_mismatch", f"amount {g:,.0f} is not nett {n:,.0f} + deduction {d:,.0f}."))
    if g is not None and n is not None and n > g + 1.0:
        res.flags.append(("net_above_gross", "nett is larger than amount."))
    if g == 0 or n == 0:
        res.flags.append(("zero_amount", "An amount is zero."))

    # ---- bill / payment references and dates
    packed = _get(raw, mapping, "bill_payment_ref")
    ref = parse_bill_payment(packed) if packed else parse_bill_payment(None)
    if packed and not ref["parsed"]:
        res.flags.append(("ref_not_parsed", "The bill / payment reference could not be read; no payment was recorded from this row."))
    v["bill_number"] = ref["br_no"] or clean_text(_get(raw, mapping, "bill_number")) or None
    v["cheque_reference"] = ref["cbr_no"]
    v["payment_reference"] = ref["rtgs_no"]
    dates = {}
    for label, text in (("bill_date", ref["br_date_text"] or _get(raw, mapping, "bill_date")),
                        ("cheque_date", ref["cbr_date_text"]),
                        ("payment_date", ref["rtgs_date_text"] or _get(raw, mapping, "payment_date")),
                        ("work_order_date", _get(raw, mapping, "work_order_date"))):
        if not text:
            dates[label] = None
            continue
        dt = parse_dmy(text)
        if dt is None:
            from .textnorm import parse_date
            dt = parse_date(text)
        if dt is None:
            res.flags.append(("bad_date", f"{label}: {str(text)[:30]!r} is not a valid date; left empty."))
        elif dt.year < 1990 or dt > today.replace(year=today.year + 1):
            res.flags.append(("implausible_date", f"{label}: {dt:%d-%b-%Y} is outside 1990 to next year; left empty."))
            dt = None
        dates[label] = dt
    v.update(dates)
    if dates.get("bill_date") and dates.get("payment_date") and dates["payment_date"] < dates["bill_date"]:
        res.flags.append(("paid_before_bill", "The payment date is earlier than the bill date."))
    fy_start = _fy_start(fiscal_year)
    if fy_start and dates.get("payment_date") and not (datetime(fy_start, 4, 1) <= dates["payment_date"] < datetime(fy_start + 1, 4, 1)):
        res.flags.append(("payment_outside_fy", f"Payment date {dates['payment_date']:%d-%b-%Y} is outside financial year {fiscal_year}."))

    # a payment exists only when the row says one was made (an RTGS reference or date) and carries an amount
    pay_amount = n if n is not None else amounts.get("payment_amount")
    v["has_payment"] = bool((v["payment_reference"] or dates.get("payment_date")) and pay_amount is not None)
    v["paid_amount"] = pay_amount if v["has_payment"] else None
    if (v["payment_reference"] or dates.get("payment_date")) and pay_amount is None:
        res.flags.append(("no_net_amount", "A payment reference is present but there is no nett amount; no payment was recorded."))
    if not (v["payment_reference"] or dates.get("payment_date")) and packed and ref["parsed"]:
        res.flags.append(("no_payment_reference", "The row has a bill but no payment (RTGS) reference or date."))
    return res


def _fy_start(label):
    mm = re.search(r"(\d{4})\D+(\d{2,4})", str(label or ""))
    return int(mm.group(1)) if mm else None


def fiscal_label(label):
    """'2024-2025' / 'FY 2024-25' / '2024/25' -> '2024-25'; None if unreadable."""
    mm = re.search(r"(?<!\d)(\d{4})\s*[-/–]\s*(\d{2}|\d{4})(?!\d)", str(label or ""))
    if not mm:
        return None
    y = int(mm.group(1))
    end = mm.group(2)
    nxt = int(end) if len(end) == 4 else (y // 100) * 100 + int(end)
    return f"{y}-{str(y + 1)[2:]}" if nxt == y + 1 else None


def regime_from_text(text):
    """'... for 243 Wards Regime' -> '243'; 'Common Wards' -> 'common'; '198 ward' -> '198'. None if not stated."""
    t = str(text or "").lower()
    mm = re.search(r"\b(198|225|243|369)\s*[- ]?\s*wards?\b", t)
    if mm:
        return mm.group(1)
    if "common" in t and "ward" in t:
        return "common"
    return None
