"""Cross-document matching engine.

Signal order (strongest first), exactly as specified for the project:
  1. BBMP job number           exact  -> 0.99
  2. Tender ID                 exact  -> 0.97 (0.95 when reached through a tender record's job number)
  3. Work ID / contract ref    exact  -> 0.95 (work order number counts as a contract reference)
  4. Contractor name           fuzzy
  5. Road name / location      fuzzy
  6. Work description          fuzzy
Exact identifiers decide on their own. Fuzzy signals are only a fallback, are capped below the auto-accept
level, and ALWAYS end as 'suggested' (needs manual verification): uncertain matches are never merged.

Tiers for a document -> roadwork decision
  auto          one exact identifier match, no contradicting identifier        (document.roadwork_id is set)
  needs_review  conflicting identifiers, several exact candidates, or fuzzy-only evidence
  unmatched     nothing credible. The document is kept and listed as unmatched.
Every decision is appended to match_events (history) and tied to a match_runs row.
"""
import re
from datetime import datetime
from rapidfuzz import fuzz
from sqlalchemy import or_
from sqlalchemy.orm import Session
from . import models as m
from .sources import add_payment, sync_tender_link, sync_all_tender_links
from .textnorm import (norm_id, norm_text, norm_contractor, norm_road_name, road_names_in_text,
                       distinctive_tokens, parse_amount, parse_date)

EXACT_STRENGTH = {"job_number": 0.99, "tender_id": 0.97, "work_id": 0.95, "work_order_id": 0.95}
TENDER_VIA_JOB = 0.95
FUZZY_WEIGHTS = {"contractor": 0.25, "road": 0.30, "description": 0.35, "amount": 0.10}
FUZZY_CAP = 0.88
SUGGEST_MIN = 0.50
AUTO_STATUSES = ("auto", "verified")


# ----------------------------------------------------------------------------- helpers
def log_event(db, run, subject_type, subject_id, action, detail, link=None, user=None):
    db.add(m.MatchEvent(run_id=run.id if run else None, link_id=link.id if link else None, subject_type=subject_type,
                        subject_id=subject_id, action=action, detail=detail, by_user=user.id if user else None))


def new_run(db, kind, params=None, user=None):
    r = m.MatchRun(kind=kind, params=params or {}, stats={}, created_by=user.id if user else None)
    db.add(r); db.flush()
    return r


def upsert_link(db, a_type, a_id, b_type, b_id, method, conf, evidence, status, run=None):
    """Create a link unless one exists. A human decision (verified / rejected) is never overwritten."""
    ex = db.query(m.RecordLink).filter_by(a_type=a_type, a_id=a_id, b_type=b_type, b_id=b_id).first()
    if ex:
        if ex.status in ("verified", "rejected"):
            return ex, False
        ex.method, ex.confidence, ex.evidence, ex.status = method, conf, evidence, status
        return ex, False
    link = m.RecordLink(a_type=a_type, a_id=a_id, b_type=b_type, b_id=b_id, method=method, confidence=conf,
                        evidence=evidence, status=status, run_id=run.id if run else None)
    db.add(link); db.flush()
    return link, True


def doc_values(db, doc):
    """field -> list of {value, norm, page, conf, extraction_id, status}, best first. Rejected values are ignored;
    reviewer-verified/corrected values rank above everything else."""
    out = {}
    rows = db.query(m.Extraction).filter(m.Extraction.document_id == doc.id, m.Extraction.status != "rejected").all()
    for e in rows:
        rank = 2 if e.status in ("verified", "corrected") else 1
        out.setdefault(e.field, []).append({"value": e.raw_value, "norm": e.norm_value, "page": e.page,
                                            "conf": 1.0 if rank == 2 else (e.confidence or 0), "extraction_id": e.id,
                                            "status": e.status, "_rank": rank})
    for k in out:
        out[k].sort(key=lambda x: (-x["_rank"], -x["conf"], x["page"] or 0))
    return out


def _first(vals, field, min_conf=0.0):
    for v in vals.get(field, []):
        if v["conf"] >= min_conf:
            return v
    return None


