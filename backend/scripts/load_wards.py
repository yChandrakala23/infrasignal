"""Load ward boundaries (GeoJSON or KML) into the database.

    python scripts/load_wards.py --regime 243 --file data/wards/wards_243.geojson --source "BBMP 2022 (DataMeet)"
    python scripts/load_wards.py --regime gba369 --file Final_Wards_369.kml --source "GBA final wards map, OpenCity" [--replace]

Ward numbers belong to their regime ("243", "198", "gba369") and are never compared across regimes.
Use --number-field / --name-field when the file's property names are not recognised automatically.
"""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import SessionLocal, init_db
from app.wards import load_ward_file
from app import spatial


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True); ap.add_argument("--regime", required=True)
    ap.add_argument("--source", required=True, help="where the file came from (stored with every ward)")
    ap.add_argument("--number-field"); ap.add_argument("--name-field")
    ap.add_argument("--replace", action="store_true", help="replace existing wards of this regime")
    a = ap.parse_args()
    init_db(); db = SessionLocal()
    stats = load_ward_file(db, a.file, a.regime, a.source, number_field=a.number_field, name_field=a.name_field, replace=a.replace)
    db.commit(); spatial.clear_cache()
    try:
        spatial.sync_postgis(db); db.commit()
    except Exception:
        pass
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
