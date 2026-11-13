"""Normalisers shared by import, extraction and matching. Pure functions, no database access."""
import re
from datetime import datetime

MONTHS = {m_: i for i, m_ in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def norm_text(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())).strip()


def norm_contractor(s) -> str:
    t = norm_text(s)
    t = re.sub(r"^(?:(?:m s|ms|sri|shri|smt)\s+)+", "", t)
    t = re.sub(r"\s+(pvt|private|ltd|limited|and co|co)\b", "", t)
    return t.strip()


def norm_id(s) -> str:
    """Identifier comparison key: upper-case, separators removed. 'WO/2024/77' == 'wo-2024-77'."""
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def clean_id(s) -> str:
    """Identifier as displayed: upper-case, surrounding punctuation stripped."""
    return re.sub(r"\s+", "", str(s or "").strip().strip(".,;:()[]")).upper()


_ABBR = {"rd": "road", "st": "street", "ave": "avenue", "mn": "main", "cr": "cross", "crs": "cross",
         "stg": "stage", "blk": "block", "lyt": "layout", "ly": "layout", "nr": "near", "opp": "opposite",
         "jn": "junction", "jct": "junction", "flyovr": "flyover"}
_ORD = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b")
ROAD_STOP = {"road", "main", "cross", "street", "lane", "avenue", "layout", "stage", "block", "the", "of", "and",
             "near", "to", "from", "via", "in", "at", "bbmp", "ring", "inner", "outer", "service", "first",
             "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth", "ward"}


def norm_road_name(s) -> str:
    t = norm_text(_ORD.sub(r"\1", str(s or "").lower()))
    t = _ORD.sub(r"\1", t)
    return " ".join(_ABBR.get(w, w) for w in t.split())


def distinctive_tokens(road_norm: str):
    return [w for w in road_norm.split() if w not in ROAD_STOP and not w.isdigit() and len(w) > 2]


# ------------------------------------------------------------------ amounts
_UNITS = {"lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "crore": 1e7, "crores": 1e7, "cr": 1e7, "cr.": 1e7,
          "thousand": 1e3, "k": 1e3}


def parse_amount(v):
    """'Rs. 12,50,000/-' -> 1250000.0 ; 'Rs 1.25 Cr' -> 12500000.0 ; '12.5 Lakhs' -> 1250000.0. None if not a number."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "-"):
        return None
    mm = re.search(r"([\d,]*\.?\d+)\s*(lakhs?|lacs?|crores?|cr\.?|thousand)?\b", s, re.I)
    if not mm:
        return None
    try:
        num = float(mm.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (mm.group(2) or "").lower()
    return num * _UNITS.get(unit, 1.0)


_DMY = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\b")
_DMONY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]*([A-Za-z]{3,9})[\s\-/.,]*(\d{2,4})\b")
_MONDY = re.compile(r"\b([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _y(y):
    y = int(y)
    return y + 2000 if y < 100 else y


def parse_date(v):
    """First recognisable date in v -> datetime, else None. Day-first for numeric dates (Indian convention)."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v)
    for rx, order in ((_ISO, "ymd"), (_DMY, "dmy"), (_DMONY, "dMy"), (_MONDY, "Mdy")):
        mm = rx.search(s)
        if not mm:
            continue
        try:
            if order == "ymd":
                y, mo, d = int(mm.group(1)), int(mm.group(2)), int(mm.group(3))
            elif order == "dmy":
                d, mo, y = int(mm.group(1)), int(mm.group(2)), _y(mm.group(3))
            elif order == "dMy":
                d, mo, y = int(mm.group(1)), MONTHS.get(mm.group(2)[:3].lower()), _y(mm.group(3))
            else:
                mo, d, y = MONTHS.get(mm.group(1)[:3].lower()), int(mm.group(2)), int(mm.group(3))
            if mo is None:
                continue
            return datetime(y, mo, d)
        except ValueError:
            continue
    return None


def fiscal_year(d):
    if not d:
        return None
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{str(y + 1)[2:]}"


# ------------------------------------------------------------------ road names inside free text
_ROAD_PHRASE = re.compile(
    r"((?:(?:\d+(?:st|nd|rd|th)?|[A-Z][A-Za-z.'\-]+|Dr\.?|Sri|Smt)\s+){1,5}"
    r"(?:Road|Rd\.?|Main\s+Road|Main|Cross|Street|St\.?|Lane|Avenue|Marg|Circle|Flyover|Junction|Layout\s+Road))\b")


def road_names_in_text(text_: str, limit=3):
    """Candidate road names in a work description, e.g. 'Asphalting of Hosur Road from ...' -> ['Hosur Road']."""
    out = []
    for mm in _ROAD_PHRASE.finditer(text_ or ""):
        name = re.sub(r"^(?:(?:of|to|from|on|at|for|in|the|and)\s+)+", "", mm.group(1).strip(), flags=re.I)
        name = re.sub(r"^(?:Asphalting|Resurfacing|Providing|Laying|Repairs?|Restoration|Improvements?|Development|"
                      r"Construction|White\s*topping|Widening|Maintenance|Pothole|Potholes|Filling|Works?)\s+", "", name,
                      flags=re.I).strip()
        if len(name) >= 5 and name.lower() not in [o.lower() for o in out]:
            out.append(name)
        if len(out) >= limit:
            break
    return out


WORK_CATEGORY_RULES = [
    ("white_topping", re.compile(r"white\s*-?topping", re.I)),
    ("pothole_repair", re.compile(r"pot\s*-?holes?|patch\s*work|patching", re.I)),
    ("dug_road_restoration", re.compile(r"restor\w*\s+(?:of\s+)?(?:dug|damaged|cut)|dug\s*-?\s*up|road\s*cutting|utility\s*cut", re.I)),
    ("resurfacing", re.compile(r"asphalt\w*|resurfac\w*|tarr?ing|overlay|bituminous|\bBC\b|\bDBM\b", re.I)),
    ("road_other", re.compile(r"\broad\b|\bcross\b|\bmain\b|\bstreet\b|\blane\b|carriageway|footpath|\bcc\s*road", re.I)),
]


def work_category(desc: str):
    for cat, rx in WORK_CATEGORY_RULES:
        if rx.search(desc or ""):
            return cat
    return None