class WorkIndex:
    """In-memory lookup over roadworks and tenders for one matching run."""

    def __init__(self, db):
        self.works = {w.id: w for w in db.query(m.Roadwork).filter(or_(m.Roadwork.verification_status.is_(None), m.Roadwork.verification_status != "merged"))}  # a merged profile is history, not a target
        self.contractors = {c.id: c for c in db.query(m.Contractor).all()}
        self.by_field = {f: {} for f in ("job_number", "tender_id", "work_id", "work_order_id")}
        for w in self.works.values():
            for f in self.by_field:
                v = getattr(w, f)
                if v:
                    self.by_field[f].setdefault(norm_id(v), []).append(w.id)
        self.tender_by_id = {}
        for t in db.query(m.Tender).all():
            if t.tender_id:
                self.tender_by_id.setdefault(norm_id(t.tender_id), []).append(t)
        self.road_names = {}
        for w in self.works.values():
            self.road_names[w.id] = {norm_road_name(n) for n in road_names_in_text(w.description or "")}

    def contractor_norm(self, w):
        c = self.contractors.get(w.contractor_id)
        return c.name_norm if c else None


def _exact_hits(vals, idx: WorkIndex):
    """-> {work_id: [signal dicts]}, plus the list of (field, value) identifiers that point at some work."""
    hits = {}

    def add(wid, signal, strength, field, v, work_value):
        hits.setdefault(wid, []).append({"signal": signal, "strength": strength, "doc_field": field, "doc_value": v["value"],
                                         "work_value": work_value, "page": v["page"], "extraction_id": v["extraction_id"]})
    for field, wfield in (("job_number", "job_number"), ("tender_id", "tender_id"), ("work_id", "work_id"),
                          ("work_order_id", "work_order_id"), ("work_id", "work_order_id"), ("work_order_id", "work_id")):
        for v in vals.get(field, [])[:3]:
            key = norm_id(v["norm"])
            for wid in idx.by_field[wfield].get(key, []):
                sig = field if field == wfield else f"{field}~{wfield}"
                add(wid, sig, EXACT_STRENGTH[wfield if field == wfield else "work_id"], field, v, getattr(idx.works[wid], wfield))
    # tender ID -> tender record -> its job number -> roadwork
    for v in vals.get("tender_id", [])[:3]:
        for t in idx.tender_by_id.get(norm_id(v["norm"]), []):
            if t.job_number:
                for wid in idx.by_field["job_number"].get(norm_id(t.job_number), []):
                    add(wid, "tender_id->tender.job_number", TENDER_VIA_JOB, "tender_id", v, t.job_number)
    return hits


def _fuzzy_signals(vals, w, idx: WorkIndex):
    """Contractor / road / description / amount similarity between a document and one roadwork."""
    sig = {}
    dc = _first(vals, "contractor")
    wc = idx.contractor_norm(w)
    if dc and wc:
        dn = norm_contractor(dc["norm"])
        s = max(fuzz.ratio(dn, wc), fuzz.token_set_ratio(dn, wc) * 0.95)
        sig["contractor"] = {"score": round(s / 100, 2), "doc": dc["value"], "work": wc, "page": dc["page"]}
    # road / location
    dr = _first(vals, "road_name") or _first(vals, "location")
    wnames = idx.road_names.get(w.id, set())
    wdesc_norm = w.description_norm or norm_text(w.description)
    if dr and (wnames or wdesc_norm):
        dn = norm_road_name(dr["norm"])
        dist = distinctive_tokens(dn)
        best = 0.0
        if dn and wdesc_norm:
            if dn in norm_road_name(w.description or ""):
                best = 1.0
            elif wnames:
                best = max(fuzz.ratio(dn, wn) for wn in wnames) / 100
            if not dist:  # a generic name like 'main road' carries little information
                best *= 0.5
        sig["road"] = {"score": round(best, 2), "doc": dr["value"], "work": sorted(wnames)[:3], "page": dr["page"]}
    dd = _first(vals, "work_description")
    if dd and wdesc_norm:
        s = fuzz.token_set_ratio(norm_text(dd["norm"]), wdesc_norm) / 100
        sig["description"] = {"score": round(s, 2), "doc": dd["value"][:120], "work": (w.description or "")[:120], "page": dd["page"]}
    da = _first(vals, "awarded_amount") or _first(vals, "estimated_cost")
    wa = w.awarded_amount or w.sanctioned_amount or w.estimated_cost
    if da and wa:
        a = parse_amount(da["norm"])
        if a:
            rel = abs(a - wa) / max(wa, 1)
            sig["amount"] = {"score": 1.0 if rel <= 0.01 else (0.5 if rel <= 0.05 else 0.0), "doc": a, "work": wa, "page": da["page"]}
    return sig


