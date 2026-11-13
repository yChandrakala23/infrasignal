"""Writes data/roads/sample_major_roads.geojson.

!! SAMPLE / APPROXIMATE GEOMETRY !!
Each road is a polyline through a handful of well-known landmark coordinates, placed by hand.
It is NOT survey data, NOT BBMP centerline data and NOT OpenStreetMap. Positions can be off by
100-300 m. It exists only so the road map, ward lookup and complaint-to-road matching can be
demonstrated offline. Replace it with a real import:
    python scripts/fetch_osm_roads.py           (OpenStreetMap via Overpass, needs internet)
    python scripts/load_roads.py --file bbmp_centerlines.geojson --source "BBMP road centerlines"
"""
import json
from pathlib import Path

# (name, class, [(lat, lon), ...])
ROADS = [
    ("Outer Ring Road (Hebbal to Silk Board section)", "primary", [(13.0358, 77.5970), (13.0430, 77.6150), (13.0420, 77.6330), (13.0280, 77.6450), (13.0150, 77.6560), (13.0067, 77.6700), (12.9910, 77.6970), (12.9569, 77.7011), (12.9260, 77.6762), (12.9217, 77.6495), (12.9176, 77.6233)]),
    ("Hosur Road", "primary", [(12.9400, 77.6030), (12.9300, 77.6130), (12.9176, 77.6233), (12.9060, 77.6290), (12.8930, 77.6370)]),
    ("Bannerghatta Road", "primary", [(12.9400, 77.6000), (12.9300, 77.6000), (12.9179, 77.5995), (12.9050, 77.5990), (12.8890, 77.5970)]),
    ("Old Madras Road", "primary", [(12.9730, 77.6200), (12.9784, 77.6408), (12.9890, 77.6600), (13.0000, 77.6730), (13.0067, 77.6770)]),
    ("Bellary Road", "primary", [(13.0143, 77.5806), (13.0250, 77.5900), (13.0358, 77.5970), (13.0550, 77.5990), (13.0800, 77.5960), (13.1007, 77.5963)]),
    ("Tumkur Road", "primary", [(13.0290, 77.5420), (13.0340, 77.5260), (13.0400, 77.5100), (13.0490, 77.4900)]),
    ("Mysore Road", "primary", [(12.9650, 77.5750), (12.9560, 77.5560), (12.9420, 77.5300), (12.9300, 77.5100), (12.9150, 77.4830)]),
    ("Kanakapura Road", "primary", [(12.9381, 77.5750), (12.9150, 77.5700), (12.8920, 77.5640), (12.8700, 77.5580)]),
    ("Sarjapur Road", "secondary", [(12.9230, 77.6340), (12.9140, 77.6540), (12.9050, 77.6740), (12.8950, 77.6900)]),
    ("Magadi Road", "secondary", [(12.9770, 77.5600), (12.9800, 77.5450), (12.9850, 77.5300), (12.9890, 77.5150)]),
    ("Whitefield Main Road", "secondary", [(12.9910, 77.6980), (12.9880, 77.7150), (12.9860, 77.7300), (12.9855, 77.7360)]),
    ("MG Road", "secondary", [(12.9759, 77.6010), (12.9755, 77.6068), (12.9740, 77.6130), (12.9730, 77.6200)]),
    ("Brigade Road", "tertiary", [(12.9733, 77.6072), (12.9695, 77.6068), (12.9655, 77.6063)]),
    ("Residency Road", "tertiary", [(12.9709, 77.6047), (12.9680, 77.6000), (12.9654, 77.5985)]),
    ("Cunningham Road", "secondary", [(12.9920, 77.5890), (12.9880, 77.5940), (12.9840, 77.5990)]),
    ("Sampige Road", "tertiary", [(12.9980, 77.5705), (13.0040, 77.5708), (13.0100, 77.5712)]),
    ("Dr Rajkumar Road", "secondary", [(12.9930, 77.5520), (12.9990, 77.5530), (13.0050, 77.5540)]),
    ("100 Feet Road Indiranagar", "tertiary", [(12.9795, 77.6408), (12.9720, 77.6410), (12.9650, 77.6412)]),
    ("80 Feet Road Koramangala", "tertiary", [(12.9352, 77.6245), (12.9390, 77.6265), (12.9440, 77.6290)]),
    ("Koramangala Inner Ring Road", "secondary", [(12.9352, 77.6245), (12.9300, 77.6200), (12.9250, 77.6160)]),
    # Unnamed service lanes: kept on purpose so the map shows roads that have no name or no record at all.
    (None, "service", [(12.9569, 77.7030), (12.9400, 77.6900), (12.9260, 77.6790)]),
    (None, "service", [(13.0420, 77.6350), (13.0300, 77.6470), (13.0160, 77.6580)]),
]

feats = [{"type": "Feature",
          "properties": {"name": n, "road_class": c, "source": "SAMPLE_APPROX", "is_approx": True,
                         "note": "Hand-placed approximate geometry for demonstration. Not survey or official data."},
          "geometry": {"type": "LineString", "coordinates": [[lon, lat] for lat, lon in pts]}}
         for n, c, pts in ROADS]
out = Path(__file__).resolve().parents[1] / "data" / "roads" / "sample_major_roads.geojson"
json.dump({"type": "FeatureCollection", "features": feats}, open(out, "w"), indent=1)
print(out, len(feats), "roads")
