"""Load the bundled SAMPLE data into the configured database (DATABASE_URL).

    python scripts/seed_sample_data.py              # wards + roads + works + documents + complaints
    python scripts/seed_sample_data.py --no-documents --no-complaints   # geography and works only

Everything loaded here is synthetic and labelled "SAMPLE (synthetic, not a government record)" or is_demo=True.
It is for trying the system out and for the evaluation. Never present it as real government or citizen data.
Safe to run twice: existing records are detected and skipped.
"""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import SessionLocal, init_db
from app import seed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-documents", action="store_true")
    ap.add_argument("--no-complaints", action="store_true")
    ap.add_argument("--llm", action="store_true", help="also run the optional LLM extraction pass (needs LLM_API_KEY)")
    a = ap.parse_args()
    init_db()
    db = SessionLocal()
    out = seed.seed_all(db, None, documents=not a.no_documents, complaints=not a.no_complaints, use_llm=a.llm)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
