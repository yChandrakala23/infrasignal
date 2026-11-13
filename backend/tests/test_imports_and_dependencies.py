"""Requirement 6: runtime dependencies are declared, .xls and .xlsx import, a missing LLM key never breaks rule-based extraction."""
import ast, io, re, sys
from pathlib import Path
import pytest
from app import models as m
from app.db import SessionLocal
from app.extract import process_document
from app.ingest import read_table
from pdfmaker import make_pdf

ROOT = Path(__file__).resolve().parents[1]
ROWS = [("XLS/J/1", "Asphalting of Xls Test Road", "150", "750000", "M/s Xls Test Co"), ("XLS/J/2", "Resurfacing of Xls Second Road", "151", "320000", "M/s Xls Test Co")]
HEAD = ["Job No", "Work Description", "Ward", "Work Order Amount", "Contractor"]


def make_xls(title_rows=0):
    xlwt = pytest.importorskip("xlwt")
    wb = xlwt.Workbook(); ws = wb.add_sheet("Works")
    for i in range(title_rows):
        ws.write(i, 0, "BBMP road works, printed report" if i == 0 else "")
    for j, h in enumerate(HEAD): ws.write(title_rows, j, h)
    for i, r in enumerate(ROWS):
        for j, v in enumerate(r): ws.write(title_rows + 1 + i, j, v)
    b = io.BytesIO(); wb.save(b); return b.getvalue()


def make_xlsx(title_rows=0):
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active
    for i in range(title_rows): ws.append(["BBMP road works, printed report"])
    ws.append(HEAD)
    for r in ROWS: ws.append(list(r))
    b = io.BytesIO(); wb.save(b); return b.getvalue()


def post(client, officer, data, name, **extra):
    return client.post("/api/gov/imports", headers=officer, files={"file": (name, io.BytesIO(data), "application/octet-stream")},
                       data={"kind": "works", "source_name": "unit test", "ward_regime": "243", "is_sample": "true", **extra})


# ----------------------------------------------------------------------------- dependencies
def declared(path):
    out = set()
    for ln in Path(path).read_text().splitlines():
        ln = ln.split("#")[0].strip()
        if ln and not ln.startswith("-"):
            out.add(re.split(r"[<>=\[ ]", ln)[0].lower().replace("_", "-"))
    return out


