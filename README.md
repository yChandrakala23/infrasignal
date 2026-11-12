# InfraSignal

Bengaluru road infrastructure monitoring: a citizen reporting portal and a
government style monitoring portal sharing one backend.

RV College of Engineering, EL (SDG 9) project.
Team: Kartik Shekhar, Yarragunta Chandrakala, Ankitha A, K V Nihal Mouni, Kartik Kaul.
Mentor: Dr. Manas M N.

> Student research prototype. Not an official BBMP or GBA service. Demo
> accounts are prototype roles, not government accounts.

## Features
* Citizen portal (/citizen): geotagged reports, photo upload, tracking, public map
* Monitoring portal (/gov): dashboard, roadwork explorer, lifecycle evidence,
  clusters, alerts, record matching review, imports, PDF extraction, audit log
* Spatial, temporal and text clustering (DBSCAN)
* Source provenance on every imported row

## Quick start
    cp .env.example .env
    docker compose up --build

Open http://localhost:8000/citizen and http://localhost:8000/gov

Without Docker (run from the project root after creating `.env`):
    pip install -r backend/requirements.txt
    cd backend && uvicorn app.main:app --reload

Tests:
    cd backend && pytest -q

## Demo accounts (prototype only)
| Role    | Email                    | Password    |
|---------|--------------------------|-------------|
| Admin   | admin@infrasignal.demo   | Admin@123   |
| Officer | officer@infrasignal.demo | Officer@123 |

Use unique values in `.env`; do not deploy default or test credentials. Account seeding is disabled unless the relevant email and password are explicitly configured.

## Data sources
* BBMP Work Orders and Payments (OpenCity, public domain): https://data.opencity.in
* Karnataka Public Procurement Portal (manual collection): https://kppp.karnataka.gov.in
* Map tiles: OpenStreetMap contributors

Historical ward numbers (198, 243, 225 regimes) are not comparable with the
369 GBA wards.

## Data integrity rules
* Payment records are never treated as proof of completed work.
* Complaint clusters show repeated reports, not negligence.
* Synthetic reports are flagged is_demo and labelled in both portals.

## Known limitations
See the Known limitations section of the project report.
