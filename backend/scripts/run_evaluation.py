"""Runs the evaluation on a database seeded with the sample data and writes docs/EVALUATION_RESULTS.json (+ a readable summary).
Usage:  python scripts/run_evaluation.py            (uses a throw-away SQLite database; your data is untouched)"""
import json, os, sys, tempfile
from pathlib import Path
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(suffix=".db")
from app.db import init_db, SessionLocal  # noqa: E402
from app.seed import seed_all  # noqa: E402
from app.evaluation import run_all  # noqa: E402

init_db()
db = SessionLocal()
seed_all(db, complaints=False)
res = run_all(db)
out = BACKEND.parent / "docs"; out.mkdir(exist_ok=True)
(out / "EVALUATION_RESULTS.json").write_text(json.dumps(res, indent=1, default=str))
pc = lambda v: "n/a" if v is None else f"{v:.0%}"
ex, mt, cl = res["extraction"], res["matching"], res["clustering"]
lines = ["# Evaluation results", "", res["caveat"], "", "## Field extraction", ""]
lines += [f"- {s['title']}: **{pc(s['accuracy'])}** ({s['correct']}/{s['total']} values in {s['documents']} documents)" for s in ex["sets"]]
lines += ["", f"Document type accuracy: {pc(ex['type_accuracy'])} over {ex['documents']} documents.", "", "## Matching (automatic links only)", "", "| Task | Precision | Recall | F1 | TP/FP/FN |", "|---|---|---|---|---|"]
for k, v in (("Document to work", mt["document_to_work"]), ("Tender to work", mt["tender_to_work"]), ("Work to road", mt["work_to_road"])):
    lines.append(f"| {k} | {pc(v['precision'])} | {pc(v['recall'])} | {pc(v['f1'])} | {v['tp']}/{v['fp']}/{v['fn']} |")
lines += ["", f"Suggested hits among missed documents: {mt['document_to_work']['suggested_hits']}. Fuzzy-only automatic links: {mt['document_to_work']['fuzzy_only_auto_links']}.", "",
          f"## Clustering ({cl['n']} complaints, {cl['groups']} groups)", "", f"ARI {cl['ari']:.2f}, pairwise F1 {pc(cl['pairwise']['f1'])}.", "", "| Variant | ARI | Pair F1 |", "|---|---|---|"]
for r in cl["ablation"]: lines.append(f"| {r['name']} | {r['ari']:.2f} | {pc(r['f1'])} |")
for r in cl["gps_noise"]: lines.append(f"| GPS noise {r['gps_noise_m']} m | {r['ari']:.2f} | {pc(r['f1'])} |")
lines += ["", "## Alerts", ""] + [f"- {r['rule']}: {r['alerts']} alerts, {r['false_positive']} false positives" for r in res["alerts"]["by_rule"]]
lines += [f"- Real groups with no alert: {res['alerts']['missed_groups'] or 'none'}", "", "## Limitations", ""] + [f"- {l}" for l in res["limitations"]]
(out / "EVALUATION_RESULTS.md").write_text("\n".join(lines) + "\n")
print("\n".join(lines))
