# Test output (13 Nov 2026)

Code tests and real-data validation are reported separately. Code tests use local databases and replaced network transports; real-data
validation uses files captured from the live portals.

## 1. Code tests

| Check | Command (from `backend/`) | Result |
|---|---|---|
| Python compilation | `python -m compileall -q app scripts tests` | exit 0, no errors |
| Full suite on PostgreSQL 16 + PostGIS 3.4 | `TEST_DATABASE_URL=postgresql+psycopg2://infra:infra@localhost:5432/infrasignal_test pytest -q` | **316 passed**, 0 failed, 0 skipped, 90 s |
| Full suite on SQLite | `pytest -q` | **315 passed**, 0 failed, 0 skipped, 75 s (one test is PostGIS-only) |
| Order independence | `pytest -q $(ls tests/test_*.py \| sort -r)` (files in reverse order) | **315 passed** (one older order dependence between `test_provisional` and `test_matching` found and fixed) |
| Browser end-to-end (Playwright, Chromium) | `node scripts/e2e_screenshots.js` against a sample-seeded server | **25 / 25 steps passed** |
| Browser walk-through on the real-data database | screenshots `30_real_*` to `37_real_*` | no page errors |

What the suite covers (selected files):

| Area | Tests |
|---|---|
| Unit / core | `test_core.py`, `test_extraction.py`, `test_boq.py` |
| API and roles | `test_api.py` (roles, login, report validation, private photos, rate limit, contact privacy, upload dedupe, extraction review, import preview) |
| Database integration and migrations | `test_migrations.py`, `test_migration_compat.py` (up / down / check, upgrade of an old database with data) |
| CSV and Excel import | `test_imports_and_dependencies.py`, `test_z1_bbmp_pipeline.py`, `test_z2_bbmp_collection_api.py` |
| PDF extraction and OCR | `test_extraction.py`, `test_ocr.py` (text and scanned pages, page flags; **Kannada tests run on generated Kannada pages, not on real government scans**) |
| Collectors | `test_z2_bbmp_collection_api.py`, `test_z3_web_crawler.py` (incl. a verbatim excerpt of the live bbmp.gov.in bundle), `test_z4_prototype_workflows.py` (CKAN API, HTML fallback, IFMS rows, IFMS HTTP 500) |
| Matching | `test_matching.py`, `test_sources_provenance.py`, `test_z4_prototype_workflows.py` (generic wording rejected, specific match kept, timing check) |
| Clustering and alerts | `test_clusters_alerts.py`, `test_z4_prototype_workflows.py` (complaints 11 m apart cluster; 2 km apart do not; same place 200 days apart does not) |
| Citizen portal workflow | `test_z4_prototype_workflows.py::test_citizen_submit_track_and_public_map` (GPS, photo, description, reference + private code, tracking, contact never public) |
| Government portal workflow | `test_z4_prototype_workflows.py::test_officer_and_authority_workflow` (dashboard, works search, sources, contractors, payments, accept + reject suggestions, clusters, alert acknowledge, complaint status, audit admin-only) |

## 2. Real-source validation

The build environment and the Linux shell on the developer's PC block government hosts (proxy: `403` to `CONNECT`). Sources were opened
in a normal browser on the developer's PC; files fetched there were run through the unchanged collector with `scripts/replay_capture.py`.

| URL accessed | When (IST) | Records retrieved | Fields extracted | Failures | Inserted into the database |
|---|---|---|---|---|---|
| `https://data.opencity.in/api/3/action/package_show?id=bbmp-work-orders-and-payments-2024-25` and `...-2025-26` (CKAN API), then the 7 CSV resources | 13 Nov 2026, 00:05 | 17,233 rows (7 files, 6.3 MB) | work code, description, ward + ward map, contractor + vendor code, bill no. + date, gross, deduction, net, payment date, RTGS reference | 1 row rejected (negative net amount in the source) | **Yes: 17,232 payments and bills, 9,612 works, 1,886 contractors** (PostgreSQL and SQLite) |
| same, second run | 13 Nov 2026 | 0 | | 0 | 0 new, all 7 files `304 Not Modified` |
| `https://data.opencity.in/.../361319f8-...csv` (BBMP Tenders 2017-18) | 13 Nov 2026, 00:05 | 6,842 rows | tender number, title, value (5,031 rows have one), published date, closing date, department, category | 0 | **Yes: 6,842 tenders** |
| `https://bbmp.gov.in/` + `/static/js/main.075d4bdf.js` | 12 Nov 2026, 23:10 | 89 PDF links (77 land-use maps) | link, label, category | static HTML has no links (React app) | page and link records (crawler) |
| `https://bbmp.gov.in/csr/TENDER_DOCUMENT_TOWNPLANNING.pdf`, `/Notification_Files/RetiredEmployeeNotification.pdf` | 12 Nov 2026, 23:28 | 2 documents (123 + 4 pages) | document type (NIT 0.83), Tender ID GBA/2025-26/SE0022, dates; OCR on 3 scanned pages | several labelled fields not found in the long tender document | **Yes: 2 documents, 1 tender record, page-level extractions** |
| `https://accounts.bbmp.gov.in/PublicView/vss00CvStatusData.php?pAction=LoadPaymentGridData&...` (IFMS) | 12-13 Nov 2026, 23:58-00:00 | **0** | none | **every request HTTP 500, empty body**, including the page's own requests | No |
| `https://kppp.karnataka.gov.in/#/portal/searchTender/live` | 13 Nov 2026, 00:02 | 0 | | CAPTCHA required (not bypassed) | No |
| `https://site.bbmp.gov.in/` (IFMS documentation) | 13 Nov 2026, 00:03 | 0 | | no response within 21 s | No |

Tender to work matching on the real data: 215 suggestions (all "suggested", none applied automatically), about 2 minutes for
6,842 tenders x 9,612 works. Examples of suggestions: "Development of Mangammanapalya Lake in ward no 190" (both sources);
"Water reclamation plant at Kudlu Chikkere"; "Construction of 2nd Floor ... Urdu medium School at Madduramma Colony" (work 172-17-000025).

One-command setup from an empty database (`python -m scripts.setup_demo --with-sample-complaints`): PostgreSQL 344 s, SQLite 274 s,
identical counts (works 9,612, payments 17,232, tenders 6,843, documents 2, wards 441). Starting the server with only the `.env` written by
`make_env`: officer login 200, wrong password 401, `/citizen`, `/gov`, `/authority`, `/docs` 200.

**Not done:** a direct live run of `python -m scripts.collect_bbmp` from a machine with open internet access.
