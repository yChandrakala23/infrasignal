"""PDF-first work profiles.

A document does not need an existing municipal work record. After extraction and matching, a document that matched
nothing can start a *provisional* work profile:

    verification_status = 'provisional'   created from a document, needs an officer to check it
    verification_status = 'confirmed'     an officer checked it
    verification_status = 'merged'        folded into another work (kept for history, hidden from lists)

Rules
  * Every value on the profile comes from the document that created it (document id + page stay in the extractions).
  * Nothing is created when the document matched, or may match, an existing work: exact identifiers link, fuzzy
    candidates stay suggestions. Only an officer can force a profile for a document that has candidates.
  * Duplicates are prevented by a key: the strongest identifier (job no > tender ID > work order > work/contract ID),
    or - when there is no identifier - a fingerprint of description + contractor/road + amount. A second document with the
    same key links to the existing profile (identifiers) or is suggested to it (fingerprint); it never creates another.
  * Provisional works are kept out of the default lists and money totals until confirmed (see routers/gov.py filters).
"""
import hashlib
import re
from sqlalchemy import or_
from sqlalchemy.orm import Session
from . import models as m
from .matching import (WorkIndex, doc_values, _first, apply_document_to_work, upsert_link, log_event)
from .textnorm import (norm_id, norm_text, norm_contractor, norm_road_name, parse_amount, parse_date, work_category, fiscal_year,
                       road_names_in_text)

WORK_DEFINING = {"nit", "work_order", "loa", "agreement", "admin_approval", "technical_sanction", "boq"}
ID_FIELDS = (("job_number", "job"), ("tender_id", "tender"), ("work_order_id", "wo"), ("work_id", "work"))
MIN_ID_CONF = 0.6
LOW_CONF = 0.75


def identifiers(vals):
    """{field: normalised id} for the identifiers the document states with reasonable confidence."""
    out = {}
    for field, _ in ID_FIELDS:
        v = _first(vals, field, MIN_ID_CONF)
        if v and norm_id(v["norm"]):
            out[field] = v["norm"]
    return out


def fingerprint(vals):
    """Stable key for a document with no identifier: needs a description plus a contractor, a road/location or an amount."""
    d = _first(vals, "work_description", 0.5) or _first(vals, "description", 0.5)
    if not d:
        return None
    parts = [norm_text(d["value"])]
    c = _first(vals, "contractor", 0.5)
    r = _first(vals, "road_name", 0.5) or _first(vals, "location", 0.5)
    a = _first(vals, "awarded_amount", 0.5) or _first(vals, "estimated_cost", 0.5)
    extra = [norm_contractor(c["value"]) if c else None, norm_road_name(r["value"]) if r else None, (a or {}).get("norm")]
    if not any(extra):
        return None
    parts += [str(x) for x in extra if x]
    return "fp:" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]


def key_for(vals):
    """-> (key, kind) or (None, reason)."""
    ids = identifiers(vals)
    for field, tag in ID_FIELDS:
        if field in ids:
            return f"{tag}:{norm_id(ids[field])}", tag
    fp = fingerprint(vals)
    if fp:
        return fp, "fingerprint"
    return None, "no identifier and not enough description to recognise the same work again"


def _contractor(db, name, is_demo):
    cn = norm_contractor(name)
    if not cn:
        return None
    c = db.query(m.Contractor).filter_by(name_norm=cn).first()
    if not c:
        c = m.Contractor(name=re.sub(r"^m\s*/\s*s\.?\s*", "", name.strip(), flags=re.I), name_norm=cn, is_demo=is_demo)
        db.add(c)
        db.flush()
    return c


