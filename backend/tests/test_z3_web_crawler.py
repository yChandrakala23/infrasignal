"""Live web crawler: discovery (HTML, JS bundle, Playwright fallback), table extraction, document download and processing,
change detection between runs, duplicate safety. Network is replaced by httpx.MockTransport; the page structure mirrors what the
real bbmp.gov.in served on 12 Nov 2026 (React shell, links inside main.*.js). Record values are invented."""
import httpx, fitz, pytest

from app.connectors.bbmp_connector import Fetcher
from app.connectors import web_crawler as wc
from app import web_collection as w, models as m
from app.db import SessionLocal

SHELL = b'<!doctype html><html><head><base href="/"/><script defer="defer" src="/static/js/main.abc.js"></script></head><body><div id="root"></div></body></html>'
BUNDLE_V1 = ('var x=[{href:"https://bbmp.gov.in/csr/TENDER_DOCUMENT_TOWNPLANNING.pdf",children:"\\u25c6Global tender for selection of Agency"},'
             '{href:"https://updates.bbmpgov.in/v1/api/file/9209-openhousevenues.pdf",children:"\\ud83e\\uddfe Details of the venues of the e-Khatha Open House"},'
             '{href:"https://evil.example.com/x.pdf",children:"outside"},{to:"/tenders",children:"Tenders"},{href:"/logo.png"}];')
BUNDLE_V2 = BUNDLE_V1 + 'var y={href:"https://bbmp.gov.in/Notification_Files/RoadWorksNotice.pdf",children:"Notification: white topping road works Ward No. 12"};'
TENDERS = b"""<html><body><h1>Tenders</h1><table><thead><tr><th>Tender ID</th><th>Name of Work</th><th>Estimated Cost (Rs)</th><th>Last Date</th></tr></thead>
<tr><td>GBA/2026-27/RD/0001</td><td>Resurfacing of 5th Cross, Ward No. 12</td><td>45,00,000</td><td>20-11-2026</td></tr>
<tr><td>GBA/2026-27/RD/0002</td><td>Storm water drain repair</td><td>12,50,000</td><td>22-11-2026</td></tr></table>
<table><tr><td>layout</td></tr></table></body></html>"""


def make_pdf(text):
    d = fitz.open(); p = d.new_page(); p.insert_text((72, 72), text, fontsize=11); b = d.tobytes(); d.close(); return b


PDF1 = make_pdf("NOTICE INVITING TENDER\nTender ID: GBA/2026-27/TP/0042\nName of work: Consultancy for town planning\nEstimated cost: Rs. 25,00,000")
PDF2 = make_pdf("Details of venues for e-Khatha open house")
PDF3 = make_pdf("Notification regarding white topping works")


class Site:
    def __init__(self):
        self.bundle, self.pdf1, self.calls = BUNDLE_V1, PDF1, []

    def __call__(self, req: httpx.Request):
        u = str(req.url); self.calls.append(u)
        if u.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow:\n", headers={"content-type": "text/plain"})
        if u == "https://bbmp.gov.in/":
            return httpx.Response(200, content=SHELL, headers={"content-type": "text/html"})
        if u.endswith("/static/js/main.abc.js"):
            return httpx.Response(200, text=self.bundle, headers={"content-type": "application/javascript"})
        if u == "https://bbmp.gov.in/tenders":
            return httpx.Response(200, content=TENDERS, headers={"content-type": "text/html"})
        if u.endswith("TENDER_DOCUMENT_TOWNPLANNING.pdf"):
            et = 'W/"' + str(len(self.pdf1)) + '"'
            if req.headers.get("if-none-match") == et:
                return httpx.Response(304)
            return httpx.Response(200, content=self.pdf1, headers={"content-type": "application/pdf", "etag": et})
        if u.endswith("openhousevenues.pdf"):
            return httpx.Response(200, content=PDF2, headers={"content-type": "application/pdf"})
        if u.endswith("RoadWorksNotice.pdf"):
            return httpx.Response(200, content=PDF3, headers={"content-type": "application/pdf"})
        return httpx.Response(404, text="nf")


SRC = {"name": "GBA test", "seeds": ["https://bbmp.gov.in/"], "allowed_hosts": ["bbmp.gov.in"], "document_hosts": ["updates.bbmpgov.in"], "max_depth": 2, "max_pages": 10}


