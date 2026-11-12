import io, re
import pandas as pd
from sqlalchemy.orm import Session
from . import models as m

SYN = {
    "job_number": ["job number", "job no", "job code", "jobnumber"],
    "work_order_id": ["work order id", "work order no", "work order number", "wo no", "wo id", "wo number"],
    "tender_id": ["tender id", "tender no", "nit no", "indent no"],
    "description": ["work description", "name of work", "description", "work name"],
    "ward_number": ["ward number", "ward no", "ward"],
    "ward_name": ["ward name"],
    "contractor": ["contractor name", "contractor", "vendor", "agency"],
    "sanctioned_amount": ["work order amount", "sanctioned amount", "wo amount", "estimate amount"],
    "bill_number": ["bill number", "bill no", "br no"],
    "bill_amount": ["bill amount", "gross amount"],
    "payment_amount": ["paid amount", "payment amount", "amount paid", "net amount"],
    "date": ["work order date", "wo date", "date"],
    "payment_date": ["payment date", "paid date", "br date"],
}
ROAD_RE = re.compile(
    r"\b(road|roads|rd|asphalt|resurfac\w*|pothole\w*|white ?topping|tarr?ing|carriageway|"
    r"cross|main|lane|street|restoration of dug)\b", re.I)


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(h).lower()).strip()


def guess_mapping(columns) -> dict:
    normed = {_norm_header(c): c for c in columns}
    mapping = {}
    for field, syns in SYN.items():
        for s in syns:  # exact match first
            if s in normed:
                mapping[field] = normed[s]
                break
        else:
            for s in syns:  # then containment
                hit = next((orig for n, orig in normed.items() if s in n), None)
                if hit and hit not in mapping.values():
                    mapping[field] = hit
                    break
    return mapping


def norm_amount(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = re.sub(r"[^\d.]", "", str(v))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def norm_date(v):
    if v is None or str(v).strip() == "":
        return None
    d = pd.to_datetime(v, dayfirst=True, errors="coerce")
    return None if pd.isna(d) else d.to_pydatetime()


def norm_text(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())).strip()


def norm_contractor(s) -> str:
    t = norm_text(s)
    t = re.sub(r"^(?:(?:m s|ms|sri|shri)\s+)+", "", t)
    t = re.sub(r"\s+(pvt|private|ltd|limited|and co|co)\b", "", t)
    return t.strip()


def read_table(data: bytes, filename: str) -> pd.DataFrame:
    if filename.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(data), dtype=str)
    return pd.read_csv(io.BytesIO(data), dtype=str, encoding_errors="replace")


def _clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s or None


def import_table(db: Session, data: bytes, filename: str, source_name: str, source_url: str,
                 ward_regime: str, user_id: int | None, mapping: dict | None = None,
                 roads_only: bool = True) -> m.ImportJob:
    df = read_table(data, filename)
    mapping = mapping or guess_mapping(df.columns)
    job = m.ImportJob(filename=filename, kind="table", source_name=source_name, source_url=source_url,
                      ward_regime=ward_regime, column_map=mapping, created_by=user_id,
                      rows_total=len(df), errors=[])
    db.add(job); db.flush()
    errors = []
    if "description" not in mapping:
        errors.append({"row": None, "error": "No description column found. Pass an explicit mapping."})
    get = lambda row, f: _clean(row.get(mapping[f])) if f in mapping else None
    contractors = {}
    for i, row in df.iterrows():
        try:
            raw = {k: _clean(v) for k, v in row.to_dict().items()}
            desc = get(row, "description") or ""
            if roads_only and not ROAD_RE.search(desc):
                job.rows_skipped += 1
                continue
            src = m.SourceRecord(import_job_id=job.id, row_number=int(i) + 2, raw=raw)
            db.add(src); db.flush()
            job_no, wo = get(row, "job_number"), get(row, "work_order_id")
            rw = None
            if job_no:
                rw = db.query(m.Roadwork).filter_by(job_number=job_no).first()
            if not rw and wo:
                rw = db.query(m.Roadwork).filter_by(work_order_id=wo).first()
            if not rw:
                rw = m.Roadwork(job_number=job_no, work_order_id=wo, source_record_id=src.id,
                                source_name=source_name, source_url=source_url)
                db.add(rw)
            rw.tender_id = rw.tender_id or get(row, "tender_id")
            rw.description = rw.description or desc
            rw.description_norm = norm_text(rw.description)
            rw.ward_regime = ward_regime
            rw.ward_number = rw.ward_number or get(row, "ward_number")
            rw.ward_name = rw.ward_name or get(row, "ward_name")
            rw.sanctioned_amount = rw.sanctioned_amount or norm_amount(get(row, "sanctioned_amount"))
            rw.work_order_date = rw.work_order_date or norm_date(get(row, "date"))
            cname = get(row, "contractor")
            if cname:
                cn = norm_contractor(cname)
                c = contractors.get(cn) or db.query(m.Contractor).filter_by(name_norm=cn).first()
                if not c:
                    c = m.Contractor(name=cname, name_norm=cn)
                    db.add(c); db.flush()
                contractors[cn] = c
                rw.contractor_id = c.id
            db.flush()
            bill = None
            bamt = norm_amount(get(row, "bill_amount"))
            if bamt or get(row, "bill_number"):
                bill = m.Bill(roadwork_id=rw.id, bill_number=get(row, "bill_number"), amount=bamt,
                              source_record_id=src.id)
                db.add(bill); db.flush()
            pamt = norm_amount(get(row, "payment_amount"))
            if pamt:
                db.add(m.Payment(roadwork_id=rw.id, bill_id=bill.id if bill else None, amount=pamt,
                                 paid_on=norm_date(get(row, "payment_date")), source_record_id=src.id))
            job.rows_ok += 1
        except Exception as e:  # keep importing, report the row
            job.rows_failed += 1
            errors.append({"row": int(i) + 2, "error": str(e)[:300]})
    job.errors = errors[:500]
    job.status = "done" if not job.rows_failed else "done_with_errors"
    db.commit()
    return job