def create_from_document(db: Session, doc: m.Document, user=None, force=False, idx: WorkIndex | None = None):
    """-> (work | None, info). `force=True` is an officer's explicit request (allowed even when candidates exist)."""
    info = {"created": False}
    if doc.roadwork_id:
        return db.get(m.Roadwork, doc.roadwork_id), {**info, "reason": "document is already linked to a work"}
    if doc.doc_type in (None, "other"):
        return None, {**info, "reason": "document type could not be recognised, so no work profile is started from it"}
    vals = doc_values(db, doc)
    key, kind = key_for(vals)
    if not key:
        return None, {**info, "reason": kind}
    if not force:
        if doc.match_status != "unmatched":
            return None, {**info, "reason": "document has candidate works to review; not creating a new profile"}
        if kind == "fingerprint" and doc.doc_type not in WORK_DEFINING:
            return None, {**info, "reason": "no identifier, and this type of document does not describe a whole work"}
    idx = idx or WorkIndex(db)
    ids_in_doc = identifiers(vals)
    # --- duplicate prevention
    existing = db.query(m.Roadwork).filter(m.Roadwork.provisional_key == key, m.Roadwork.verification_status != "merged").first()
    if not existing:
        for field, _ in ID_FIELDS:
            hit = idx.by_field.get(field, {}).get(norm_id(ids_in_doc[field])) if field in ids_in_doc else None
            if hit:
                existing = idx.works[hit[0]]
                break
    if existing:
        link, _ = upsert_link(db, "document", doc.id, "roadwork", existing.id, "provisional_" + kind, 0.7 if kind == "fingerprint" else 0.9,
                              {"note": "same key as an existing work; not creating a duplicate profile", "key": key},
                              "suggested" if kind == "fingerprint" else "auto")
        if link.status in ("auto", "verified"):
            doc.roadwork_id, doc.match_status = existing.id, "auto"
            apply_document_to_work(db, doc, existing, vals, user=user)
        else:
            doc.match_status = "needs_review"
        log_event(db, None, "document", doc.id, "provisional_dup_prevented", {"roadwork_id": existing.id, "key": key}, link=link, user=user)
        return existing, {**info, "duplicate_prevented": True, "roadwork_id": existing.id, "key": key}
    # --- create
    g = lambda f, mc=0.5: _first(vals, f, mc)
    ids = identifiers(vals)
    desc = (g("work_description") or g("description") or {}).get("value")
    w = m.Roadwork(job_number=ids.get("job_number"), tender_id=ids.get("tender_id"), work_order_id=ids.get("work_order_id"), work_id=ids.get("work_id"),
                   description=(desc or "")[:2000] or None, description_norm=norm_text(desc) if desc else None,
                   verification_status="provisional", data_origin="document", created_from_document_id=doc.id, provisional_key=key,
                   source_name=doc.source_name or f"Document {doc.filename}", source_url=doc.source_page_url or doc.download_url,
                   is_demo=bool(doc.is_demo), work_status="not_stated")
    cv = g("contractor")
    if cv:
        c = _contractor(db, cv["value"], bool(doc.is_demo))
        w.contractor_id = c.id if c else None
    wv = g("ward")
    if wv and wv.get("norm"):
        w.ward_number = str(wv["norm"]).strip()[:20]  # the regime (243 / 198 / 369) is not stated in a PDF, so it stays empty
        w.ward_is_multi = bool(w.ward_number.isdigit() and int(w.ward_number) > 300)
    names = road_names_in_text(w.description or "", limit=1) if w.description else []
    w.road_name_extracted = names[0] if names else None
    w.category = work_category(w.description or "") if w.description else None
    db.add(w)
    db.flush()
    doc.roadwork_id, doc.match_status = w.id, "provisional"
    apply_document_to_work(db, doc, w, vals, user=user)
    fy_date = w.work_order_date or doc.doc_date
    if fy_date and not w.fiscal_year:
        w.fiscal_year = fiscal_year(fy_date)
    link = m.RecordLink(a_type="document", a_id=doc.id, b_type="roadwork", b_id=w.id, method="created_from_document", confidence=1.0, status="auto",
                        evidence={"key": key, "key_kind": kind, "note": "This work profile was started from this document and still needs verification."})
    db.add(link)
    db.flush()
    log_event(db, None, "document", doc.id, "provisional_created", {"roadwork_id": w.id, "key": key, "key_kind": kind}, link=link, user=user)
    idx.works[w.id] = w
    for field, _ in ID_FIELDS:
        v = getattr(w, field)
        if v:
            idx.by_field[field].setdefault(norm_id(v), []).append(w.id)
    return w, {**info, "created": True, "roadwork_id": w.id, "key": key, "key_kind": kind, "reasons": reasons(db, w)}


# ----------------------------------------------------------------------------- why a person should look at it
def reasons(db: Session, w: m.Roadwork):
    """Plain-language reasons a provisional profile needs checking."""
    out = []
    doc = db.get(m.Document, w.created_from_document_id) if w.created_from_document_id else None
    if not any((w.job_number, w.tender_id, w.work_order_id, w.work_id)):
        out.append("The document gives no job number, tender ID or work/contract ID, so the profile is recognised only by its description.")
    if doc:
        low = db.query(m.Extraction).filter(m.Extraction.document_id == doc.id, m.Extraction.status != "rejected",
                                            m.Extraction.confidence < LOW_CONF, m.Extraction.status == "unverified",
                                            m.Extraction.field.in_(["job_number", "tender_id", "work_order_id", "work_id", "work_description",
                                                                    "contractor", "awarded_amount", "estimated_cost"])).count()
        if low:
            out.append(f"{low} key value(s) were read with low confidence.")
        if doc.ocr_pages:
            out.append("The document was read with OCR, so numbers and names may contain recognition errors.")
        flagged = db.query(m.DocumentPage).filter_by(document_id=doc.id, needs_review=True).count()
        if flagged:
            out.append(f"{flagged} page(s) of the source document are flagged for manual correction (low OCR confidence or other reading problems).")
    if w.ward_number and not w.ward_regime:
        out.append("A ward number is stated but not which ward map (243 / 198 / 369) it belongs to.")
    out.append("Only one document supports this profile; no imported government record has been matched to it.")
    return out


