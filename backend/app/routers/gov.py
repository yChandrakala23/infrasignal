"""Government monitoring API (officer / admin). Everything returned here keeps its provenance: source document,
page, extraction method, confidence and link status. Payments never imply completion."""
import os, shutil, tempfile
from threading import Lock
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, and_, exists, select, true
from sqlalchemy.orm import Session
from ..db import get_db
from .. import models as m
from ..classify import DOC_TYPES
from ..deps import officer, audit
from ..ingest import import_table, import_tenders, preview_table
from ..matching import (review_link, manual_assign_document, run_document_matching, match_tenders, link_roads_to_works, doc_values)
from ..pipeline import ingest_document, road_gov_status, clean_url, set_provenance
from ..sources import work_sources, review_source_link, counted_payments
from .. import provisional as prov
from .. import boq as boq_mod
from .. import scope
from ..textnorm import parse_date, norm_text

router = APIRouter(prefix="/api/gov", tags=["government monitoring"])
_matching_run_lock = Lock()
WORK_STATUS = {"not_stated": "Not stated in records", "work_order_issued": "Work order issued (document on file)",
               "completion_certificate_on_file": "Completion certificate on file"}
LIFECYCLE = ["Proposal", "Estimate", "Administrative approval", "Technical sanction", "Tender publication", "BOQ", "Letter of acceptance",
             "Contractor identified", "Agreement", "Work order", "Measurement and running bills", "Payment", "Completion certificate"]


# ----------------------------------------------------------------------------- filters
class Filters:
    def __init__(self, q: str = "", ward: str = "", regime: str = "", contractor: str = "", department: str = "", category: str = "",
                 tender_status: str = "", work_status: str = "", fiscal_year: str = "", date_from: str = "", date_to: str = "",
                 id_search: str = "", flag: str = "", verification: str = "", min_amount: float | None = None, max_amount: float | None = None,
                 origin: str = ""):
        self.__dict__.update(locals())

    @property
    def active(self):
        """True when the officer narrowed the view (the record-status choice alone does not count)."""
        return any(v not in ("", None) for k, v in self.__dict__.items() if k not in ("self", "verification"))


def apply_filters(qs, f: Filters, joined_contractor=True):
    R = m.Roadwork
    # Works that began as a PDF and are not yet checked ("provisional") are shown only when asked for, and never as part of the
    # official counts and totals. verification: "" = official + confirmed (default), "provisional", "all" = those plus provisional.
    V = R.verification_status
    if f.verification == "provisional": qs = qs.filter(V == "provisional")
    elif f.verification == "all": qs = qs.filter(or_(V.is_(None), V.in_(["official", "confirmed", "provisional"])))
    else: qs = qs.filter(or_(V.is_(None), V.in_(["official", "confirmed"])))
    if f.q: qs = qs.filter(or_(R.description.ilike(f"%{f.q}%"), R.location_text.ilike(f"%{f.q}%"), R.road_name_extracted.ilike(f"%{f.q}%")))
    if f.ward:
        wc = scope.ward_condition(f.ward)
        qs = qs.filter(wc) if wc is not None else qs.filter(False)
    if f.regime: qs = qs.filter(R.ward_regime == f.regime)
    # origin: "collected" = records collected from BBMP open data, "genuine" = anything that is not sample data, "sample" = labelled sample data
    if f.origin == "collected": qs = qs.filter(R.data_origin == "bbmp_open_data")
    elif f.origin == "genuine": qs = qs.filter(R.is_demo.is_(False))
    elif f.origin == "sample": qs = qs.filter(R.is_demo.is_(True))
    if f.contractor: qs = qs.filter(m.Contractor.name.ilike(f"%{f.contractor}%"))
    if f.department: qs = qs.filter(scope.department_condition(f.department))
    if f.category: qs = qs.filter(R.category == f.category)
    if f.work_status: qs = qs.filter(R.work_status == f.work_status)
    if f.fiscal_year: qs = qs.filter(scope.fiscal_year_condition(f.fiscal_year))
    d1, d2 = parse_date(f.date_from), parse_date(f.date_to)
    if d1: qs = qs.filter(R.work_order_date >= d1)
    if d2: qs = qs.filter(R.work_order_date < d2 + timedelta(days=1))  # the end date itself is included
    if f.min_amount is not None: qs = qs.filter(func.coalesce(R.awarded_amount, R.sanctioned_amount) >= f.min_amount)
    if f.max_amount is not None: qs = qs.filter(func.coalesce(R.awarded_amount, R.sanctioned_amount) <= f.max_amount)
    if f.id_search:
        like = f"%{f.id_search}%"
        qs = qs.filter(or_(R.job_number.ilike(like), R.work_order_id.ilike(like), R.tender_id.ilike(like), R.work_id.ilike(like)))
    if f.tender_status:
        qs = qs.filter(exists().where(and_(m.Tender.tender_id == R.tender_id, m.Tender.status == f.tender_status)))
    if f.flag == "no_documents": qs = qs.filter(~exists().where(m.Document.roadwork_id == R.id))
    elif f.flag == "no_tender": qs = qs.filter(or_(R.tender_id.is_(None), R.tender_id == ""))
    elif f.flag == "no_payment": qs = qs.filter(~exists().where(m.Payment.roadwork_id == R.id))
    elif f.flag == "past_deadline_no_completion":
        qs = qs.filter(R.completion_deadline < datetime.utcnow(), R.work_status != "completion_certificate_on_file")
    return qs


def filtered_query(db, f):
    qs = db.query(m.Roadwork).outerjoin(m.Contractor, m.Roadwork.contractor_id == m.Contractor.id)
    return apply_filters(qs, f)


FILTER_DEPS = Depends(Filters)


@router.get("/filters")
def filter_options(u=Depends(officer), db: Session = Depends(get_db)):
    R = m.Roadwork
    vis = scope.visible_works()
    col = lambda c: [x for (x,) in db.query(c).filter(c.isnot(None), c != "", vis).distinct().order_by(c)]
    wards = db.query(R.ward_regime, R.ward_number, R.ward_name).filter(R.ward_number.isnot(None), R.ward_is_multi.is_(False), vis).distinct().all()
    deps = {}  # "Ward Engineering" and "WARD ENGINEERING " are one department
    for (d,) in db.query(R.department).filter(R.department.isnot(None), R.department != "", vis).order_by(R.department):
        deps.setdefault(" ".join(d.split()).lower(), " ".join(d.split()))
    return {"departments": sorted(deps.values(), key=str.lower), "categories": col(R.category), "fiscal_years": scope.fiscal_years_present(db),
            "work_statuses": [{"key": k, "label": v} for k, v in WORK_STATUS.items()],
            "tender_statuses": col(m.Tender.status), "regimes": col(R.ward_regime),
            "wards": sorted({(a, scope.norm_ward_number(b)): {"regime": a, "number": scope.norm_ward_number(b), "name": c,
                                                              "value": f"{a}:{scope.norm_ward_number(b)}" if a else scope.norm_ward_number(b)} for a, b, c in wards}.values(),
                           key=lambda x: (x["regime"] or "", int(x["number"]) if str(x["number"]).isdigit() else 9999)),
            "contractors": [x for (x,) in db.query(m.Contractor.name).order_by(m.Contractor.name)],
            "doc_types": [{"key": k, "label": v} for k, v in DOC_TYPES.items()]}


# ----------------------------------------------------------------------------- dashboard
def tender_attached():
    """A tender that backs a work: a live roadwork<->tender link, or the same tender ID on a (non-merged) work."""
    L, R, T = m.RecordLink, m.Roadwork, m.Tender
    linked = exists().where(and_(L.a_type == "roadwork", L.b_type == "tender", L.b_id == T.id, L.status.in_(["auto", "verified", "suggested"])))
    same_id = exists().where(and_(R.tender_id == T.tender_id, T.tender_id.isnot(None), R.tender_id != "", R.verification_status != "merged"))
    return or_(linked, same_id)


