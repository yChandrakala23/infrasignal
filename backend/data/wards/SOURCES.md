# Ward boundary sources

| File | Regime key | Wards | Origin | Licence |
|---|---|---|---|---|
| `wards_243.geojson` | `243` | 243 | BBMP delimitation 2022, scraped by DataMeet from KSRSAC KGIS (https://kgis.ksrsac.in/bengalurugis/). Repo: https://github.com/datameet/Municipal_Spatial_Data (`Bangalore/BBMP.geojson`) | CC BY-SA 2.5 India |
| `wards_198.geojson` | `198` | 198 | BBMP ward map 2012 (DataMeet / Open Bangalore). `Bangalore/BBMP_oldWards.geojson` | CC BY-SA 2.5 India |

Changes made: invalid polygons repaired with `shapely.make_valid`; coordinates rounded to 6 decimals.

**GBA 369-ward map (Nov 2025)** is published by the Greater Bengaluru Authority as KML on OpenCity
(https://data.opencity.in/dataset/gba-wards-delimitation-2025, "Final Wards Map, 369 Wards"). It could not be
downloaded in the build environment. Load it with
`python scripts/load_wards.py --regime gba369 --file <downloaded>.kml`; the loader accepts KML and GeoJSON.

Ward numbers belong to their own regime and are never compared across regimes.
