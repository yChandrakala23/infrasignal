"""Evaluation harness. Every number comes from running the real pipeline against labelled files in data/eval/.

What each part measures, and what it cannot:
  extraction   field-level accuracy against values printed in synthetic PDFs (three sets with different independence)
  matching     precision / recall / F1 of automatic links against a hand-made answer key (doc->work, tender->work, work->road)
  clustering   adjusted Rand index and pairwise P/R/F1 against hand-labelled synthetic complaints (+ ablation, sweep, GPS-noise stress)
  alerts       false-positive analysis on the same labelled complaints, in a scratch database
All inputs are synthetic and were written by the people who wrote the algorithms, so the scores are an upper-bound sanity check, not a field accuracy.
"""
import json, random, time
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np
from sklearn.metrics import adjusted_rand_score
from . import models as m
from .clustering import cluster as run_cluster
from .textnorm import norm_text

DATA = Path(__file__).resolve().parents[1] / "data"
TEXT_FIELDS = {"work_description", "contractor", "department", "location", "road_name"}
CAVEAT = ("All documents, records and complaints used here are synthetic and were written by the developers. Scores show that the pipeline works on these "
          "examples. They are not an estimate of accuracy on real BBMP, KPPP or citizen data.")
LIMITATIONS = [
    "Extraction: the first sample pack uses 'Label: value' tables and the extractor was tuned on it, so its 100% is not independent. Narrative set 1 was used to find and fix gaps (its post-fix score is not independent either). Only narrative set 2 was never used for tuning.",
    "Real scanned documents (skewed, stamped, handwritten, Kannada government documents) were not available. OCR and Kannada OCR (eng / kan / eng+kan) were tested only on pages the developers rendered themselves, including degraded versions; this shows the pipeline works, not how accurate it is on real documents.",
    "The optional LLM extraction pass and the optional multilingual sentence-embedding similarity were implemented but not run against live services here. All numbers above use the rule-based and TF-IDF paths.",
    "Matching: a link is only automatic on an exact identifier. Name, road and description similarity only suggests. The recall figures therefore deliberately exclude fuzzy-only cases, which are reported as 'suggested hits'.",
    "Clustering labels were assigned by the author when generating the complaints, so the clusters are cleaner than real life. The GPS-noise stress test shows how quickly scores fall once the data gets less tidy.",
    "Road geometry is a small hand-placed approximation (22 roads), not a surveyed centreline. Work-to-road linking is by road name and ward because roadwork records carry no coordinates.",
    "Alert analysis runs without road data, so segment-based rules (road hotspot) are not exercised.",
]


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = 2 * p * r / (p + r) if p and r else (0.0 if (p is not None and r is not None) else None)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f}


# ---------------------------------------------------------------- extraction
_cache = {}


def _process(path):
    from .extract import process_document
    k = (str(path), Path(path).stat().st_mtime)
    if k not in _cache:
        _cache[k] = process_document(str(path))
    return _cache[k]


EXTRACTION_SETS = [
    ("sample_pack", "Table-style sample pack (extractor tuned on this)", "extraction_gold.json", "sample/documents", False),
    ("narrative_1", "Narrative set 1 (gaps fixed afterwards; not independent)", "extraction_heldout_gold.json", "heldout", False),
    ("narrative_2", "Narrative set 2 (never used for tuning)", "extraction_heldout2_gold.json", "heldout2", True),
]


def eval_extraction():
    per_set, by_field, misses = [], {}, []
    tot_ok = tot_n = type_ok = type_n = 0
    for key, title, gold_file, folder, independent in EXTRACTION_SETS:
        gp = DATA / "eval" / gold_file
        if not gp.exists():
            continue
        gold = json.loads(gp.read_text())["documents"]
        ok = n = 0
        for name, g in gold.items():
            p = DATA / folder / name
            if not p.exists():
                continue
            r = _process(p)
            by = {}
            for f in r["fields"]:
                by.setdefault(f["field"], []).append(f["norm_value"])
            type_n += 1; type_ok += r["doc_type"] == g["doc_type"]
            if r["doc_type"] != g["doc_type"]:
                misses.append({"set": key, "doc": name, "field": "document_type", "expected": g["doc_type"], "got": r["doc_type"]})
            for field, want in g["fields"].items():
                got = by.get(field, [])
                good = any((norm_text(str(c)) == norm_text(str(want))) if field in TEXT_FIELDS else str(c) == str(want) for c in got)
                n += 1; ok += good
                d = by_field.setdefault(field, [0, 0]); d[0] += good; d[1] += 1
                if not good:
                    misses.append({"set": key, "doc": name, "field": field, "expected": want, "got": [str(x) for x in got[:2]]})
        per_set.append({"key": key, "title": title, "independent": independent, "documents": len(gold), "correct": ok, "total": n, "accuracy": ok / n if n else None})
        tot_ok += ok; tot_n += n
    return {"sets": per_set, "by_field": [{"field": k, "correct": v[0], "total": v[1], "accuracy": v[0] / v[1]} for k, v in sorted(by_field.items(), key=lambda kv: kv[1][0] / kv[1][1])],
            "type_accuracy": type_ok / type_n if type_n else None, "documents": type_n, "misses": misses[:40],
            "accuracy": tot_ok / tot_n if tot_n else None, "correct": tot_ok, "total": tot_n}


