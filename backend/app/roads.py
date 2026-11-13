"""Road import and segmentation.

Accepted inputs: GeoJSON (BBMP centerlines, OSM exports) and Overpass JSON (`out geom;`).
Every road is cut at ward boundaries (so each piece has exactly one ward) and then into
segments of at most MAX_SEG_M metres. Roads without a name are kept: citizens can still
report on them.
"""
import json, os
from shapely.geometry import shape, LineString, MultiLineString, mapping
from shapely.strtree import STRtree
from . import models as m
from . import spatial
from .textnorm import norm_road_name

MAX_SEG_M = float(os.getenv("ROAD_SEGMENT_MAX_M", 400))
HIGHWAY_KEEP = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
                "service", "living_street", "motorway_link", "trunk_link", "primary_link", "secondary_link",
                "tertiary_link", "road"}


def overpass_to_features(data: dict):
    """Overpass JSON with geometry (`out geom;`) -> GeoJSON features."""
    feats = []
    for el in data.get("elements", []):
        if el.get("type") != "way" or not el.get("geometry"):
            continue
        tags = el.get("tags", {})
        hw = tags.get("highway")
        if hw and hw not in HIGHWAY_KEEP:
            continue
        coords = [[p["lon"], p["lat"]] for p in el["geometry"]]
        if len(coords) < 2:
            continue
        feats.append({"type": "Feature",
                      "properties": {"name": tags.get("name"), "road_class": hw, "ref": tags.get("ref"),
                                     "source_ref": f"way/{el['id']}"},
                      "geometry": {"type": "LineString", "coordinates": coords}})
    return feats


def _lines(geom):
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type in ("MultiLineString", "GeometryCollection"):
        return [g for g in geom.geoms if g.geom_type == "LineString"]
    return []


def import_roads(db, features, source: str, is_approx=False, is_demo=False, regime=None, max_seg_m=MAX_SEG_M):
    """Insert roads + segments. Returns counts. Re-importing the same (source, source_ref) is skipped."""
    regime = regime or spatial.primary_regime(db)
    wards = db.query(m.Ward).filter(m.Ward.regime == regime, m.Ward.geojson.isnot(None)).all() if regime else []
    wgeoms = [shape(json.loads(w.geojson)) for w in wards]
    tree = STRtree(wgeoms) if wgeoms else None
    stats = {"roads": 0, "segments": 0, "skipped_existing": 0, "segments_without_ward": 0, "regime": regime}
    for f in features:
        props = f.get("properties") or {}
        try:
            geom = shape(f["geometry"])
        except Exception:
            continue
        lines = _lines(geom)
        if not lines:
            continue
        name = (props.get("name") or "").strip() or None
        ref = props.get("source_ref") or props.get("osm_id") or props.get("@id") or props.get("id")
        ref = str(ref) if ref not in (None, "") else None
        if ref and db.query(m.Road.id).filter_by(source=source, source_ref=ref).first():
            stats["skipped_existing"] += 1
            continue
        road = m.Road(name=name, name_norm=norm_road_name(name) if name else None,
                      road_class=props.get("road_class") or props.get("highway"), source=source,
                      source_ref=ref, is_approx=bool(props.get("is_approx", is_approx)), is_demo=is_demo)
        db.add(road); db.flush()
        seq, total = 0, 0.0
        for line in lines:
            pieces = []  # (ward row or None, LineString)
            if tree is not None:
                covered = []
                for i in tree.query(line):
                    inter = line.intersection(wgeoms[i])
                    for piece in _lines(inter):
                        if piece.length > 0:
                            pieces.append((wards[i], piece)); covered.append(piece)
                if covered:
                    rest = line.difference(__import__("shapely").union_all(covered).buffer(1e-9))
                    pieces += [(None, p) for p in _lines(rest) if spatial.line_length_m(list(p.coords)) > 5]
                else:
                    pieces = [(None, line)]
            else:
                pieces = [(None, line)]
            for ward, piece in pieces:
                for ch in spatial.chunk_line(piece, max_seg_m):
                    coords = list(ch.coords)
                    ln = spatial.line_length_m(coords)
                    if ln < 1:
                        continue
                    mid = ch.interpolate(0.5, normalized=True)
                    minx, miny, maxx, maxy = ch.bounds
                    seq += 1; total += ln
                    db.add(m.RoadSegment(road_id=road.id, seq=seq, ward_id=ward.id if ward else None,
                                         ward_regime=ward.regime if ward else None,
                                         ward_number=ward.number if ward else None,
                                         ward_name=ward.name if ward else None, length_m=round(ln, 1),
                                         geojson=json.dumps(mapping(ch), separators=(",", ":")),
                                         mid_lat=mid.y, mid_lon=mid.x, minx=minx, miny=miny, maxx=maxx, maxy=maxy))
                    stats["segments"] += 1
                    if not ward:
                        stats["segments_without_ward"] += 1
        road.length_m = round(total, 1)
        allc = [c for l in lines for c in l.coords]
        road.centroid_lat = sum(c[1] for c in allc) / len(allc)
        road.centroid_lon = sum(c[0] for c in allc) / len(allc)
        stats["roads"] += 1
    db.commit()
    spatial.sync_postgis(db)
    return stats


def import_road_file(db, path, source, **kw):
    data = json.load(open(path, encoding="utf-8"))
    feats = overpass_to_features(data) if "elements" in data else data["features"]
    return import_roads(db, feats, source, **kw)
