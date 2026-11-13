"""Spatial helpers with two interchangeable back-ends.

* PostgreSQL + PostGIS: ST_Contains / ST_DWithin / <-> on GiST-indexed `geom` columns
  (created and kept in sync by migration 0002).
* SQLite (tests, quick local runs): bounding-box pre-filter in SQL, exact work with shapely.

All public functions take a SQLAlchemy session and plain lat/lon degrees (EPSG:4326).
"""
import json, math
from functools import lru_cache
from shapely.geometry import shape, Point, LineString, mapping
from shapely.strtree import STRtree
from sqlalchemy import text
from . import models as m
from .db import is_postgres

EARTH_R = 6371008.8
# Order of preference when a point is inside wards of several regimes
REGIME_PREFERENCE = [r.strip() for r in __import__("os").getenv("WARD_REGIME_PREFERENCE", "gba369,243,198").split(",")]


# ------------------------------------------------------------------ plain geometry maths
def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def line_length_m(coords):
    """coords: [(lon, lat), ...]"""
    return sum(haversine_m(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:]))


def _local(lat0):
    kx = 111320.0 * math.cos(math.radians(lat0))
    return kx, 110540.0


def distance_point_line_m(lat, lon, line: LineString):
    """Distance in metres from a point to a lon/lat line (local equirectangular projection)."""
    kx, ky = _local(lat)
    proj = lambda g: LineString([((x - lon) * kx, (y - lat) * ky) for x, y in g.coords])
    return Point(0, 0).distance(proj(line))


def bbox_of(geom):
    minx, miny, maxx, maxy = geom.bounds
    return minx, miny, maxx, maxy


def meters_to_deg(m_, lat=13.0):
    return m_ / (111320.0 * math.cos(math.radians(lat)))  # use the (larger) lon degree: conservative bbox


# ------------------------------------------------------------------ ward lookup
class _WardIndex:
    def __init__(self, rows):
        self.ids = [r.id for r in rows]
        self.rows = {r.id: (r.id, r.regime, r.number, r.name) for r in rows}
        self.geoms = [shape(json.loads(r.geojson)) for r in rows]
        self.tree = STRtree(self.geoms) if self.geoms else None

    def find(self, lon, lat):
        if not self.tree:
            return None
        p = Point(lon, lat)
        for i in self.tree.query(p):
            if self.geoms[i].covers(p):
                return self.rows[self.ids[i]]
        return None


_ward_cache: dict[str, _WardIndex] = {}


def clear_cache():
    _ward_cache.clear()
    _regimes_cache.clear()


_regimes_cache: dict[str, list] = {}


def available_regimes(db):
    if "r" not in _regimes_cache:
        have = {r for (r,) in db.query(m.Ward.regime).distinct()}
        order = [r for r in REGIME_PREFERENCE if r in have] + sorted(have - set(REGIME_PREFERENCE))
        _regimes_cache["r"] = order
    return _regimes_cache["r"]


def primary_regime(db):
    r = available_regimes(db)
    return r[0] if r else None


def _index(db, regime):
    if regime not in _ward_cache:
        rows = db.query(m.Ward).filter(m.Ward.regime == regime, m.Ward.geojson.isnot(None)).all()
        _ward_cache[regime] = _WardIndex(rows)
    return _ward_cache[regime]


def ward_for_point(db, lat, lon, regime=None):
    """Return a Ward row (or None) containing the point. With regime=None the preferred regime
    that contains the point wins."""
    regimes = [regime] if regime else available_regimes(db)
    for r in regimes:
        if is_postgres():
            row = db.execute(text("SELECT id FROM wards WHERE regime=:r AND ST_Covers(geom, ST_SetSRID(ST_MakePoint(:lon,:lat),4326)) LIMIT 1"),
                             {"r": r, "lat": lat, "lon": lon}).first()
            if row:
                return db.get(m.Ward, row[0])
        else:
            hit = _index(db, r).find(lon, lat)
            if hit:
                return db.get(m.Ward, hit[0])
    return None