def test_every_third_party_import_in_the_app_is_a_declared_runtime_requirement():
    dist = {"fitz": "pymupdf", "pymupdf": "pymupdf", "PIL": "pillow", "sklearn": "scikit-learn", "dotenv": "python-dotenv", "multipart": "python-multipart",
            "psycopg2": "psycopg2-binary", "jose": "python-jose", "bs4": "beautifulsoup4", "yaml": "pyyaml"}
    need = set()
    for f in (ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            names = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else [node.module.split(".")[0]] if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 else []
            for n in names:
                if n not in sys.stdlib_module_names and n != "app":
                    need.add(dist.get(n, n).lower().replace("_", "-"))
    have = declared(ROOT / "requirements.txt")
    optional = {"sentence-transformers"}  # documented optional extra (SEMANTIC_BACKEND=sbert)
    missing = sorted(n for n in need - have - optional)
    assert not missing, f"imported by the app but not in requirements.txt: {missing}"


def test_engines_that_are_only_loaded_through_other_libraries_are_declared():
    have = declared(ROOT / "requirements.txt")
    assert {"httpx", "xlrd", "openpyxl", "pdfplumber", "pymupdf", "pytesseract", "pillow", "scipy", "numpy", "pandas"} <= have
    assert "xlwt" in declared(ROOT / "requirements-dev.txt"), ".xls test fixtures need xlwt in the dev requirements"


def test_all_runtime_libraries_actually_import():
    import httpx, xlrd, openpyxl, pdfplumber, fitz, pytesseract, PIL, scipy, numpy, pandas  # noqa: F401
    assert xlrd.__version__ >= "2"


# ----------------------------------------------------------------------------- .xls / .xlsx
@pytest.mark.parametrize("maker,name", [(make_xls, "works.xls"), (make_xlsx, "works.xlsx")])
def test_xls_and_xlsx_read_to_the_same_table(maker, name):
    df = read_table(maker(), name)
    assert list(df.columns) == HEAD and df.shape == (2, 5)
    assert df.iloc[0].tolist() == list(ROWS[0])


@pytest.mark.parametrize("maker,name", [(make_xls, "titled.xls"), (make_xlsx, "titled.xlsx")])
def test_title_rows_above_the_header_are_skipped(maker, name):
    df = read_table(maker(title_rows=3), name)
    assert list(df.columns) == HEAD and df.shape[0] == 2 and df.attrs["row_offset"] == 3


@pytest.mark.parametrize("maker,name,tag", [(make_xls, "imp.xls", "xls"), (make_xlsx, "imp.xlsx", "xlsx")])
def test_xls_and_xlsx_import_end_to_end_with_row_numbers(client, officer, maker, name, tag):
    rows = [(a.replace("XLS", tag.upper()), b + f" {tag}", c, d, e) for a, b, c, d, e in ROWS]
    # job numbers differ per format so both can be imported into one database; two rows sit above the header
    if tag == "xlsx":
        import openpyxl
        wb = openpyxl.Workbook(); ws = wb.active
        ws.append(["title"]); ws.append([None]); ws.append(HEAD); [ws.append(list(r)) for r in rows]
        b = io.BytesIO(); wb.save(b); data = b.getvalue()
    else:
        xlwt = pytest.importorskip("xlwt"); wb = xlwt.Workbook(); ws = wb.add_sheet("W"); ws.write(0, 0, "title")
        for j, h in enumerate(HEAD): ws.write(2, j, h)
        for i, r in enumerate(rows):
            for j, v in enumerate(r): ws.write(3 + i, j, v)
        b = io.BytesIO(); wb.save(b); data = b.getvalue()
    pre = client.post("/api/gov/imports/preview", headers=officer, files={"file": (name, io.BytesIO(data), "application/octet-stream")}, data={"kind": "works"})
    assert pre.status_code == 200 and pre.json()["mapping"]["job_number"] == "Job No" and pre.json()["rows"] == 2
    r = post(client, officer, data, name)
    assert r.status_code == 200, r.text
    j = r.json(); assert (j["ok"], j["failed"]) == (2, 0)
    db = SessionLocal()
    w = db.query(m.Roadwork).filter_by(job_number=f"{tag.upper()}/J/1").one()
    assert w.awarded_amount == 750000 or w.sanctioned_amount == 750000
    src = db.query(m.SourceRecord).filter_by(import_job_id=j["job_id"]).order_by(m.SourceRecord.row_number).all()
    assert [s.row_number for s in src] == [4, 5], "row numbers count the title rows, so they match the spreadsheet (header on row 3)"


def test_web_page_saved_as_xls_gets_a_plain_message(client, officer):
    html = b"<html><head></head><body><p>Session expired. Please log in.</p></body></html>"
    for url, files in (("/api/gov/imports/preview", {"kind": "works"}), ("/api/gov/imports", {"kind": "works", "source_name": "x"})):
        r = client.post(url, headers=officer, files={"file": ("report.xls", io.BytesIO(html), "application/vnd.ms-excel")}, data=files)
        assert r.status_code == 422 and "web page" in r.json()["detail"] and "xlsx or CSV" in r.json()["detail"], r.text


def test_html_table_saved_as_xls_is_still_readable():
    html = b"<html><body><table><tr><th>Job No</th><th>Work Description</th></tr><tr><td>H/1</td><td>Road widening</td></tr></table></body></html>"
    try:
        df = read_table(html, "portal.xls")
    except ValueError as e:
        pytest.skip(f"no HTML table parser available: {e}")
    assert df.iloc[0].tolist() == ["H/1", "Road widening"]


def test_semicolon_and_tab_separated_text_files():
    assert read_table("Job No;Work Description\nA;Road work\n".encode(), "a.csv").iloc[0].tolist() == ["A", "Road work"]
    assert read_table("Job No\tWork Description\nA\tRoad work\n".encode(), "a.csv").iloc[0].tolist() == ["A", "Road work"]
    assert read_table("Job No,Work Description\nA,Rōad wörk\n".encode("cp1252", errors="replace"), "a.csv").shape == (1, 2)


def test_empty_or_headerless_files_are_refused_clearly():
    for bad in (b"", b"Job No,Work Description\n"):
        with pytest.raises(ValueError):
            read_table(bad, "x.csv")


def test_missing_xlrd_is_explained_not_a_crash(monkeypatch):
    monkeypatch.setitem(sys.modules, "xlrd", None)  # import xlrd -> ImportError
    with pytest.raises(ValueError, match="xlrd"):
        read_table(make_xls(), "works.xls")


# ----------------------------------------------------------------------------- no LLM key / failing LLM
def sample_pdf(tmp_path):
    return str(make_pdf(tmp_path / "wo.pdf", "WORK ORDER", ["Work Order No: LLM/WO/1", "BBMP Job No: LLM/J/1", "Name of Work: Asphalting of Llm Test Road",
                                                           "Contractor: M/s Llm Test Co", "Work Order Amount: Rs. 4,50,000", "Work Order Date: 12/03/2026"]))


def fields(r):
    return sorted((f["field"], f["norm_value"], f["page"]) for f in r["fields"])


def test_missing_key_gives_exactly_the_rule_based_result(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = sample_pdf(tmp_path)
    base = process_document(p)
    with_llm = process_document(p, use_llm=True)
    assert with_llm["llm_used"] is False and fields(with_llm) == fields(base)
    assert any(f["field"] == "job_number" and f["norm_value"] == "LLM/J/1" for f in base["fields"])


def test_network_failure_and_garbage_replies_leave_rule_based_values_untouched(tmp_path):
    p = sample_pdf(tmp_path); base = process_document(p)

    class Down:
        def complete(self, prompt): raise ConnectionError("network unreachable")
    class Garbage:
        def complete(self, prompt): return "Sorry, I cannot help with that."
    class WrongShape:
        def complete(self, prompt): return '["not", "an", "object"]'
    for prov in (Down(), Garbage(), WrongShape()):
        r = process_document(p, use_llm=True, llm=prov)
        assert r["llm_used"] is False and fields(r) == fields(base), type(prov).__name__


def test_llm_answers_that_are_not_on_the_page_are_dropped(tmp_path):
    from app.llm_extract import MockProvider
    p = sample_pdf(tmp_path); base = process_document(p)
    r = process_document(p, use_llm=True, llm=MockProvider({"job_number": "INVENTED/J/9", "contractor": "Nobody Ltd", "estimated_cost": "999999"}))
    assert r["llm_used"] is True
    assert not [f for f in r["fields"] if f["norm_value"] in ("INVENTED/J/9", "Nobody Ltd")] and fields(r) == fields(base)


def test_unavailable_provider_object_is_skipped_without_error(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from app.llm_extract import AnthropicProvider
    assert AnthropicProvider().available is False


def test_upload_with_use_llm_but_no_key_still_extracts(client, officer, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = Path(sample_pdf(tmp_path)); p.write_bytes(p.read_bytes() + b"\n% no-key variant")
    r = client.post("/api/gov/documents", headers=officer, data={"use_llm": "true"}, files=[("files", ("nokey.pdf", p.read_bytes(), "application/pdf"))])
    assert r.status_code == 200 and r.json()["llm_used"] is False
    res = r.json()["results"][0]
    assert res["doc_type"] == "work_order" and "error" not in res
