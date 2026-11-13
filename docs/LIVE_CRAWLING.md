# Live web crawling (primary collection path)

The crawler visits the official GBA / BBMP website and the other portals listed under `crawl_sources` in
`backend/data/bbmp_sources.json`, discovers notices, tenders, work orders and payment documents, downloads them, extracts
what it can read and stores everything in the database with its source. Manual file / URL import stays available as a fallback.

## Run it

```
cd backend
python scripts/crawl_live.py                      # all enabled sources
python scripts/crawl_live.py --report run1.json   # plus an evidence file (pages, documents, hashes, records)
python scripts/crawl_live.py                      # run again: unchanged pages and files are detected, nothing is duplicated
```

Or press **Crawl now** on the Government dashboard, page **Live web collection**. The scheduled collection (Collected data >
Schedule) crawls the websites after reading the OpenCity datasets unless `WEB_CRAWL_ON_SCHEDULE=0`.

Optional: `pip install playwright && playwright install chromium` and `BBMP_USE_PLAYWRIGHT=1` for pages whose links appear
only after JavaScript and are not found in the page's script bundle.

## How a run works

| Step | Code | What happens |
|---|---|---|
| Visit | `connectors/web_crawler.py` `WebCrawler.crawl` | robots.txt, 1 s between requests, retries with back-off, host allow-list, private addresses refused |
| Discover | same | 1) `<a>`, `<iframe>`, `<embed>` links; 2) links compiled into the site's JavaScript bundle; 3) Playwright, only if 1 and 2 found nothing |
| Extract | `extract_tables` | every HTML table with a header row becomes records, cells kept as published |
| Download | `web_collection._download_item` | conditional request (ETag / Last-Modified), SHA-256 compare; unchanged files are not stored again, changed files become a new version |
| Process | `_process_document` | PDFs go through the existing document pipeline (type, fields, OCR, tender record, matching) with source page, download URL and time; PDF tables, CSV and Excel rows become `web_records` |
| Store | tables `web_pages`, `web_items`, `web_records` (migration 0005) | pages: first seen, last changed, times changed, HTML snapshot; items: unique per URL, first and last seen, still listed; records: unique per row hash |
| Report | `collection_runs` (connector `web`) | counts and a log: new links, changed pages and files, failures with the exact reason |

Login pages, CAPTCHAs, 401 and 403 are reported and never worked around.

## What the real site looks like (checked 12 Nov 2026)

* `https://bbmp.gov.in/` is now the **Greater Bengaluru Authority** site, a React single-page app. Its static HTML contains
  **no links**. Everything is in `/static/js/main.075d4bdf.js` (585,999 bytes): **89 PDF links**, 77 of them land-use maps.
  The bundle stores links as configuration keys (`csr:{CSRTender:"/csr/TENDER_DOCUMENT_TOWNPLANNING.pdf"}`), and many paths
  contain spaces. The crawler reads the bundle directly, so no browser is needed for this site.
* `robots.txt` allows everything. PDFs are served with `ETag` and `Last-Modified`, so change detection uses 304 responses.
* `updates.bbmpgov.in` (e-Khatha files) refused a cross-origin request from the page; not yet verified from a normal client.
* Land-use maps are listed but not downloaded (`skip_download_patterns`), so they do not use up the per-run download limit.

## Evidence and its limits

**The automated live fetch has not been run by the developer.** This account's network policy blocks `bbmp.gov.in`,
`site.bbmp.gov.in`, `data.opencity.in` and `updates.bbmpgov.in` from both the cloud build environment (proxy answers
403 to `CONNECT bbmp.gov.in:443`) and the Linux environment on the user's PC. Only the desktop browser pane could reach the site.

What was done instead, on 12 Nov 2026:

1. The live home page, bundle, `robots.txt` and headers were read in the browser pane (findings above).
2. Two real PDFs were downloaded in that browser: `TENDER_DOCUMENT_TOWNPLANNING.pdf` (2,753,974 bytes, sha256 `ce4eb0d1...`)
   and `RetiredEmployeeNotification.pdf` (1,771,858 bytes, sha256 `147557ee...`). Sizes match the server's Content-Length.
3. Those captured responses were replayed through the **unchanged** crawler, pipeline and a PostgreSQL 16 + PostGIS 3.4
   database. Only the network transport was replaced. Results:

| | Run 1 | Run 2 (same content) |
|---|---|---|
| Pages | 1 new (read via JS bundle) | 1 unchanged |
| Document links | 15 found, 15 new | 15 found, 0 new, 0 no longer listed |
| Downloads | 2 downloaded and processed | 0; both answered 304 to the conditional request |
| Records | 170 inserted (3 repeated rows inside one PDF table dropped) | 0 inserted |

Stored from real data: the tender PDF classified as a Notice Inviting Tender (123 pages), Tender ID **GBA/2025-26/SE0022**
extracted and a tender record created; the notification is scanned and was read by OCR (3 of 4 pages). Some extracted fields
are wrong (for example a sentence fragment as "department"); they are stored as unverified for review.
The 9 other downloads in those runs failed only because those files were not captured.

Bugs found by the real data and fixed: labels empty for config-key links; paths with spaces missed; duplicate rows within one
table crashed the insert; every link marked "no longer listed" on the second run; stored filename truncated.

**To finish the live demonstration**, run `python scripts/crawl_live.py --report run1.json` twice on a machine that can reach the
sites (any normal PC or server), then open **Live web collection**. Compare the evidence files with the table above.