# ---------------------------------------------------------------- matching
def eval_matching(db):
    gold = json.loads((DATA / "eval" / "matching_gold.json").read_text())
    out = {}
    works = {w.id: w for w in db.query(m.Roadwork)}
    job_of = {i: w.job_number for i, w in works.items()}
    # documents -> work
    tp = fp = fn = sug_hit = sug_n = fuzzy_auto = 0
    rows = []
    docs = {d.filename: d for d in db.query(m.Document)}
    for name, g in gold["documents"].items():
        d = docs.get(name)
        if not d:
            continue
        want = g["work_job"]
        auto = d.match_status in ("auto", "verified", "manual") and d.roadwork_id
        got = job_of.get(d.roadwork_id) if auto else None
        if auto:
            top = db.query(m.RecordLink).filter_by(a_type="document", a_id=d.id, b_id=d.roadwork_id).first()
            if top and top.status == "auto" and (top.method or "").startswith("fuzzy"):
                fuzzy_auto += 1
        if want and got == want: tp += 1
        elif got and got != want: fp += 1; fn += 1 if want else 0
        elif want and not got:
            fn += 1
            cand = db.query(m.RecordLink).filter_by(a_type="document", a_id=d.id, b_type="roadwork").filter(m.RecordLink.status.in_(["suggested", "auto"])).order_by(m.RecordLink.confidence.desc()).first()
            sug_n += 1; sug_hit += bool(cand and job_of.get(cand.b_id) == want)
        rows.append({"doc": name, "expected": want, "auto_linked_to": got, "status": d.match_status})
    out["document_to_work"] = {**prf(tp, fp, fn), "suggested_hits": f"{sug_hit}/{sug_n}", "fuzzy_only_auto_links": fuzzy_auto, "rows": rows}
    # tenders -> work
    tp = fp = fn = 0
    tenders = {t.tender_id: t for t in db.query(m.Tender)}
    for tid, want in gold["tenders"].items():
        t = tenders.get(tid)
        if not t: continue
        l = db.query(m.RecordLink).filter(m.RecordLink.a_type == "roadwork", m.RecordLink.b_type == "tender", m.RecordLink.b_id == t.id,
                                          m.RecordLink.status.in_(["auto", "verified"])).first()
        got = job_of.get(l.a_id) if l else None
        if want and got == want: tp += 1
        elif got and got != want: fp += 1; fn += 1 if want else 0
        elif want: fn += 1
    out["tender_to_work"] = prf(tp, fp, fn)
    # work -> road
    tp = fp = fn = 0
    road_name = {r.id: r.name for r in db.query(m.Road)}
    by_job = {w.job_number: w.id for w in works.values()}
    for job, want in gold["work_road"].items():
        wid = by_job.get(job)
        if not wid: continue
        got = {road_name.get(l.a_id) for l in db.query(m.RecordLink).filter(m.RecordLink.a_type == "road", m.RecordLink.b_type == "roadwork", m.RecordLink.b_id == wid,
                                                                             m.RecordLink.status.in_(["auto", "verified"]))}
        if want in got: tp += 1; fp += len(got - {want})
        else: fn += 1; fp += len(got)
    for job in gold["no_road_expected"]:
        wid = by_job.get(job)
        if wid:
            fp += db.query(m.RecordLink).filter(m.RecordLink.a_type == "road", m.RecordLink.b_type == "roadwork", m.RecordLink.b_id == wid, m.RecordLink.status.in_(["auto", "verified"])).count()
    out["work_to_road"] = prf(tp, fp, fn)
    return out


# ---------------------------------------------------------------- clustering
def _complaints(now=None):
    now = now or datetime.utcnow()
    data = json.loads((DATA / "eval" / "complaints_labelled.json").read_text())
    reps = [{"lat": c["lat"], "lon": c["lon"], "category": c["category"], "description": c["description"], "created_at": now - timedelta(days=c["days_ago"]), "photo_hashes": []} for c in data["complaints"]]
    truth = [c["group"] or f"iso{i}" for i, c in enumerate(data["complaints"])]
    return reps, truth, data["groups"]


def _pairwise(truth, labels):
    n = len(truth)
    lab = [l if l >= 0 else f"s{i}" for i, l in enumerate(labels)]
    tp = fp = fn = 0
    for i in range(n):
        for j in range(i + 1, n):
            same_t, same_p = truth[i] == truth[j], lab[i] == lab[j]
            tp += same_t and same_p; fp += (not same_t) and same_p; fn += same_t and not same_p
    return prf(tp, fp, fn)


