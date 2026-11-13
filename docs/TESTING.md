# Testing results and limitations

## Automated tests (`cd backend && pytest -q`)
295 tests on SQLite (**all pass**) and 296 on PostgreSQL 16 + PostGIS 3.4 (same suite plus one PostgreSQL-only test; `TEST_DATABASE_URL`). Run on Python 3.13, SQLAlchemy 2.1, pandas 3.0. Not run on other Python versions.

| File | Covers |
|---|---|
| `test_core.py` | The original prototype's behaviour (clustering basics, extraction regexes, normalisers, permissions, report submit/track, CSV import) |
| `test_extraction.py` | Accuracy floors on three document sets, page + provenance on every value, nothing invented for an unrelated document, values appear verbatim in the PDF, OCR path, wrapped-line classification, LLM hallucination guard |
| `test_matching.py` | Exact ID auto-links; conflicting IDs never merged; fuzzy-only never automatic; orphan/unclassifiable stay visible; payment never sets completion; documents do not overwrite official fields; human verify / reject survive re-runs; history kept; precision floors |
| `test_spatial_roads.py` | Ward from coordinates; nearest segment with a distance limit; roads without records shown; road profile provenance; reports with no road kept; PostGIS geometry populated (PostgreSQL run only) |
| `test_clusters_alerts.py` | Clusters, stable IDs across re-clustering, text and category rules, configurable thresholds and validation, disabled rules, auto-close and re-open, alert wording never accuses, status updates, rejected clusters frozen |
| `test_api.py` | Role separation, login errors, report validation, photo privacy (token required, EXIF removed), rate limit, contact only for admin, document upload + dedupe, extraction review, import dedupe, filters, evaluation endpoint |
| `test_migrations.py` | Alembic upgrade, no model/migration drift, downgrade |
| `test_migration_compat.py` | A database created by the earlier version (0001 schema with data) is upgraded to the latest revision with data kept, links backfilled, FKs and indexes present, re-run safe; also on PostgreSQL |
| `test_sources_provenance.py` | `work_source_links`, many-to-many, roles/statuses, provenance fields, merged rows |
| `test_provisional.py` | PDF-first provisional works, duplicate prevention, confirm / merge, exclusion from totals |
| `test_boq.py` | Multi-page and two-block BoQ tables, wrapped descriptions, amount and total checks, reviewed rows protected, API |
| `test_dashboard_accuracy.py` | Ward / department / fiscal-year filters agree across screens, no double counting, duplicate payments counted once, totals equal the database |
| `test_ocr.py` | Language planning (never an uninstalled language), preprocessing, page confidence, flags, legacy Kannada-font text, rendered Kannada+English scans |
| `test_z1_bbmp_pipeline.py` (76) | BBMP collection, part 1, on saved fixtures: discovery from saved HTML (pagination, metadata, login / CAPTCHA pages), CSV / Excel reading, the three years' schemas and column mapping, normalisation of names, dates, amounts and IDs, invalid rows reported with their original text, duplicates, insertion, payments kept separate and counted once, many-to-many links, verified values never overwritten, rollback of a whole file on error |
| `test_z2_bbmp_collection_api.py` (67) | BBMP collection, part 2: the full 2024-25 workflow (discover, download, validate, insert, dashboard), repeated runs, conditional requests and checksums, changed files, failed downloads and retry, 2023-24 and 2025-26, manual file and URL import, schedule (including cron weekdays), API access by role, dashboard numbers equal to the database, sample data excluded |
| `test_imports_and_dependencies.py` | Runtime imports, `.xls` / `.xlsx` / CSV reading, extraction works without an LLM key, LLM failure falls back to rules |

`scripts/check_clean_install.sh` creates a fresh virtualenv from `requirements.txt`, starts the app, runs a rule-based extraction without an LLM key, a BoQ extraction, `.xlsx` and OCR language checks, a BBMP-format import that repeats as a no-op, and (with `--with-tests`) the suite: 147 passed, 1 skipped (`.xls` writing needs the dev-only `xlwt`).

Writing the PostGIS run exposed real bugs (several more in the second round: `match_events.action`, `fiscal_year` and BoQ `method` longer than their columns) that SQLite hid. The first one that SQLite hid (a `varchar(40)` column too short for exact-match descriptions); it is fixed in the model and in migration 0002. `alembic upgrade`, `check`, `downgrade base` and `upgrade` were also run on PostgreSQL.

## Browser end-to-end (`scripts/e2e_screenshots.js`, Playwright + Chromium)
All steps pass (26 with the Collected data step), no failures and no console or 5xx errors: citizen map, road search and profile, report with a photo, validation error, tracking; officer login (wrong role refused), overview, filter by ward, work profile, documents and extractions, verifying a suggested match, import preview, roads, contractors, evaluation; authority login, overview, cluster detail and status change, alerts and settings, unmatched, reports; phone-sized citizen portal; upload with provenance, document provenance panel, provisional work, BoQ table, Kannada+English scan with OCR page flags (when `KN_SCAN` is given), filtered overview. Screenshots in `screenshots/`. Map tiles and web fonts are blocked in these runs, so the maps show roads without a basemap.

## Evaluation (`docs/EVALUATION_RESULTS.md`)
| Measure | Result | How much it tells you |
|---|---|---|
| Field extraction, sample pack | 100% (116/116) | Extractor was tuned on these documents: **not independent** |
| Field extraction, narrative set 1 | 97% (28/29) | Used to fix gaps afterwards: **not independent** |
| Field extraction, narrative set 2 | **66% (19/29)** | Never used for tuning; the only honest estimate, on 6 author-written documents |
| Document type | 97% over 31 documents | Synthetic |
| Matching, automatic links | P 100% / R 75-88% / F1 86-94% | Fuzzy-only cases are excluded from "automatic" by design; 2 of 2 missed documents had the right work as top suggestion |
| Clustering vs labels | ARI 1.00 clean; 0.88 at 20 m GPS noise; 0.42 at 35 m | Labels were assigned by the author when generating the data. Real data will score lower |
| Alerts | 0 false positives, 0 missed groups | Small synthetic set; road-hotspot rule not exercised in this analysis |

## Limitations to keep in mind
* **BBMP collection was never run against the live websites.** The build environment could not reach `data.opencity.in` or `site.bbmp.gov.in`. Its tests use saved fixtures that reproduce the real layout (columns, `wodetails` / `contractor` / `brnumber` formats, resource names) but contain invented descriptions, amounts and wards. They show the pipeline works on that layout, not that real files parse as expected. See `docs/BBMP_COLLECTION.md`.
* Every number above is on **synthetic data written by the developers**. None of it estimates accuracy on real BBMP/KPPP documents or real citizen reports.
* Not exercised: the LLM extraction pass and the sentence-embedding similarity (no network / model download in the build environment), real scanned or Kannada documents (OCR was validated only on pages rendered by the developers: clean 0.98, skewed 0.95, shaded 0.88, low-res 0.90, poor 0.69 mean line similarity, the last flagged for review), handwritten scans, unruled or unusual BoQ layouts, the live Overpass road download.
* No security testing, accessibility audit, load test, or testing on other browsers or on gov/authority pages at phone width.
* Clustering is sensitive to GPS error above roughly 20 m (see the noise test). The 40 m radius is a setting, not a tuned optimum.
