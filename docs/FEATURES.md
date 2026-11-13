# Implemented and not implemented

Status against the build specification ("INFRASIGNAL — build to 60% completion"). "Implemented" means it runs end to end and is covered by a test or the browser run; the sample data caveat applies throughout: **no real government record or real complaint has been loaded**.

## Automated BBMP data collection (third round)
| Requirement | Status | Notes |
|---|---|---|
| Connector for the OpenCity BBMP datasets (2023-24, 2024-25, 2025-26) | Implemented; **not run live** | Discovers links and metadata from the dataset pages, pagination, retries, timeouts, delays, `robots.txt`, unchanged files skipped |
| Official site `site.bbmp.gov.in` | Scan implemented; **never inspected** | Looks for CSV / Excel links; what the site offers is unknown. Playwright only if enabled and installed |
| Extraction and normalisation, per-year column mapping | Implemented | Unknown columns kept; bad rows reported, never stored; estimated / approved amounts and work-order dates are **not in the published files** and stay empty |
| Duplicate-safe import, no overwrite of verified values, payments never merged | Implemented | File-level transaction, rollback, many-to-many source links, repeat payments counted once |
| History, failures, retry, last successful collection | Implemented | `collection_runs`, `source_files`, `import_issues`; API and page |
| Manual file / URL import fallback | Implemented | Same checks and history; private addresses refused |
| Scheduler | Implemented | APScheduler in-process, off by default, cron + time zone, editable by admin |
| Dashboard: by year, ward, contractor, payments, missing information, unmatched rows, source + year on each record | Implemented | **Collected data** page; sample data never mixed in |
| No CAPTCHA / login bypass | By design | Reported as a failed download |

## Government monitoring module
| Requirement | Status | Notes |
|---|---|---|
| Ingest CSV / Excel | Implemented | Column-mapping preview, roads-only filter, duplicate rows skipped, source name + sample flag stored per import |
| Ingest PDF, manual upload | Implemented | Multi-file upload, byte-identical files are deduplicated (sha256), non-PDF rejected |
| Scanned PDFs (OCR), English + Kannada | Implemented; **not validated on real documents** | Tesseract per page, `OCR_LANG` = auto / eng / kan / eng+kan (auto tries English, adds Kannada when confidence is low and `kan` data is installed). Light preprocessing (contrast, conditional de-speckle, deskew, resolution target), page-level confidence, pages below `OCR_MIN_CONFIDENCE` are flagged "needs review" and can be marked checked by an officer. Tested only on pages the developers rendered themselves (clean, skewed, shaded, low-resolution, poor), not on real Kannada government documents |
| Classify 10 document types | Implemented | NIT, BoQ, admin approval, technical sanction, LoA, work order, agreement, measurement book, payment record, completion certificate; "other" when unsure (never forced) |
| Extract fields with source document + page | Implemented | 18 field types (IDs, description, road, location, ward, department, contractor, three amounts, five dates). Every value stores document, page, snippet, method, confidence; officers can verify or correct |
| LLM-assisted pass | Implemented, **not run live** | Needs `ANTHROPIC_API_KEY`; discards any value not found verbatim on the cited page. Tested only for the guard |
| Never invent data | Implemented | Nothing is filled from the LLM or from other documents; missing stays missing |
| Cross-document matching, ordered signals | Implemented | job no > tender ID > work/contract ID > contractor > road/location > description; exact first, fuzzy fallback |
| No auto-merge of uncertain matches | Implemented | Fuzzy-only = suggested; conflicting identifiers = needs review; human decisions survive re-runs; match history kept |
| Confidence, unmatched, manual-verification list | Implemented | Matching review page |
| Dashboard with filters | Implemented | ward, contractor, department, category, tender status, stated work status, fiscal year, date range, ID search, flags |
| Per-work profile | Implemented | Source row, documents, lifecycle from documents, payments, linked roads, provenance on each item |
| Payment never = completion | Implemented, tested | |
| Many-to-many work and source-record links | Implemented | `work_source_links` (method, confidence, evidence, verification status auto / suggested / verified / rejected). `Roadwork.source_record_id` stays as a legacy pointer; the links table is the source of truth |
| Source provenance for uploaded PDFs | Implemented | Page URL, download URL, filename, source, download date, sha256 checksum stored per document; each extracted field shows document, page and snippet; "open original" link |
| PDF-first provisional works | Implemented | A work can start from a PDF as `provisional` with uncertainty flags, duplicate prevention by identifier, and a confirm / merge step. Provisional works are **left out of financial totals** and counted separately |
| BoQ items (`boq_items`) | Implemented, best-effort on unruled layouts | Multi-page, multi-column and side-by-side tables via PyMuPDF with pdfplumber as second reader (Camelot not used); quantity x rate is checked against the printed amount and the sum against the printed total; statuses ok / mismatch / incomplete; reviewed rows are never overwritten unless `replace=true` |
| Dashboard accuracy | Implemented, tested | One shared definition of visible works and filters (`app/scope.py`): ward (regime-aware, leading zeros ignored), department (case / space-insensitive), fiscal year (label variants or work-order date). Payments are summed separately; a payment recorded twice is marked `duplicate_of_id`, not deleted, and counted once. Totals are tested against the database |
| Excel `.xls` / `.xlsx`, CSV | Implemented | `xlrd` for `.xls`; header row detected, source row numbers match the spreadsheet. HTML saved as `.xls` needs `lxml`/`bs4` (clear message when missing) |
| Fetching from KPPP / OpenCity automatically | **Not done** | Upload / import only; no scheduled or scraped ingestion |

