"""Load road centrelines (GeoJSON LineStrings or an Overpass JSON export) and attach every report to its nearest road.

    python scripts/load_roads.py --file data/roads/osm_bengaluru.json --source "OpenStreetMap contributors (ODbL)"
    python scripts/load_roads.py --file my_roads.geojson --source "BBMP road centreline" --approximate

Roads are cut into segments of at most ROAD_SEGMENT_MAX_M metres and each segment gets its ward from the ward
boundaries already loaded, so load wards first. Re-importing the same file is safe (existing ways are skipped).
"""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import SessionLocal, init_db
from app.roads import import_road_file
from app import spatial
from app.pipeline import relocate_all_reports


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True); ap.add_argument("--source", required=True)
    ap.add_argument("--approximate", action="store_true", help="mark the geometry as approximate")
    a = ap.parse_args()
    init_db(); db = SessionLocal()
    stats = import_road_file(db, a.file, a.source, is_approx=a.approximate)
    db.commit(); spatial.sync_postgis(db); db.commit()
    relocate_all_reports(db)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
