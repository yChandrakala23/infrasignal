import os, subprocess, sys, tempfile
from pathlib import Path


def test_alembic_upgrade_downgrade_and_no_drift():
    backend = Path(__file__).resolve().parents[1]
    url = "sqlite:///" + tempfile.mktemp(suffix=".db")
    env = {**os.environ, "DATABASE_URL": url}
    run = lambda *a: subprocess.run([sys.executable, "-m", "alembic", *a], cwd=backend, env=env, capture_output=True, text=True)
    r = run("upgrade", "head"); assert r.returncode == 0, r.stderr[-800:]
    r = run("check"); assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
    r = run("downgrade", "base"); assert r.returncode == 0, r.stderr[-800:]
