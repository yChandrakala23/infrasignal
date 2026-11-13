"""Re-run a collection from files captured from the live OpenCity portal, through the real collector, importer and database.

    python -m scripts.replay_capture                          # data/captures/opencity-2026-11-13 (real BBMP files, captured 13 Nov 2026)
    python -m scripts.replay_capture --capture DIR --twice    # run twice: the second run must find nothing new

Only the network is replaced: every request the collector makes is answered from the capture folder (the CKAN package_show answer is
rebuilt from the metadata recorded in manifest.json; the CSV bytes, URLs, resource ids, ETags and Last-Modified are the real ones).
A URL that is not in the capture answers 599, so it shows up as a failure instead of being invented.
Use it to demonstrate the pipeline without internet access. For real collection use: python -m scripts.collect_bbmp
"""
import argparse, json, os, sys
from pathlib import Path
import httpx

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)
from app.db import init_db, SessionLocal  # noqa: E402
from app import models as m, collection as col  # noqa: E402
from app.connectors.bbmp_connector import Fetcher  # noqa: E402

DEFAULT = BACKEND / "data" / "captures" / "opencity-2026-11-13"


def transport(cap: Path):
    man = json.loads((cap / "manifest.json").read_text(encoding="utf-8"))
    files, pkgs = {}, {}
    for slug, d in man["datasets"].items():
        pkgs[slug] = {"success": True, "result": {"id": slug, "name": slug, "title": d.get("title"), "license_title": d.get("license"),
                      "author": d.get("author"), "metadata_modified": d.get("metadata_modified"), "organization": {"title": d.get("organization")},
                      "notes": "Rebuilt from the metadata recorded at capture time.",
                      "resources": [{"id": r["id"], "name": r["name"], "format": r["format"], "url": r["url"]} for r in d["resources"]]}}
        for r in d["resources"]:
            files[r["url"]] = r
    served = []

    def handler(req: httpx.Request):
        u = str(req.url)
        if u.endswith("/robots.txt"):
            served.append((200, u)); return httpx.Response(200, text="User-agent: *\nDisallow:\n", headers={"content-type": "text/plain"})
        if "/api/3/action/package_show" in u:
            slug = req.url.params.get("id")
            if slug in pkgs:
                served.append((200, u)); return httpx.Response(200, json=pkgs[slug])
        r = files.get(u)
        if r:
            h = {"content-type": "text/csv", "etag": r["etag"], "last-modified": r["last_modified"]}
            if req.headers.get("if-none-match") == r["etag"]:
                served.append((304, u)); return httpx.Response(304, headers=h)
            served.append((200, u)); return httpx.Response(200, content=(cap / r["file"]).read_bytes(), headers=h)
        served.append((599, u)); return httpx.Response(599, text="not in capture")
    return handler, served, man


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capture", default=str(DEFAULT))
    ap.add_argument("--twice", action="store_true")
    a = ap.parse_args()
    cap = Path(a.capture)
    init_db()
    handler, served, man = transport(cap)
    cfg = {"source_website": "https://data.opencity.in", "datasets": [{"slug": s, "url": f"https://data.opencity.in/dataset/{s}", "enabled": True,
                                                                       "column_overrides": {}} for s in man["datasets"]], "site_pages": []}
    print(f"Replaying capture {cap.name} (captured {man.get('captured_at')})")
    for n in range(2 if a.twice else 1):
        served.clear()
        db = SessionLocal(); rid = col.start_run(db, "replay_capture", None, {"capture": cap.name}).id; db.close()
        col.execute_run(rid, fetcher=Fetcher(httpx.Client(transport=httpx.MockTransport(handler)), delay=0, retries=0), config=cfg)
        db = SessionLocal(); r = db.get(m.CollectionRun, rid)
        print(f"\nRun {r.id}: {r.status} | datasets {r.datasets_found} | files {r.files_found} listed, {r.files_downloaded} downloaded, "
              f"{r.files_unchanged} unchanged, {r.files_failed} failed | rows {r.records_processed} processed, {r.records_inserted} new, "
              f"{r.records_updated} updated, {r.records_duplicate} duplicate, {r.records_failed} failed")
        for x in r.log or []:
            print(f"  [{x['level']}] {x['message']}")
        print("  requests answered: " + ", ".join(f"{s}" for s, _ in served))
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
