"""Create the .env file the application needs, with a random secret key and random passwords. Run once, from the backend folder.

    python -m scripts.make_env              # local run with SQLite (no database server needed)  <- simplest
    python -m scripts.make_env --docker     # for: docker compose up   (PostgreSQL + PostGIS in Docker)
    python -m scripts.make_env --postgres "postgresql+psycopg2://user:password@localhost:5432/infrasignal"   # your own PostgreSQL

The passwords are printed once. They are stored only in .env, which is never committed (see .gitignore). Re-running refuses to
overwrite an existing .env unless you pass --force.
"""
import argparse, secrets, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / ".env.example"
TARGET = ROOT / ".env"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--docker", action="store_true")
    g.add_argument("--postgres")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if TARGET.exists() and not a.force:
        print(f"{TARGET} already exists. Use --force to replace it (the old passwords stop working).")
        return 1
    db = ("postgresql+psycopg2://infra:infra@db:5432/infrasignal" if a.docker else a.postgres or "sqlite:///./infrasignal.db")
    pw = {k: secrets.token_urlsafe(12) for k in ("ADMIN", "OFFICER", "AUTHORITY")}
    out = []
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key == "DATABASE_URL":
            line = f"DATABASE_URL={db}"
        elif key == "SECRET_KEY":
            line = f"SECRET_KEY={secrets.token_urlsafe(48)}"
        elif key and key.endswith("_PASSWORD") and key[:-9] in pw:
            line = f"{key}={pw[key[:-9]]}"
        out.append(line)
    TARGET.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"Wrote {TARGET}\nDatabase: {db}\n\nLog in with:")
    for role, email in (("ADMIN", "admin@example.org"), ("OFFICER", "officer@example.org"), ("AUTHORITY", "authority@example.org")):
        print(f"  {role.lower():9}  {email:24}  password: {pw[role]}")
    print("\nThese passwords are only in .env. Keep that file private.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
