# Project status and verification notes

This source was assembled from the InfraSignal prototype material supplied in the conversation. It has not been executed against a live government data source or deployed server. The included test suite has been run locally; current results are recorded in the handoff message, and should be rerun in your own environment.

Before a pilot:

1. Copy `.env.example` to `.env`, replace all `CHANGE_ME` values, install dependencies, and run `python -m pytest -q` from `backend/`.
2. Fix every failing test and add integration tests for both portals.
3. Check the actual CSV headers with `python scripts/inspect_csv.py path/to/file.csv` before importing.
4. Review the privacy/security of report tracking, contact details, uploaded media, session tokens, rate limiting, authentication, and admin endpoints.
5. Change all credentials and the signing secret; never deploy the defaults.
6. Confirm the legal basis, licences, source dates, and current administrative boundaries for any imported dataset.

Interpretation rules: payments do not establish completion; complaint clusters do not establish negligence; fuzzy matching is candidate generation only and requires human review; historical ward regimes must not be conflated with current GBA wards. This is a student research prototype, not an official government system.