def tender_attached_to(idsel):
    """Same, but only to works in the current view."""
    L, R, T = m.RecordLink, m.Roadwork, m.Tender
    linked = exists().where(and_(L.a_type == "roadwork", L.b_type == "tender", L.b_id == T.id, L.status.in_(["auto", "verified", "suggested"]), L.a_id.in_(idsel)))
    same_id = exists().where(and_(R.id.in_(idsel), R.tender_id == T.tender_id, T.tender_id.isnot(None), R.tender_id != ""))
    return or_(linked, same_id)


@router.get("/dashboard")
def dashboard(f: Filters = FILTER_DEPS, u=Depends(officer), db: Session = Depends(get_db)):
    """Every number below comes from the same set of works (`idsel`, built by apply_filters). Payments are summed in their own
    queries, once per payment (repeats excluded), so joining documents or payments can never multiply a work's amounts.
    Records that belong to no work (unmatched documents, tenders with no work, reports) cannot be filtered by ward or contractor;
    they are reported under `unscoped` and labelled as such."""
    R, P, D = m.Roadwork, m.Payment, m.Document
    ids = filtered_query(db, f).with_entities(R.id).subquery()
    idsel = select(ids.c.id)
    scalar = lambda q: q.scalar() or 0
    n_works = scalar(db.query(func.count()).select_from(ids))
    pay_fy = scope.payment_fiscal_year_condition(f.fiscal_year) if f.fiscal_year else true()  # payments of the chosen year only
    counted = lambda *c: counted_payments(db, *c).filter(P.roadwork_id.in_(idsel), pay_fy)
    paid = scalar(counted(func.sum(P.amount)))
    n_pay = scalar(counted(func.count(P.id)))
    repeats = db.query(func.count(P.id), func.sum(P.amount)).filter(P.roadwork_id.in_(idsel), P.duplicate_of_id.isnot(None), pay_fy).one()
    awarded = scalar(db.query(func.sum(func.coalesce(R.awarded_amount, R.sanctioned_amount))).filter(R.id.in_(idsel)))
    n_awarded = scalar(db.query(func.count(func.coalesce(R.awarded_amount, R.sanctioned_amount))).filter(R.id.in_(idsel)))
    estimated = scalar(db.query(func.sum(R.estimated_cost)).filter(R.id.in_(idsel)))
    group = lambda *cols: db.query(*cols, func.count(R.id)).filter(R.id.in_(idsel)).group_by(*cols)
    by_ward = group(R.ward_regime, R.ward_number, R.ward_name).order_by(func.count(R.id).desc()).limit(15).all()
    award_c = func.coalesce(R.awarded_amount, R.sanctioned_amount)
    paid_by_work = counted(P.roadwork_id.label("rid"), func.sum(P.amount).label("paid")).group_by(P.roadwork_id).subquery()
    by_contractor = db.query(m.Contractor.name, func.count(R.id), func.sum(award_c), func.count(award_c), func.sum(paid_by_work.c.paid)) \
        .join(R, R.contractor_id == m.Contractor.id).outerjoin(paid_by_work, paid_by_work.c.rid == R.id).filter(R.id.in_(idsel)) \
        .group_by(m.Contractor.name).order_by(func.count(R.id).desc()).limit(10).all()
    dep_key = func.lower(func.trim(R.department))
    by_dept = db.query(func.min(R.department), func.count(R.id)).filter(R.id.in_(idsel)).group_by(dep_key).order_by(func.count(R.id).desc()).all()
    today = datetime.utcnow()
    overdue = and_(R.completion_deadline < today, R.work_status != "completion_certificate_on_file")
    past = db.query(R).filter(R.id.in_(idsel), overdue).order_by(R.completion_deadline).limit(10).all()
    soon = scalar(db.query(func.count(R.id)).filter(R.id.in_(idsel), R.completion_deadline >= today, R.completion_deadline <= today + timedelta(days=30)))

    # documents: those linked to a work in view (one row per document, no join), and the rest reported separately
    d_in_view = D.roadwork_id.in_(idsel)
    docs_by_type = dict(db.query(D.doc_type, func.count(D.id)).filter(d_in_view).group_by(D.doc_type))
    doc_status = dict(db.query(D.match_status, func.count(D.id)).filter(d_in_view).group_by(D.match_status))
    n_docs = scalar(db.query(func.count(D.id)).filter(d_in_view))
    # a document waiting for a decision belongs to the view when its suggested work is in the view
    sug = and_(m.RecordLink.a_type == "document", m.RecordLink.b_type == "roadwork", m.RecordLink.status == "suggested", m.RecordLink.b_id.in_(idsel))
    docs_review = scalar(db.query(func.count(func.distinct(m.RecordLink.a_id))).filter(sug))
    no_docs = scalar(db.query(func.count(R.id)).filter(R.id.in_(idsel), ~exists().where(D.roadwork_id == R.id)))
    no_tender = scalar(db.query(func.count(R.id)).filter(R.id.in_(idsel), or_(R.tender_id.is_(None), R.tender_id == "")))
    no_payment = scalar(db.query(func.count(R.id)).filter(R.id.in_(idsel), ~exists().where(and_(P.roadwork_id == R.id, P.duplicate_of_id.is_(None), pay_fy))))
    paid_by = dict(counted(P.roadwork_id, func.sum(P.amount)).group_by(P.roadwork_id))
    over = [w for w in db.query(R).filter(R.id.in_(paid_by.keys()))
            if (w.awarded_amount or w.sanctioned_amount) and paid_by[w.id] > (w.awarded_amount or w.sanctioned_amount) * 1.001]
    n_tenders = scalar(db.query(func.count(m.Tender.id)).filter(tender_attached_to(idsel)))
    unmatched_tenders = db.query(m.Tender).filter(~tender_attached()).count()
    pending_view = dict(db.query(m.RecordLink.b_type, func.count(m.RecordLink.id)).filter(m.RecordLink.status == "suggested", m.RecordLink.a_type == "document", m.RecordLink.b_type == "roadwork", m.RecordLink.b_id.in_(idsel)).group_by(m.RecordLink.b_type))
    pending = dict(db.query(m.RecordLink.a_type, func.count(m.RecordLink.id)).filter(m.RecordLink.status == "suggested").group_by(m.RecordLink.a_type))
    month = func.strftime("%Y-%m", R.work_order_date) if db.bind.dialect.name == "sqlite" else func.to_char(R.work_order_date, "YYYY-MM")
    by_month = db.query(month, func.count(R.id)).filter(R.id.in_(idsel), R.work_order_date.isnot(None)).group_by(month).order_by(month).all()
    # works started from a PDF and not yet checked: shown beside the totals, never inside them
    prov_f = Filters(**{**{k: v for k, v in f.__dict__.items() if k != "self"}, "verification": "provisional"})
    n_prov = scalar(db.query(func.count()).select_from(filtered_query(db, prov_f).with_entities(R.id).subquery()))
    # documents tied to no work at all
    unlinked_docs = dict(db.query(D.match_status, func.count(D.id)).filter(D.roadwork_id.is_(None)).group_by(D.match_status))
    return {
        "scope": {"filters_active": f.active, "verification": f.verification or "official",
                  "note": ("Filters apply to works and to everything attached to them: payments, documents, tenders and deadlines. "
                           "Items that belong to no work are listed under 'unscoped' and are not filtered.")},
        "totals": {"roadworks": n_works, "contractors": scalar(db.query(func.count(func.distinct(R.contractor_id))).filter(R.id.in_(idsel))),
                   "tenders": n_tenders, "documents": n_docs,
                   "awarded_amount": awarded, "works_with_award_amount": n_awarded, "estimated_cost": estimated, "paid_amount_recorded": paid, "payments": n_pay,
                   "repeat_payment_records_excluded": repeats[0] or 0, "repeat_payment_amount_excluded": float(repeats[1] or 0),
                   "provisional_works_not_counted": n_prov,
                   "sample_roadworks": scalar(db.query(func.count(R.id)).filter(R.id.in_(idsel), R.is_demo.is_(True))),
                   "collected_roadworks": scalar(db.query(func.count(R.id)).filter(R.id.in_(idsel), R.data_origin == "bbmp_open_data"))},
        "works_by_ward": [{"regime": a, "number": b, "name": c, "count": n} for a, b, c, n in by_ward],
        "works_by_status": [{"key": k or "not_stated", "label": WORK_STATUS.get(k or "not_stated", k), "count": n} for k, n in group(R.work_status).all()],
        "works_by_category": [{"key": k or "unclassified", "count": n} for k, n in group(R.category).all()],
        "works_by_department": [{"key": " ".join(k.split()) if k else "not stated", "count": n} for k, n in by_dept],
        "works_by_month": [{"month": a, "count": n} for a, n in by_month],
        # awarded is None (shown as "–") when no work of the contractor has a published award / sanction amount: the BBMP open-data
        # files are bill-payment lists and do not publish one. 0 would wrongly read as "nothing awarded".
        "top_contractors": [{"name": a, "works": n, "awarded": float(t) if na else None, "works_with_award": na, "paid": float(pd or 0)}
                            for a, n, t, na, pd in by_contractor],
        "deadlines": {"due_in_30_days": soon, "past_deadline_no_completion_record": scalar(db.query(func.count(R.id)).filter(R.id.in_(idsel), overdue)),
                      "note": "A passed deadline without a completion certificate in the loaded records is a prompt to check, not a finding of delay.",
                      "items": [{"id": w.id, "description": w.description, "deadline": w.completion_deadline, "job_number": w.job_number} for w in past]},
        "documents": {"by_type": [{"key": k, "label": DOC_TYPES.get(k, k), "count": n} for k, n in docs_by_type.items()], "by_match_status": doc_status},
        "gaps": {"works_without_documents": no_docs, "works_without_tender_id": no_tender, "works_without_payment_record": no_payment,
                 "documents_needing_review": docs_review, "links_awaiting_review": pending_view.get("roadwork", 0),
                 "paid_above_award_to_verify": [{"id": w.id, "description": w.description, "paid": paid_by[w.id], "awarded": w.awarded_amount or w.sanctioned_amount} for w in over[:10]]},
        "unscoped": {"note": "These records are not attached to any work, so ward, contractor, department and year filters cannot apply to them.",
                     "unmatched_documents": unlinked_docs.get("unmatched", 0), "documents_without_a_work": sum(unlinked_docs.values()),
                     "tenders_without_work": unmatched_tenders, "all_tenders": scalar(db.query(func.count(m.Tender.id))), "all_documents": scalar(db.query(func.count(D.id))),
                     "links_awaiting_review": pending},
        "reports": {"total": db.query(func.count(m.Report.id)).scalar(), "clusters": db.query(func.count(m.Cluster.id)).filter(m.Cluster.status.notin_(["rejected", "dissolved"])).scalar(),
                    "open_alerts": db.query(func.count(m.Alert.id)).filter(m.Alert.status == "open").scalar(), "scoped": False},
        "imports": [{"file": j.filename, "kind": j.kind, "ok": j.rows_ok, "duplicate": j.rows_duplicate, "failed": j.rows_failed, "sample": j.is_demo, "at": j.created_at}
                    for j in db.query(m.ImportJob).order_by(m.ImportJob.id.desc()).limit(6)],
        "notes": ["Payment records are shown as payments. They are never used to infer that a work is complete.",
                  "Status shown is only what a document states. Absent documents mean 'not stated', not 'not done'.",
                  "A payment recorded twice (a spreadsheet row and a payment document) is counted once; the repeat stays on file and is shown on the work."]}


