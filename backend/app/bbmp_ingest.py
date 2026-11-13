"""Database ingestion of BBMP work-order / payment files into the existing tables.

One published row is one payment (bill) against one work order. For every row the service
  1. validates it (app/bbmp_schema.py) - rows that cannot be stored are reported, never silently dropped;
  2. gives it a stable key (financial year + ward regime + published id + serial) so a re-run finds it again;
  3. finds the municipal work it belongs to by the published work-order code (the same code in a later year's file is the same work),
     or creates the work when none exists;
  4. stores the original row (SourceRecord.raw), what was derived from it (SourceRecord.normalised) and links the two to the work
     through work_source_links, so one work can be backed by many rows;
  5. records the bill and, only when the row says a payment was made, the payment (net amount, RTGS reference and date).

Three kinds of value are kept apart:
  original   SourceRecord.raw - exactly as published, never edited
  normalised SourceRecord.normalised and the Roadwork / Contractor / Bill / Payment columns - derived, and only ever filling empty fields
  verified   Roadwork.verified_fields - fields an officer confirmed or corrected; imports never change them (a differing value is logged)

Repeat runs: a row seen before with the same content is counted as a duplicate and changes nothing; a row seen before whose published
content changed updates its own bill / payment; a payment that reappears in another file (same work, RTGS reference, date and amount)
is stored but marked duplicate_of_id and left out of every total.

Everything for one file happens in one database transaction. Rows that fail validation are reported and skipped; if the share of failed
rows is above BBMP_MAX_FAILED_FRACTION (default 0.5) or the database raises an error, the whole file is rolled back.
"""
import csv, hashlib, io, os, re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models as m
from .bbmp_schema import map_columns, missing_required, normalise_row, row_hash, fiscal_label
from .ingest import read_table, _decode
from .sources import link_source
from .textnorm import norm_text, norm_contractor, work_category, road_names_in_text

ORIGIN = "bbmp_open_data"
SOURCE_NAME = "BBMP work orders and payments (OpenCity open data)"
MAX_FAILED_FRACTION = float(os.getenv("BBMP_MAX_FAILED_FRACTION", "0.5"))
WARNING_CAP = 2000  # stored warnings per file; the total is still counted


class ImportAborted(Exception):
    """The file could not be imported and nothing was stored."""


class SchemaError(ImportAborted):
    pass


@dataclass
class FileContext:
    fiscal_year: str
    ward_regime: str
    filename: str = ""
    file_format: str | None = None
    dataset_slug: str = ""
    dataset_title: str = ""
    dataset_url: str = ""
    download_url: str = ""
    source_name: str = SOURCE_NAME
    overrides: dict = field(default_factory=dict)
    source_file_id: int | None = None


@dataclass
class Counts:
    processed: int = 0
    inserted: int = 0
    updated: int = 0
    duplicate: int = 0
    failed: int = 0
    unmatched: int = 0
    warnings: int = 0
    works_created: int = 0
    works_matched: int = 0
    payments_created: int = 0
    payments_duplicate: int = 0
    contractors_created: int = 0


# ----------------------------------------------------------------------------- reading files
def _split_lines(text: str):
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _repair_line(line: str, ncols: int, free_idx: int | None):
    """A line whose quotes are unbalanced is split by position instead: the columns before the free-text column and after it are
    plain (no commas), everything in between is the free-text column. Returns the fields or None."""
    if free_idx is None or ncols < 3:
        return None
    left, right = free_idx, ncols - free_idx - 1
    parts = line.split(",", left)
    if len(parts) < left + 1:
        return None
    head, rest = parts[:left], parts[left]
    tail = rest.rsplit(",", right) if right else [rest]
    if len(tail) < right + 1:
        return None
    middle, tail = tail[0], tail[1:]
    return [p.strip() for p in head] + [middle.strip().strip('"').strip()] + [p.strip().strip('"') for p in tail]