def _fuzzy_score(sig):
    avail = {k: v["score"] for k, v in sig.items()}
    strong = [k for k, v in avail.items() if v >= 0.75]
    if len(strong) < 2 or not ({"description", "road"} & set(strong)):
        return 0.0  # needs two independent agreeing signals, one of them road or description
    num = sum(FUZZY_WEIGHTS[k] * v for k, v in avail.items())
    den = sum(FUZZY_WEIGHTS[k] for k in avail)
    return round(min(FUZZY_CAP, num / den), 2)


def _candidate_works(vals, idx):
    """Cheap pre-filter for fuzzy matching: same contractor, or token overlap with the description."""
    dd = _first(vals, "work_description")
    dtoks = set(norm_text(dd["norm"]).split()) if dd else set()
    dc = _first(vals, "contractor")
    dcn = norm_contractor(dc["norm"]) if dc else None
    scored = []
    for w in idx.works.values():
        s = 0.0
        if dtoks:
            wt = set((w.description_norm or norm_text(w.description)).split())
            if wt:
                s = len(dtoks & wt) / len(dtoks | wt)
        if dcn and idx.contractor_norm(w) == dcn:
            s += 0.3
        if s > 0.25:
            scored.append((s, w.id))
    return [wid for _, wid in sorted(scored, reverse=True)[:25]]


# ----------------------------------------------------------------------------- document -> roadwork
def match_document(db: Session, doc: m.Document, idx: WorkIndex | None = None, run=None, user=None):
    """Decide the roadwork for one document. Returns dict(status, candidates, conflicts)."""
    idx = idx or WorkIndex(db)
    vals = doc_values(db, doc)
    hits = _exact_hits(vals, idx)
    conflicts = []
    cands = []  # (work_id, confidence, method, evidence)
    # identifiers that DO exist in the data but point at different works contradict each other
    if len(hits) > 1:
        by_signal = {}
        for wid, sigs in hits.items():
            for s in sigs:
                by_signal.setdefault(s["doc_field"], set()).add(wid)
        conflicts = [{"field": f, "works": sorted(ws)} for f, ws in by_signal.items()]
    if hits:
        for wid, sigs in hits.items():
            base = max(s["strength"] for s in sigs)
            fz = _fuzzy_signals(vals, idx.works[wid], idx)
            evidence = {"exact": sigs, "corroboration": fz, "conflicts": conflicts}
            method = "exact:" + "+".join(sorted({s["signal"] for s in sigs}))
            cands.append((wid, base, method, evidence))
        cands.sort(key=lambda c: -c[1])
        single = len(hits) == 1
        status = "auto" if single and not conflicts else "suggested"
        if not single or conflicts:
            cands = [(wid, round(conf * 0.7, 2), meth, ev) for wid, conf, meth, ev in cands]
    else:
        for wid in _candidate_works(vals, idx):
            fz = _fuzzy_signals(vals, idx.works[wid], idx)
            sc = _fuzzy_score(fz)
            if sc >= SUGGEST_MIN:
                cands.append((wid, sc, "fuzzy:" + "+".join(k for k, v in fz.items() if v["score"] >= 0.75),
                              {"fuzzy": fz, "note": "Fuzzy evidence only. Never merged automatically."}))
        cands.sort(key=lambda c: -c[1])
        status = "suggested"
    cands = cands[:3]
    result = {"status": "unmatched", "candidates": [], "conflicts": conflicts}
    if not cands:
        doc.match_status = "unmatched"
        log_event(db, run, "document", doc.id, "unmatched", {"reason": "no exact identifier and no credible fuzzy candidate"}, user=user)
        return result
    for wid, conf, method, evidence in cands:
        link, created = upsert_link(db, "document", doc.id, "roadwork", wid, method, conf, evidence, status, run)
        effective = link.status
        result["candidates"].append({"link_id": link.id, "roadwork_id": wid, "confidence": link.confidence, "method": link.method,
                                     "status": effective})
        if created:
            log_event(db, run, "document", doc.id, "auto_linked" if effective == "auto" else ("conflict" if conflicts else "suggested"),
                      {"roadwork_id": wid, "confidence": conf, "method": method, "conflicts": conflicts}, link=link, user=user)
    top = result["candidates"][0]
    if top["status"] in AUTO_STATUSES:
        doc.roadwork_id = top["roadwork_id"]
        doc.match_status = "verified" if top["status"] == "verified" else "auto"
        apply_document_to_work(db, doc, idx.works[top["roadwork_id"]], vals, run=run, user=user)
        result["status"] = "auto"
    elif top["status"] == "rejected":
        doc.match_status = "unmatched"; result["status"] = "unmatched"
    else:
        doc.match_status = "needs_review"
        result["status"] = "needs_review"
    return result