# ----------------------------------------------------------------------------- roadworks
def work_row(w, c, paid=0, ndocs=0):
    return {"id": w.id, "job_number": w.job_number, "work_order_id": w.work_order_id, "tender_id": w.tender_id, "work_id": w.work_id,
            "description": w.description, "ward": w.ward_number, "ward_name": w.ward_name, "regime": w.ward_regime, "multi_ward": w.ward_is_multi,
            "department": w.department, "category": w.category, "contractor": c.name if c else None, "estimated_cost": w.estimated_cost,
            "awarded_amount": w.awarded_amount or w.sanctioned_amount, "work_order_date": w.work_order_date, "completion_deadline": w.completion_deadline,
            "work_status": w.work_status, "work_status_label": WORK_STATUS.get(w.work_status, w.work_status), "paid": paid, "documents": ndocs, "sample": w.is_demo,
            "verification_status": w.verification_status or "official"}


@router.get("/roadworks")
def list_roadworks(f: Filters = FILTER_DEPS, sort: str = "newest",
                   page: int = 1, page_size: int = Query(50, le=200), u=Depends(officer), db: Session = Depends(get_db)):
    R = m.Roadwork
    qs = filtered_query(db, f)
    total = qs.count()
    idsel = select(qs.with_entities(R.id).subquery().c.id)  # the whole filtered set, not just this page
    sum_awarded = db.query(func.sum(func.coalesce(R.awarded_amount, R.sanctioned_amount))).filter(R.id.in_(idsel)).scalar() or 0
    pay_fy = scope.payment_fiscal_year_condition(f.fiscal_year) if f.fiscal_year else true()
    sum_paid = counted_payments(db, func.sum(m.Payment.amount)).filter(m.Payment.roadwork_id.in_(idsel), pay_fy).scalar() or 0
    order = {"newest": R.id.desc(), "amount": func.coalesce(R.awarded_amount, R.sanctioned_amount).desc(), "deadline": R.completion_deadline.asc(),
             "date": R.work_order_date.desc()}.get(sort, R.id.desc())
    rows = qs.with_entities(R, m.Contractor).order_by(order).offset((page - 1) * page_size).limit(page_size).all()
    ids = [w.id for w, _ in rows]
    paid = dict(counted_payments(db, m.Payment.roadwork_id, func.sum(m.Payment.amount)).filter(m.Payment.roadwork_id.in_(ids), pay_fy).group_by(m.Payment.roadwork_id)) if ids else {}
    nd = dict(db.query(m.Document.roadwork_id, func.count(m.Document.id)).filter(m.Document.roadwork_id.in_(ids)).group_by(m.Document.roadwork_id)) if ids else {}
    return {"total": total, "page": page, "page_size": page_size, "totals": {"awarded_amount": float(sum_awarded), "paid_amount_recorded": float(sum_paid)}, "items": [work_row(w, c, paid.get(w.id, 0), nd.get(w.id, 0)) for w, c in rows]}


def _desc(db, typ, i):
    """One-line label for any linkable record."""
    o = {"document": m.Document, "roadwork": m.Roadwork, "tender": m.Tender, "road": m.Road, "cluster": m.Cluster}.get(typ)
    x = db.get(o, i) if o else None
    if not x: return f"{typ} #{i} (missing)"
    if typ == "document": return f"{x.filename} [{DOC_TYPES.get(x.doc_type, x.doc_type)}]"
    if typ == "roadwork": return f"{x.job_number or x.work_order_id or '#' + str(x.id)}: {(x.description or '')[:90]}"
    if typ == "tender": return f"{x.tender_id or '#' + str(x.id)}: {(x.title or '')[:90]}"
    if typ == "road": return x.name or "Unnamed road"
    return f"Cluster #{x.id}: {x.report_count} reports ({x.category})"


def link_json(db, l):
    return {"id": l.id, "a_type": l.a_type, "a_id": l.a_id, "a_label": _desc(db, l.a_type, l.a_id), "b_type": l.b_type, "b_id": l.b_id,
            "b_label": _desc(db, l.b_type, l.b_id), "method": l.method, "confidence": l.confidence, "status": l.status, "evidence": l.evidence,
            "reviewed_at": l.reviewed_at, "created_at": l.created_at}