def _score(reps, truth, **kw):
    labels, _ = run_cluster(reps, **kw)
    lab = [l if l >= 0 else f"s{i}" for i, l in enumerate(labels)]
    s = _pairwise(truth, labels)
    return {"ari": float(adjusted_rand_score(truth, lab)), **{k: s[k] for k in ("precision", "recall", "f1")}, "clusters": len({l for l in labels if l >= 0})}


def eval_clustering():
    reps, truth, groups = _complaints()
    full = _score(reps, truth)
    ablation = [{"name": n, **_score(reps, truth, **kw)} for n, kw in [
        ("Distance only (plain DBSCAN, 40 m)", dict(use_text=False, use_photo=False, use_time=False, use_category=False)),
        ("+ category rule", dict(use_text=False, use_photo=False, use_time=False, use_category=True)),
        ("+ category + text similarity", dict(use_text=True, use_photo=False, use_time=False, use_category=True)),
        ("+ category + text + time gap (full)", dict())]]
    sweep = [{"eps_m": e, **_score(reps, truth, eps_m=e)} for e in (15, 25, 40, 60, 90, 150)]
    rng = random.Random(11)
    noisy = []
    for sigma in (0, 10, 20, 35):
        r2 = []
        for r in reps:
            dlat = rng.gauss(0, sigma) / 111000; dlon = rng.gauss(0, sigma) / (111000 * 0.975)
            r2.append({**r, "lat": r["lat"] + dlat, "lon": r["lon"] + dlon})
        noisy.append({"gps_noise_m": sigma, **_score(r2, truth)})
    return {"n": len(reps), "groups": len(groups), "ari": full["ari"], "pairwise": {k: full[k] for k in ("precision", "recall", "f1")},
            "ablation": ablation, "sweep": sweep, "gps_noise": noisy}


# ---------------------------------------------------------------- alerts
def eval_alerts():
    """Runs the real pipeline in a scratch in-memory database so the live data is never touched."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from .db import Base
    from .pipeline import recluster
    from .alerts import run_alerts
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False)()
    now = datetime.utcnow()
    data = json.loads((DATA / "eval" / "complaints_labelled.json").read_text())
    for i, c in enumerate(data["complaints"]):
        db.add(m.Report(public_ref=f"E{i}", token_hash="x", category=c["category"], description=c["description"], lat=c["lat"], lon=c["lon"], is_demo=True,
                        created_at=now - timedelta(days=c["days_ago"]), client_hash="e"))
    db.commit()
    recluster(db, run_alert_rules=False); run_alerts(db, now)
    idx = {f"E{i}": c["group"] for i, c in enumerate(data["complaints"])}
    groups = data["groups"]
    by_rule, covered = {}, set()
    for a in db.query(m.Alert).filter(m.Alert.status == "open"):
        refs = (a.evidence or {}).get("reports") or [r.public_ref for r in db.query(m.Report).filter_by(cluster_id=a.cluster_id)] if a.cluster_id else (a.evidence or {}).get("reports", [])
        gs = [idx.get(r) for r in refs]
        top = max(set(gs), key=gs.count) if gs else None
        ok = bool(top and gs.count(top) / len(gs) >= 0.5 and groups.get(top, {}).get("expect_alert"))
        d = by_rule.setdefault(a.rule, {"rule": a.rule, "alerts": 0, "on_true_group": 0, "false_positive": 0})
        d["alerts"] += 1; d["on_true_group" if ok else "false_positive"] += 1
        if ok: covered.add(top)
    rows = []
    for d in by_rule.values():
        rows.append({**d, "precision": d["on_true_group"] / d["alerts"]})
    expect = {g for g, v in groups.items() if v["expect_alert"]}
    # which rules did the labelled cases actually need? a case with only 2 days of reports is not 'repeat_days'
    return {"by_rule": sorted(rows, key=lambda r: r["rule"]), "missed_groups": sorted(expect - covered), "groups_expected": len(expect),
            "false_alarm_groups": sorted({g for g, v in groups.items() if not v["expect_alert"]}),
            "note": "An alert counts as a false positive when the reports behind it do not belong to a labelled group that should raise an alert (isolated reports, or the 2-report pair 'D1'). Several rules can fire for the same real group, so alert counts exceed group counts."}


def run_all(db):
    t = time.time()
    res = {"caveat": CAVEAT, "extraction": eval_extraction(), "matching": eval_matching(db), "clustering": eval_clustering(), "alerts": eval_alerts(),
           "limitations": LIMITATIONS, "generated_at": datetime.utcnow().isoformat(), "seconds": round(time.time() - t, 1)}
    return res