def apply_document_to_work(db, doc, work, vals=None, run=None, user=None):
    """Copy values from a linked document into EMPTY roadwork fields only (never overwrite official data),
    and record what was copied. Statuses come only from what the document literally is."""
    vals = vals or doc_values(db, doc)
    filled = {}

    def put(attr, val, field):
        if val is not None and getattr(work, attr) in (None, ""):
            setattr(work, attr, val); filled[attr] = {"from": field, "document_id": doc.id}
    g = lambda f, mc=0.7: _first(vals, f, mc)
    for attr, field in (("tender_id", "tender_id"), ("job_number", "job_number"), ("work_id", "work_id"), ("work_order_id", "work_order_id"),
                        ("department", "department")):
        v = g(field)
        if v: put(attr, v["norm"] if attr.endswith(("_id", "_number")) else v["value"], field)
    for attr, field in (("estimated_cost", "estimated_cost"), ("awarded_amount", "awarded_amount")):
        v = g(field)
        if v: put(attr, parse_amount(v["norm"]), field)
    for attr, field in (("commencement_date", "commencement_date"), ("completion_deadline", "completion_deadline"), ("work_order_date", "work_order_date")):
        v = g(field)
        if v and not (doc.doc_type == "completion_certificate" and attr == "completion_deadline"):
            put(attr, parse_date(v["norm"]), field)
    v = g("road_name")
    if v: put("road_name_extracted", v["value"], "road_name")
    v = g("location")
    if v: put("location_text", v["value"][:300], "location")
    if doc.doc_type == "payment_record":
        amt = parse_amount((g("payment_amount") or {}).get("norm"))
        pdate = parse_date((g("payment_date") or {}).get("norm")) or doc.doc_date
        if amt and not db.query(m.Payment).filter_by(roadwork_id=work.id, amount=amt, document_id=doc.id).first():
            pay = add_payment(db, work.id, amt, pdate, reference=f"document:{doc.id}", document_id=doc.id)
            filled["payment"] = {"amount": amt, "document_id": doc.id, "counted": pay.duplicate_of_id is None,
                                 **({"duplicate_of_payment_id": pay.duplicate_of_id} if pay.duplicate_of_id else {})}
    # Declared status: only a document that says so can set it. Payments never do.
    status_by_doc = {"work_order": "work_order_issued", "completion_certificate": "completion_certificate_on_file"}
    if doc.doc_type in status_by_doc:
        order = ["not_stated", "work_order_issued", "completion_certificate_on_file"]
        new = status_by_doc[doc.doc_type]
        if order.index(new) > order.index(work.work_status or "not_stated"):
            work.work_status = new; work.work_status_source = f"document:{doc.id} ({doc.doc_type})"
            filled["work_status"] = {"value": new, "document_id": doc.id}
    if filled:
        log_event(db, run, "document", doc.id, "enriched", {"roadwork_id": work.id, "fields": filled}, user=user)
    return filled