## Road module
| Requirement | Status | Notes |
|---|---|---|
| PostgreSQL + PostGIS storage | Implemented | Geometry columns + GiST indexes + triggers; SQLite fallback uses GeoJSON text + shapely. Whole test suite passes on both |
| Road map and segments | Implemented | Roads are cut into <= 400 m segments, each with its ward |
| Ward from coordinates | Implemented | 243-ward (2022) and 198-ward (2012) boundaries bundled; GBA 369-ward KML/GeoJSON loader provided but the file **was not available to load** |
| Issue markers, clusters on the map | Implemented | |
| Road to work links | Implemented | Auto only on exact road name **and** matching ward; otherwise "inferred" suggestion. Work records carry no coordinates, so this cannot be better without more data |
| Verified / inferred / citizen provenance | Implemented | Badges, legend, line style |
| Roads without government records still shown | Implemented, tested | Including unnamed roads |
| Real road geometry | **Not loaded** | 22 hand-placed *approximate* sample roads. `fetch_osm_roads.py` + `load_roads.py` are written but could not be run against the live Overpass API |

## Citizen reporting
| Requirement | Status |
|---|---|
| Photos (up to 3), GPS or map pin, category, description, contact optional | Implemented |
| Reference number + secret code, status tracking with public notes | Implemented |
| Nearest road + ward + related works on submission | Implemented (reports with no road within 60 m are kept as "no road matched") |
| Intelligent grouping: DBSCAN + text + time + category + photo | Implemented. Text = concept tokens + TF-IDF (sentence-embedding option exists, not run). Photo = perceptual hash only, no learned image features |
| Configurable alerts | Implemented: 8 rules, thresholds editable with validation limits, history kept, auto-closed when the condition clears |
| Separate authority dashboard | Implemented |
| Notifications (SMS / email / push) to citizens or authorities | **Not done**; alerts appear in the dashboard only |
| Kannada interface / complaint text | **Not done** (the sentence-embedding option is multilingual, untested). Kannada *document OCR* is a separate item, see above; extraction still keys on English labels, identifiers, numbers and dates (Kannada numerals parse) |

## Backend, roles, UI
| Requirement | Status |
|---|---|
| FastAPI, validation | Implemented (category, coordinates inside Bengaluru, size and type of photos, text length, status values, alert limits) |
| Roles | Implemented: officer, authority, admin; citizens anonymous. Authority endpoints are read-only for officers |
| Audit log | Implemented (admin API). **No admin page in the UI**: users, sample-data loading and geography upload are API calls (`/docs`) |
| Three areas, civic-tech look, responsive | Implemented. Mobile checked for the citizen portal only |
| Accessibility | Labels, focus styles, contrast considered; **no audit** done |
| Evaluation | Implemented: extraction accuracy, matching P/R/F1, clustering ARI and pairwise F1 with ablations and noise test, false-positive alert analysis; shown on `/gov` (Evaluation) |

## Not done / needs work before any real use
* Real data: no actual BBMP / KPPP records, OSM roads or GBA-369 ward map were loaded (not reachable from the build environment).
* No real Kannada government document was available, so Kannada OCR accuracy is unmeasured. There is no Kannada field-label dictionary.
* Extraction accuracy on real, messy documents is unknown. The one independent synthetic set scored 66%.
* Security review, HTTPS / reverse-proxy setup, password reset, per-user rate limiting (the current limit is in-memory, per process), backups, retention policy for photos and contact details, and a legal basis for processing personal data.
* No load or performance testing; the clustering and surge rule re-scan all reports (fine for thousands, untested for hundreds of thousands).
* Basemap tiles and the map libraries' attribution come from OpenStreetMap's public tile server, so the maps need internet access and are not suitable for heavy traffic.
* Authentication uses signed bearer tokens held in sessionStorage; there is no token revocation.