@router.get("/roadworks/{rid}")
def roadwork_profile(rid: int, u=Depends(officer), db: Session = Depends(get_db)):
    w = db.get(m.Roadwork, rid)
    if not w: raise HTTPException(404)
    c = db.get(m.Contractor, w.contractor_id) if w.contractor_id else None
    sources = work_sources(db, rid)
    prim = next((x for x in sources if x["role"] in ("primary", "legacy_primary")), None)
    src = db.get(m.SourceRecord, w.source_record_id) if w.source_record_id else None
    job = db.get(m.ImportJob, src.import_job_id) if src else None
    docs = db.query(m.Document).filter_by(roadwork_id=rid).order_by(m.Document.doc_date, m.Document.id).all()
    cand_docs = [l for l in db.query(m.RecordLink).filter_by(a_type="document", b_type="roadwork", b_id=rid, status="suggested")]
    links = db.query(m.RecordLink).filter(or_(and_(m.RecordLink.a_type == "roadwork", m.RecordLink.a_id == rid),
                                              and_(m.RecordLink.b_type == "roadwork", m.RecordLink.b_id == rid))).all()
    bills = db.query(m.Bill).filter_by(roadwork_id=rid).all()
    pays = db.query(m.Payment).filter_by(roadwork_id=rid).order_by(m.Payment.paid_on).all()
    dtypes = {d.doc_type for d in docs}
    tenders = [db.get(m.Tender, l.b_id) for l in links if l.a_type == "roadwork" and l.b_type == "tender" and l.status != "rejected"]
    tenders = [t for t in tenders if t]
    ev = {"Estimate": f"Estimated cost {w.estimated_cost:,.0f}" if w.estimated_cost else None,
          "Administrative approval": "Document on file" if "admin_approval" in dtypes else None,
          "Technical sanction": "Document on file" if "technical_sanction" in dtypes else None,
          "Tender publication": f"Tender {w.tender_id}" if w.tender_id else ("Tender record linked" if tenders else None),
          "BOQ": "Document on file" if "boq" in dtypes else None, "Letter of acceptance": "Document on file" if "loa" in dtypes else None,
          "Contractor identified": c.name if c else None, "Agreement": "Document on file" if "agreement" in dtypes else None,
          "Work order": (f"{w.work_order_id or ''} {w.work_order_date:%d %b %Y}" if w.work_order_date else w.work_order_id) if (w.work_order_id or w.work_order_date) or "work_order" in dtypes else None,
          "Measurement and running bills": f"{len(bills)} bill record(s)" if bills or "measurement_book" in dtypes else None,
          "Payment": f"{len(pays)} payment record(s). A payment does not show that the work is complete." if pays else None,
          "Completion certificate": "Document on file" if "completion_certificate" in dtypes else None}
    stages = [{"stage": s, "evidence": ev.get(s), "available": bool(ev.get(s))} for s in LIFECYCLE if s != "Proposal"]
    paid = sum(p.amount or 0 for p in pays if p.duplicate_of_id is None)  # a payment recorded twice is counted once
    dup_pays = [p for p in pays if p.duplicate_of_id is not None]
    missing = [f for f in ("job_number", "tender_id", "work_order_id", "ward_number", "contractor_id", "awarded_amount", "commencement_date", "completion_deadline")
               if getattr(w, f) in (None, "") and not (f == "awarded_amount" and w.sanctioned_amount)]
    roads = [l for l in links if l.a_type == "road"]
    return {"roadwork": {**work_row(w, c, paid, len(docs)), "location_text": w.location_text, "road_name_extracted": w.road_name_extracted,
                         "commencement_date": w.commencement_date, "fiscal_year": w.fiscal_year, "work_status_source": w.work_status_source,
                         "source_name": w.source_name, "source_url": w.source_url, "data_origin": w.data_origin,
                         "verification_status": w.verification_status, "created_from_document_id": w.created_from_document_id,
                         "merged_into_id": w.merged_into_id},
            "contractor": {"id": c.id, "name": c.name} if c else None,
            "documents": [{"id": d.id, "filename": d.filename, "type": d.doc_type, "type_label": DOC_TYPES.get(d.doc_type, d.doc_type), "pages": d.pages,
                           "date": d.doc_date, "match_status": d.match_status, "type_confidence": d.doc_type_confidence, "sample": d.is_demo,
                           "source_page_url": d.source_page_url, "download_url": d.download_url, "downloaded_at": d.downloaded_at} for d in docs],
            "boq": [{"document_id": d.id, "filename": d.filename, **boq_mod.summary_for(db, d.id)} for d in docs
                    if db.query(m.BoqItem.id).filter_by(document_id=d.id).first()],
            "documents_to_review": [link_json(db, l) for l in cand_docs],
            "tenders": [{"id": t.id, "tender_id": t.tender_id, "title": t.title, "status": t.status, "estimated_value": t.estimated_value, "awarded_value": t.awarded_value,
                         "document_id": t.document_id, "sample": t.is_demo} for t in tenders],
            "bills": [{"id": b.id, "number": b.bill_number, "amount": b.amount, "date": b.bill_date, "deduction": b.deduction, "net_amount": b.net_amount,
                       "source_record_id": b.source_record_id} for b in bills],
            "payments": [{"id": p.id, "amount": p.amount, "gross_amount": p.gross_amount, "deduction": p.deduction, "paid_on": p.paid_on, "reference": p.reference,
                          "document_id": p.document_id, "source_record_id": p.source_record_id, "counted": p.duplicate_of_id is None,
                          "duplicate_of_id": p.duplicate_of_id} for p in pays],
            "values": {"verified_fields": w.verified_fields or [], "data_origin": w.data_origin, "external_key": w.external_key, "first_seen_fiscal_year": w.fiscal_year,
                       "note": "Original values are in the source rows below (never edited). The fields above are normalised from them. Fields in verified_fields were confirmed "
                               "or corrected by an officer and are not changed by imports."},
            "payment_summary": {"paid_total": paid, "awarded": w.awarded_amount or w.sanctioned_amount,
                                "records": len(pays) - len(dup_pays), "repeat_records_excluded": len(dup_pays),
                                "repeat_amount_excluded": sum(p.amount or 0 for p in dup_pays),
                                "note": "Payments are listed as recorded. They are not treated as evidence of completion. A payment that appears in two sources "
                                        "(for example a CSV row and a payment PDF) is shown twice but counted once."},
            "sources": sources,
            "provisional": ({"status": w.verification_status, "created_from_document_id": w.created_from_document_id, "key": w.provisional_key,
                             "reasons": prov.reasons(db, w) if w.verification_status == "provisional" else [],
                             "fields": prov.provisional_fields(db, w),
                             "note": "Started from a PDF, not from an imported government record. Every value below comes from that document. "
                                     "Confirm it, or merge it into an existing work, after checking the document."}
                            if w.data_origin == "document" and w.verification_status in ("provisional", "confirmed", "merged") else None),
            "roads": [{**link_json(db, l), "road_id": l.a_id} for l in roads],
            "links": [link_json(db, l) for l in links], "lifecycle": stages, "missing_fields": missing,
            "complaints_nearby": [{"cluster_id": l.a_id, "count": (db.get(m.Cluster, l.a_id).report_count if db.get(m.Cluster, l.a_id) else 0), "status": l.status, "confidence": l.confidence}
                                  for l in links if l.a_type == "cluster"],
            "source_row": ({"import_job": job.filename if job else None, "row": src.row_number, "raw": src.raw} if src else
                           ({"import_job": (prim["import"] or {}).get("file"), "row": prim["row"], "raw": prim["raw"]} if prim else None))}


@router.post("/documents/{did}/create-work")
def create_work_from_document(did: int, u=Depends(officer), db: Session = Depends(get_db)):
    """Officer action: start a provisional work profile from this document (also when it has unconfirmed candidate works)."""
    d = _doc(db, did)
    w, info = prov.create_from_document(db, d, user=u, force=True)
    if w is None:
        raise HTTPException(422, info.get("reason", "A work profile cannot be started from this document"))
    audit(db, u, "provisional_work_create", "document", did, info); db.commit()
    return {"ok": True, "roadwork_id": w.id, **info}


VERIFIABLE = {"description": str, "ward_number": str, "ward_regime": str, "department": str, "road_name_extracted": str, "location_text": str,
              "estimated_cost": float, "awarded_amount": float, "sanctioned_amount": float, "fiscal_year": str, "contractor_id": int}


