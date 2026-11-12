# Testing results and limitations

## Automated tests (`cd backend && pytest -q`)
59 tests, **all pass on SQLite and on PostgreSQL 16 + PostGIS 3.4** (same suite, `TEST_DATABASE_URL`). Run on Python 3.13, SQLAlchemy 2.1, pandas 3.0. Not run on other Python versions.

| File | Covers |
|---|---|
| `test_core.py` | The original prototype's behaviour (clustering basics, extraction regexes, normalisers, permissions, report submit/track, CSV import) |
| `test_extraction.py` | Accuracy floors on three document sets, page + provenance on every value, nothing invented for an unrelated document, values appear verbatim in the PDF, OCR path, wrapped-line classification, LLM hallucination guard |
| `test_matching.py` | Exact ID auto-links; conflicting IDs never merged; fuzzy-only never automatic; orphan/unclassifiable stay visible; payment never sets completion; documents do not overwrite official fields; human verify / reject survive re-runs; history kept; precision floors |
| `test_spatial_roads.py` | Ward from coordinates; nearest segment with a distance limit; roads without records shown; road profile provenance; reports with no road kept; PostGIS geometry populated (PostgreSQL run only) |
| `test_clusters_alerts.py` | Clusters, stable IDs across re-clustering, text and category rules, configurable thresholds and validation, disabled rules, auto-close and re-open, alert wording never accuses, status updates, rejected clusters frozen |
| `test_api.py` | Role separation, login errors, report validation, photo privacy (token required, EXIF removed), rate limit, contact only for admin, document upload + dedupe, extraction review, import dedupe, filters, evaluation endpoint |
| `test_migrations.py` | Alembic upgrade, no model/migration drift, downgrade |

Writing the PostGIS run exposed one real bug that SQLite hid (a `varchar(40)` column too short for exact-match descriptions); it is fixed in the model and in migration 0002. `alembic upgrade`, `check`, `downgrade base` and `upgrade` were also run on PostgreSQL.

## Browser end-to-end (`scripts/e2e_screenshots.js`, Playwright + Chromium)
19 steps, no failures and no console or 5xx errors: citizen map, road search and profile, report with a photo, validation error, tracking; officer login (wrong role refused), overview, filter by ward, work profile, documents and extractions, verifying a suggested match, import preview, roads, contractors, evaluation; authority login, overview, cluster detail and status change, alerts and settings, unmatched, reports; phone-sized citizen portal. Screenshots in `screenshots/`. Map tiles and web fonts are blocked in these runs, so the maps show roads without a basemap.

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
* Every number above is on **synthetic data written by the developers**. None of it estimates accuracy on real BBMP/KPPP documents or real citizen reports.
* Not exercised: the LLM extraction pass and the sentence-embedding similarity (no network / model download in the build environment), Kannada OCR, skewed or handwritten scans, the live Overpass road download.
* No security testing, accessibility audit, load test, or testing on other browsers or on gov/authority pages at phone width.
* Clustering is sensitive to GPS error above roughly 20 m (see the noise test). The 40 m radius is a setting, not a tuned optimum.
