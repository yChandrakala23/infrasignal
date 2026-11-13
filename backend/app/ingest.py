"""CSV / Excel import of work-order, payment and tender tables.

Government portals rarely offer clean downloads, so import is a two-step flow: `preview_table` shows the real
headers with a guessed mapping and sample rows, the officer fixes the mapping if needed, then `import_table`
loads it. Every loaded row keeps its original values (SourceRecord.raw) and file row number.

Cleaning and normalisation: amounts (Indian digit grouping, lakh/crore words), dates (day-first), contractor
names, ward numbers (numbers above 300 mark multi-ward / non-geographic works in BBMP data), road category.
Duplicate detection: identical rows (hash of the key columns) are skipped, repeated bill/payment lines are not
inserted twice, and roadworks are matched on job number, then work-order id, then a description+ward+amount fingerprint.
"""
import hashlib, io, re
import pandas as pd
from sqlalchemy.orm import Session
from . import models as m
from .sources import link_source, add_payment
from .textnorm import (norm_text, norm_contractor, parse_amount, parse_date, fiscal_year, work_category,
                       road_names_in_text)

SYN = {
    "job_number": ["job number", "job no", "job code", "jobnumber", "job id", "bbmp job number"],
    "work_order_id": ["work order id", "work order no", "work order number", "wo no", "wo id", "wo number"],
    "tender_id": ["tender id", "tender no", "tender number", "nit no", "indent no", "e procurement id"],
    "work_id": ["work id", "contract no", "contract number", "agreement no", "agreement number", "work code"],
    "description": ["work description", "name of work", "description", "work name", "particulars", "work details", "name of the work"],
    "ward_number": ["ward number", "ward no", "ward"],
    "ward_name": ["ward name"],
    "department": ["department", "dept", "division", "executing agency"],
    "contractor": ["contractor name", "contractor", "vendor", "agency", "name of contractor", "name of the contractor"],
    "sanctioned_amount": ["work order amount", "sanctioned amount", "wo amount", "contract amount", "awarded amount"],
    "estimated_cost": ["estimate amount", "estimated cost", "estimated amount", "estimate", "ecv"],
    "bill_number": ["bill number", "bill no", "br no"],
    "bill_amount": ["bill amount", "gross amount"],
    "payment_amount": ["paid amount", "payment amount", "amount paid", "net amount"],
    "date": ["work order date", "wo date", "date"],
    "bill_date": ["bill date"],
    "payment_date": ["payment date", "paid date", "br date"],
    "commencement_date": ["commencement date", "start date", "date of commencement"],
    "completion_deadline": ["completion deadline", "completion date", "date of completion", "due date", "stipulated completion date"],
    "data_status": ["data status"],
}
TENDER_SYN = {
    "tender_id": ["tender id", "tender no", "tender number", "nit no", "nit number", "tender reference"],
    "job_number": ["bbmp job number", "job number", "job no"],
    "title": ["title", "tender title", "name of work", "work description", "description", "tender description"],
    "department": ["department", "dept", "division"],
    "category": ["category", "work category", "type of work"],
    "published_date": ["published date", "publish date", "publication date", "published on"],
    "closing_date": ["closing date", "bid closing date", "last date", "due date"],
    "award_date": ["award date", "date of award"],
    "estimated_value": ["estimated value", "estimated cost", "tender value", "estimate"],
    "awarded_value": ["awarded value", "awarded amount", "accepted amount", "contract value"],
    "status": ["status", "tender status"],
    "contractor": ["contractor", "awarded to", "bidder", "successful bidder"],
    "location": ["location", "place of work", "site"],
    "url": ["url", "link", "tender url"],
}
ROAD_RE = re.compile(
    r"\b(road|roads|rd|asphalt\w*|resurfac\w*|pothole\w*|white ?topping|tarr?ing|carriageway|"
    r"cross|main|lane|street|restoration of dug|patch ?work|footpath)\b", re.I)


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(h).lower()).strip()