def wards_for_point(db, lat, lon):
    """{regime: Ward} for every loaded regime that contains the point."""
    out = {}
    for r in available_regimes(db):
        w = ward_for_point(db, lat, lon, r)
        if w:
            out[r] = w
    return out


def ward_label(w):
    if not w:
        return None
    return f"Ward {w.number} {w.name or ''} ({w.regime} regime)".replace("  ", " ").strip()


# ------------------------------------------------------------------ nearest road segment
def nearest_segment(db, lat, lon, max_m=60.0):
    """-> (RoadSegment, distance_m) or None. The distance is metres on the ground."""
    if is_postgres():
        deg = meters_to_deg(max_m * 1.5, lat)
        row = db.execute(text("""
            SELECT id, ST_Distance(geom::geography, pt::geography) AS d
            FROM road_segments, (SELECT ST_SetSRID(ST_MakePoint(:lon,:lat),4326) AS pt) q
            WHERE geom IS NOT NULL AND ST_DWithin(geom, pt, :deg)
            ORDER BY d ASC LIMIT 1"""), {"lat": lat, "lon": lon, "deg": deg}).first()
        if row and row[1] <= max_m:
            return db.get(m.RoadSegment, row[0]), float(row[1])
        return None
    d = meters_to_deg(max_m * 1.5, lat)
    cands = db.query(m.RoadSegment).filter(m.RoadSegment.minx <= lon + d, m.RoadSegment.maxx >= lon - d,
                                           m.RoadSegment.miny <= lat + d, m.RoadSegment.maxy >= lat - d).all()
    best = None
    for s in cands:
        dist = distance_point_line_m(lat, lon, shape(json.loads(s.geojson)))
        if best is None or dist < best[1]:
            best = (s, dist)
    return best if best and best[1] <= max_m else None


def segments_in_bbox(db, minx, miny, maxx, maxy, limit=4000):
    return db.query(m.RoadSegment).filter(m.RoadSegment.minx <= maxx, m.RoadSegment.maxx >= minx,
                                          m.RoadSegment.miny <= maxy, m.RoadSegment.maxy >= miny).limit(limit).all()


def reports_near(db, lat, lon, radius_m, since=None):
    """Reports within radius_m of a point (PostGIS: ST_DWithin on geography)."""
    if is_postgres():
        q = "SELECT id FROM reports WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(:lon,:lat),4326)::geography, :r)"
        params = {"lat": lat, "lon": lon, "r": radius_m}
        if since:
            q += " AND created_at >= :since"; params["since"] = since
        ids = [r[0] for r in db.execute(text(q), params)]
        return db.query(m.Report).filter(m.Report.id.in_(ids)).all() if ids else []
    d = meters_to_deg(radius_m * 1.2, lat)
    qs = db.query(m.Report).filter(m.Report.lat.between(lat - d, lat + d), m.Report.lon.between(lon - d, lon + d))
    if since:
        qs = qs.filter(m.Report.created_at >= since)
    return [r for r in qs if haversine_m(lat, lon, r.lat, r.lon) <= radius_m]


# ------------------------------------------------------------------ line splitting for import
def chunk_line(line: LineString, max_len_m: float):
    """Split a lon/lat LineString into pieces no longer than max_len_m (by interpolation)."""
    total = line_length_m(list(line.coords))
    if total <= max_len_m or total == 0:
        return [line]
    n = math.ceil(total / max_len_m)
    L = line.length  # in degrees: only used proportionally
    from shapely.ops import substring
    return [substring(line, L * i / n, L * (i + 1) / n) for i in range(n)]


def sync_postgis(db):
    """Back-fill / refresh PostGIS geometry that triggers cannot derive (roads.geom = union of its
    segments). No-op on SQLite."""
    if not is_postgres():
        return
    db.execute(text("""UPDATE roads r SET geom = s.g FROM
                       (SELECT road_id, ST_Multi(ST_Collect(geom)) AS g FROM road_segments GROUP BY road_id) s
                       WHERE s.road_id = r.id"""))
    db.commit()


def spatial_backend():
    return "postgis" if is_postgres() else "shapely"