def run_document_matching(db, user=None, only_unmatched=True, doc_ids=None):
    """Match documents against roadworks. Returns summary dict and writes one MatchRun."""
    run = new_run(db, "documents", {"only_unmatched": only_unmatched}, user)
    idx = WorkIndex(db)
    q = db.query(m.Document)
    if doc_ids:
        q = q.filter(m.Document.id.in_(doc_ids))
    elif only_unmatched:
        q = q.filter(m.Document.match_status.in_(["unmatched", "needs_review"]))
    stats = {"documents": 0, "auto": 0, "needs_review": 0, "unmatched": 0}
    for d in q.all():
        if d.match_status in ("verified", "manual"):
            continue
        r = match_document(db, d, idx, run, user)
        stats["documents"] += 1; stats[r["status"]] += 1
    run.stats = stats
    db.commit()
    return {"run_id": run.id, **stats}


# ----------------------------------------------------------------------------- tender -> roadwork
FUZZY_CHUNK = 400


def _title_candidates(tenders, idx):
    """{tender_id: [(text_score, work)]} for every pair with token_set_ratio >= 80, computed in vectorised chunks (rapidfuzz.process.cdist).
    Same scorer and threshold as a pair-by-pair loop, but fast enough for real volumes (thousands of tenders x thousands of works)."""
    import numpy as np
    from rapidfuzz import process
    works = [w for w in idx.works.values() if w.description]
    wtexts = [w.description_norm or norm_text(w.description) for w in works]
    todo = [t for t in tenders if t.title]
    out = {}
    for i in range(0, len(todo), FUZZY_CHUNK):
        chunk = todo[i:i + FUZZY_CHUNK]
        mat = process.cdist([norm_text(t.title) for t in chunk], wtexts, scorer=fuzz.token_set_ratio, score_cutoff=80, workers=-1, dtype=np.uint8)
        for r, c in zip(*np.nonzero(mat)):
            out.setdefault(chunk[r].id, []).append((int(mat[r, c]), works[c]))
    return out


GENERIC = set("""a an and the of to in at on for by with from near its surrounding surroundings main cross road roads rd street st lane drain drains
improvement improvements improving providing provide construction constructing repair repairs special maintenance development works work ward wards no
number division sub zone layout area areas nagar nagara bbmp bengaluru bangalore package consisting consists balance various other existing new
asphalting asphalt footpath footpaths cc storm water swd side both kerb laying supply supplying installation
includes include including lakhs lakh crore crores amount total cost estimated estimate""".split())
_WARD_NO = re.compile(r"\bward\s*(?:no\.?|number)?\s*[-:.]?\s*(\d{1,3})\b", re.I)
_CODE_YEAR = re.compile(r"^\d{3}-(\d{2})-\d{6}$")


def _distinctive(text):
    return {w for w in re.findall(r"[a-z][a-z]{3,}", (text or "").lower()) if w not in GENERIC}


def word_rarity(works):
    """Document frequency of each distinctive word over all work descriptions; a word used by many works says little about a match."""
    from collections import Counter
    df = Counter()
    for w in works:
        df.update(_distinctive(w.description))
    limit = max(15, int(0.003 * max(len(works), 1)))
    return df, limit


def _plausibility(t, w, rarity=None):
    """Checks that can rule a description match out. -> (ok, evidence)."""
    ev = {}
    shared = sorted(_distinctive(t.title) & _distinctive(w.description))
    ev["shared_distinctive_words"] = shared[:8]
    if len(shared) < 2:
        return False, {**ev, "rejected": "fewer than 2 shared distinctive words (only generic wording in common)"}
    if rarity is not None:
        df, limit = rarity
        rare = [x for x in shared if df.get(x, 0) <= limit]
        ev["shared_rare_words"] = rare[:6]
        if not rare:
            return False, {**ev, "rejected": "no shared word that is rare among work descriptions (for example a place or road name)"}
    tw, ww = set(_WARD_NO.findall(t.title or "")), set(_WARD_NO.findall(w.description or ""))
    tyear = t.published_date.year if t.published_date else None
    if tw and ww and w.ward_regime == "198" and tyear and tyear <= 2022:  # ward numbers are comparable only within one regime
        ev["ward_numbers"] = {"tender": sorted(tw), "work": sorted(ww)}
        if not tw & ww:
            return False, {**ev, "rejected": "different ward numbers (both 198-ward regime)"}
    mm = _CODE_YEAR.match(w.work_order_id or "")
    if mm and tyear:
        jy = 2000 + int(mm.group(1))
        ev["years"] = {"tender_published": tyear, "work_code_year": jy}
        if not (tyear - 1 <= jy <= tyear + 2):
            return False, {**ev, "rejected": "work code year is not within 2 years after the tender"}
    return True, ev