def guess_mapping(columns, syn=SYN) -> dict:
    """Column header -> field. Exact header matches are settled first for every field. Only then is a header matched by
    containment, and never when it also contains a longer synonym of another field: "Payment Date" contains "date", but it is
    the payment date, not the work-order date."""
    normed = {_norm_header(c): c for c in columns}
    mapping = {}
    for field, syns in syn.items():
        for s in syns:
            if s in normed and normed[s] not in mapping.values():
                mapping[field] = normed[s]
                break
    for field, syns in syn.items():
        if field in mapping:
            continue
        others = [(o, f2) for f2, ss in syn.items() if f2 != field for o in ss]
        for s in syns:
            hit = next((orig for n, orig in normed.items()
                        if s in n and orig not in mapping.values() and not any(o in n and len(o) > len(s) for o, _ in others)), None)
            if hit:
                mapping[field] = hit
                break
    return mapping


def _fy_label(label):
    """'FY 2024-2025' / '2024/25' -> '2024-25' (the column holds 10 characters); anything unreadable is cut to fit."""
    if not label:
        return None
    from .scope import fy_start, fy_canonical
    y = fy_start(label)
    return fy_canonical(y) if y is not None else str(label).strip()[:10] or None


def norm_amount(v):
    return parse_amount(v)


def norm_date(v):
    d = parse_date(v) if v is not None and str(v).strip() != "" else None
    return d


XLS_MAGIC, ZIP_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"PK\x03\x04"
_ALL_SYNONYMS = {x for d in (SYN, TENDER_SYN) for v in d.values() for x in v}


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _with_header_row(raw: pd.DataFrame) -> pd.DataFrame:
    """Portal exports often put a title or notes above the column names. Use the first of the top 15 rows that holds the most
    recognisable column names (at least two); otherwise the first row is the header, as usual."""
    best, best_i = 0, 0
    for i in range(min(15, len(raw))):
        score = sum(1 for v in raw.iloc[i] if isinstance(v, str) and _norm_header(v) in _ALL_SYNONYMS)
        if score > best:
            best, best_i = score, i
    h = best_i if best >= 2 else 0
    cols, seen = [], {}
    for j, v in enumerate(raw.iloc[h]):
        name = str(v).strip() if isinstance(v, str) and str(v).strip() else f"Unnamed: {j}"
        seen[name] = seen.get(name, 0) + 1
        cols.append(name if seen[name] == 1 else f"{name}.{seen[name] - 1}")
    df = raw.iloc[h + 1:].reset_index(drop=True)
    df.columns = cols
    df = df.dropna(how="all")
    df.attrs["row_offset"] = h
    return df


def read_table(data: bytes, filename: str) -> pd.DataFrame:
    """CSV, .xlsx and .xls, recognised by what the file contains (not only its extension). Raises ValueError with a plain message
    when the file cannot be read, e.g. a web page saved with an .xls name."""
    if not data:
        raise ValueError("The file is empty")
    head = data[:8]
    if head.startswith(XLS_MAGIC):
        try:
            import xlrd  # noqa: F401
        except ImportError:
            raise ValueError("Reading .xls files needs the 'xlrd' package (pip install xlrd). Or save the file as .xlsx or CSV.")
        engine = "xlrd"
    elif head.startswith(ZIP_MAGIC):
        engine = "openpyxl"
    else:
        engine = None
    if engine:
        try:
            sheets = pd.read_excel(io.BytesIO(data), dtype=str, header=None, sheet_name=None, engine=engine)
        except Exception as e:
            raise ValueError(f"The Excel file could not be read ({type(e).__name__}). Save it again as .xlsx or CSV.")
        for name, raw in sheets.items():  # first sheet that has any data
            if raw.dropna(how="all").shape[0] >= 2:
                return _with_header_row(raw)
        raise ValueError("The Excel file has no sheet with data rows")
    text = _decode(data)
    lead = text.lstrip()[:200].lower()
    if lead.startswith(("<!doctype", "<html", "<table", "<?xml", "<head", "<body")):
        try:
            tables = pd.read_html(io.StringIO(text), flavor=None)
        except Exception:
            raise ValueError("This file is a web page saved with a spreadsheet name, not a spreadsheet. Open it in Excel and save it as .xlsx or CSV.")
        t = max(tables, key=len) if tables else None
        if t is None or len(t) < 1:
            raise ValueError("This file is a web page with no table of data. Save the table as .xlsx or CSV.")
        header = [" ".join(map(str, c)) if isinstance(c, tuple) else str(c) for c in t.columns]
        body = t.astype(object).where(t.notna(), None).astype(object).map(lambda v: None if v is None else str(v)).values.tolist()
        return _with_header_row(pd.DataFrame([header] + body, dtype=object))
    sep = max((",", "\t", ";", "|"), key=lambda c: text[:5000].count(c))
    try:
        raw = pd.read_csv(io.StringIO(text), dtype=str, sep=sep, header=None, engine="python", on_bad_lines="skip")
    except Exception as e:
        raise ValueError(f"The file could not be read as CSV ({type(e).__name__})")
    if raw.dropna(how="all").shape[0] < 2:
        raise ValueError("The file has no data rows")
    return _with_header_row(raw)


