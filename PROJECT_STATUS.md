# Project status

**Build level: about 60% of the specification** (see `docs/FEATURES.md` for the line-by-line list). The software pipeline, three interfaces, PostGIS storage, matching, clustering, alerts and evaluation are built and tested. What is missing is mostly *real data*, live-service paths, and production hardening.

Second round added: many-to-many work/source links, PDF provenance, provisional works, BoQ items, dashboard accuracy, `.xls` support, Kannada-capable OCR with page flags, and migration 0003 with backward compatibility.

Third round added: automated BBMP collection (OpenCity datasets 2023-24, 2024-25, 2025-26 and a scan of the official site): connector, per-year column mapping, validated duplicate-safe import, run and file history, retry, manual import, APScheduler, API and a **Collected data** page (`docs/BBMP_COLLECTION.md`). **Not yet run against the live websites** (not reachable from the build environment); tests use saved fixtures that reproduce the real layout with invented values.

Verified: 295 automated tests on SQLite, 296 on PostGIS; migrations up/down/check and an upgrade from the old schema with data; clean-virtualenv install check; browser run (all steps pass, including the new Collected data page) (`screenshots/`). Details and caveats: `docs/TESTING.md`. All data bundled with the project is synthetic and labelled SAMPLE.

Fourth round added: **live web crawler as the primary collection path** (`docs/LIVE_CRAWLING.md`): crawls the GBA / BBMP site, legacy site and OpenCity search, discovers links from HTML, the site's JavaScript bundle or Playwright, extracts HTML and PDF tables, downloads PDF / CSV / Excel with ETag and checksum change detection, runs PDFs through the document pipeline, stores pages, items and records (migration 0005), API under `/api/gov/collection/web/*`, dashboard page **Live web collection**, CLI `scripts/crawl_live.py`, scheduled with the existing schedule. Checked against the real site's structure and two real PDFs replayed into PostgreSQL; the automated live fetch itself could not run from the build environments (network policy) and still has to be run once from an unrestricted machine. 299 tests pass on PostGIS, 298 on SQLite.

Fifth round: OpenCity collection through the CKAN API, IFMS collector, `python -m scripts.collect_bbmp`, real BBMP data imported
(17,232 work-order/payment rows, 6,842 tenders, 2 GBA PDFs), scalable and stricter tender matching, Windows README, `setup_demo`,
`make_env`, `process_pdf`, audit log page. **Current status: see `IMPLEMENTATION_STATUS.md`** (this file is the history).

## Next steps toward a pilot
1. Load real data: GBA 369-ward map, OSM roads, real BBMP work-order / payment exports and KPPP tenders; re-run `scripts/run_evaluation.py` after adding real, hand-labelled documents (the 66% independent extraction score is the number to improve).
2. Turn on and test the LLM extraction pass and the multilingual embedding option. Kannada OCR is wired and tested on self-rendered pages only; validate it on real Kannada government documents (and add a Kannada label dictionary for extraction).
3. Security review, HTTPS, persistent rate limiting, token revocation, backup and retention rules for photos and contact details, and a legal basis for personal data.
4. Notifications for alerts and for citizens; an admin page for users and data loading.
5. Load test clustering and alerts at realistic volumes; accessibility audit; check gov/authority pages on phones.

Interpretation rules (built into the interface and tests): payments do not establish completion; complaint clusters do not establish negligence; fuzzy matching only suggests; "no record found" is not "no work done"; ward regimes are never conflated. This is a student research prototype, not an official government system.