def match_tenders(db: Session, user=None, tender_ids=None) -> int:
    """Connect tenders to roadworks. Exact identifiers become 'auto' links, fuzzy ones stay 'suggested'. tender_ids limits the run."""
    run = new_run(db, "tenders", {"tender_ids": tender_ids} if tender_ids else {}, user)
    idx = WorkIndex(db)
    n = 0
    q = db.query(m.Tender)
    if tender_ids:
        q = q.filter(m.Tender.id.in_(tender_ids))
    tenders = q.all()
    cands = _title_candidates(tenders, idx)
    rarity = word_rarity([w for w in idx.works.values() if w.description])
    for t in tenders:
        exact = {}
        if t.job_number:
            for wid in idx.by_field["job_number"].get(norm_id(t.job_number), []):
                exact[wid] = ("exact_job_number", 0.99, {"job_number": t.job_number})
        if t.tender_id:
            for wid in idx.by_field["tender_id"].get(norm_id(t.tender_id), []):
                if wid not in exact:
                    exact[wid] = ("exact_tender_id", 0.97, {"tender_id": t.tender_id})
        if exact:
            for wid, (meth, conf, ev) in exact.items():
                single = len(exact) == 1
                link, created = upsert_link(db, "roadwork", wid, "tender", t.id, meth, conf if single else conf * 0.7, ev,
                                            "auto" if single else "suggested", run)
                if created:
                    log_event(db, run, "tender", t.id, "auto_linked" if single else "suggested", {"roadwork_id": wid, **ev}, link=link, user=user)
                n += 1
                if single and link.status in AUTO_STATUSES:
                    w = idx.works[wid]
                    if not w.tender_id and t.tender_id:
                        w.tender_id = t.tender_id
            continue
        if not t.title:
            continue
        best = []
        for s, w in cands.get(t.id, []):
            amt_ok = bool(t.awarded_value and (w.awarded_amount or w.sanctioned_amount) and
                          abs(t.awarded_value - (w.awarded_amount or w.sanctioned_amount)) / max(w.awarded_amount or w.sanctioned_amount, 1) < 0.02)
            ctr_ok = bool(t.contractor_id and t.contractor_id == w.contractor_id)
            plausible, pev = _plausibility(t, w, rarity)
            if not plausible:
                continue
            score = 0.0
            if s >= 85 and amt_ok:
                score = 0.75 + (0.05 if ctr_ok else 0) + (0.03 if s >= 95 else 0)
            elif s >= 93:
                score = 0.5
            if score:
                best.append((score, w.id, {"text_score": s, "tender_value": t.awarded_value, "wo_amount": w.awarded_amount or w.sanctioned_amount,
                                           "contractor_match": ctr_ok, **pev}))
        for score, wid, ev in sorted(best, reverse=True)[:2]:
            meth = "description_plus_amount" if score >= 0.75 else "description_only"
            link, created = upsert_link(db, "roadwork", wid, "tender", t.id, meth, round(score, 2), ev, "suggested", run)
            if created:
                log_event(db, run, "tender", t.id, "suggested", {"roadwork_id": wid, "method": meth, **ev}, link=link, user=user)
            n += 1
    run.stats = {"links": n}
    db.flush()
    sync_all_tender_links(db)  # the tender's own source row now also backs the work (work_source_links)
    db.commit()
    return n


