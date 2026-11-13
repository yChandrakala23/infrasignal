"""One definition of "which works count" and how each filter is applied, shared by every list, total and chart.

Why this exists: the dashboard, the works table and the contractor list used to apply the filters (or the "official only" rule)
separately, so the same ward or fiscal year could give different numbers on different screens.

Rules
  visible works     official + confirmed. Provisional works (started from a PDF, not yet checked) are counted separately and
                    never added to financial totals. Merged works are hidden everywhere (they live on in their target).
  ward              "243:12" = ward 12 of the 243-ward map. A bare "12" matches ward 12 in any map; leading zeros are ignored.
  department        case-insensitive, surrounding spaces ignored ("Ward Engineering" = "WARD ENGINEERING ").
  fiscal year       April to March. A work belongs to its stored label if it has one (any of 2024-25 / 2024-2025 / FY 2024-25),
                    otherwise to the year of its work-order date.
  payments          counted once: a payment recorded twice (CSV row and payment-advice PDF) points at the first through
                    duplicate_of_id and is left out of every sum (see sources.counted_payments).
"""
import re
from datetime import datetime
from sqlalchemy import and_, or_, func, exists, true
from . import models as m

VISIBLE_STATUSES = ("official", "confirmed")


def visible_works():
    V = m.Roadwork.verification_status
    return or_(V.is_(None), V.in_(VISIBLE_STATUSES))


# ----------------------------------------------------------------------------- fiscal year
_FY = re.compile(r"(?<!\d)(\d{4})\s*[-/–]\s*(\d{2}|\d{4})(?!\d)")


def fy_start(label):
    """2024-25 / 2024-2025 / FY 2024-25 / 2024/25 -> 2024, else None."""
    mo = _FY.search(str(label or ""))
    if not mo:
        return None
    y = int(mo.group(1))
    end = mo.group(2)
    nxt = int(end) if len(end) == 4 else (y // 100) * 100 + int(end)
    return y if nxt == y + 1 else None


def fy_canonical(y):
    return f"{y}-{str(y + 1)[2:]}"


def fy_of_date(d):
    if not d:
        return None
    return d.year if d.month >= 4 else d.year - 1


def fy_variants(y):
    a, b = str(y), str(y + 1)
    out = set()
    for sep in ("-", "/", " - "):
        for end in (b[2:], b):
            for pre in ("", "FY ", "FY", "F.Y. ", "F.Y."):
                out.add(f"{pre}{a}{sep}{end}")
    return sorted(out)


def fiscal_year_condition(label):
    """SQL condition for 'this work belongs to fiscal year <label>'. An unreadable label matches nothing."""
    R = m.Roadwork
    y = fy_start(label)
    if y is None:
        return R.fiscal_year == label
    stored = func.lower(func.trim(R.fiscal_year)).in_([v.lower() for v in fy_variants(y)])
    unlabelled = or_(R.fiscal_year.is_(None), R.fiscal_year == "")
    in_year = and_(R.work_order_date >= datetime(y, 4, 1), R.work_order_date < datetime(y + 1, 4, 1))
    # collected data: a work also belongs to every financial year in which one of its source rows was published
    in_source = exists().where(and_(m.WorkSourceLink.roadwork_id == R.id, m.WorkSourceLink.status != "rejected",
                                    m.SourceRecord.id == m.WorkSourceLink.source_record_id, m.SourceRecord.fiscal_year == fy_canonical(y)))
    return or_(stored, and_(unlabelled, in_year), in_source)


def payment_fiscal_year_condition(label):
    """Which payments are shown in a financial-year view. A payment from a collected file belongs to the year of that file (so one work paid
    in two years shows each payment in its own year). Every other payment follows its work, as it always did: it is shown whenever its work
    is in the year."""
    P = m.Payment
    y = fy_start(label)
    if y is None:
        return true()
    has_src_fy = exists().where(and_(m.SourceRecord.id == P.source_record_id, m.SourceRecord.fiscal_year.isnot(None)))
    src_is_year = exists().where(and_(m.SourceRecord.id == P.source_record_id, m.SourceRecord.fiscal_year == fy_canonical(y)))
    return or_(src_is_year, ~has_src_fy)


def fiscal_years_present(db):
    """Canonical labels that occur among visible works (from the stored label, or from the work-order date when there is none)."""
    R = m.Roadwork
    ys = set()
    for (lab,) in db.query(R.fiscal_year).filter(R.fiscal_year.isnot(None), R.fiscal_year != "", visible_works()).distinct():
        y = fy_start(lab)
        if y is not None:
            ys.add(y)
    for (d,) in db.query(R.work_order_date).filter(or_(R.fiscal_year.is_(None), R.fiscal_year == ""), R.work_order_date.isnot(None), visible_works()):
        y = fy_of_date(d)
        if y is not None:
            ys.add(y)
    for (lab,) in db.query(m.SourceRecord.fiscal_year).join(m.WorkSourceLink, m.WorkSourceLink.source_record_id == m.SourceRecord.id) \
            .join(R, R.id == m.WorkSourceLink.roadwork_id).filter(m.SourceRecord.fiscal_year.isnot(None), visible_works()).distinct():
        y = fy_start(lab)
        if y is not None:
            ys.add(y)
    return [fy_canonical(y) for y in sorted(ys)]


# ----------------------------------------------------------------------------- ward
def norm_ward_number(n):
    n = re.sub(r"\.0+$", "", str(n or "").strip())
    n2 = n.lstrip("0")
    return n2 or ("0" if n else "")


def ward_condition(ward):
    """'243:12' or '12'. Matches the stored number with or without leading zeros."""
    R = m.Roadwork
    regime, _, num = ward.rpartition(":") if ":" in ward else ("", "", ward)
    num = norm_ward_number(num)
    if not num:
        return None
    forms = {num, num.zfill(2), num.zfill(3)}
    c = R.ward_number.in_(sorted(forms))
    return and_(c, R.ward_regime == regime.strip()) if regime.strip() else c


# ----------------------------------------------------------------------------- department
def department_condition(dep):
    R = m.Roadwork
    return func.lower(func.trim(R.department)) == " ".join(str(dep).split()).lower()