def read_csv_rows(text: str, free_text_header_hint=("wodetails", "wo details", "description", "work description")):
    """-> (columns, [(line_number, {header: value}, repaired)], [issues]). Uses the csv module; a record with the wrong number of fields
    (an unbalanced quote swallowing the next lines) is re-read line by line so one bad quote does not hide its neighbours."""
    lines = _split_lines(text)
    reader = csv.reader(io.StringIO("\n".join(lines)), strict=False)
    try:
        header = next(reader)
    except StopIteration:
        raise SchemaError("The file is empty")
    columns, seen = [], {}
    for j, h in enumerate(header):
        name = h.strip().lstrip("﻿") or f"Unnamed: {j}"
        seen[name] = seen.get(name, 0) + 1
        columns.append(name if seen[name] == 1 else f"{name}.{seen[name] - 1}")
    n = len(columns)
    free_idx = next((i for i, c in enumerate(columns) if re.sub(r"[^a-z0-9]+", " ", c.lower()).strip() in free_text_header_hint), None)
    rows, issues = [], []
    prev = reader.line_num
    for rec in reader:
        start, end = prev + 1, reader.line_num
        prev = end
        if not any(c.strip() for c in rec):
            continue
        if len(rec) == n:
            rows.append((start, dict(zip(columns, [c.strip() for c in rec])), False))
            continue
        # wrong width: look at every physical line the record covers
        for ln in range(start, end + 1):
            line = lines[ln - 1] if ln - 1 < len(lines) else ""
            if not line.strip():
                continue
            try:
                alone = next(csv.reader([line], strict=False))
            except (StopIteration, csv.Error):
                alone = []
            if len(alone) == n:  # fine on its own: only a neighbour's quote had swallowed it
                rows.append((ln, dict(zip(columns, [c.strip() for c in alone])), False))
                continue
            fixed = _repair_line(line, n, free_idx)
            if fixed and len(fixed) == n:
                rows.append((ln, dict(zip(columns, fixed)), True))
            else:
                issues.append({"row": ln, "code": "malformed_row", "message": f"expected {n} fields, found {len(rec)}; the line could not be split reliably", "raw": {"line": line[:400]}})
    return columns, rows, issues


