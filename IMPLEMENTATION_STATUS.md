# Implementation status

InfraSignal, Bengaluru road infrastructure transparency and citizen monitoring. Student research prototype, **not an official BBMP or
GBA application**. Status as of 13 Nov 2026 (round 5).

## Live data: what was actually retrieved

| Source | How it exposes data | Result on 12-13 Nov 2026 |
|---|---|---|
| **OpenCity** (data.opencity.in) | CKAN portal: public JSON API (`package_show`, `package_search`) plus CSV downloads with ETag / Last-Modified. 145 BBMP datasets. | **Real data retrieved and imported.** 7 work-order and payment CSVs (2024-25: 198, 225, 243 ward maps; 2025-26: 198, 225, 243, common), 17,233 rows: **17,232 imported, 1 rejected** (negative net amount in the source). BBMP tenders 2017-18: **6,842 imported, 0 failed**. Second run: all 7 files answered `304 Not Modified`, **0 duplicates**. |
| **GBA / BBMP website** (bbmp.gov.in) | React app. Static HTML has no links; 89 PDF links are compiled into `/static/js/main.*.js`. `robots.txt` allows all. | Structure read live. 2 real PDFs retrieved and processed (123-page tender document, Tender ID GBA/2025-26/SE0022 extracted; 4-page scanned notification read by OCR). |
| **BBMP IFMS Works Bill Public View** (accounts.bbmp.gov.in/PublicView) | Public search, no login, no CAPTCHA. Grid filled from JSON service `vss00CvStatusData.php?pAction=LoadPaymentGridData` (fields: bill id, ward/division, job code, name of work, bill details, amount). | **No data: the portal's own server error.** Every request answered HTTP 500 with an empty body, including the requests the page makes itself when it loads. Collector built against the page's declared fields; it reports the 500 and stores nothing. |
| **KPPP** (kppp.karnataka.gov.in) | Angular app; "Search Tenders" requires a CAPTCHA. | **Not automated** (CAPTCHAs are not bypassed). KPPP exports can be imported by hand (Import data > Tenders). OpenCity's `bbmp-tenders` dataset is used instead. |
| **IFMS documentation** (site.bbmp.gov.in) | Legacy site. | **Did not respond** (timed out after 21 s, also from a normal browser). |

**How the real data got in.** Both the build environment and the Linux shell on the developer's PC block government hosts by network
policy (the proxy answers 403 to `CONNECT data.opencity.in:443`). The files were fetched by a normal browser on the PC, and then fed through
the **unchanged** collector, importer and database with `python -m scripts.replay_capture`. Only the network step is replaced; URLs,
resource ids, ETags and Last-Modified are the real ones and each file's size matches the size in its server ETag. The captures ship in
`backend/data/captures/` with their manifests. **On a computer with normal internet access, `python -m scripts.collect_bbmp` fetches the
same files live.** That direct live run has not been done by the developer.

Fields obtained from real sources: work-order code (stored as `work_order_id`), work description, ward number in its own ward map,
contractor (name and vendor code; the phone number is kept only in the original row), bill number and date, gross amount, deductions, net
paid, payment date and RTGS reference, source URL, collection time; for tenders: tender number, title, value, published and closing dates,
department, category. **Not published by these sources** and therefore left empty: sanctioned amount, work-order date, job number as a
separate field, division.

## Completed (implemented and tested)

* **Collectors**: OpenCity via CKAN API (falls back to the web page), new-dataset discovery report, GBA site crawler (HTML, JS bundle,
  Playwright fallback), IFMS client, one command `python -m scripts.collect_bbmp` with per-source statistics, a failure report,
  `logs/collection.log` and a JSON report per run. Timeouts, retries with back-off, 1 request/s rate limit, robots.txt, host allow-list,
  conditional requests, SHA-256 duplicate detection, stored copies of every file, import history (`collection_runs`, `source_files`,
  `import_issues`), schedule.
* **Database**: PostgreSQL 16 + PostGIS 3.4 (or SQLite), Alembic migrations 0001-0005. Original rows, source URLs, import job ids,
  timestamps, file names, error logs and ward regime are kept for every record. Ward numbers are never compared across ward maps.
