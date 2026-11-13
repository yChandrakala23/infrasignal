"""Requirement 8: a database that already holds data (revision 0002, and the original prototype) upgrades to the latest schema without
losing or changing a record; the new tables are filled from what was there; foreign keys and indexes exist; downgrade and re-upgrade work.
Runs on SQLite always, and also on PostgreSQL when TEST_DATABASE_URL points at one (a separate scratch database is created for it)."""
import os, subprocess, sys, tempfile
from datetime import datetime
from pathlib import Path
import pytest
from sqlalchemy import MetaData, create_engine, inspect, text

BACKEND = Path(__file__).resolve().parents[1]
PG = (os.getenv("TEST_DATABASE_URL") or "").startswith("postgresql")
NOW = datetime(2026, 1, 15, 10, 0, 0)


def scratch_url(kind):
    if kind == "sqlite":
        return "sqlite:///" + tempfile.mktemp(suffix=".db")
    base = os.environ["TEST_DATABASE_URL"]
    name = "infrasignal_migtest"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name}")); c.execute(text(f"CREATE DATABASE {name}"))
    admin.dispose()
    url = base.rsplit("/", 1)[0] + "/" + name
    e = create_engine(url, isolation_level="AUTOCOMMIT")
    with e.connect() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    e.dispose()
    return url


def alembic(url, *args):
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True)


def seed_legacy(url):
    """Rows exactly as revision 0002 could hold them: a work with two source rows and a tender row, documents, bills, payments."""
    e = create_engine(url)
    md = MetaData(); md.reflect(bind=e)
    T = md.tables
    with e.begin() as c:
        def ins(t, *rows):  # one statement per row: the rows have different columns
            for r in rows:
                c.execute(T[t].insert(), [r])
        ins("contractors", dict(id=1, name="Legacy Builders", name_norm="legacy builders", is_demo=False))
        ins("import_jobs", dict(id=1, filename="legacy_works.csv", kind="works", source_name="Legacy works", status="done", rows_total=2, rows_ok=2, created_at=NOW, is_demo=False),
            dict(id=2, filename="legacy_payments.csv", kind="works", source_name="Legacy payments", status="done", rows_total=1, rows_ok=1, created_at=NOW, is_demo=False),
            dict(id=3, filename="legacy_tenders.csv", kind="tenders", source_name="Legacy tenders", status="done", rows_total=1, rows_ok=1, created_at=NOW, is_demo=False))
        ins("source_records", dict(id=1, import_job_id=1, row_number=2, raw={"Job No": "LEG/J/1"}, row_hash="h1"),
            dict(id=2, import_job_id=1, row_number=3, raw={"Job No": "LEG/J/2"}, row_hash="h2"),
            dict(id=3, import_job_id=2, row_number=2, raw={"Job No": "LEG/J/1", "Paid Amount": "200000"}, row_hash="h3"),
            dict(id=4, import_job_id=3, row_number=2, raw={"Tender ID": "LEG/T/1"}, row_hash="h4"))
        ins("roadworks", dict(id=1, job_number="LEG/J/1", tender_id="LEG/T/1", description="Asphalting of Legacy Road", description_norm="asphalting of legacy road", ward_regime="243",
                              ward_number="12", contractor_id=1, sanctioned_amount=500000.0, awarded_amount=500000.0, work_order_date=NOW, source_record_id=1, is_demo=False,
                              created_at=NOW, data_origin="import", fiscal_year="2025-26", department="Ward Engineering"),
            dict(id=2, job_number="LEG/J/2", description="Resurfacing of Second Legacy Road", description_norm="resurfacing of second legacy road", ward_regime="243", ward_number="13",
                 sanctioned_amount=120000.0, source_record_id=2, is_demo=False, created_at=NOW, data_origin="import"),
            dict(id=3, description="Pothole filling near Legacy Junction", description_norm="pothole filling near legacy junction", is_demo=False, created_at=NOW))
        ins("documents", dict(id=1, roadwork_id=1, filename="legacy_wo.pdf", path="data/uploads/documents/legacy_wo.pdf", doc_type="work_order", pages=2, sha256="a" * 64,
                              created_at=NOW, match_status="auto", is_demo=False, status="processed"),
            dict(id=2, filename="legacy_unmatched.pdf", path="data/uploads/documents/legacy_unmatched.pdf", doc_type="other", pages=1, sha256="b" * 64, created_at=NOW,
                 match_status="unmatched", is_demo=False, status="needs_review"))
        ins("bills", dict(id=1, roadwork_id=1, bill_number="B-1", amount=250000.0, bill_date=NOW, source_record_id=3))
        ins("payments", dict(id=1, roadwork_id=1, bill_id=1, amount=200000.0, paid_on=NOW, source_record_id=3),
            dict(id=2, roadwork_id=1, amount=50000.0, paid_on=NOW, reference="document:1", document_id=1))
        ins("tenders", dict(id=1, tender_id="LEG/T/1", title="Legacy tender", status="awarded", source_record_id=4, source_name="Legacy tenders", is_demo=False, created_at=NOW))
        ins("record_links", dict(id=1, a_type="roadwork", a_id=1, b_type="tender", b_id=1, method="exact_tender_id", confidence=0.99, status="auto", created_at=NOW),
            dict(id=2, a_type="roadwork", a_id=2, b_type="tender", b_id=1, method="fuzzy", confidence=0.4, status="rejected", created_at=NOW))
    e.dispose()