# ----------------------------------------------------------------------------- road <-> roadwork
def link_roads_to_works(db: Session, user=None, regime=None):
    """Infer which mapped roads a roadwork refers to. Roadwork records carry no coordinates, so the road
    name in the description plus the ward are the only evidence.

    auto       exact normalised road name AND the road runs through the roadwork's ward
    suggested  everything else that scores >= 0.5  (shown to the public as 'inferred')
    """
    run = new_run(db, "roads", {}, user)
    roads = db.query(m.Road).filter(m.Road.name_norm.isnot(None)).all()
    by_name = {}
    for r in roads:
        by_name.setdefault(r.name_norm, []).append(r)
    ward_by_road = {}
    for rid, regime_, num in db.query(m.RoadSegment.road_id, m.RoadSegment.ward_regime, m.RoadSegment.ward_number).distinct():
        ward_by_road.setdefault(rid, set()).add((regime_, num))
    inv = {}
    for nm in by_name:
        for tok in distinctive_tokens(nm):
            inv.setdefault(tok, set()).add(nm)
    made = {"auto": 0, "suggested": 0, "works_with_no_road_name": 0}
    for w in db.query(m.Roadwork).filter(m.Roadwork.description.isnot(None)).all():
        names = [norm_road_name(n) for n in road_names_in_text(w.description)]
        if not names:
            made["works_with_no_road_name"] += 1
            continue
        cand_names = set()
        for nm in names:
            if nm in by_name:
                cand_names.add(nm)
            for tok in distinctive_tokens(nm):
                cand_names |= inv.get(tok, set())
        wkey = (w.ward_regime, w.ward_number) if w.ward_number and not w.ward_is_multi else None
        for cn in cand_names:
            exact = cn in names
            best = 1.0 if exact else max(fuzz.ratio(cn, n) for n in names) / 100
            if best < 0.8:
                continue
            generic = not distinctive_tokens(cn)
            for r in by_name[cn]:
                ward_ok = bool(wkey and wkey in ward_by_road.get(r.id, set()))
                ward_known = bool(wkey and any(k[0] == w.ward_regime for k in ward_by_road.get(r.id, set())))
                if exact and ward_ok and not generic:
                    conf, status, why = 0.9, "auto", "exact road name and the road runs through the roadwork's ward"
                elif exact and ward_ok:
                    conf, status, why = 0.65, "suggested", "generic road name; ward agrees"
                elif exact and not ward_known:
                    conf, status, why = 0.6, "suggested", "exact road name; ward not comparable (different ward regime or no ward)"
                elif exact:
                    conf, status, why = 0.35, "suggested", "exact road name but the road does not pass through the roadwork's ward"
                else:
                    conf, status, why = round(0.6 * best + (0.1 if ward_ok else 0), 2), "suggested", f"fuzzy road name ({best:.2f})"
                if conf < SUGGEST_MIN and status != "auto":
                    continue
                ev = {"road_name_in_work": names[:2], "road_name": r.name, "name_score": round(best, 2), "ward_match": ward_ok,
                      "work_ward": f"{w.ward_regime}:{w.ward_number}", "why": why,
                      "note": "Roadwork records carry no coordinates; this link is inferred from the road name and ward."}
                link, created = upsert_link(db, "road", r.id, "roadwork", w.id, "road_name_ward" if exact else "road_name_fuzzy", conf, ev, status, run)
                if created:
                    made[link.status if link.status in made else "suggested"] += 1
                    log_event(db, run, "roadwork", w.id, "auto_linked" if status == "auto" else "suggested",
                              {"road_id": r.id, "confidence": conf, "why": why}, link=link, user=user)
    run.stats = made
    db.commit()
    return made


def works_for_road(db, road_id, include_rejected=False):
    qs = db.query(m.RecordLink).filter_by(a_type="road", a_id=road_id, b_type="roadwork")
    if not include_rejected:
        qs = qs.filter(m.RecordLink.status != "rejected")
    return qs.all()


