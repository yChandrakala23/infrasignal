"""Download named Bengaluru roads from OpenStreetMap (Overpass API) into data/roads/osm_bengaluru.json.

    python scripts/fetch_osm_roads.py                     # trunk/primary/secondary/tertiary roads, 4 x 4 tiles
    python scripts/fetch_osm_roads.py --classes trunk,primary,secondary,tertiary,residential --tiles 8
    python scripts/load_roads.py --file data/roads/osm_bengaluru.json --source "OpenStreetMap contributors (ODbL)"

Needs internet access to an Overpass endpoint (it could NOT be run in the build sandbox, so this script is untested
against the live service; the converter it feeds, app.roads.overpass_to_features, is tested).
Licence: ODbL. Credit "OpenStreetMap contributors" wherever the geometry is shown. Be polite to the public server:
tiles are fetched one at a time with a pause between requests.
"""
import argparse, json, time, urllib.parse, urllib.request
from pathlib import Path

BBOX = (12.80, 77.45, 13.20, 77.80)  # south, west, north, east: Greater Bengaluru
OUT = Path(__file__).resolve().parents[1] / "data" / "roads" / "osm_bengaluru.json"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--classes", default="trunk,primary,secondary,tertiary")
    ap.add_argument("--tiles", type=int, default=4, help="split the bounding box into N x N requests")
    ap.add_argument("--endpoint", default="https://overpass-api.de/api/interpreter")
    ap.add_argument("--pause", type=float, default=5.0)
    a = ap.parse_args()
    s, w, n, e = BBOX
    dy, dx = (n - s) / a.tiles, (e - w) / a.tiles
    classes = "|".join(c.strip() for c in a.classes.split(",") if c.strip())
    ways = {}
    for i in range(a.tiles):
        for j in range(a.tiles):
            box = f"{s + i * dy:.5f},{w + j * dx:.5f},{s + (i + 1) * dy:.5f},{w + (j + 1) * dx:.5f}"
            q = f'[out:json][timeout:120];way["highway"~"^({classes})$"]["name"]({box});out geom;'
            req = urllib.request.Request(a.endpoint, data=urllib.parse.urlencode({"data": q}).encode(), headers={"User-Agent": "InfraSignal-roads-fetch/1.0"})
            with urllib.request.urlopen(req, timeout=180) as r:
                for el in json.load(r).get("elements", []):
                    ways[el["id"]] = el
            print(f"tile {i},{j}: {len(ways)} ways so far", flush=True)
            time.sleep(a.pause)
    OUT.write_text(json.dumps({"elements": list(ways.values()), "attribution": "OpenStreetMap contributors (ODbL)"}))
    print("wrote", OUT, len(ways), "ways")


if __name__ == "__main__":
    main()