def snapshot(url):
    e = create_engine(url); out = {}
    with e.connect() as c:
        for t, cols in (("roadworks", "id, job_number, description, awarded_amount, contractor_id, source_record_id, ward_number, department"),
                        ("documents", "id, roadwork_id, filename, sha256, match_status"), ("payments", "id, roadwork_id, amount, bill_id, source_record_id, document_id"),
                        ("bills", "id, roadwork_id, bill_number, amount"), ("source_records", "id, import_job_id, row_number, row_hash"),
                        ("tenders", "id, tender_id, source_record_id"), ("contractors", "id, name"), ("record_links", "id, a_id, b_id, status")):
            out[t] = [tuple(r) for r in c.execute(text(f"SELECT {cols} FROM {t} ORDER BY id"))]
    e.dispose()
    return out


KINDS = ["sqlite"] + (["postgres"] if PG else [])


@pytest.mark.parametrize("kind", KINDS)
def test_populated_0002_database_upgrades_keeping_every_record_and_backfills_sources(kind):
    url = scratch_url(kind)
    assert alembic(url, "upgrade", "0002").returncode == 0
    seed_legacy(url)
    before = snapshot(url)
    r = alembic(url, "upgrade", "head"); assert r.returncode == 0, r.stderr[-1500:]
    assert snapshot(url) == before, "no existing row was changed, moved or removed"

    e = create_engine(url); ins = inspect(e)
    with e.connect() as c:
        q = lambda sql: [tuple(r) for r in c.execute(text(sql))]
        assert q("SELECT id, verification_status FROM roadworks ORDER BY id") == [(1, "official"), (2, "official"), (3, "official")], "every existing work stays official"
        assert q("SELECT id, original_filename FROM documents ORDER BY id") == [(1, "legacy_wo.pdf"), (2, "legacy_unmatched.pdf")]
        assert q("SELECT count(*) FROM payments WHERE duplicate_of_id IS NOT NULL") == [(0,)], "nothing is retro-actively treated as a repeat"
        links = q("SELECT roadwork_id, source_record_id, source_type, role, status FROM work_source_links ORDER BY roadwork_id, source_record_id")
        assert (1, 1, "works_row", "legacy_primary", "auto") in links and (2, 2, "works_row", "legacy_primary", "auto") in links, "each work's original row is linked"
        assert (1, 3, "payment_row", "merged_row", "auto") in links or any(l[:2] == (1, 3) for l in links), "the payment/bill row that fed work 1 is linked"
        assert any(l[:2] == (1, 4) and l[2] == "tender_row" for l in links), "the tender row behind a live tender link is linked"
        assert not any(l[:2] == (2, 4) for l in links), "a rejected tender link is not turned into a source link"
        assert q("SELECT count(*) FROM work_source_links WHERE roadwork_id = 3") == [(0,)], "a work with no source row gets no invented link"
        assert q("SELECT count(*) FROM boq_items") == [(0,)] and q("SELECT count(*) FROM document_pages") == [(0,)]
    # foreign keys and indexes
    fks = {(t, tuple(f["constrained_columns"]), f["referred_table"]) for t in ("work_source_links", "boq_items", "document_pages", "payments", "roadworks") for f in ins.get_foreign_keys(t)}
    for want in (("work_source_links", ("roadwork_id",), "roadworks"), ("work_source_links", ("source_record_id",), "source_records"), ("boq_items", ("document_id",), "documents"),
                 ("document_pages", ("document_id",), "documents"), ("payments", ("duplicate_of_id",), "payments"), ("roadworks", ("merged_into_id",), "roadworks"),
                 ("roadworks", ("created_from_document_id",), "documents")):
        assert want in fks, f"missing foreign key {want}"
    idx = {t: {tuple(i["column_names"]) for i in ins.get_indexes(t)} for t in ("work_source_links", "boq_items", "document_pages", "roadworks", "payments")}
    assert ("roadwork_id",) in idx["work_source_links"] and ("source_record_id",) in idx["work_source_links"] and ("document_id", "page") in idx["boq_items"]
    assert ("verification_status",) in idx["roadworks"] and ("provisional_key",) in idx["roadworks"] and ("duplicate_of_id",) in idx["payments"]
    uniq = {tuple(u["column_names"]) for u in ins.get_unique_constraints("work_source_links")} | {tuple(i["column_names"]) for i in ins.get_indexes("work_source_links") if i.get("unique")}
    assert ("roadwork_id", "source_record_id") in uniq
    e.dispose()
    # the new schema equals the models (no drift), and the old data survives a downgrade and a second upgrade
    r = alembic(url, "check"); assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
    r = alembic(url, "downgrade", "0002"); assert r.returncode == 0, r.stderr[-1500:]
    assert snapshot(url) == before, "downgrade keeps the original data"
    r = alembic(url, "upgrade", "head"); assert r.returncode == 0, r.stderr[-1500:]
    e = create_engine(url)
    with e.connect() as c:
        assert c.execute(text("SELECT count(*) FROM work_source_links")).scalar() >= 4, "the backfill runs again after a downgrade"
    e.dispose()