def suggest_roadworks_for_cluster(db: Session, cluster: m.Cluster, reports, limit=3):
    """Link a complaint cluster to roadworks via its road (preferred), else via the road names citizens typed.
    Every link stays 'suggested': it needs officer review before the public sees it as related."""
    out = []
    if cluster.road_id:
        for l in sorted(works_for_road(db, cluster.road_id), key=lambda l: -l.confidence)[:limit]:
            conf = round(l.confidence * 0.9, 2)
            link, _ = upsert_link(db, "cluster", cluster.id, "roadwork", l.b_id, "via_road_link", conf,
                                  {"road_id": cluster.road_id, "road_link_id": l.id, "road_link_status": l.status,
                                   "note": "Cluster is on this road; the road is linked to this roadwork by name and ward."}, "suggested")
            out.append(link)
        if out:
            return out
    names = {r.road_name.strip().lower() for r in reports if r.road_name and len(r.road_name.strip()) >= 4}
    if not names:
        return []
    scored = []
    for w in db.query(m.Roadwork).filter(m.Roadwork.description.isnot(None)).all():
        best = max(fuzz.partial_ratio(n, w.description.lower()) for n in names)
        if best >= 85:
            scored.append((best, w))
    scored.sort(key=lambda x: -x[0])
    for score, w in scored[:limit]:
        link, _ = upsert_link(db, "cluster", cluster.id, "roadwork", w.id, "road_name_fuzzy", round(score / 100 * 0.6, 2),
                              {"road_names": sorted(names), "name_score": score,
                               "note": "Roadwork records carry no coordinates. Match is by road name only."}, "suggested")
        out.append(link)
    return out


# ----------------------------------------------------------------------------- human review
def review_link(db, link: m.RecordLink, status: str, user):
    """verified / rejected / unverified (reopen). Keeps document <-> roadwork pointers consistent."""
    old = link.status
    link.status, link.reviewer_id, link.reviewed_at = status, user.id, datetime.utcnow()
    if link.a_type == "document" and link.b_type == "roadwork":
        doc = db.get(m.Document, link.a_id)
        if doc:
            if status == "verified":
                doc.roadwork_id, doc.match_status = link.b_id, "verified"
                # other candidate links for this document are superseded
                for o in db.query(m.RecordLink).filter(m.RecordLink.a_type == "document", m.RecordLink.a_id == doc.id,
                                                       m.RecordLink.id != link.id, m.RecordLink.status.in_(["suggested", "auto"])):
                    o.status = "rejected"
                    log_event(db, None, "document", doc.id, "rejected", {"link_id": o.id, "reason": "another candidate verified"}, link=o, user=user)
                apply_document_to_work(db, doc, db.get(m.Roadwork, link.b_id), user=user)
            elif status == "rejected" and doc.roadwork_id == link.b_id:
                doc.roadwork_id = None
                still = db.query(m.RecordLink).filter(m.RecordLink.a_type == "document", m.RecordLink.a_id == doc.id,
                                                      m.RecordLink.status.in_(["suggested", "auto"]), m.RecordLink.id != link.id).count()
                doc.match_status = "needs_review" if still else "unmatched"
            elif status == "rejected":
                still = db.query(m.RecordLink).filter(m.RecordLink.a_type == "document", m.RecordLink.a_id == doc.id,
                                                      m.RecordLink.status.in_(["suggested", "auto", "verified"]), m.RecordLink.id != link.id).count()
                if not still:
                    doc.match_status = "unmatched"
    if link.a_type == "roadwork" and link.b_type == "tender":
        sync_tender_link(db, link)
    log_event(db, None, link.a_type, link.a_id, status if status != "unverified" else "reopened",
              {"link_id": link.id, "from": old, "to": status, "target": f"{link.b_type}:{link.b_id}"}, link=link, user=user)
    return link


def manual_assign_document(db, doc, roadwork_id, user, note=""):
    w = db.get(m.Roadwork, roadwork_id)
    if not w:
        raise ValueError("Unknown roadwork")
    link, _ = upsert_link(db, "document", doc.id, "roadwork", roadwork_id, "manual", 1.0, {"note": note or "assigned by an officer"}, "verified")
    link.status, link.reviewer_id, link.reviewed_at = "verified", user.id, datetime.utcnow()
    return review_link(db, link, "verified", user)
