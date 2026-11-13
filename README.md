# InfraSignal

**Bengaluru Road Infrastructure Transparency and Citizen Monitoring System.** One FastAPI backend, one database, three web areas:

| Area | URL | Who | What it does |
|---|---|---|---|
| **Citizen portal** | http://localhost:8000/citizen | anyone, no account | Report a road problem with GPS (or a map pick), photo and description; get a reference and a private tracking code; track the report; public map of reports and clusters |
| **Government monitoring** | http://localhost:8000/gov | officer, admin | Dashboard, works search, contractors, tenders, bills and payments, original source records, collected BBMP data, live web collection, document upload and extraction review, match review, audit log (admin) |
| **Authority dashboard** | http://localhost:8000/authority | authority (officer: read-only), admin | Complaint clusters with their supporting reports, explainable alerts (acknowledge / dismiss / resolve), complaint status updates, roadwork candidates per cluster |

RV College of Engineering, EL (SDG 9) project. Team: Kartik Shekhar, Yarragunta Chandrakala, Ankitha A, K V Nihal Mouni, Kartik Kaul. Mentor: Dr. Manas M N.

> **Research prototype. Not an official BBMP or GBA application.** The BBMP work orders, payments, tenders and documents loaded by
> `setup_demo` are **real public data** (OpenCity, bbmp.gov.in) with their source links. The demonstration citizen complaints are
> **synthetic and labelled SAMPLE**. What works and what does not: [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md).

---

## 1. Run it on a fresh Windows computer

### Prerequisites

| Needed | Version | Get it | Notes |
|---|---|---|---|
| **Python** | 3.12 or 3.13, 64-bit | https://www.python.org/downloads/windows/ | In the installer tick **"Add python.exe to PATH"** |
| Git | any | https://git-scm.com/download/win | Only to clone. You can also extract the ZIP |
| Tesseract OCR | 5.x | https://github.com/UB-Mannheim/tesseract/wiki | Optional, only for **scanned** PDFs. In the installer, under "Additional language data", tick **Kannada** if you want Kannada |
| Docker Desktop | any | https://www.docker.com/products/docker-desktop/ | Optional, only for the PostgreSQL + PostGIS option (section 4) |

You do **not** need a database server for the standard setup: it uses SQLite, a file inside the project.

### Installation (PowerShell, about 5 minutes)

Open **PowerShell** (Start menu, type `powershell`) and run these lines one at a time:

```powershell
cd $HOME\Documents
git clone https://github.com/yChandrakala23/infrasignal.git     # or extract the ZIP here and skip this line
cd infrasignal
py -3.13 -m venv .venv                       # use  py -3.12  if that is the version you installed
.\.venv\Scripts\Activate.ps1                 # the prompt now starts with (.venv)
python -m pip install --upgrade pip
pip install -r backend\requirements.txt
cd backend
python -m scripts.make_env                   # writes ..\.env with a random secret key and prints the three passwords: note them
```

If `Activate.ps1` is refused ("running scripts is disabled"), run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, answer `Y`, and try again.

### Load the data (once, about 5-10 minutes)

```powershell
python -m scripts.setup_demo --with-sample-complaints
```

This loads, into the database: the real 198- and 243-ward maps, **17,232 real BBMP work-order and payment rows** (2024-25 and 2025-26,
four ward maps), **6,842 real BBMP tenders** (2017-18), two real PDFs from bbmp.gov.in, runs matching, and adds 85 **synthetic** sample
complaints so the clustering and alerts can be shown. Leave out `--with-sample-complaints` for real data only. Running it again adds nothing twice.

### Start the application

```powershell
python -m uvicorn app.main:app --port 8000
```

Leave this window open. Then open in your browser:

* Citizen portal: **http://localhost:8000/citizen**
* Government monitoring: **http://localhost:8000/gov** (log in with the officer or admin account printed by `make_env`)
* Authority dashboard: **http://localhost:8000/authority** (authority or admin account)
* API documentation: http://localhost:8000/docs

The web pages are served by the same backend: there is no separate frontend to start.

**Which command first?** `python -m scripts.make_env` (after installing the requirements). Then `setup_demo`, then `uvicorn`.

### Stop and restart

* **Stop:** press `Ctrl + C` in the PowerShell window running uvicorn.
* **Restart later:** open PowerShell and run