def preview_table(data: bytes, filename: str, kind="works", rows=5):
    df = read_table(data, filename)
    syn = TENDER_SYN if kind == "tenders" else SYN
    mapping = guess_mapping(df.columns, syn)
    road_share = None
    if kind == "works" and "description" in mapping:
        road_share = round(float(df[mapping["description"]].fillna("").str.contains(ROAD_RE).mean()), 3)
    return {"rows": len(df), "columns": [str(c) for c in df.columns], "mapping": mapping,
            "unmapped_columns": [str(c) for c in df.columns if c not in mapping.values()],
            "road_related_share": road_share,
            "sample": df.head(rows).fillna("").to_dict(orient="records"),
            "required_missing": [f for f in (["description"] if kind == "works" else ["tender_id"]) if f not in mapping]}


def _clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s or None


def _row_hash(raw: dict) -> str:
    return hashlib.sha256("|".join(f"{k}={raw[k]}" for k in sorted(raw) if raw[k]).encode()).hexdigest()


def import_table(db: Session, data: bytes, filename: str, source_name: str, source_url: str,
                 ward_regime: str, user_id: int | None, mapping: dict | None = None,
                 roads_only: bool = True, is_demo: bool | None = None, fiscal_year_label: str | None = None) -> m.ImportJob:
    df = read_table(data, filename)
    OFF = df.attrs.get("row_offset", 0)  # spreadsheet rows above the header row
    mapping = mapping or guess_mapping(df.columns)
    job = m.ImportJob(filename=filename, kind="table", source_name=source_name, source_url=source_url,
                      ward_regime=ward_regime, column_map=mapping, created_by=user_id, rows_total=len(df), errors=[],
                      fiscal_year=fiscal_year_label, is_demo=bool(is_demo), rows_duplicate=0)
    db.add(job); db.flush()
    errors = []
    if "description" not in mapping:
        errors.append({"row": None, "error": "No description column found. Pass an explicit mapping."})
    get = lambda row, f: _clean(row.get(mapping[f])) if f in mapping else None
    contractors = {}
    seen_hashes = {h for (h,) in db.query(m.SourceRecord.row_hash).filter(m.SourceRecord.row_hash.isnot(None))}
    for i, row in df.iterrows():
        try:
            raw = {k: _clean(v) for k, v in row.to_dict().items()}
            desc = get(row, "description") or ""
            if roads_only and not ROAD_RE.search(desc):
                job.rows_skipped += 1
                continue
            h = _row_hash(raw)
            if h in seen_hashes:
                job.rows_duplicate += 1
                continue
            seen_hashes.add(h)
            row_demo = bool(is_demo) or "sample" in (get(row, "data_status") or "").lower()
            src = m.SourceRecord(import_job_id=job.id, row_number=int(i) + 2 + OFF, raw=raw, row_hash=h)
            db.add(src); db.flush()
            job_no, wo = get(row, "job_number"), get(row, "work_order_id")
            amount = norm_amount(get(row, "sanctioned_amount"))
            wdate = norm_date(get(row, "date"))
            rw, how = None, None
            live = lambda: db.query(m.Roadwork).filter(m.Roadwork.verification_status != "merged")  # a work folded into another is not a target
            if job_no:
                rw = live().filter_by(job_number=job_no).first()
                how = ("exact_job_number", 0.99, "auto") if rw else None
            if not rw and wo:
                rw = live().filter_by(work_order_id=wo).first()
                how = ("exact_work_order_id", 0.95, "auto") if rw else None
            if not rw and not job_no and not wo:  # no identifiers at all: fingerprint (weaker, so the link is left for review)
                dn, wn = norm_text(desc), get(row, "ward_number")
                rw = next((x for x in live().filter_by(description_norm=dn, ward_number=wn)
                           if x.sanctioned_amount == amount), None)
                how = ("fingerprint_description_ward_amount", 0.8, "suggested") if rw else None
            created_work = rw is None
            if not rw:
                rw = m.Roadwork(job_number=job_no, work_order_id=wo, source_record_id=src.id, source_name=source_name,
                                source_url=source_url, is_demo=row_demo, data_origin="import")
                db.add(rw)
                db.flush()
            elif rw.verification_status == "provisional":
                rw.verification_status = "official"  # an imported government record now backs a work that began as a PDF-only profile
            rw.tender_id = rw.tender_id or get(row, "tender_id")
            rw.work_id = rw.work_id or get(row, "work_id")
            rw.description = rw.description or desc
            rw.description_norm = norm_text(rw.description)
            rw.ward_regime = ward_regime
            wn = get(row, "ward_number")
            rw.ward_number = rw.ward_number or (re.sub(r"\.0+$", "", wn) if wn else None)
            rw.ward_name = rw.ward_name or get(row, "ward_name")
            rw.ward_is_multi = bool(rw.ward_number and rw.ward_number.isdigit() and int(rw.ward_number) > 300)
            rw.sanctioned_amount = rw.sanctioned_amount or amount
            rw.awarded_amount = rw.awarded_amount or amount
            rw.estimated_cost = rw.estimated_cost or norm_amount(get(row, "estimated_cost"))
            rw.work_order_date = rw.work_order_date or wdate
            rw.commencement_date = rw.commencement_date or norm_date(get(row, "commencement_date"))
            rw.completion_deadline = rw.completion_deadline or norm_date(get(row, "completion_deadline"))
            rw.department = rw.department or get(row, "department")
            rw.category = rw.category or work_category(rw.description)
            rw.fiscal_year = rw.fiscal_year or _fy_label(fiscal_year_label) or fiscal_year(rw.work_order_date)
            names = road_names_in_text(rw.description, limit=1)
            rw.road_name_extracted = rw.road_name_extracted or (names[0] if names else None)
            if row_demo:
                rw.is_demo = True
            cname = get(row, "contractor")
            if cname:
                cn = norm_contractor(cname)
                c = contractors.get(cn) or db.query(m.Contractor).filter_by(name_norm=cn).first()
                if not c:
                    c = m.Contractor(name=re.sub(r"^m\s*/\s*s\.?\s*", "", cname, flags=re.I), name_norm=cn, is_demo=row_demo)
                    db.add(c); db.flush()
                contractors[cn] = c
                rw.contractor_id = c.id
            db.flush()
            # every original row stays linked to the work, whether it created the work or merged into it
            has_pay = bool(norm_amount(get(row, "payment_amount")))
            ev = {k: v for k, v in (("job_number", job_no), ("work_order_id", wo), ("tender_id", get(row, "tender_id")), ("row", int(i) + 2 + OFF),
                                    ("file", filename)) if v}
            if created_work:
                link_source(db, rw.id, src.id, "works_row", "primary", "created_from_row", 1.0, ev, "auto")
            else:
                link_source(db, rw.id, src.id, "payment_row" if has_pay else "works_row", "merged_row", how[0], how[1], ev, how[2])
            bill = None
            bnum, bamt = get(row, "bill_number"), norm_amount(get(row, "bill_amount"))
            if bamt or bnum:
                bill = db.query(m.Bill).filter_by(roadwork_id=rw.id, bill_number=bnum, amount=bamt).first() if bnum else None
                if not bill:
                    bill = m.Bill(roadwork_id=rw.id, bill_number=bnum, amount=bamt, bill_date=norm_date(get(row, "bill_date")),
                                  source_record_id=src.id)
                    db.add(bill); db.flush()
            pamt = norm_amount(get(row, "payment_amount"))
            if pamt:
                pdate = norm_date(get(row, "payment_date"))
                dupe = db.query(m.Payment).filter_by(roadwork_id=rw.id, amount=pamt, paid_on=pdate,
                                                     bill_id=bill.id if bill else None).first()
                dsr = db.get(m.SourceRecord, dupe.source_record_id) if dupe and dupe.source_record_id else None
                if not (dupe and dsr and dsr.import_job_id == job.id):  # same line repeated inside this file: skip, as before
                    add_payment(db, rw.id, pamt, pdate, bill_id=bill.id if bill else None, source_record_id=src.id, import_job_id=job.id)
            job.rows_ok += 1
        except Exception as e:  # keep importing, report the row
            job.rows_failed += 1
            errors.append({"row": int(i) + 2 + OFF, "error": str(e)[:300]})
    job.errors = errors[:500]
    job.status = "done" if not job.rows_failed else "done_with_errors"
    db.commit()
    return job


