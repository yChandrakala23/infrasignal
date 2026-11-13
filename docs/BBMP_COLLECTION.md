# BBMP automated data collection

Collects BBMP work-order and payment files from open data, checks them, stores them in the existing tables and shows them on the Government Monitoring page (**Collected data**). It reuses the existing backend: the same models, `work_source_links`, import jobs, dashboard filters and roles. Nothing was rebuilt.

> **Read this first: what was and was not verified.**
> The environment this was built in could not download from `data.opencity.in` or `site.bbmp.gov.in` (outbound access was limited to package registries). So **no live end-to-end run against the real websites has been done**, and the official BBMP site (`site.bbmp.gov.in`) was never inspected. The file layout the parsers expect (column names, the `wodetails`, `contractor` and `brnumber` formats, resource names and download URL shapes) was read from the public OpenCity dataset pages. All automated tests run on saved fixtures in `backend/tests/fixtures/bbmp/`. Those fixtures reproduce the real layout and use real-looking `brnumber` / contractor strings, but their descriptions, amounts and wards are **invented**. They are test data, not BBMP records. The first live run on your machine (below) is the real test; expect to adjust `data/bbmp_sources.json` or the column synonyms if a site has changed.

## What the published files contain (and do not)

Every 2023-24 / 2024-25 / 2025-26 file has the columns `slno, id, Ward, wodetails, contractor, brnumber, amount, nett, deduction` (header case varies). One row is **one payment against one work order**.

| Needed field | Where it comes from |
|---|---|
| Work ID / job number | the code at the start of `wodetails` (`NNN-YY-NNNNNN`) |
| Description, road | text after the code; the road is read from it when one is named |
| Ward, ward map | `Ward`; the map (198 / 225 / 243 / common) comes from the file name or dataset page |
| Contractor name and ID | `contractor`: optional 6-digit code, name (often shortened), phone number glued on. **The phone number is dropped, never stored** |
| Bill number, payment reference and date | `brnumber` (`BR - … / date`, `Rtgs - … / date`) |
| Gross, deduction, net | `amount`, `deduction`, `nett` |
| Financial year | year of the payment (RTGS) date; the file's year |
| **Estimated amount, approved amount, work-order date, department** | **Not published in these files. They stay empty and are shown as "Not published", never guessed.** A file that does contain such columns (a different schema) is mapped through `COLUMN_SYNONYMS` in `app/bbmp_schema.py` |

## How a run works

1. **Discover.** Each dataset page in `data/bbmp_sources.json` is fetched and its resource links, names, formats and metadata (licence, dates) are read (BeautifulSoup; follows pagination). Download URLs are *never stored as fixed links*: they are read from the page on every run. `site.bbmp.gov.in` is scanned for CSV / Excel links the same way. Playwright is used only if `BBMP_USE_PLAYWRIGHT=1`, a page has no links in its static HTML and Playwright is installed.
2. **Download.** Timeouts, retries with back-off (timeouts, connection errors, 429, 5xx), a pause between requests, a size cap, `robots.txt` honoured, only allowed hosts, private addresses refused. A file the server says is unchanged (ETag / Last-Modified, HTTP 304) or whose SHA-256 equals the last imported copy is **skipped**. The downloaded bytes are stored unchanged.
3. **Validate and normalise** (`app/bbmp_schema.py`). Per-year column mapping (synonyms plus `column_overrides` per dataset), names, dates, amounts and IDs normalised, unknown columns kept in the raw record. A bad row (no work ID and no amount, negative or unreadable amount, bad date) is **reported with its original text and not stored**; `nett + deduction ≠ amount` is stored with a warning.
4. **Import** (`app/bbmp_ingest.py`), one transaction per file. Row-level problems are reported. A database error, or more than half the rows failing (≥ 20 rows), **rolls the whole file back** and marks it `import_failed`.
5. **Record.** A `collection_runs` row (counts and a log) and a `source_files` row per file: source website, dataset URL, download URL, file name, financial year, download date, SHA-256, import date, rows processed / inserted / duplicate / failed, status and error.

### What goes where, and how duplicates are handled

* **Original, normalised, verified stay apart.** `source_records.raw` is the row exactly as published; `source_records.normalised` and the work's columns are the cleaned values; `roadworks.verified_fields` lists values an officer confirmed.
* **Import never overwrites better data.** An empty field is filled; an existing value is kept; a value in `verified_fields` is locked. A different incoming value is logged as a *conflict* in the import issues list.
* **Works.** `roadworks.external_key = bbmp:{ward map}:{work-order code}`. The same work in several rows and years is one work with many source rows (`work_source_links` is many-to-many).
* **Payments are never merged.** Each published row is its own bill and payment (`external_key` = the source row). Two payments for one work stay two. When the same RTGS reference, date and amount appear in two files, the later one is marked `duplicate_of_id` and left out of every sum (`sources.counted_payments`), so one payment is not counted twice.
* **Re-running adds nothing.** An unchanged file is skipped; a forced or changed file updates its own rows in place.

## Running it

### From the Government Monitoring page
**Collected data**: last successful collection, **Collect now** (all datasets or one), retry failed files, run history (click a run for its log), files with checksum / status / stored copy, rows with problems, payment history, manual file or URL import, and (admin) the schedule. Totals by year, ward, contractor, missing information, rows not linked to a work, and where every record came from.