@pytest.mark.parametrize("kind", ["sqlite"])
def test_original_prototype_database_without_alembic_version_is_adopted_by_init_db(kind):
    """The very first version created tables with create_all and had no alembic_version table. init_db stamps it and upgrades."""
    url = scratch_url(kind)
    assert alembic(url, "upgrade", "0001").returncode == 0
    e = create_engine(url)
    with e.begin() as c:
        md = MetaData(); md.reflect(bind=e)
        users = md.tables["users"]
        c.execute(users.insert(), [dict(id=1, email="old@example.org", password_hash="x", role="officer")])
        c.execute(text("DROP TABLE alembic_version"))
    before = [tuple(r) for r in e.connect().execute(text("SELECT id, email FROM users"))]
    e.dispose()
    run = subprocess.run([sys.executable, "-W", "ignore", "-c", "from app.db import init_db; init_db()"], cwd=BACKEND,
                         env={**os.environ, "DATABASE_URL": url, "SECRET_KEY": "migration-test-secret-key"}, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr[-1500:]
    e = create_engine(url)
    with e.connect() as c:
        assert [tuple(r) for r in c.execute(text("SELECT id, email FROM users"))] == before
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0005"
        assert c.execute(text("SELECT count(*) FROM work_source_links")).scalar() == 0
    e.dispose()
    assert alembic(url, "check").returncode == 0


def test_init_db_twice_is_harmless():
    url = scratch_url("sqlite")
    cmd = [sys.executable, "-W", "ignore", "-c", "from app.db import init_db; init_db(); init_db()"]
    r = subprocess.run(cmd, cwd=BACKEND, env={**os.environ, "DATABASE_URL": url, "SECRET_KEY": "migration-test-secret-key"}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-1500:]