@router.patch("/roadworks/{rid}/fields")
def set_verified_fields(rid: int, body: dict, u=Depends(officer), db: Session = Depends(get_db)):
    """An officer corrects or confirms fields of a work. Each field named in the body is set to the given value (or confirmed as it is when
    the value is null) and locked: later automated imports will not change it, and log a conflict instead if their value differs."""
    w = db.get(m.Roadwork, rid)
    if not w: raise HTTPException(404, "Work not found")
    bad = [k for k in body if k not in VERIFIABLE]
    if bad: raise HTTPException(422, f"Cannot verify: {', '.join(bad)}. Allowed: {', '.join(sorted(VERIFIABLE))}")
    locked = list(w.verified_fields or [])
    for k, val in body.items():
        if val is not None and val != "":
            try: val = VERIFIABLE[k](val)
            except (TypeError, ValueError): raise HTTPException(422, f"{k} must be a {VERIFIABLE[k].__name__}")
            setattr(w, k, val)
            if k == "description": w.description_norm = norm_text(val)
        if k not in locked: locked.append(k)
    w.verified_fields = locked
    audit(db, u, "work_fields_verified", "roadwork", rid, {"fields": list(body)}); db.commit()
    return {"ok": True, "id": rid, "verified_fields": w.verified_fields}


@router.delete("/roadworks/{rid}/fields/{name}")
def unlock_field(rid: int, name: str, u=Depends(officer), db: Session = Depends(get_db)):
    w = db.get(m.Roadwork, rid)
    if not w: raise HTTPException(404, "Work not found")
    w.verified_fields = [x for x in (w.verified_fields or []) if x != name]
    audit(db, u, "work_field_unlocked", "roadwork", rid, {"field": name}); db.commit()
    return {"ok": True, "id": rid, "verified_fields": w.verified_fields}


@router.post("/roadworks/{rid}/confirm")
def confirm_provisional(rid: int, u=Depends(officer), db: Session = Depends(get_db)):
    w = db.get(m.Roadwork, rid)
    if not w: raise HTTPException(404)
    try: prov.confirm(db, w, u)
    except ValueError as e: raise HTTPException(422, str(e))
    audit(db, u, "provisional_work_confirm", "roadwork", rid, {}); db.commit()
    return {"ok": True, "id": rid, "verification_status": w.verification_status}


@router.post("/roadworks/{rid}/merge")
def merge_provisional(rid: int, target_id: int = Form(...), u=Depends(officer), db: Session = Depends(get_db)):
    w, t = db.get(m.Roadwork, rid), db.get(m.Roadwork, target_id)
    if not w or not t: raise HTTPException(404, "Work not found")
    try: moved = prov.merge(db, w, t, u)
    except ValueError as e: raise HTTPException(422, str(e))
    audit(db, u, "provisional_work_merge", "roadwork", rid, {"into": target_id, "moved": moved}); db.commit()
    return {"ok": True, "merged_into": target_id, "moved": moved}


@router.get("/roadworks/{rid}/sources")
def roadwork_sources(rid: int, u=Depends(officer), db: Session = Depends(get_db)):
    """Every original record behind one work: how it was matched, how confident that is, and whether a person checked it."""
    if not db.get(m.Roadwork, rid): raise HTTPException(404)
    return {"roadwork_id": rid, "sources": work_sources(db, rid),
            "note": "Original records are never changed or merged. A link says only that the record is about this work."}


@router.patch("/source-links/{lid}")
def set_source_link(lid: int, status: str = Form(...), u=Depends(officer), db: Session = Depends(get_db)):
    if status not in ("verified", "rejected", "suggested"): raise HTTPException(422, "Status must be verified, rejected or suggested")
    l = db.get(m.WorkSourceLink, lid)
    if not l: raise HTTPException(404)
    review_source_link(db, l, status, u)
    audit(db, u, "source_link_review", "work_source_link", lid, {"status": status, "roadwork_id": l.roadwork_id}); db.commit()
    return {"ok": True, "id": lid, "status": l.status}


@router.patch("/payments/{pid}")
def set_payment_duplicate(pid: int, duplicate_of: str = Form(""), u=Depends(officer), db: Session = Depends(get_db)):
    """Mark a payment row as a repeat of another row for the same work (so it is not counted twice), or clear the mark."""
    p = db.get(m.Payment, pid)
    if not p: raise HTTPException(404, "Payment not found")
    if duplicate_of.strip() == "":
        p.duplicate_of_id = None
    else:
        try: other = db.get(m.Payment, int(duplicate_of))
        except ValueError: raise HTTPException(422, "duplicate_of must be a payment id")
        if not other or other.id == p.id or other.roadwork_id != p.roadwork_id or other.duplicate_of_id is not None:
            raise HTTPException(422, "duplicate_of must be a different, counted payment of the same work")
        p.duplicate_of_id = other.id
    audit(db, u, "payment_duplicate_flag", "payment", pid, {"duplicate_of": p.duplicate_of_id}); db.commit()
    return {"ok": True, "id": pid, "counted": p.duplicate_of_id is None, "duplicate_of_id": p.duplicate_of_id}


@router.get("/contractors")
def contractors(q: str = "", u=Depends(officer), db: Session = Depends(get_db)):
    """Works per contractor with the published award amounts (None when the sources publish none, as the BBMP open-data bill lists do)
    and the payments recorded against those works (net paid and gross bill amount, repeat records counted once)."""
    R, P = m.Roadwork, m.Payment
    award = func.coalesce(R.awarded_amount, R.sanctioned_amount)
    pay = counted_payments(db, P.roadwork_id.label("rid"), func.sum(P.amount).label("paid"),
                           func.sum(func.coalesce(P.gross_amount, P.amount)).label("gross"), func.count(P.id).label("n")).group_by(P.roadwork_id).subquery()
    qs = db.query(m.Contractor.id, m.Contractor.name, func.count(R.id), func.sum(award), func.count(award), func.sum(pay.c.paid), func.sum(pay.c.gross),
                  func.sum(pay.c.n), m.Contractor.is_demo) \
        .outerjoin(R, and_(R.contractor_id == m.Contractor.id, scope.visible_works())).outerjoin(pay, pay.c.rid == R.id).group_by(m.Contractor.id)
    if q: qs = qs.filter(m.Contractor.name.ilike(f"%{q}%"))
    return [{"id": i, "name": n, "works": c, "awarded": float(t) if na else None, "works_with_award": na, "paid": float(pd or 0),
             "gross_billed": float(g or 0), "payments": int(np or 0), "sample": bool(d)}
            for i, n, c, t, na, pd, g, np, d in qs.order_by(func.count(R.id).desc()).limit(200)]


# ----------------------------------------------------------------------------- documents
@router.get("/documents")
def list_documents(doc_type: str = "", match_status: str = "", q: str = "", page: int = 1, u=Depends(officer), db: Session = Depends(get_db)):
    qs = db.query(m.Document)
    if doc_type: qs = qs.filter(m.Document.doc_type == doc_type)
    if match_status: qs = qs.filter(m.Document.match_status == match_status)
    if q: qs = qs.filter(m.Document.filename.ilike(f"%{q}%"))
    total = qs.count()
    out = []
    for d in qs.order_by(m.Document.id.desc()).offset((page - 1) * 50).limit(50):
        top = db.query(m.RecordLink).filter_by(a_type="document", a_id=d.id, b_type="roadwork").filter(m.RecordLink.status != "rejected").order_by(m.RecordLink.confidence.desc()).first()
        out.append({"id": d.id, "filename": d.filename, "doc_type": d.doc_type, "type_label": DOC_TYPES.get(d.doc_type, d.doc_type),
                    "type_confidence": d.doc_type_confidence, "pages": d.pages, "ocr_pages": d.ocr_pages, "match_status": d.match_status, "status": d.status,
                    "ocr_flagged_pages": db.query(m.DocumentPage).filter_by(document_id=d.id, needs_review=True).count(), "roadwork_id": d.roadwork_id, "date": d.doc_date, "uploaded_at": d.created_at, "sample": d.is_demo,
                    "best_candidate": {"link_id": top.id, "roadwork_id": top.b_id, "label": _desc(db, "roadwork", top.b_id), "confidence": top.confidence, "status": top.status, "method": top.method} if top else None})
    return {"total": total, "items": out}