```powershell
cd $HOME\Documents\infrasignal
.\.venv\Scripts\Activate.ps1
cd backend
python -m uvicorn app.main:app --port 8000
```

Your data stays in `backend\infrasignal.db`. To start over, stop the server, delete that file and run `setup_demo` again.

---

## 2. Everyday tasks

All commands are run from the `backend` folder with the virtual environment active (`(.venv)` at the start of the prompt).

| Task | Command or page |
|---|---|
| **Live BBMP collection** (needs internet) | `python -m scripts.collect_bbmp` (all sources) or `python -m scripts.collect_bbmp --sources opencity`. On the website: Government monitoring > **Collected data** > *Collect now*, or > **Live web collection** > *Crawl now* |
| Check what was collected / imported | `python -m scripts.collect_bbmp --status`, or the **Collected data** page (runs, files, rows with problems, totals) |
| Import a BBMP CSV you downloaded | `python scripts\bbmp_collect.py --file C:\path\file.csv --fiscal-year 2024-25 --ward-regime 243`, or Collected data > *Import a file yourself* |
| Import another works / tender CSV or Excel | Government monitoring > **Import data** (shows the column mapping before anything is stored) |
| Process PDFs | `python -m scripts.process_pdf C:\path\file.pdf` (or a folder), or Government monitoring > **Documents** > Upload. Review values on the Documents page |
| Demonstrate collection without internet | `python -m scripts.replay_capture --twice` (replays the real captured files through the collector) |
| Run the tests | `pip install -r requirements-dev.txt` then `pytest -q` |

**Where things are stored**

| What | Where |
|---|---|
| Database (standard setup) | `backend\infrasignal.db` |
| Files downloaded by the collector | `backend\uploads\bbmp\<financial year>\` (OpenCity files), `backend\uploads\bbmp\web\` (website documents and page snapshots) |
| Uploaded / processed PDFs | `backend\data\uploads\documents\` |
| Citizen photos (private) | `backend\uploads\` |
| Collection logs | `backend\logs\collection.log` (every run) and `backend\logs\collection_<date>_<time>.json` (full report of one run) |
| Captured real files used by `setup_demo` | `backend\data\captures\` (with `manifest.json`: URLs, ETags, checksums, capture time) |

**What `collect_bbmp` does.** For each source: discovers the files (OpenCity through its CKAN API), downloads only what changed (ETag /
Last-Modified, then SHA-256), keeps a copy, validates and normalises every row, skips duplicates, imports into the database, and prints
per source: files downloaded / unchanged / failed and rows new / updated / duplicate / failed, followed by every failure with its reason.
It never logs in, never solves a CAPTCHA and respects robots.txt. Current state of each source (IFMS answers HTTP 500, KPPP needs a
CAPTCHA): see `IMPLEMENTATION_STATUS.md`.

---

## 3. Troubleshooting (Windows)

| Problem | Fix |
|---|---|
| `python` / `py` is not recognized | Reinstall Python with "Add python.exe to PATH" ticked, then open a **new** PowerShell window |
| `Activate.ps1 cannot be loaded because running scripts is disabled` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, answer `Y` |
| `pip install` fails building a package (pymupdf, scikit-learn, psycopg2) | Use 64-bit Python 3.12 or 3.13 (ready-made wheels exist for both). Run `python -m pip install --upgrade pip` first |
| `RuntimeError: Replace SECRET_KEY in .env` | Run `python -m scripts.make_env` (or `--force` to replace an old `.env`) |
| Login says the email or password is wrong | Use the accounts printed by `make_env` (they are in the `.env` file in the project folder). Accounts are created when the server starts |
| `[Errno 10048]` / address already in use | Another program uses port 8000: `python -m uvicorn app.main:app --port 8001` and open http://localhost:8001 |
| `database is locked` | Only run one server (and do not run `setup_demo` while the server is importing) |
| Scanned PDFs give no text / "tesseract is not installed" | Install Tesseract (prerequisites). If it is not in `C:\Program Files\Tesseract-OCR\`, set `TESSERACT_CMD=C:\path\to\tesseract.exe` in `.env` |
| `collect_bbmp` shows `network error`, `ProxyError` or `403` for every source | Your network blocks the sites (college / office proxy or firewall). Try another connection. Use `replay_capture` to demonstrate meanwhile |
| IFMS shows `HTTP 500 from the IFMS data service` | The BBMP portal itself is failing (it fails in a normal browser too). Nothing to fix locally; try another day |
| Project inside OneDrive gives odd file errors | Move the folder out of OneDrive (for example to `C:\infrasignal`) |
| Docker: `WSL 2 installation is incomplete` / Docker Desktop will not start | Run `wsl --install` in an administrator PowerShell, restart Windows, start Docker Desktop |
| Docker: `port 5432 is already allocated` | A local PostgreSQL is running. Stop it (Services > postgresql) or change `127.0.0.1:5432:5432` to `127.0.0.1:5433:5432` in `docker-compose.yml` |
| Docker: the api container exits | `docker compose logs api` shows why; usually `.env` was not created with `make_env --docker` |

---

## 4. Optional: PostgreSQL + PostGIS with Docker

```powershell
cd $HOME\Documents\infrasignal\backend
python -m scripts.make_env --docker          # needs Python only for this one step; or copy .env.example to .env and edit it
cd ..
docker compose up --build -d                 # starts PostGIS and the app
docker compose exec api python -m scripts.setup_demo --with-sample-complaints
```

Open the same URLs (http://localhost:8000/...). Collection inside Docker: `docker compose exec api python -m scripts.collect_bbmp`.
Logs: `docker compose logs -f api`. Stop: `docker compose down` (data is kept in a Docker volume; `docker compose down -v` deletes it).

Your own PostgreSQL instead: create a database with the `postgis` extension and run `python -m scripts.make_env --postgres "postgresql+psycopg2://user:password@localhost:5432/infrasignal"`.

---

## 5. How it fits together

```
Public sources (OpenCity CKAN API, bbmp.gov.in, IFMS, manual CSV/PDF)
  -> collectors (app/connectors/, collection.py, web_collection.py, ifms_collection.py)
  -> stored original files (uploads/bbmp) + collection_runs / source_files history
  -> parsing and normalisation (bbmp_schema.py, bbmp_ingest.py, ingest.py, extract.py, ocr.py)
  -> validation and duplicate detection (checksums, external keys, row hashes, import_issues)
  -> SQLAlchemy database (PostgreSQL + PostGIS, or SQLite), Alembic migrations 0001-0005
  -> matching (matching.py), clustering and alerts (clustering.py, alerts.py)
  -> portals (static/ pages + routers/)
