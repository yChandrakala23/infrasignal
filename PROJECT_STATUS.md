# Project status

**Build level: about 60% of the specification** (see `docs/FEATURES.md` for the line-by-line list). The software pipeline, three interfaces, PostGIS storage, matching, clustering, alerts and evaluation are built and tested. What is missing is mostly *real data*, live-service paths, and production hardening.

Verified: 59 automated tests on SQLite and PostGIS; migrations up/down/check on PostgreSQL; 19-step browser run with screenshots (`screenshots/`). Details and caveats: `docs/TESTING.md`. All data bundled with the project is synthetic and labelled SAMPLE.

## Next steps toward a pilot
1. Load real data: GBA 369-ward map, OSM roads, real BBMP work-order / payment exports and KPPP tenders; re-run `scripts/run_evaluation.py` after adding real, hand-labelled documents (the 66% independent extraction score is the number to improve).
2. Turn on and test the LLM extraction pass and the multilingual embedding option; enable Kannada OCR.
3. Security review, HTTPS, persistent rate limiting, token revocation, backup and retention rules for photos and contact details, and a legal basis for personal data.
4. Notifications for alerts and for citizens; an admin page for users and data loading.
5. Load test clustering and alerts at realistic volumes; accessibility audit; check gov/authority pages on phones.

Interpretation rules (built into the interface and tests): payments do not establish completion; complaint clusters do not establish negligence; fuzzy matching only suggests; "no record found" is not "no work done"; ward regimes are never conflated. This is a student research prototype, not an official government system.
