"""Source-record bookkeeping for municipal works.

work_source_links (many-to-many)
    One work can be backed by many original records: the BBMP row that created it, later rows that merged into it
    (work orders, payments), and KPPP tender rows linked to it. Each link keeps how it was made (method), how sure we
    are (confidence), the identifiers behind it (evidence) and whether a person checked it (status).
    Source records are never edited or merged; the link only says "this record is about that work".

Payments
    A payment can reach the database twice: once from an imported CSV and once from a payment-advice PDF. Both rows are
    kept (so provenance is complete) but the later one points at the first through `duplicate_of_id` and is left out of
    every total. `counted_payments()` is the single place that rule lives.
"""
from datetime import datetime
from sqlalchemy import func
from sqlalchemy.orm import Session
from . import models as m

LINK_STATUSES = ("auto", "suggested", "verified", "rejected")


# ----------------------------------------------------------------------------- source links
def link_source(db: Session, work_id: int, source_record_id: int | None, source_type: str, role: str, method: str,
                confidence: float, evidence: dict | None = None, status: str = "auto"):
    """Attach one source record to a work. Idempotent: an existing link is kept (a human decision is never overwritten)."""
    if not work_id or not source_record_id:
        return None
    ex = db.query(m.WorkSourceLink).filter_by(roadwork_id=work_id, source_record_id=source_record_id).first()
    if ex:
        return ex
    link = m.WorkSourceLink(roadwork_id=work_id, source_record_id=source_record_id, source_type=source_type, role=role,
                            match_method=method, confidence=confidence, evidence=evidence or {}, status=status)
    db.add(link)
    db.flush()
    return link


def sync_tender_link(db: Session, rl: m.RecordLink):
    """Mirror a roadwork<->tender RecordLink into work_source_links (the tender's source row backs the work)."""
    if rl.a_type != "roadwork" or rl.b_type != "tender":
        return None
    t = db.get(m.Tender, rl.b_id)
    if not t or not t.source_record_id:
        return None
    ex = db.query(m.WorkSourceLink).filter_by(roadwork_id=rl.a_id, source_record_id=t.source_record_id).first()
    status = rl.status if rl.status in LINK_STATUSES else "suggested"
    if ex:
        if ex.role == "tender":  # follow the match decision; rows created another way keep their own status
            ex.match_method, ex.confidence, ex.status = rl.method, rl.confidence, status
            ex.evidence = {**(ex.evidence or {}), **(rl.evidence or {}), "tender_id": t.tender_id}
            ex.reviewer_id, ex.reviewed_at = rl.reviewer_id, rl.reviewed_at
        return ex
    return link_source(db, rl.a_id, t.source_record_id, "tender_row", "tender", rl.method or "tender_link", rl.confidence or 0.0,
                       {**(rl.evidence or {}), "tender_id": t.tender_id}, status)


def sync_all_tender_links(db: Session):
    n = 0
    for rl in db.query(m.RecordLink).filter(m.RecordLink.a_type == "roadwork", m.RecordLink.b_type == "tender"):
        if sync_tender_link(db, rl):
            n += 1
    return n


def review_source_link(db: Session, link: m.WorkSourceLink, status: str, user):
    """verified / rejected / suggested (reopen). The link row and the source record both stay in the database."""
    link.status = status
    link.reviewer_id, link.reviewed_at = (user.id if user else None), datetime.utcnow()
    return link


def work_sources(db: Session, roadwork_id: int, include_rejected: bool = True):
    """Every source record behind a work, with the import it came from and the raw original row."""
    q = db.query(m.WorkSourceLink, m.SourceRecord, m.ImportJob, m.SourceFile) \
        .join(m.SourceRecord, m.SourceRecord.id == m.WorkSourceLink.source_record_id) \
        .outerjoin(m.ImportJob, m.ImportJob.id == m.SourceRecord.import_job_id) \
        .outerjoin(m.SourceFile, m.SourceFile.id == m.SourceRecord.source_file_id) \
        .filter(m.WorkSourceLink.roadwork_id == roadwork_id)
    if not include_rejected:
        q = q.filter(m.WorkSourceLink.status != "rejected")
    out = []
    for l, s, j, sfile in q.order_by(m.WorkSourceLink.id):
        out.append({"link_id": l.id, "source_record_id": s.id, "source_type": l.source_type, "role": l.role, "method": l.match_method,
                    "confidence": l.confidence, "evidence": l.evidence, "status": l.status, "reviewed_at": l.reviewed_at,
                    "row": s.row_number, "raw": s.raw, "normalised": s.normalised, "external_id": s.external_id, "fiscal_year": s.fiscal_year,
                    "file": ({"id": sfile.id, "filename": sfile.original_filename, "dataset": sfile.dataset_title, "dataset_url": sfile.dataset_url,
                              "download_url": sfile.download_url, "fiscal_year": sfile.fiscal_year, "ward_regime": sfile.ward_regime,
                              "downloaded_at": sfile.downloaded_at, "sha256": sfile.sha256} if sfile else None),
                    "import": ({"id": j.id, "file": j.filename, "kind": j.kind, "source_name": j.source_name, "source_url": j.source_url,
                                "imported_at": j.created_at, "sample": j.is_demo} if j else None)})
    return out


# ----------------------------------------------------------------------------- payments
def counted_payments(db: Session, *cols):
    """Query over payments that count toward totals: a payment recorded twice is counted once."""
    return db.query(*(cols or (m.Payment,))).filter(m.Payment.duplicate_of_id.is_(None))


def _day(d):
    return d.date() if isinstance(d, datetime) else d


def find_duplicate_payment(db: Session, roadwork_id: int, amount: float, paid_on, source_record_id=None, document_id=None,
                           import_job_id=None, exclude_id=None):
    """The earlier payment this one repeats, or None.

    A repeat means: same work, same amount, same payment date, and it arrives from a *different source* (another file,
    another import, or a document instead of a CSV row). Two equal lines inside one file are separate payments."""
    if not amount or paid_on is None:  # without a payment date we cannot tell a repeat from a second payment: count both
        return None
    q = counted_payments(db).filter(m.Payment.roadwork_id == roadwork_id, func.abs(m.Payment.amount - amount) < 0.005)
    if exclude_id:
        q = q.filter(m.Payment.id != exclude_id)
    for p in q.order_by(m.Payment.id):
        if _day(p.paid_on) != _day(paid_on):
            continue
        same_row = source_record_id is not None and p.source_record_id == source_record_id
        same_doc = document_id is not None and p.document_id == document_id
        if same_row or same_doc:
            continue
        if import_job_id is not None and p.source_record_id:
            sr = db.get(m.SourceRecord, p.source_record_id)
            if sr and sr.import_job_id == import_job_id:
                continue  # two lines of the same file
        return p
    return None


def add_payment(db: Session, roadwork_id: int, amount: float, paid_on=None, bill_id=None, reference=None,
                source_record_id=None, document_id=None, import_job_id=None):
    """Create a payment row, marking it as a repeat of an earlier one when the evidence says so."""
    dup = find_duplicate_payment(db, roadwork_id, amount, paid_on, source_record_id, document_id, import_job_id)
    p = m.Payment(roadwork_id=roadwork_id, bill_id=bill_id, amount=amount, paid_on=paid_on, reference=reference,
                  source_record_id=source_record_id, document_id=document_id, duplicate_of_id=dup.id if dup else None)
    db.add(p)
    db.flush()
    return p