@router.post("/documents")
async def upload_documents(files: list[UploadFile] = File(...), source_name: str = Form(""), use_llm: bool = Form(False),
                           source_page_url: str = Form(""), download_url: str = Form(""), downloaded_at: str = Form(""),
                           u=Depends(officer), db: Session = Depends(get_db)):
    """source_page_url / download_url / downloaded_at record where the file came from (optional; one set applies to every file in the call)."""
    try:
        source_page_url, download_url = clean_url(source_page_url), clean_url(download_url)
    except ValueError as e:
        raise HTTPException(422, str(e))
    dl_at = parse_date(downloaded_at) if downloaded_at.strip() else None
    if downloaded_at.strip() and not dl_at:
        raise HTTPException(422, "downloaded_at must be a date such as 2026-11-12")
    results = []
    use_llm = bool(use_llm and os.getenv("ANTHROPIC_API_KEY"))
    for up in files:
        if not (up.filename or "").lower().endswith(".pdf"):
            results.append({"filename": up.filename, "error": "Only PDF files are accepted"}); continue
        raw = await up.read()
        if len(raw) > 30 * 1024 * 1024 or not raw.startswith(b"%PDF"):
            results.append({"filename": up.filename, "error": "Not a valid PDF, or larger than 30 MB"}); continue
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, os.path.basename(up.filename))
            open(p, "wb").write(raw)
            try:
                doc, created = ingest_document(db, p, os.path.basename(up.filename), user=u, source_name=source_name or None, use_llm=use_llm,
                                               source_page_url=source_page_url, download_url=download_url, downloaded_at=dl_at, provisional=True)
            except Exception as e:  # unreadable PDF must not break the batch
                db.rollback(); results.append({"filename": up.filename, "error": f"Could not process: {type(e).__name__}"}); continue
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        db.refresh(doc)
        results.append({"filename": up.filename, "document_id": doc.id, "duplicate_of_existing": not created, "doc_type": doc.doc_type,
                        "type_label": DOC_TYPES.get(doc.doc_type), "type_confidence": doc.doc_type_confidence, "pages": doc.pages,
                        "match_status": doc.match_status, "roadwork_id": doc.roadwork_id,
                        "source_page_url": doc.source_page_url, "download_url": doc.download_url, "created_work_id": getattr(doc, "_created_work_id", None),  # set only when this upload started a provisional work
                        "boq": boq_mod.summary_for(db, doc.id) if doc.doc_type == "boq" else None})
        audit(db, u, "document_upload", "document", doc.id, {"file": up.filename, "new": created})
    db.commit()
    return {"results": results, "llm_used": use_llm}


def _doc(db, did):
    d = db.get(m.Document, did)
    if not d: raise HTTPException(404, "Document not found")
    return d


@router.get("/documents/{did}")
def document_detail(did: int, u=Depends(officer), db: Session = Depends(get_db)):
    d = _doc(db, did)
    ex = db.query(m.Extraction).filter_by(document_id=did).order_by(m.Extraction.field, m.Extraction.page).all()
    cands = db.query(m.RecordLink).filter_by(a_type="document", a_id=did).order_by(m.RecordLink.confidence.desc()).all()
    events = db.query(m.MatchEvent).filter_by(subject_type="document", subject_id=did).order_by(m.MatchEvent.id).all()
    return {"id": d.id, "filename": d.filename, "doc_type": d.doc_type, "type_label": DOC_TYPES.get(d.doc_type), "type_confidence": d.doc_type_confidence,
            "classification_cues": (d.classification or {}).get("cues"), "pages": d.pages, "ocr_pages": d.ocr_pages, "match_status": d.match_status,
            "roadwork_id": d.roadwork_id, "date": d.doc_date, "source_name": d.source_name, "sample": d.is_demo, "sha256": d.sha256,
            "provenance": {"source_name": d.source_name, "source_page_url": d.source_page_url, "download_url": d.download_url,
                           "downloaded_at": d.downloaded_at, "original_filename": d.original_filename or d.filename,
                           "stored_filename": os.path.basename(d.path) if d.path else None, "sha256": d.sha256, "uploaded_at": d.created_at,
                           "uploaded_by": d.uploaded_by, "file_available": bool(d.path and os.path.exists(d.path)),
                           "note": None if (d.source_page_url or d.download_url) else "No web address was recorded for this file."},
            "extractions": [{"id": e.id, "field": e.field, "raw": e.raw_value, "norm": e.norm_value, "page": e.page, "confidence": e.confidence,
                             "method": e.method, "status": e.status, "snippet": e.snippet, "bbox": e.bbox} for e in ex],
            "candidates": [link_json(db, l) for l in cands],
            "ocr": {"summary": d.ocr_summary, "pages": [{"page": pg.page, "method": pg.method, "language": pg.ocr_lang, "confidence": pg.ocr_confidence, "dpi": pg.dpi,
                                                          "deskew_deg": pg.deskew_deg, "preprocess": pg.preprocess, "text_chars": pg.text_chars, "kannada_chars": pg.kannada_chars,
                                                          "needs_review": pg.needs_review, "review_reason": pg.review_reason}
                                                         for pg in db.query(m.DocumentPage).filter_by(document_id=did).order_by(m.DocumentPage.page)]},
            "boq": boq_mod.summary_for(db, did) if d.doc_type == "boq" or db.query(m.BoqItem.id).filter_by(document_id=did).first() else None,
            "history": [{"action": e.action, "detail": e.detail, "at": e.at, "by": e.by_user} for e in events]}


@router.patch("/documents/{did}/pages/{page}")
def review_page(did: int, page: int, note: str = Form(""), u=Depends(officer), db: Session = Depends(get_db)):
    """An officer has checked a flagged page against the original and corrected the extracted values (Extracted fields > Edit)."""
    pg = db.query(m.DocumentPage).filter_by(document_id=did, page=page).first()
    if not pg:
        raise HTTPException(404, "No such page")
    before = pg.review_reason
    pg.needs_review, pg.review_reason = False, ("checked by an officer" + (f": {note.strip()[:120]}" if note.strip() else ""))[:200]
    d = _doc(db, did)
    if not db.query(m.DocumentPage).filter_by(document_id=did, needs_review=True).count() and d.status == "needs_review" and d.doc_type != "other" and (d.doc_type_confidence or 0) >= 0.5:
        d.status = "processed"
    audit(db, u, "ocr_page_checked", "document", did, {"page": page, "was": before}); db.commit()
    return {"ok": True, "document_status": d.status}


@router.get("/ocr")
def ocr_status(u=Depends(officer)):
    """What the OCR engine on this server can read, and the settings in force."""
    from .. import ocr as ocrmod
    have = ocrmod.installed_languages()
    tries, warns = ocrmod.plan()
    return {"configured_language": ocrmod.configured_lang(), "will_try": tries, "warnings": warns, "installed": sorted(have & {"eng", "kan"}),
            "kannada_available": "kan" in have, "tesseract": ocrmod.tesseract_version(), "min_confidence": ocrmod.min_confidence(),
            "note": "Kannada pages are read when the 'kan' data is installed. Pages below the confidence threshold are flagged for manual correction."}


@router.patch("/documents/{did}/source")
def set_document_source(did: int, source_page_url: str = Form(None), download_url: str = Form(None), downloaded_at: str = Form(None),
                        source_name: str = Form(None), clear: str = Form(""), u=Depends(officer), db: Session = Depends(get_db)):
    """Add or correct where a document came from. Fields that are not sent are left alone; list field names in `clear`
    (comma separated: source_page_url, download_url, downloaded_at, source_name) to remove a stored value. Every change is audited."""
    d = _doc(db, did)
    to_clear = {x.strip() for x in clear.split(",") if x.strip()}
    if not to_clear <= {"source_page_url", "download_url", "downloaded_at", "source_name"}:
        raise HTTPException(422, "clear may only list source_page_url, download_url, downloaded_at, source_name")
    before = {"source_page_url": d.source_page_url, "download_url": d.download_url, "downloaded_at": d.downloaded_at, "source_name": d.source_name}
    try:
        if source_page_url is not None: d.source_page_url = clean_url(source_page_url)
        if download_url is not None: d.download_url = clean_url(download_url)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if downloaded_at is not None:
        dt = parse_date(downloaded_at) if downloaded_at.strip() else None
        if downloaded_at.strip() and not dt: raise HTTPException(422, "downloaded_at must be a date such as 2026-11-12")
        d.downloaded_at = dt
    if source_name is not None: d.source_name = source_name.strip()[:100] or None
    for f in to_clear:
        setattr(d, f, None)
    audit(db, u, "document_source_edit", "document", did, {"before": {k: str(v) for k, v in before.items()},
                                                          "after": {"source_page_url": d.source_page_url, "download_url": d.download_url}}); db.commit()
    return {"ok": True, "source_page_url": d.source_page_url, "download_url": d.download_url, "downloaded_at": d.downloaded_at, "source_name": d.source_name}