### From the command line (use this for the first live run)
    cd backend
    python scripts/bbmp_collect.py --dataset bbmp-work-orders-and-payments-2024-25     # the 2024-25 workflow first
    python scripts/bbmp_collect.py                                                   # all three years
    python scripts/bbmp_collect.py --status
    python scripts/bbmp_collect.py --retry
    python scripts/bbmp_collect.py --file my.csv --fiscal-year 2024-25 --ward-regime 243   # a file you downloaded yourself
Exit code 0 = all good, 1 = some files or rows failed (read the printed reasons), 2 = could not start.

### Through the API (officer role; the schedule needs admin)
| | |
|---|---|
| `POST /api/gov/collection/run` `{"datasets": [...], "force": false}` | start (runs in the background; 409 if one is already running) |
| `GET /api/gov/collection/status` | last run, **last successful collection**, schedule, configured datasets |
| `GET /runs`, `/runs/{id}` · `GET /files`, `/files/{id}`, `/files/{id}/download` | history, files, the stored copy |
| `GET /records` (`unmatched=true`) · `GET /issues` (`kind=failed\|duplicate\|updated\|conflict\|warning`) · `GET /payments` | imported rows, failures and duplicates, payment history |
| `POST /retry` `{"file_ids": [...]}` | retry failed downloads / imports |
| `POST /manual-import` (file) · `POST /manual-url` | the fallback when a site cannot be read automatically |
| `GET/PUT /schedule` | APScheduler cron |
| `GET /summary` | the numbers on the dashboard page (same filters as the other screens) |
| `PATCH /api/gov/roadworks/{id}/fields`, `DELETE …/fields/{name}` | officer-verified values (locked against imports) |

### Schedule
In-process APScheduler (no queue, no extra service), **off by default**. Set it on the page / `PUT /schedule`, or `BBMP_SCHEDULE_ENABLED=1`, `BBMP_SCHEDULE_CRON="0 3 * * 0"` (standard 5-field cron, **0 = Sunday**), `BBMP_SCHEDULE_TZ=Asia/Kolkata`. A scheduled run is skipped while another is running. With several web workers enable it in one only.

### Settings
`BBMP_SOURCES_FILE` (another sources file) · `BBMP_ALLOWED_HOSTS` (default `data.opencity.in,site.bbmp.gov.in`) · `BBMP_USE_PLAYWRIGHT` (default off) · `BBMP_DOWNLOAD_DIR` (default `UPLOAD_DIR/bbmp`) · `BBMP_MAX_FAILED_FRACTION` (0.5) · `BBMP_RUN_LOCK_MINUTES` (120; an older "running" run is treated as interrupted) · `BBMP_USER_AGENT` · `BBMP_SCHEDULE_*`.

Playwright is optional and not in `requirements.txt`: `pip install playwright && playwright install chromium`.

## What it will not do
* It does not log in, solve CAPTCHAs, or get round access restrictions. A login or CAPTCHA page, 401/403, or a `robots.txt` refusal is **recorded as a failed download with the reason** and left alone. Use the manual import for such files.
* It invents nothing: no estimated or approved amounts, no work-order dates, no ward, contractor or road that the file does not give.
* Sample data is never counted as collected data (and the reverse): the page and `/summary` count only `data_origin = bbmp_open_data`; the Overview has a **Data origin** filter.

Screenshots `screenshots/26_gov_collected_work.png` and `27_gov_collected_data.png` show the page after importing one saved fixture file. The figures in them come from invented fixture values, not real BBMP records.

## Tests
`backend/tests/test_z1_bbmp_pipeline.py` (schemas, mapping, normalisation, invalid rows, duplicates, insertion, payments, many-to-many, rollback, discovery from saved HTML, CSV / Excel) and `test_z2_bbmp_collection_api.py` (the full 2024-25 workflow, repeated runs, changed files, failures and retry, 2023-24 and 2025-26, manual import, schedule, API access, dashboard numbers equal to the database, sample data excluded, verified values surviving a collection). They use a pretend website made from the saved fixtures; nothing touches the network. See `docs/TESTING.md`.

## Known gaps
* Never run against the live sites (see the top of this page). Resource URLs, headers or page markup may differ from what was read.
* The official site `site.bbmp.gov.in` is scanned for data links, but what it offers is unknown, so only the OpenCity files are known to work.
* Contractor names are as printed (often cut to about 20 characters), so two spellings of one firm may be two contractors unless they share the 6-digit code.
* The 2023-24 file uses the old 198-ward numbering; it is read as such (`default_ward_regime`). The ward map of a file is not inside the file, so it comes from the dataset page or file name; check it on the first live run.
* Excel (`.xls`, `.xlsx`) reading is tested on files written by the tests, not on a published BBMP Excel file.

## Update (round 5)

* Datasets are now discovered through OpenCity's **CKAN API** (`/api/3/action/package_show`), which gives exact resource ids, formats and
  modification times; the dataset web page is read only if the API fails (`BBMP_USE_CKAN_API=0` forces the web page).
* Each run also searches the portal (`discover_query` in `data/bbmp_sources.json`) and lists BBMP work-order datasets that are not configured yet.
* Run everything with `python -m scripts.collect_bbmp`. Real data was imported on 13 Nov 2026 from captured files; see `docs/TEST_OUTPUT.md`.
