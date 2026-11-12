"""Ward boundary loading (GeoJSON or KML). A ward number only has meaning inside its own regime."""
import json, re
import xml.etree.ElementTree as ET
from pathlib import Path
from shapely.geometry import shape, mapping, Polygon, MultiPolygon
from shapely.validation import make_valid
from . import models as m
from . import spatial

NUMBER_KEYS = ["number", "ward_no", "wardno", "ward_number", "kgiswardno", "wardnumber", "ward no", "ward_num", "wardnum"]
NAME_KEYS = ["name", "ward_name", "wardname", "kgiswardname", "ward name"]


def _first(props, keys):
    low = {str(k).lower(): v for k, v in props.items()}
    for k in keys:
        if k in low and low[k] not in (None, ""):
            return low[k]
    return None


def parse_kml(text: str):
    """Minimal KML reader -> GeoJSON features (Polygon / MultiPolygon placemarks)."""
    text = re.sub(r'\sxmlns="[^"]+"', "", text, count=1)
    root = ET.fromstring(text)

    def coords(el):
        pts = []
        for tok in (el.text or "").split():
            parts = tok.split(",")
            if len(parts) >= 2:
                pts.append((float(parts[0]), float(parts[1])))
        return pts

    def polygon(pel):
        outer = pel.find(".//outerBoundaryIs//coordinates")
        if outer is None:
            return None
        holes = [coords(h) for h in pel.findall(".//innerBoundaryIs//coordinates")]
        return Polygon(coords(outer), holes)

    feats = []
    for pm in root.iter("Placemark"):
        polys = [polygon(p) for p in pm.iter("Polygon")]
        polys = [p for p in polys if p is not None and not p.is_empty]
        if not polys:
            continue
        props = {"name": (pm.findtext("name") or "").strip()}
        for sd in pm.iter("SimpleData"):
            props[sd.get("name")] = (sd.text or "").strip()
        for d in pm.iter("Data"):
            props[d.get("name")] = (d.findtext("value") or "").strip()
        geom = polys[0] if len(polys) == 1 else MultiPolygon(polys)
        feats.append({"type": "Feature", "properties": props, "geometry": mapping(geom)})
    return feats


def load_wards(db, features, regime: str, source: str, number_field=None, name_field=None, replace=False):
    """Insert or update wards of one regime. Returns {'inserted', 'updated', 'skipped'}."""
    stats = {"inserted": 0, "updated": 0, "skipped": 0}
    if replace:
        db.query(m.Ward).filter(m.Ward.regime == regime).delete()
    for i, f in enumerate(features, start=1):
        props = f.get("properties") or {}
        number = props.get(number_field) if number_field else _first(props, NUMBER_KEYS)
        name = props.get(name_field) if name_field else _first(props, NAME_KEYS)
        if number in (None, "") and name:
            mm = re.search(r"\b(\d{1,3})\b", str(name))
            number = mm.group(1) if mm else None
        if number in (None, ""):
            number = str(i)  # last resort: file order. The loader reports it so it can be checked.
        number = str(int(float(number))) if re.fullmatch(r"\d+(\.0+)?", str(number)) else str(number)
        try:
            g = shape(f["geometry"])
            if not g.is_valid:
                g = make_valid(g)
            if g.is_empty:
                raise ValueError("empty geometry")
        except Exception:
            stats["skipped"] += 1
            continue
        minx, miny, maxx, maxy = g.bounds
        c = g.representative_point()
        w = db.query(m.Ward).filter_by(regime=regime, number=number).first()
        if w:
            stats["updated"] += 1
        else:
            w = m.Ward(regime=regime, number=number); db.add(w); stats["inserted"] += 1
        w.name = str(name) if name else w.name
        w.geojson = json.dumps(mapping(g), separators=(",", ":"))
        w.source = source
        w.props = {k: v for k, v in props.items() if k != "regime"}
        w.minx, w.miny, w.maxx, w.maxy = minx, miny, maxx, maxy
        w.centroid_lat, w.centroid_lon = c.y, c.x
    db.commit()
    spatial.clear_cache()
    return stats


def load_ward_file(db, path, regime, source, **kw):
    path = Path(path)
    if path.suffix.lower() == ".kml":
        feats = parse_kml(path.read_text(encoding="utf-8", errors="replace"))
    else:
        feats = json.loads(path.read_text(encoding="utf-8"))["features"]
    return load_wards(db, feats, regime, source, **kw)