def _boq_json(b):
    return {"id": b.id, "document_id": b.document_id, "page": b.page, "table_index": b.table_index, "row": b.row_index, "kind": b.row_kind,
            "item_no": b.item_no, "description": b.description, "unit": b.unit, "quantity": b.quantity, "unit_rate": b.unit_rate,
            "total_amount": b.total_amount, "calc_total": b.calc_total, "check_status": b.check_status, "check_note": b.check_note,
            "method": b.method, "confidence": b.confidence, "status": b.status}


@router.get("/documents/{did}/boq")
def document_boq(did: int, u=Depends(officer), db: Session = Depends(get_db)):
    """Bill of quantities items read from this document, each with its page and the arithmetic check."""
    d = _doc(db, did)
    rows = db.query(m.BoqItem).filter_by(document_id=did).order_by(m.BoqItem.page, m.BoqItem.row_index).all()
    return {"document_id": did, "doc_type": d.doc_type, "summary": boq_mod.summary_for(db, did), "items": [_boq_json(b) for b in rows],
            "note": None if rows else ("No BOQ table was read from this document." if d.doc_type == "boq" else "This document is not classified as a BOQ.")}


@router.post("/documents/{did}/boq/extract")
def document_boq_extract(did: int, replace: bool = Form(False), u=Depends(officer), db: Session = Depends(get_db)):
    """Read the BOQ tables again (works for any PDF an officer says is a BOQ). Reviewed items are kept unless replace=true."""
    d = _doc(db, did)
    if not d.path or not os.path.exists(d.path):
        raise HTTPException(404, "File is no longer on the server")
    try:
        info = boq_mod.store(db, d, replace_reviewed=replace)
    except ValueError as e:
        raise HTTPException(409, str(e))
    audit(db, u, "boq_extract", "document", did, {"items": info["items"], "replace": replace}); db.commit()
    return {"ok": True, **info}


@router.patch("/boq-items/{bid}")
def review_boq_item(bid: int, status: str = Form(...), description: str = Form(None), unit: str = Form(None), quantity: float = Form(None),
                    unit_rate: float = Form(None), total_amount: float = Form(None), u=Depends(officer), db: Session = Depends(get_db)):
    """verified / rejected / unverified, or corrected with new values. Checks are recomputed for the whole document afterwards."""
    b = db.get(m.BoqItem, bid)
    if not b or status not in ("verified", "corrected", "rejected", "unverified"):
        raise HTTPException(422, "Unknown BOQ item or status")
    before = {k: getattr(b, k) for k in ("description", "unit", "quantity", "unit_rate", "total_amount")}
    if status == "corrected":
        if all(v is None for v in (description, unit, quantity, unit_rate, total_amount)):
            raise HTTPException(422, "Send at least one corrected value")
        for k, v in (("quantity", quantity), ("unit_rate", unit_rate), ("total_amount", total_amount)):
            if v is not None and v < 0:
                raise HTTPException(422, f"{k} cannot be negative")
        if description is not None: b.description = description.strip()[:500]
        if unit is not None: b.unit = unit.strip()[:30] or None
        if quantity is not None: b.quantity = quantity
        if unit_rate is not None: b.unit_rate = unit_rate
        if total_amount is not None: b.total_amount = total_amount
    b.status = status
    db.flush()
    summary = boq_mod.revalidate_stored(db, b.document_id)
    audit(db, u, "boq_item_review", "boq_item", bid, {"status": status, "before": before}); db.commit()
    db.refresh(b)
    return {"ok": True, "item": _boq_json(b), "summary": summary}


@router.get("/documents/{did}/file")
def document_file(did: int, u=Depends(officer), db: Session = Depends(get_db)):
    d = _doc(db, did)
    if not d.path or not os.path.exists(d.path): raise HTTPException(404, "File is no longer on the server")
    return FileResponse(d.path, media_type="application/pdf", filename=d.filename)


@router.get("/documents/{did}/extractions")
def get_extractions(did: int, u=Depends(officer), db: Session = Depends(get_db)):
    return document_detail(did, u, db)["extractions"]


@router.patch("/extractions/{eid}")
def review_extraction(eid: int, status: str = Form(...), norm_value: str = Form(None), u=Depends(officer), db: Session = Depends(get_db)):
    e = db.get(m.Extraction, eid)
    if not e or status not in ("verified", "corrected", "rejected", "unverified"):
        raise HTTPException(422, "Unknown extraction or status")
    if status == "corrected" and not (norm_value or "").strip():
        raise HTTPException(422, "A corrected value is required")
    e.status, e.corrected_by = status, u.id
    if norm_value is not None and status == "corrected":
        e.norm_value = norm_value.strip()
        e.raw_value = e.raw_value if e.raw_value else norm_value.strip()
    audit(db, u, "extraction_review", "extraction", eid, {"status": status, "field": e.field}); db.commit()
    d = db.get(m.Document, e.document_id)
    rematched = None
    if d and d.match_status not in ("verified", "manual"):
        rematched = run_document_matching(db, user=u, doc_ids=[d.id], only_unmatched=False)
    return {"ok": True, "rematched": rematched}


@router.post("/documents/{did}/rematch")
def rematch(did: int, u=Depends(officer), db: Session = Depends(get_db)):
    d = _doc(db, did)
    if d.match_status in ("verified", "manual"): raise HTTPException(409, "This document was assigned by an officer. Reopen the link to change it.")
    return run_document_matching(db, user=u, doc_ids=[did], only_unmatched=False)


@router.post("/documents/{did}/assign")
def assign(did: int, roadwork_id: int = Form(...), note: str = Form(""), u=Depends(officer), db: Session = Depends(get_db)):
    d = _doc(db, did)
    try:
        manual_assign_document(db, d, roadwork_id, u, note)
    except ValueError as e:
        raise HTTPException(422, str(e))
    audit(db, u, "document_assign", "document", did, {"roadwork_id": roadwork_id}); db.commit()
    return {"ok": True}


# ----------------------------------------------------------------------------- matching
@router.get("/matching/overview")
def matching_overview(u=Depends(officer), db: Session = Depends(get_db)):
    L = m.RecordLink
    link_counts = {f"{a}->{b}:{s}": n for a, b, s, n in db.query(L.a_type, L.b_type, L.status, func.count(L.id)).group_by(L.a_type, L.b_type, L.status)}
    docs = {s: db.query(func.count(m.Document.id)).filter(m.Document.match_status == s).scalar() for s in ("auto", "verified", "manual", "needs_review", "unmatched")}
    orphan_t = db.query(m.Tender).filter(~tender_attached()).limit(50).all()  # same rule as the dashboard
    runs = db.query(m.MatchRun).order_by(m.MatchRun.id.desc()).limit(8).all()
    return {"documents": docs, "link_counts": link_counts,
            "unmatched_documents": [{"id": d.id, "filename": d.filename, "type": DOC_TYPES.get(d.doc_type), "sample": d.is_demo}
                                    for d in db.query(m.Document).filter_by(match_status="unmatched").limit(50)],
            "needs_manual_verification": [{"id": d.id, "filename": d.filename, "type": DOC_TYPES.get(d.doc_type)} for d in db.query(m.Document).filter_by(match_status="needs_review").limit(50)],
            "unmatched_tenders": [{"id": t.id, "tender_id": t.tender_id, "title": t.title, "sample": t.is_demo} for t in orphan_t],
            "recent_runs": [{"id": r.id, "kind": r.kind, "stats": r.stats, "at": r.created_at} for r in runs],
            "policy": "Exact identifiers (job number, tender ID, work/contract ID) can link automatically. Name, road and description similarity only ever suggests a link for an officer to confirm."}