def provisional_fields(db: Session, w: m.Roadwork):
    """For each filled value: which document, page and text it came from."""
    if not w.created_from_document_id:
        return []
    pairs = [("job_number", "job_number"), ("tender_id", "tender_id"), ("work_order_id", "work_order_id"), ("work_id", "work_id"),
             ("description", "work_description"), ("contractor", "contractor"), ("ward_number", "ward"), ("estimated_cost", "estimated_cost"),
             ("awarded_amount", "awarded_amount"), ("work_order_date", "work_order_date"), ("commencement_date", "commencement_date"),
             ("completion_deadline", "completion_deadline"), ("location_text", "location"), ("department", "department")]
    out = []
    for attr, field in pairs:
        if attr == "contractor":
            has = w.contractor_id is not None
        else:
            has = getattr(w, attr) not in (None, "")
        if not has:
            continue
        e = db.query(m.Extraction).filter(m.Extraction.document_id == w.created_from_document_id, m.Extraction.field == field,
                                          m.Extraction.status != "rejected").order_by(m.Extraction.confidence.desc()).first()
        if e:
            out.append({"field": attr, "value": e.norm_value or e.raw_value, "document_id": e.document_id, "page": e.page,
                        "snippet": e.snippet, "confidence": e.confidence, "method": e.method, "review": e.status})
    return out


# ----------------------------------------------------------------------------- officer decisions
def confirm(db: Session, w: m.Roadwork, user):
    if w.verification_status != "provisional":
        raise ValueError("Only a provisional work can be confirmed")
    w.verification_status = "confirmed"
    log_event(db, None, "roadwork", w.id, "provisional_confirmed", {"by": user.id if user else None}, user=user)
    return w


def merge(db: Session, src: m.Roadwork, dst: m.Roadwork, user):
    """Fold a provisional/confirmed document-created profile into an existing work. Nothing is deleted:
    documents, payments, bills, links and source rows are re-pointed; the old profile stays as 'merged' for history."""
    if src.id == dst.id:
        raise ValueError("Choose a different work to merge into")
    if src.verification_status not in ("provisional", "confirmed") or src.data_origin != "document":
        raise ValueError("Only a profile created from a document can be merged")
    if dst.verification_status == "merged":
        raise ValueError("The target work was itself merged into another work")
    moved = {}
    for attr in ("job_number", "work_order_id", "tender_id", "work_id", "description", "description_norm", "department", "category",
                 "location_text", "road_name_extracted", "estimated_cost", "awarded_amount", "work_order_date", "commencement_date",
                 "completion_deadline", "contractor_id", "ward_number", "fiscal_year"):
        if getattr(dst, attr) in (None, "") and getattr(src, attr) not in (None, ""):
            setattr(dst, attr, getattr(src, attr)); moved[attr] = True  # fill gaps only; official values are never overwritten
    for model, col in ((m.Document, "roadwork_id"), (m.Payment, "roadwork_id"), (m.Bill, "roadwork_id")):
        n = db.query(model).filter(getattr(model, col) == src.id).update({col: dst.id}, synchronize_session=False)
        moved[model.__tablename__] = n
    n = 0
    for l in db.query(m.WorkSourceLink).filter_by(roadwork_id=src.id):
        if not db.query(m.WorkSourceLink).filter_by(roadwork_id=dst.id, source_record_id=l.source_record_id).first():
            l.roadwork_id = dst.id; n += 1
    moved["source_links"] = n
    n = 0
    for l in db.query(m.RecordLink).filter(or_((m.RecordLink.a_type == "roadwork") & (m.RecordLink.a_id == src.id),
                                               (m.RecordLink.b_type == "roadwork") & (m.RecordLink.b_id == src.id))):
        side = "a" if (l.a_type == "roadwork" and l.a_id == src.id) else "b"
        other = (l.b_type, l.b_id) if side == "a" else (l.a_type, l.a_id)
        clash = db.query(m.RecordLink).filter(
            ((m.RecordLink.a_type == "roadwork") & (m.RecordLink.a_id == dst.id) & (m.RecordLink.b_type == other[0]) & (m.RecordLink.b_id == other[1])) |
            ((m.RecordLink.b_type == "roadwork") & (m.RecordLink.b_id == dst.id) & (m.RecordLink.a_type == other[0]) & (m.RecordLink.a_id == other[1]))).first()
        if clash:
            continue
        if side == "a": l.a_id = dst.id
        else: l.b_id = dst.id
        n += 1
    moved["record_links"] = n
    src.verification_status, src.merged_into_id = "merged", dst.id
    src.provisional_key = None  # frees the key: a later document may start a fresh profile if this merge is ever reversed
    # a payment that now sits beside an identical one from another source must not be counted twice
    from .sources import find_duplicate_payment
    for p in db.query(m.Payment).filter(m.Payment.roadwork_id == dst.id, m.Payment.duplicate_of_id.is_(None)).order_by(m.Payment.id):
        dup = find_duplicate_payment(db, dst.id, p.amount, p.paid_on, p.source_record_id, p.document_id, exclude_id=p.id)
        if dup and dup.id < p.id:
            p.duplicate_of_id = dup.id
    log_event(db, None, "roadwork", src.id, "provisional_merged", {"into": dst.id, "moved": moved}, user=user)
    return moved
