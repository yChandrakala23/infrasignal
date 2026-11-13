# InfraSignal: implementation plan (written before the code changes)

## 1. What the inspected ZIP already had
| Area | State |
|---|---|
| Stack | FastAPI + SQLAlchemy 2, SQLite fallback, Postgres/PostGIS container in compose (but **no geometry columns anywhere**), pytest suite |
| Citizen reporting | Done: report form, photo upload (EXIF stripped), tracking code, rate limit, duplicate check, public map of clusters |
| Clustering / alerts | Done: DBSCAN over distance+time+TF-IDF; alerts for multi-report, recurring, surge, roadwork-multi-cluster, incomplete info. Thresholds hard-coded; clusters are deleted and re-created on every submission (IDs churn) |
| Gov data | CSV/Excel import with provenance; roads-only filter; contractor normalisation; manual tender entry |
| Documents | PDF text/OCR + regex for 6 fields. **No document classification, no tender/approval/award types, no LLM step, no cross-document matching** |
| Matching | Tender<->roadwork by ID/fuzzy only; cluster->roadwork by fuzzy road name |
| UI | Two single-file pages (citizen, gov). Gov mixes monitoring and complaint handling, uses `prompt()` dialogs, no filters/charts, **no road map, no wards** |
| Missing entirely | Road/segment/ward tables, road map, ward lookup from GPS, road<->work links, complaint->road->work matching, authority dashboard, configurable thresholds, alert history, evaluation, migrations, sample data |

## 2. What will be built (reusing the above)
1. **Schema**: keep every existing table. Add `roads`, `road_segments`, ward geometry/bbox, richer `roadworks`/`tenders`/`documents`/`extractions`, `match_runs`/`match_events` (matching history), `alert_settings`. Alembic baseline (= the original schema) + one upgrade migration; PostGIS `geometry` columns + GiST indexes created on PostgreSQL only.
2. **Spatial layer** (`spatial.py`): one API, two back-ends: PostGIS SQL (`ST_Contains`, `ST_DWithin`, `ST_Distance` on geography) on PostgreSQL, shapely STRtree on SQLite.
3. **Data**: real ward boundaries (243-ward 2022 and 198-ward 2012, datameet, CC BY-SA 2.5 IN) bundled; road importers for GeoJSON / Overpass JSON / shapefile-as-GeoJSON; **clearly labelled SAMPLE** roads, work orders, tenders, PDFs and labelled complaints because OpenCity/KPPP/Overpass are not reachable from the build sandbox.
4. **Extraction**: classify 10 document types, extract 16 fields with page + snippet + bbox, rule-based first, optional LLM pass whose output is only kept if the value literally occurs on the cited page.
5. **Matching engine**: ordered signals (job no > tender ID > work ID > contractor > road/location > description), exact first / fuzzy fallback, conflict detection, tiers (auto / needs-verification / unmatched), history log, review UI.
6. **Complaint pipeline**: nearest road + ward on submit, related works, `matched / possible / unmatched` status, photo hash + semantic text similarity in clustering, stable cluster IDs, configurable alert rules.
7. **Three interfaces**: Public Portal `/citizen`, Government Monitoring `/gov`, Authority Dashboard `/authority`; shared design system, role-based sign-in.
8. **Evaluation**: extraction field accuracy, matching P/R/F1, clustering ARI + pairwise P/R/F1, false-positive alert analysis; gold files + CLI + UI page.
9. **Docs/tests**: pytest on SQLite and real PostGIS, browser end-to-end run, screenshots.

## 3. Rules followed
Payment is never read as completion; no work is labelled delayed/completed/fraudulent without a document saying so; every extracted value keeps document + page; fuzzy matches are never auto-merged; sample records are flagged `is_demo` and labelled in every UI.

## 4. Outcome
Everything in section 2 was built; see `docs/FEATURES.md` for the status of each requirement and what was left out, and `docs/TESTING.md` for the results. Deviation from the plan: shapefile import was not added (GeoJSON, Overpass JSON and KML wards only).
