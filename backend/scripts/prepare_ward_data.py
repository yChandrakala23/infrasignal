"""Copies the datameet ward boundary files into data/wards/, repairing invalid
polygons and rounding coordinates to 6 decimals (about 0.1 m).

Sources (CC BY-SA 2.5 India, https://github.com/datameet/Municipal_Spatial_Data):
  Bangalore/BBMP.geojson           243 wards, 2022 delimitation (KGIS / KSRSAC)
  Bangalore/BBMP_oldWards.geojson  198 wards, 2012
Usage: python scripts/prepare_ward_data.py /path/to/Municipal_Spatial_Data/Bangalore
"""
import json, sys
from pathlib import Path
from shapely.geometry import shape, mapping
from shapely.validation import make_valid

src = Path(sys.argv[1]); out = Path(__file__).resolve().parents[1] / "data" / "wards"
out.mkdir(parents=True, exist_ok=True)


def rnd(c):
    return [round(c[0], 6), round(c[1], 6)] if isinstance(c[0], (int, float)) else [rnd(x) for x in c]


def clean(geom):
    g = shape(geom)
    if not g.is_valid:
        g = make_valid(g)
        if g.geom_type == "GeometryCollection":
            polys = [x for x in g.geoms if x.geom_type in ("Polygon", "MultiPolygon")]
            g = max(polys, key=lambda x: x.area)
    m = mapping(g)
    return {"type": m["type"], "coordinates": rnd(m["coordinates"])}


def build(fname, regime, props_fn):
    d = json.load(open(src / fname))
    feats = [{"type": "Feature", "properties": props_fn(f["properties"]) | {"regime": regime},
              "geometry": clean(f["geometry"])} for f in d["features"]]
    path = out / f"wards_{regime}.geojson"
    json.dump({"type": "FeatureCollection", "features": feats}, open(path, "w"), separators=(",", ":"))
    print(path, len(feats), path.stat().st_size // 1024, "KB")


build("BBMP.geojson", "243", lambda p: {"number": str(p["KGISWardNo"]), "name": p["KGISWardName"],
                                         "code": p["KGISWardCode"], "lgd": p["LGD_WardCode"]})
build("BBMP_oldWards.geojson", "198", lambda p: {"number": str(int(p["WARD_NO"])), "name": p["WARD_NAME"],
                                                  "assembly": p["ASS_CONST1"]})