def read_rows(data: bytes, filename: str, file_format: str | None = None):
    """-> (columns, [(row_number, {header: text}, repaired)], read_issues). CSV through the tolerant reader, Excel through read_table."""
    fmt = file_format or ("xlsx" if data[:4] == b"PK\x03\x04" else "xls" if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" else "csv")
    if fmt == "csv":
        return read_csv_rows(_decode(data))
    try:
        df = read_table(data, filename)
    except ValueError as e:
        raise SchemaError(str(e))
    off = df.attrs.get("row_offset", 0)
    cols = [str(c) for c in df.columns]
    rows = []
    for i, rec in enumerate(df.itertuples(index=False)):
        vals = {c: (None if (v is None or (isinstance(v, float) and v != v)) else str(v).strip()) for c, v in zip(cols, rec)}
        if any(v for v in vals.values()):
            rows.append((i + 2 + off, vals, False))
    return cols, rows, []


# ----------------------------------------------------------------------------- lookups
class _Cache:
    def __init__(self, db: Session):
        self.db = db
        self.works: dict[str, m.Roadwork] = {}
        self.by_code: dict[tuple, m.Roadwork | None] = {}
        self.contractors_code: dict[str, m.Contractor] = {}
        self.contractors_name: dict[str, m.Contractor] = {}

    def contractor(self, name, code, counts):
        if not name and not code:
            return None
        c = None
        if code:
            c = self.contractors_code.get(code) or self.db.query(m.Contractor).filter_by(vendor_code=code).first()
        nn = norm_contractor(name) if name else None
        if c is None and nn:
            c = self.contractors_name.get(nn) or self.db.query(m.Contractor).filter_by(name_norm=nn).first()
        if c is None:
            if not name:
                return None
            c = m.Contractor(name=re.sub(r"^m\s*/\s*s\.?\s*", "", name, flags=re.I)[:300], name_norm=(nn or "")[:300], vendor_code=code, is_demo=False)
            self.db.add(c); self.db.flush()
            counts.contractors_created += 1
        else:
            if code and not c.vendor_code:
                c.vendor_code = code
            if name and c.name and len(name) > len(c.name) and norm_text(name).startswith(norm_text(c.name)):
                c.name = name[:300]  # a fuller spelling of a name that was cut off earlier
        if code:
            self.contractors_code[code] = c
        if nn:
            self.contractors_name[nn] = c
        return c


def _is_verified(w: m.Roadwork, name: str) -> bool:
    return name in (w.verified_fields or [])


# ----------------------------------------------------------------------------- the import
def import_rows(db: Session, data: bytes, ctx: FileContext, user_id: int | None = None, run_id: int | None = None) -> tuple[m.ImportJob, Counts]:
    """Import one file. Commits on success. On any failure everything stored for this file is rolled back and the exception is raised."""
    try:
        return _import(db, data, ctx, user_id)
    except Exception:
        db.rollback()
        raise


def _import(db, data, ctx: FileContext, user_id):
    fy = fiscal_label(ctx.fiscal_year)
    if not fy:
        raise SchemaError(f"The financial year {ctx.fiscal_year!r} is not readable (expected something like 2024-25).")
    regime = (ctx.ward_regime or "").strip()
    if not regime:
        raise SchemaError("The ward regime (198, 225, 243, common ...) is not known for this file. Ward numbers mean different things in different regimes, so it must be stated.")
    columns, rows, read_issues = read_rows(data, ctx.filename, ctx.file_format)
    mapping, unmapped = map_columns(columns, ctx.overrides)
    miss = missing_required(mapping)
    if miss:
        raise SchemaError(f"Columns not recognised: need {', '.join(miss)}. Found: {', '.join(columns)[:300]}. Add a column override for this dataset.")
    free_unmapped = [c for c in unmapped]

    job = m.ImportJob(filename=ctx.filename[:300], kind=ORIGIN, source_name=ctx.source_name[:200], source_url=(ctx.dataset_url or ctx.download_url)[:500],
                      ward_regime=regime[:10], fiscal_year=fy, column_map=mapping, is_demo=False, created_by=user_id,
                      rows_total=len(rows) + len(read_issues), errors=[], rows_duplicate=0,
                      stats={"profile": "bbmp_work_orders_payments", "unmapped_columns": free_unmapped, "dataset": ctx.dataset_slug})
    db.add(job); db.flush()
    counts = Counts()
    issues: list[m.ImportIssue] = []
    stored_warnings = 0

    def issue(kind, code, message, row, raw=None, src_id=None):
        nonlocal stored_warnings
        if kind == "warning":
            counts.warnings += 1
            if stored_warnings >= WARNING_CAP:
                return
            stored_warnings += 1
        issues.append(m.ImportIssue(import_job_id=job.id, source_file_id=ctx.source_file_id, row_number=row, kind=kind, code=code[:40],
                                    message=message[:400], raw=raw, source_record_id=src_id))

    for ri in read_issues:
        counts.processed += 1; counts.failed += 1
        issue("failed", ri["code"], ri["message"], ri["row"], ri["raw"])

    prefix = f"bbmp|{fy}|{regime}|"
    existing = {e: (i, h) for i, e, h in db.query(m.SourceRecord.id, m.SourceRecord.external_id, m.SourceRecord.row_hash)
                .filter(m.SourceRecord.external_id.like(prefix + "%"))}
    seen_in_file: dict[str, dict[str, str]] = {}
    cache = _Cache(db)
    conflicts_logged = set()
    batch = 0

    for number, raw, repaired in rows:
        counts.processed += 1
        res = normalise_row(raw, mapping, fiscal_year=fy)
        if not res.ok:
            counts.failed += 1
            issue("failed", res.errors[0][0], "; ".join(msg for _, msg in res.errors), number, raw)
            continue
        h = row_hash(raw)
        v = res.values
        base = prefix + f"{v['source_id'] or 'h' + h[:16]}|{v['serial'] or ''}"
        same = seen_in_file.setdefault(base, {})
        if h in same:
            counts.duplicate += 1
            issue("duplicate", "repeated_row_in_file", f"Identical to row {same[h]['row']} of this file (same id, serial and every column); counted once.", number, raw)
            continue
        ext = base if not same else f"{base}#{len(same) + 1}"
        same[h] = {"ext": ext, "row": number}
        if repaired:
            res.flags.append(("row_repaired", "Quotes in this line were unbalanced; the fields were split by position."))

        prior = existing.get(ext)
        if prior and prior[1] == h:
            counts.duplicate += 1  # counted, but not written as an issue row: an unchanged re-run should not grow the issue list
            continue

        ncode = v["work_order_code"]
        if ncode:
            wkey = f"bbmp:{regime}:{ncode}"
        elif v["job_number"]:
            wkey = f"bbmp:{regime}:job:{v['job_number']}"
        elif v["description"] and len(v["description"]) >= 8:
            fp = hashlib.sha1(f"{v['ward']}|{norm_text(v['description'])}|{norm_contractor(v['contractor_name'])}".encode()).hexdigest()[:16]
            wkey = f"bbmp:{regime}:fp:{fp}"
        else:
            wkey = None

        norm_json = {k: (val.isoformat() if isinstance(val, datetime) else val) for k, val in v.items()}
        norm_json.update({"work_key": wkey, "flags": [c for c, _ in res.flags], "missing": res.missing, "financial_year": fy, "ward_regime": regime})

        if prior:  # published content changed since the last import: refresh this row's own record, bill and payment
            src = db.get(m.SourceRecord, prior[0])
            old_hash = src.row_hash
            src.raw, src.row_hash, src.normalised, src.row_number = raw, h, norm_json, number
            counts.updated += 1
            issue("updated", "row_changed", f"Published content changed (was {old_hash[:10]}, now {h[:10]}); this row's bill and payment were refreshed.", number, None, src.id)
        else:
            src = m.SourceRecord(import_job_id=job.id, row_number=number, raw=raw, row_hash=h, external_id=ext, source_file_id=ctx.source_file_id,
                                 fiscal_year=fy, normalised=norm_json)
            db.add(src); db.flush()
            existing[ext] = (src.id, h)
            counts.inserted += 1
        for code, msg in res.flags:
            issue("warning", code, msg, number, None, src.id)

        if wkey is None:  # valid row, but nothing says which work it belongs to: keep the record, link nothing, list it as unmatched
            counts.unmatched += 1
            issue("warning", "unmatched", "No work-order code, job number or description: stored without a work.", number, None, src.id)
            continue

        work, created = _find_or_create_work(db, cache, wkey, v, regime, fy, ctx, counts)
        verified = set(work.verified_fields or [])
        _fill_work(db, work, v, regime, fy, ctx, cache, counts, verified, issue, number, src.id, conflicts_logged)
        db.flush()
        has_pay = bool(v["has_payment"])
        link = link_source(db, work.id, src.id, "payment_row" if has_pay else "works_row", "primary" if created else "merged_row",
                           "created_from_row" if created else "exact_work_order_code" if ncode else "bbmp_key", 1.0 if created else (0.98 if ncode else 0.8),
                           {"work_key": wkey, "work_order_code": ncode, "file": ctx.filename, "row": number, "financial_year": fy, "ward_regime": regime},
                           "auto" if (created or ncode) else "suggested")
        _write_bill_and_payment(db, work, src, v, ext, counts)
        batch += 1
        if batch % 500 == 0:
            db.flush()

    done = counts.processed
    if done >= 20 and counts.failed / done > MAX_FAILED_FRACTION:
        raise ImportAborted(f"{counts.failed} of {done} rows failed validation (limit {int(MAX_FAILED_FRACTION * 100)}%). The file does not look like the expected layout; nothing was stored.")

    db.add_all(issues)
    job.rows_ok = counts.inserted + counts.updated
    job.rows_duplicate = counts.duplicate
    job.rows_failed = counts.failed
    job.rows_skipped = 0
    job.errors = [{"row": i.row_number, "error": f"{i.code}: {i.message}"} for i in issues if i.kind == "failed"][:200]
    job.status = "done" if not counts.failed else "done_with_errors"
    job.stats = {**(job.stats or {}), **counts.__dict__}
    db.commit()
    return job, counts


def _find_or_create_work(db, cache: _Cache, wkey, v, regime, fy, ctx, counts):
    w = cache.works.get(wkey) or db.query(m.Roadwork).filter(m.Roadwork.external_key == wkey).first()
    if w is None and v["work_order_code"]:
        # a work that exists from another source (earlier upload, PDF) with this published work-order code in this regime
        w = db.query(m.Roadwork).filter(m.Roadwork.work_order_id == v["work_order_code"], m.Roadwork.ward_regime == regime,
                                        m.Roadwork.external_key.is_(None), m.Roadwork.verification_status != "merged").first()
        if w is not None:
            w.external_key = wkey
            if w.verification_status == "provisional":
                w.verification_status = "official"
    if w is None and v["job_number"]:
        w = db.query(m.Roadwork).filter(m.Roadwork.job_number == v["job_number"], m.Roadwork.external_key.is_(None),
                                        m.Roadwork.verification_status != "merged").first()
        if w is not None:
            w.external_key = wkey
    if w is not None:
        cache.works[wkey] = w
        counts.works_matched += 1
        return w, False
    w = m.Roadwork(external_key=wkey, data_origin=ORIGIN, is_demo=False, verification_status="official", source_name=ctx.source_name[:200],
                   source_url=(ctx.dataset_url or ctx.download_url)[:500], work_order_id=v["work_order_code"], ward_regime=regime[:10], fiscal_year=fy)
    db.add(w); db.flush()
    cache.works[wkey] = w
    counts.works_created += 1
    return w, True


def _fill_work(db, w, v, regime, fy, ctx, cache, counts, verified, issue, row, src_id, logged):
    """Fill empty fields from this row. A field that already holds a different value is left alone (and logged when it matters);
    a field an officer verified is never touched."""
    def put(name, value, log_conflict=False):
        if value in (None, ""):
            return
        cur = getattr(w, name)
        if name in verified:
            if cur != value and (w.id, name, str(value)) not in logged:
                logged.add((w.id, name, str(value)))
                issue("conflict", "verified_field_kept", f"{name}: the imported value {str(value)[:80]!r} differs from the officer-verified {str(cur)[:80]!r}; the verified value was kept.", row, None, src_id)
            return
        if cur in (None, ""):
            setattr(w, name, value)
        elif log_conflict and cur != value and (w.id, name, str(value)) not in logged:
            logged.add((w.id, name, str(value)))
            issue("conflict", f"{name}_differs", f"{name}: this row says {str(value)[:80]!r}, the work already has {str(cur)[:80]!r}; the existing value was kept.", row, None, src_id)

    desc = v["description"]
    put("description", desc)
    if w.description and not w.description_norm:
        w.description_norm = norm_text(w.description)
    ward = v["ward"]
    put("ward_regime", regime[:10])
    put("ward_number", str(ward)[:20] if ward else None, log_conflict=True)
    if w.ward_number and w.ward_number.isdigit():
        w.ward_is_multi = int(w.ward_number) > 300
    put("work_order_id", v["work_order_code"])
    put("job_number", v["job_number"])
    put("department", v["department"])
    put("location_text", (v["location"] or "")[:300] or None)
    road = v["road_name"] or (road_names_in_text(w.description or "", limit=1) or [None])[0]
    put("road_name_extracted", road)
    if w.description and not w.category:
        w.category = work_category(w.description)
    put("fiscal_year", fy)
    put("estimated_cost", v["estimated_amount"])
    put("sanctioned_amount", v["approved_amount"])
    put("awarded_amount", v["approved_amount"])
    put("work_order_date", v["work_order_date"])
    c = cache.contractor(v["contractor_name"], v["contractor_id"], counts)
    if c is not None:
        if w.contractor_id is None and "contractor_id" not in verified:
            w.contractor_id = c.id
        elif w.contractor_id not in (None, c.id) and (w.id, "contractor", c.id) not in logged:
            logged.add((w.id, "contractor", c.id))
            issue("conflict", "contractor_differs", f"This row names contractor {c.name!r}, the work is recorded under another contractor; the existing one was kept. Both rows stay linked.", row, None, src_id)
    w.source_name = w.source_name or ctx.source_name[:200]


def _write_bill_and_payment(db, w, src, v, ext, counts):
    bill = db.query(m.Bill).filter(m.Bill.external_key == ext).first()
    if bill is None and (v["bill_number"] or v["gross_amount"] is not None):
        bill = m.Bill(roadwork_id=w.id, external_key=ext, source_record_id=src.id, stage="bill")
        db.add(bill)
    if bill is not None:
        bill.roadwork_id, bill.bill_number, bill.amount = w.id, v["bill_number"], v["gross_amount"]
        bill.bill_date, bill.deduction, bill.net_amount = v["bill_date"], v["deduction"], v["net_amount"]
        db.flush()
    pay = db.query(m.Payment).filter(m.Payment.external_key == ext).first()
    if not v["has_payment"]:
        return
    ref = f"Rtgs {v['payment_reference']}" if v["payment_reference"] else None
    amount, paid_on = v["paid_amount"], v["payment_date"]
    if pay is None:
        pay = m.Payment(external_key=ext, source_record_id=src.id)
        db.add(pay)
        counts.payments_created += 1
        new = True
    else:
        new = False
    pay.roadwork_id, pay.bill_id = w.id, bill.id if bill else None
    pay.amount, pay.gross_amount, pay.deduction = amount, v["gross_amount"], v["deduction"]
    pay.paid_on, pay.reference = paid_on, ref
    db.flush()
    if new and ref:
        # the same payment published again elsewhere (another file / year): same work, RTGS reference, date and amount
        q = db.query(m.Payment).filter(m.Payment.roadwork_id == w.id, m.Payment.reference == ref, m.Payment.id != pay.id,
                                       m.Payment.duplicate_of_id.is_(None), func.abs(m.Payment.amount - amount) < 0.5)
        for other in q.order_by(m.Payment.id):
            if paid_on is None or other.paid_on is None or other.paid_on.date() == paid_on.date():
                if other.source_record_id != src.id:
                    pay.duplicate_of_id = other.id
                    counts.payments_duplicate += 1
                    db.add(m.ImportIssue(import_job_id=src.import_job_id, source_file_id=src.source_file_id, row_number=src.row_number, kind="duplicate",
                                         code="payment_already_recorded", message=f"Payment {ref} of {amount:,.0f}{' on ' + paid_on.strftime('%d-%b-%Y') if paid_on else ''} for this work is already recorded (payment {other.id}); stored but not counted.",
                                         source_record_id=src.id))
                    break