@router.post("/matching/run")
def run_matching(documents: bool = Form(True), tenders: bool = Form(True), roads: bool = Form(True), reprocess_all: bool = Form(False),
                 u=Depends(officer), db: Session = Depends(get_db)):
    if not _matching_run_lock.acquire(blocking=False):
        raise HTTPException(409, "A matching run is already in progress")
    try:
        out = {}
        if documents: out["documents"] = run_document_matching(db, user=u, only_unmatched=not reprocess_all)
        if tenders: out["tender_links"] = match_tenders(db, u)
        if roads: out["road_links"] = link_roads_to_works(db, u)
        audit(db, u, "match_run", "system", 0, out); db.commit()
        return out
    finally:
        _matching_run_lock.release()


@router.get("/links")
def list_links(status: str = "suggested", a_type: str = "", b_type: str = "", min_confidence: float = 0, page: int = 1,
               u=Depends(officer), db: Session = Depends(get_db)):
    qs = db.query(m.RecordLink).filter(m.RecordLink.confidence >= min_confidence)
    if status != "all": qs = qs.filter(m.RecordLink.status == status)
    if a_type: qs = qs.filter(m.RecordLink.a_type == a_type)
    if b_type: qs = qs.filter(m.RecordLink.b_type == b_type)
    total = qs.count()
    return {"total": total, "items": [link_json(db, l) for l in qs.order_by(m.RecordLink.confidence.desc()).offset((page - 1) * 50).limit(50)]}


@router.patch("/links/{lid}")
def set_link(lid: int, status: str = Form(...), u=Depends(officer), db: Session = Depends(get_db)):
    if status not in ("verified", "rejected", "unverified"): raise HTTPException(422, "Status must be verified, rejected or unverified")
    l = db.get(m.RecordLink, lid)
    if not l: raise HTTPException(404)
    review_link(db, l, status, u)
    audit(db, u, "link_review", "record_link", lid, {"status": status}); db.commit()
    return {"ok": True, "link": link_json(db, l)}


@router.get("/history")
def history(subject_type: str = "", subject_id: int | None = None, limit: int = Query(100, le=500), u=Depends(officer), db: Session = Depends(get_db)):
    qs = db.query(m.MatchEvent)
    if subject_type: qs = qs.filter_by(subject_type=subject_type)
    if subject_id is not None: qs = qs.filter_by(subject_id=subject_id)
    return [{"id": e.id, "run_id": e.run_id, "subject": f"{e.subject_type}:{e.subject_id}", "label": _desc(db, e.subject_type, e.subject_id) if e.subject_type in ("document", "tender", "roadwork") else None,
             "action": e.action, "detail": e.detail, "by": e.by_user, "at": e.at} for e in qs.order_by(m.MatchEvent.id.desc()).limit(limit)]


# ----------------------------------------------------------------------------- imports
@router.post("/imports/preview")
async def import_preview(file: UploadFile = File(...), kind: str = Form("works"), u=Depends(officer)):
    if kind not in ("works", "tenders"): raise HTTPException(422, "kind must be works or tenders")
    if not (file.filename or "").lower().endswith((".csv", ".xlsx", ".xls")): raise HTTPException(422, "CSV or Excel file required")
    data = await file.read()
    if len(data) > 25 * 1024 * 1024: raise HTTPException(422, "File larger than 25 MB")
    try:
        return preview_table(data, file.filename, kind)
    except ValueError as e:  # plain-language reason from read_table (empty file, web page saved as .xls, ...)
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(422, f"Could not read the file: {type(e).__name__}")


@router.post("/imports")
async def import_confirm(file: UploadFile = File(...), kind: str = Form("works"), source_name: str = Form(...), source_url: str = Form(""),
                         ward_regime: str = Form("243"), mapping_json: str = Form(""), roads_only: bool = Form(False), is_sample: bool = Form(False),
                         fiscal_year: str = Form(""), u=Depends(officer), db: Session = Depends(get_db)):
    import json
    data = await file.read()
    try:
        mapping = json.loads(mapping_json) if mapping_json else None
        if kind == "tenders":
            job = import_tenders(db, data, file.filename, source_name, source_url, u.id, mapping, is_demo=is_sample or None)
        else:
            job = import_table(db, data, file.filename, source_name, source_url, ward_regime, u.id, mapping, roads_only, is_demo=is_sample or None,
                               fiscal_year_label=fiscal_year or None)
    except Exception as e:
        db.rollback(); raise HTTPException(422, str(e) if isinstance(e, ValueError) else f"Import failed: {type(e).__name__}: {e}")
    audit(db, u, "import", "import_job", job.id, {"file": file.filename, "kind": kind}); db.commit()
    follow = {"documents": run_document_matching(db, user=u), "tender_links": match_tenders(db, u), "road_links": link_roads_to_works(db, u)}
    db.commit()
    return {"job_id": job.id, "status": job.status, "total": job.rows_total, "ok": job.rows_ok, "duplicate_rows": job.rows_duplicate,
            "skipped_non_road": job.rows_skipped, "failed": job.rows_failed, "column_map": job.column_map, "errors": (job.errors or [])[:20], "matching_after_import": follow}


@router.get("/imports")
def list_imports(u=Depends(officer), db: Session = Depends(get_db)):
    return [{"id": j.id, "file": j.filename, "kind": j.kind, "source": j.source_name, "regime": j.ward_regime, "status": j.status, "total": j.rows_total,
             "ok": j.rows_ok, "duplicate": j.rows_duplicate, "skipped": j.rows_skipped, "failed": j.rows_failed, "sample": j.is_demo, "at": j.created_at}
            for j in db.query(m.ImportJob).order_by(m.ImportJob.id.desc()).limit(100)]


# ----------------------------------------------------------------------------- roads (officer view)
@router.get("/roads")
def gov_roads(q: str = "", only: str = "", page: int = 1, u=Depends(officer), db: Session = Depends(get_db)):
    """only = 'with_reports' | 'without_records' | 'with_records'."""
    nrep = dict(db.query(m.Report.road_id, func.count(m.Report.id)).filter(m.Report.road_id.isnot(None), m.Report.is_duplicate.is_(False)).group_by(m.Report.road_id))
    nwork = dict(db.query(m.RecordLink.a_id, func.count(m.RecordLink.id)).filter(m.RecordLink.a_type == "road", m.RecordLink.b_type == "roadwork", m.RecordLink.status != "rejected").group_by(m.RecordLink.a_id))
    qs = db.query(m.Road)
    if q: qs = qs.filter(m.Road.name.ilike(f"%{q}%"))
    items = [{"id": r.id, "name": r.name or "Unnamed road", "class": r.road_class, "length_m": round(r.length_m or 0), "reports": nrep.get(r.id, 0),
              "government_records": nwork.get(r.id, 0), "gov_status": road_gov_status(db, r.id), "approx": r.is_approx, "sample": r.is_demo, "source": r.source} for r in qs.order_by(m.Road.name)]
    if only == "with_reports": items = [i for i in items if i["reports"]]
    elif only == "without_records": items = [i for i in items if not i["government_records"]]
    elif only == "with_records": items = [i for i in items if i["government_records"]]
    return {"total": len(items), "items": items[(page - 1) * 100: page * 100]}


# ----------------------------------------------------------------------------- evaluation
_eval_cache = {}


@router.get("/evaluation")
def evaluation(refresh: bool = False, u=Depends(officer), db: Session = Depends(get_db)):
    """Runs the evaluation harness (extraction, matching, clustering, alerts). Cached for 10 minutes; refresh=true recomputes."""
    from ..evaluation import run_all
    now = datetime.utcnow()
    if refresh or "r" not in _eval_cache or now - _eval_cache["t"] > timedelta(minutes=10):
        _eval_cache.update(r=run_all(db), t=now)
    return _eval_cache["r"]