def import_tenders(db: Session, data: bytes, filename: str, source_name: str, source_url: str, user_id: int | None,
                   mapping: dict | None = None, is_demo: bool | None = None) -> m.ImportJob:
    """Import KPPP-style tender rows. Existing tenders (same Tender ID) are updated where fields are empty."""
    df = read_table(data, filename)
    OFF = df.attrs.get("row_offset", 0)
    mapping = mapping or guess_mapping(df.columns, TENDER_SYN)
    job = m.ImportJob(filename=filename, kind="tenders", source_name=source_name, source_url=source_url, column_map=mapping,
                      created_by=user_id, rows_total=len(df), errors=[], is_demo=bool(is_demo), rows_duplicate=0)
    db.add(job); db.flush()
    errors = []
    if "tender_id" not in mapping:
        errors.append({"row": None, "error": "No Tender ID column found. Pass an explicit mapping."})
    get = lambda row, f: _clean(row.get(mapping[f])) if f in mapping else None
    for i, row in df.iterrows():
        try:
            tid = get(row, "tender_id")
            if not tid:
                job.rows_skipped += 1
                continue
            raw = {k: _clean(v) for k, v in row.to_dict().items()}
            src = m.SourceRecord(import_job_id=job.id, row_number=int(i) + 2 + OFF, raw=raw, row_hash=_row_hash(raw))
            db.add(src); db.flush()
            t = db.query(m.Tender).filter_by(tender_id=tid).first()
            if t:
                job.rows_duplicate += 1
            else:
                t = m.Tender(tender_id=tid, source_record_id=src.id)
                db.add(t)
            row_demo = bool(is_demo) or "sample" in str(raw.get("Data Status") or "").lower()
            t.title = t.title or get(row, "title")
            t.job_number = t.job_number or get(row, "job_number")
            t.department = t.department or get(row, "department")
            t.category = t.category or get(row, "category") or work_category(t.title)
            t.published_date = t.published_date or norm_date(get(row, "published_date"))
            t.closing_date = t.closing_date or norm_date(get(row, "closing_date"))
            t.award_date = t.award_date or norm_date(get(row, "award_date"))
            t.estimated_value = t.estimated_value or norm_amount(get(row, "estimated_value"))
            t.awarded_value = t.awarded_value or norm_amount(get(row, "awarded_value"))
            t.status = t.status or get(row, "status")
            t.url = t.url or get(row, "url")
            t.location_text = t.location_text or get(row, "location")
            t.source_name = t.source_name or source_name
            t.is_demo = row_demo or bool(t.is_demo)
            cname = get(row, "contractor")
            if cname and not t.contractor_id:
                cn = norm_contractor(cname)
                c = db.query(m.Contractor).filter_by(name_norm=cn).first()
                if not c:
                    c = m.Contractor(name=re.sub(r"^m\s*/\s*s\.?\s*", "", cname, flags=re.I), name_norm=cn, is_demo=row_demo)
                    db.add(c); db.flush()
                t.contractor_id = c.id
            job.rows_ok += 1
        except Exception as e:
            job.rows_failed += 1
            errors.append({"row": int(i) + 2 + OFF, "error": str(e)[:300]})
    job.errors = errors[:500]
    job.status = "done" if not job.rows_failed else "done_with_errors"
    db.commit()
    return job
