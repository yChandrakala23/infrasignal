"""Test setup. Uses a throw-away SQLite database by default. Set TEST_DATABASE_URL to a PostGIS URL to run the same suite on PostgreSQL:
   TEST_DATABASE_URL=postgresql+psycopg2://infra:infra@localhost/infrasignal_test pytest -q
(The PostgreSQL database is wiped first.)"""
import os, tempfile
URL = os.getenv("TEST_DATABASE_URL") or "sqlite:///" + tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = URL
os.environ["UPLOAD_DIR"] = tempfile.mkdtemp()
os.environ["DOCUMENT_DIR"] = tempfile.mkdtemp()
os.environ["SECRET_KEY"] = "test-only-secret-key-not-for-deployment"
for p, role in (("ADMIN", "admin"), ("OFFICER", "officer"), ("AUTHORITY", "authority")):
    os.environ[f"{p}_EMAIL"] = f"{role}@example.test"
    os.environ[f"{p}_PASSWORD"] = f"{role.title()}-test-pass-1"
os.environ["REPORT_RATE_PER_HOUR"] = "1000"
import pytest
from fastapi.testclient import TestClient

if URL.startswith("postgresql"):
    from sqlalchemy import create_engine, text
    e = create_engine(URL, isolation_level="AUTOCOMMIT")
    with e.connect() as c:
        c.execute(text("DROP SCHEMA public CASCADE")); c.execute(text("CREATE SCHEMA public")); c.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    e.dispose()


@pytest.fixture(scope="session")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def login(client, role):
    r = client.post("/api/auth/login", data={"email": f"{role}@example.test", "password": f"{role.title()}-test-pass-1"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture(scope="session")
def admin(client): return login(client, "admin")
@pytest.fixture(scope="session")
def officer(client): return login(client, "officer")
@pytest.fixture(scope="session")
def authority(client): return login(client, "authority")


@pytest.fixture(scope="session")
def seeded(client, admin):
    """Wards, sample roads, sample works/tenders/documents and the labelled sample complaints."""
    r = client.post("/api/admin/seed-sample", headers=admin)
    assert r.status_code == 200, r.text
    return r.json()
