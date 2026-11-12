# InfraSignal

Bengaluru road infrastructure monitoring. Three web areas on one FastAPI backend:

| Area | URL | Who | What it does |
|---|---|---|---|
| **Public Portal** | `/citizen` | anyone, no account | Road map (every mapped road, with or without a government record), road profiles, report a problem with photos + GPS, track a report by reference number |
| **Government Monitoring** | `/gov` | officer, admin | Ingest CSV / Excel / PDF, classify documents, extract fields with page references, cross-document matching with confidence and manual review, filters, per-work profiles, roads, contractors, evaluation |
| **Authority Dashboard** | `/authority` | authority (read-only: officer), admin | Complaint clusters, severity, linked roads and works, configurable alerts and their history, report status management, roads with no government record |

RV College of Engineering, EL (SDG 9) project. Team: Kartik Shekhar, Yarragunta Chandrakala, Ankitha A, K V Nihal Mouni, Kartik Kaul. Mentor: Dr. Manas M N.

> Student research prototype, not an official BBMP or GBA service. **All bundled works, tenders, documents and complaints are synthetic SAMPLE data**, labelled as such in the interface and in the database. They are not real government records or real complaints. See `docs/DATA_SOURCES.md`.

Screenshots of every area are in `screenshots/`. What is and is not finished: `docs/FEATURES.md`. Test and evaluation results with their limits: `docs/TESTING.md`, `docs/EVALUATION_RESULTS.md`.

## Quick start

### A. Docker (PostgreSQL 16 + PostGIS 3.4)
    cp .env.example .env        # replace every CHANGE_ME value; set AUTO_SEED_SAMPLE=1 to load the sample data
    docker compose up --build
Open http://localhost:8000/citizen, `/gov`, `/authority`. API docs at `/docs`.

### B. Without Docker (SQLite, nothing else to install)
    python -m venv .venv && source .venv/bin/activate
    pip install -r backend/requirements.txt        # developed and tested on Python 3.13
    sudo apt install tesseract-ocr                 # optional, only needed for scanned PDFs
    export SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
    export ADMIN_EMAIL=you@example.org ADMIN_PASSWORD='a long password'
    export OFFICER_EMAIL=officer@example.org OFFICER_PASSWORD='another long password'
    export AUTHORITY_EMAIL=authority@example.org AUTHORITY_PASSWORD='a third long password'
    export AUTO_SEED_SAMPLE=1                      # optional: load labelled SAMPLE data on first start
    cd backend && uvicorn app.main:app --port 8000
Without `DATABASE_URL` the app uses `sqlite:///./infrasignal.db`; the same code runs on PostGIS when `DATABASE_URL` points at PostgreSQL (spatial queries then use `ST_*` functions instead of shapely). Migrations run automatically at start.

There are **no default accounts**. An account is created at start-up only for each `*_EMAIL` / `*_PASSWORD` pair you set, and the app refuses to start while any value still starts with `CHANGE_ME`.

### Load data
    cd backend
    python scripts/seed_sample_data.py                         # wards + sample roads + sample works/tenders/PDFs + 85 labelled sample complaints
    python scripts/load_wards.py --regime gba369 --file Final_Wards_369.kml --source "GBA wards map"   # real ward map, KML or GeoJSON
    python scripts/fetch_osm_roads.py && python scripts/load_roads.py --file data/roads/osm_bengaluru.json --source "OpenStreetMap contributors (ODbL)"
Real work-order / tender CSVs and PDFs go in through the Government Monitoring page (Import data, Documents). Check a CSV's headers first with `python scripts/inspect_csv.py file.csv`; the import page previews the column mapping before anything is stored.

### Tests and evaluation
    pip install -r backend/requirements-dev.txt
    cd backend && pytest -q                                    # 59 tests, SQLite
    TEST_DATABASE_URL=postgresql+psycopg2://user:pw@localhost/db_to_wipe pytest -q    # same suite on PostGIS (the database is WIPED)
    python scripts/run_evaluation.py                           # rewrites docs/EVALUATION_RESULTS.{md,json}
    node scripts/e2e_screenshots.js                            # browser end-to-end + screenshots (needs playwright; see the file header)

## How the pieces fit
* `backend/app/extract.py`, `narrative.py`, `classify.py`, `llm_extract.py`: PDF text (PyMuPDF) with Tesseract OCR fallback, document type, field values with document + page + snippet + method + confidence. The optional LLM pass keeps a value only if it literally occurs on the cited page.
* `matching.py`: exact identifiers first (job number > tender ID > work/contract ID), fuzzy contractor / road / description only ever *suggests*. Conflicting identifiers go to manual review. Human decisions are never overwritten; every change is logged.
* `spatial.py`, `roads.py`, `wards.py`: ward from coordinates, nearest road segment, road to work links. Roads and wards live in PostGIS geometry columns on PostgreSQL.
* `clustering.py`, `alerts.py`, `pipeline.py`: DBSCAN on distance + text + photo + time + category, stable cluster IDs, configurable alert rules with history.
* `evaluation.py`: extraction accuracy, matching precision/recall/F1, clustering vs labels, false-positive alert analysis.
* `migrations/`: Alembic, `0001` = the original prototype schema, `0002` = everything added here (databases created by the prototype are stamped and upgraded, data kept).
* `static/`: no build step. Vendored Leaflet and Chart.js, one shared stylesheet.

## Reading the data correctly
Provenance is shown by line style as well as label: **solid = verified official**, **dashed = inferred (system suggestion)**, **dotted = citizen-submitted**.

* A payment record is never read as proof that work was completed. "Completion certificate on file" appears only when such a document is loaded.
* A cluster shows repeated complaints. It does not show who is responsible or why.
* "No government record found" means none of the loaded records matches. It does not mean no work was done.
* No page says work is delayed, fraudulent or complete unless a loaded document says so; a passed deadline without a completion document is shown as "check", not as delay.
* Ward numbers belong to their regime (243, 198, gba369) and are never compared across regimes.

## Privacy and security notes
Citizen contact details are visible only to admin; photos are private (bearer-token fetch only, EXIF removed); report tracking needs the reference number and a secret code; report submission is rate-limited per client; all user text is HTML-escaped on output. This has not had a security review or load test. Do not deploy it publicly as-is (see `docs/FEATURES.md`, "Not done").