def fetcher(site):
    return Fetcher(httpx.Client(transport=httpx.MockTransport(site)), delay=0, retries=0)


def test_bundle_links_and_labels():
    links = dict(wc.extract_bundle_links(BUNDLE_V1, "https://bbmp.gov.in/"))
    assert links["https://bbmp.gov.in/csr/TENDER_DOCUMENT_TOWNPLANNING.pdf"].startswith("Global tender")
    assert links["https://updates.bbmpgov.in/v1/api/file/9209-openhousevenues.pdf"].startswith("Details of the venues")
    assert "https://bbmp.gov.in/tenders" in links
    assert wc.is_app_shell(SHELL.decode())


def test_tables_need_header():
    t = wc.extract_tables(TENDERS.decode())
    assert len(t) == 1 and t[0]["headers"][0] == "Tender ID" and len(t[0]["rows"]) == 2


def test_crawl_discovers_pages_documents_and_respects_hosts():
    site = Site()
    res = wc.WebCrawler(fetcher(site)).crawl(SRC)
    urls = {d.url for d in res.documents}
    assert "https://bbmp.gov.in/csr/TENDER_DOCUMENT_TOWNPLANNING.pdf" in urls
    assert "https://updates.bbmpgov.in/v1/api/file/9209-openhousevenues.pdf" in urls
    assert not any("evil.example.com" in u for u in urls)
    assert "https://bbmp.gov.in/tenders" in res.page_urls()
    cats = {d.url.rsplit("/", 1)[-1]: d.category for d in res.documents}
    assert cats["TENDER_DOCUMENT_TOWNPLANNING.pdf"] == "tender"
    assert res.pages[0].via == "js_bundle"


def test_playwright_used_only_when_needed():
    site = Site(); site.bundle = "var nothing=1;"
    calls = []

    def render(url):
        calls.append(url)
        return '<html><body><a href="/Notification_Files/X.pdf">Notice X</a></body></html>'
    res = wc.WebCrawler(fetcher(site), use_playwright=True, renderer=render).crawl({**SRC, "max_pages": 1})
    assert calls == ["https://bbmp.gov.in/"] and res.documents[0].via == "playwright"
    calls.clear()
    wc.WebCrawler(fetcher(Site()), use_playwright=True, renderer=render).crawl({**SRC, "max_pages": 1})
    assert calls == []  # the bundle had links: no browser started


def _run(site, **kw):
    db = SessionLocal()
    for r in db.query(m.CollectionRun).filter_by(status="running"):
        r.status = "failed"
    db.commit()
    run = w.start_web_run(db, "test")
    rid = run.id
    db.close()
    w.execute_web_run(rid, fetcher=fetcher(site), config={"crawl_sources": [SRC]}, **kw)
    db = SessionLocal()
    run = db.get(m.CollectionRun, rid)
    return db, run


def test_full_run_then_change_detection(client):
    site = Site()
    db, r1 = _run(site)
    assert r1.status in ("done", "done_with_errors"), r1.log
    assert r1.connector == "web"
    assert r1.files_downloaded == 2 and r1.records_inserted == 2
    items = {i.url.rsplit("/", 1)[-1]: i for i in db.query(m.WebItem).filter_by(source="GBA test")}
    t = items["TENDER_DOCUMENT_TOWNPLANNING.pdf"]
    assert t.status == "processed" and t.document_id and "NOTICE INVITING TENDER" in t.text_excerpt
    doc = db.get(m.Document, t.document_id)
    assert doc.download_url == t.url and doc.source_page_url == "https://bbmp.gov.in/" and doc.downloaded_at
    rec = db.query(m.WebRecord).filter_by(origin="html_table").first()
    assert rec.raw["Tender ID"].startswith("GBA/2026-27/RD/") and rec.normalised["amount"] > 0 and rec.normalised["category"] == "tender"
    db.close()

    # second run, nothing changed: no new downloads, records counted as duplicates, pages unchanged
    db, r2 = _run(site)
    assert r2.files_downloaded == 0 and r2.files_unchanged == 2 and r2.records_inserted == 0 and r2.records_duplicate == 2
    assert db.query(m.WebRecord).filter_by(source="GBA test").count() == 2
    assert any("unchanged 2" in x["message"] for x in r2.log)
    db.close()

    # third run: a new notice is published and the tender PDF is replaced
    site.bundle, site.pdf1 = BUNDLE_V2, make_pdf("NOTICE INVITING TENDER corrigendum\nTender ID: GBA/2026-27/TP/0042")
    db, r3 = _run(site)
    msgs = " ".join(x["message"] for x in r3.log)
    assert "modified 1" in msgs and "New [works] Notification: white topping" in msgs and "Changed: Global tender" in msgs
    t = db.query(m.WebItem).filter(m.WebItem.url.like("%TOWNPLANNING.pdf")).one()
    assert t.versions == 2
    assert db.query(m.WebItem).filter(m.WebItem.url.like("%RoadWorksNotice.pdf")).one().category == "works"
    page = db.query(m.WebPage).filter_by(url="https://bbmp.gov.in/").one()
    assert page.times_changed == 1 and page.stored_path
    db.close()


