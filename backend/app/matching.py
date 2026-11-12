from rapidfuzz import fuzz
from sqlalchemy.orm import Session
from . import models as m


def _link(db, a_type, a_id, b_type, b_id, method, conf, evidence, status="suggested"):
    exists = db.query(m.RecordLink).filter_by(a_type=a_type, a_id=a_id, b_type=b_type, b_id=b_id).first()
    if exists:
        return exists
    link = m.RecordLink(a_type=a_type, a_id=a_id, b_type=b_type, b_id=b_id, method=method,
                        confidence=conf, evidence=evidence, status=status)
    db.add(link)
    return link


def match_tenders(db: Session) -> int:
    """Connect roadworks to tenders. Exact IDs first, fuzzy only creates suggestions."""
    n = 0
    tenders = db.query(m.Tender).all()
    works = db.query(m.Roadwork).all()
    for t in tenders:
        for w in works:
            if w.tender_id and t.tender_id and w.tender_id.strip().upper() == t.tender_id.strip().upper():
                _link(db, "roadwork", w.id, "tender", t.id, "exact_tender_id", 0.99,
                      {"tender_id": t.tender_id}); n += 1
                continue
            if not (t.title and w.description):
                continue
            s = fuzz.token_set_ratio(t.title.lower(), w.description.lower())
            amt_ok = (t.awarded_value and w.sanctioned_amount and
                      abs(t.awarded_value - w.sanctioned_amount) / max(w.sanctioned_amount, 1) < 0.05)
            if s >= 85 and amt_ok:
                _link(db, "roadwork", w.id, "tender", t.id, "description_plus_amount", 0.75,
                      {"text_score": s, "tender_value": t.awarded_value, "wo_amount": w.sanctioned_amount}); n += 1
            elif s >= 93:
                _link(db, "roadwork", w.id, "tender", t.id, "description_only", 0.5, {"text_score": s}); n += 1
    db.commit()
    return n


def suggest_roadworks_for_cluster(db: Session, cluster: m.Cluster, reports, limit=3):
    """Roadwork records have no GPS, so the only signal is the road name.
    Every result is stored as 'suggested' and needs officer review."""
    names = {r.road_name.strip().lower() for r in reports if r.road_name and len(r.road_name.strip()) >= 4}
    if not names:
        return []
    scored = []
    for w in db.query(m.Roadwork).filter(m.Roadwork.description.isnot(None)).all():
        best = max(fuzz.partial_ratio(n, w.description.lower()) for n in names)
        if best >= 85:
            scored.append((best, w))
    scored.sort(key=lambda x: -x[0])
    out = []
    for score, w in scored[:limit]:
        out.append(_link(db, "cluster", cluster.id, "roadwork", w.id, "road_name_fuzzy",
                         round(score / 100 * 0.6, 2),
                         {"road_names": sorted(names), "name_score": score,
                          "note": "Roadwork records carry no coordinates. Match is by road name only."}))
    return out