* **PDF extraction**: PyMuPDF text, Tesseract OCR with preprocessing for scanned pages, page numbers, raw and normalised values,
  confidence, OCR-quality flags, manual review and correction; extracted values are "unverified" until an officer verifies them.
* **Matching**: exact job number > work-order id > tender id; contractor + amount; description similarity (RapidFuzz) only as
  suggestions with method, confidence and evidence; officer accepts or rejects. On real data: 215 tender-work suggestions.
* **Complaint analytics**: haversine BallTree + time window + category + TF-IDF text + photo hash, DBSCAN, stable cluster ids,
  explainable alert rules, roadwork candidates per cluster, supporting reports shown per cluster.
* **Citizen portal** (`/citizen`): report with GPS or map pick, photo, description; reference + private tracking code; tracking;
  public map with clusters. Contact details never public.
* **Government monitoring** (`/gov`, officer/admin) and **Authority dashboard** (`/authority`, complaint handling): dashboard,
  works search, contractors, tenders, bills and payments, source records, document upload and extraction review, collected data,
  live web collection, match review, clusters, alerts (acknowledge / dismiss / resolve), complaint status, **audit log (admin)**.
* **Tests**: 316 automated tests pass on PostGIS and 315 on SQLite (0 skipped); browser end-to-end 25/25 steps; real-data runs above. See `docs/TEST_OUTPUT.md`.

## Fixed in this round (found by real data)

* Tender matching compared every tender with every work in a Python loop: >10 min on real volumes. Now vectorised: 6,842 x 9,612 in about 2 min.
* Description matching produced 3,553 suggestions, mostly generic wording ("improvements to roads and drains in ward no X"). Added checks:
  at least 2 shared distinctive words including one rare word, ward numbers must agree (only within the 198-ward map), the year in the work
  code must be within 2 years after the tender. Now 215, mostly genuine (same lake, village or building).
* A tender PDF re-ran matching for all tenders (6 min per upload); now only its own tender (PDF processed in about 40 s).
* PDF extraction read "years," as an amount and sentence fragments as department or contractor; fixed.
* IFMS job codes repeat across ward maps (498 codes in the real data appear in more than one map); IFMS rows therefore list candidate works
  with their ward map for verification instead of linking them.
* The manual-import result message was wiped by the page refresh; the "Works by ward" chart did not say which ward map a number belongs to.

## Partially completed

* **Live collection**: works end to end with the real files, but the direct live fetch from the internet was not possible from the
  build environment (see above). Run `python -m scripts.collect_bbmp` once on a normal connection to confirm.
* **IFMS**: client and storage built and tested against the field layout the page declares; never received a real row (server error 500).
* **Roads**: no real road map loaded (OpenStreetMap download was blocked in the build environment). Road search and road-to-work links
  need `scripts/fetch_osm_roads.py` + `scripts/load_roads.py` run on a normal connection.
* **PDF field extraction on real documents**: Tender ID and dates read correctly from the real tender document; many other labelled fields in
  long tender documents are not found or need review. Measured accuracy on held-out documents: see `docs/EVALUATION_RESULTS.md`.
* **Kannada OCR**: tested only on Kannada pages generated by the test suite (all 7 tests pass). **Not validated on real Kannada government documents.**

## Pending

* GBA 369-ward map (published as KML on OpenCity; not yet loaded).
* Real citizen complaints: none exist yet. The 85 complaints available for demonstration are **synthetic, labelled SAMPLE**.
* Security review, HTTPS, persistent rate limiting, backups, legal basis for personal data, accessibility audit, load testing.
* KPPP tender search (CAPTCHA) and IFMS (server error) cannot be automated now.

## Known limitations

* Payments are not proof of completion; clusters are not proof of negligence; fuzzy matches are suggestions only; "no record found" is not
  "no work done". The interface says so where these appear.
* OpenCity files are a republication of BBMP data by OpenCity volunteers; they can lag the official systems.
* Contractor names in the BBMP files are cut at 20 characters (6,055 rows flagged `contractor_name_cut`); the same vendor code sometimes
  carries different names (1,123 rows flagged `contractor_differs`).