```

* `extract.py`, `ocr.py`, `classify.py`, `boq.py`: PDF text (PyMuPDF), Tesseract OCR with preprocessing for scanned pages, document type,
  field values with page, snippet, method and confidence; values stay "unverified" until an officer verifies or corrects them.
* `matching.py`: exact job number > work-order id > tender id can link automatically; contractor + amount and description similarity
  (RapidFuzz) only ever **suggest**, with evidence, for an officer to accept or reject.
* `clustering.py`, `alerts.py`: haversine BallTree candidates, time window, category, TF-IDF text and photo similarity, DBSCAN; alerts
  explain the rule and the evidence.
* `bbmp_schema.py`, `bbmp_ingest.py`: the OpenCity work-order format (work code, ward, contractor, bill/RTGS details, gross / net / deduction).
* `static/`: no build step (vendored Leaflet and Chart.js).

More: `docs/BBMP_COLLECTION.md`, `docs/LIVE_CRAWLING.md`, `docs/DATA_SOURCES.md`, `docs/TESTING.md`, `docs/TEST_OUTPUT.md`, `docs/EVALUATION_RESULTS.md`.

## 6. Reading the data correctly

* A payment record is never read as proof that work was completed.
* A cluster shows repeated complaints. It does not show who is responsible or why, and it is not evidence of negligence.
* A fuzzy match is a suggestion, not a verified relationship.
* "No government record found" means none of the loaded records matches. It does not mean no work was done.
* Ward numbers belong to their ward map (198, 225, 243, common, gba369) and are never compared across maps.

## 7. Privacy and security

Citizen contact details are visible only to the admin role; photos are private (EXIF removed, fetched only with a staff login); tracking
needs the reference and the private code; submissions are rate-limited; user text is escaped on output; secrets live only in `.env`,
which is not committed. This has not had a security review or load test: do not deploy it publicly as it is.