def test_unreachable_site_reported_not_faked(client):
    def down(req):
        raise httpx.ConnectError("refused")
    db, r = _run(down)
    assert r.status == "failed" and db.query(m.WebRecord).filter(m.WebRecord.run_id == r.id).count() == 0
    assert any("network" in x["message"] for x in r.log)
    db.close()


def test_normalise_row():
    n = w.normalise_row({"Name of Work": "Road at Ward No. 7", "Amount": "Rs. 1.5 lakh"})
    assert n["ward"] == "7" and n["description"].startswith("Road") and n["amount"] > 0


def test_web_api_lists_collected_items(client, officer):
    h = officer
    s = client.get("/api/gov/collection/web/summary", headers=h).json()
    assert s["last_run"] and "documents_by_category" in s and any(x["name"] == "GBA / BBMP official website" for x in s["sources"])
    it = client.get("/api/gov/collection/web/items?category=tender", headers=h).json()
    assert it["total"] >= 1 and it["items"][0]["found_on"]
    one = [i for i in it["items"] if i["stored"]][0]
    f = client.get(f"/api/gov/collection/web/items/{one['id']}/download", headers=h)
    assert f.status_code == 200 and f.content.startswith(b"%PDF")
    assert client.get("/api/gov/collection/web/records", headers=h).json()["total"] >= 2
    assert client.get("/api/gov/collection/web/pages", headers=h).json()["items"]
    assert client.get("/api/gov/collection/web/items").status_code == 401


def test_real_gba_bundle_excerpt():
    """Verbatim excerpt of the live bbmp.gov.in bundle (12 Nov 2026): links stored as config keys, paths with spaces."""
    import pathlib
    js = (pathlib.Path(__file__).parent / "fixtures/web/gba_main_bundle_excerpt_2026-11-12.js").read_text()
    links = dict(wc.extract_bundle_links(js, "https://bbmp.gov.in/"))
    pdfs = {u: l for u, l in links.items() if u.endswith(".pdf")}
    assert len(pdfs) == 15
    assert pdfs["https://bbmp.gov.in/csr/TENDER_DOCUMENT_TOWNPLANNING.pdf"] == "csr: CSR Tender"
    assert "https://bbmp.gov.in/LandUseMaps/Series-1Maps/1.01%20-Petta.pdf" in pdfs
    assert pdfs["https://updates.bbmpgov.in/v1/api/file/920953313893-openhousevenues.pdf"] == "e Khatha Open House"
    assert wc.categorise(pdfs["https://bbmp.gov.in/csr/TENDER_DOCUMENT_TOWNPLANNING.pdf"]) == "tender"
    assert not any(u.endswith((".png", ".mp4")) for u in pdfs)


def test_repeated_rows_in_one_table_and_relisting(client):
    """Found on the real GBA tender PDF: identical rows inside one table must not break the insert; a second crawl that
    still lists every link must not mark any as gone."""
    from app import web_collection as w
    db = SessionLocal()
    run = w.start_web_run(db, "test"); counts = {"processed": 0, "inserted": 0, "duplicate": 0}
    for _ in range(2):
        w._upsert_record(db, run, "t", "pdf_table", "https://bbmp.gov.in/", None, 0, 1, ["a", "b"], {"a": "1", "b": "2"}, "", counts)
    db.commit()
    assert counts["inserted"] == 1 and counts["duplicate"] == 1
    run.status = "done"; db.commit(); db.close()
    site = Site()
    db, _ = _run(site); db.close()
    db, r = _run(site)
    assert any("0 no longer listed" in x["message"] for x in r.log)
    db.close()
