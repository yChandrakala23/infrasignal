"""One live crawl run: visit -> discover -> extract -> download -> process -> store, with change detection between runs.

Per source in data/bbmp_sources.json -> "crawl_sources":
  * every page visited gets a web_pages row; its "content hash" (links + tables found) is compared with the previous run, so a page is
    reported as new, modified or unchanged. A copy of the HTML as fetched is kept on disk.
  * every link found gets a web_items row keyed by URL (never duplicated). New links are logged as new; links that a page stopped listing
    are marked still_listed = false (the row is kept).
  * documents are downloaded with conditional requests (ETag / Last-Modified) and compared by SHA-256: an unchanged file is not stored or
    processed again; a changed file is stored as a new version.
  * PDFs go through the existing document pipeline (classification, field extraction, OCR, matching) with source page, download URL and
    download time recorded as provenance; their tables and the HTML tables become web_records rows (original cells kept, deduplicated by hash).
  * CSV / Excel files are read into web_records rows the same way.
Nothing is invented: a value that cannot be read stays empty.
"""
import hashlib, json, os, re, tempfile
from sqlalchemy.orm import Session

from . import models as m
from .collection import start_run, RunLog, _finish, now, download_dir, RunInProgress  # noqa: F401  (RunInProgress re-exported)
from .connectors.bbmp_connector import Fetcher, FetchError, load_sources, detect_format, looks_restricted
from .connectors.web_crawler import WebCrawler, categorise
from .db import SessionLocal
from .textnorm import parse_amount, parse_date

FETCHER_FACTORY = Fetcher
MAX_DOCS_DEFAULT = int(os.getenv("WEB_MAX_DOCUMENTS", "40"))


def web_dir(*parts):
    d = os.path.join(download_dir(), "web", *parts)
    os.makedirs(d, exist_ok=True)
    return d


def _safe(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s or "x")[:100]


# ----------------------------------------------------------------------------- normalising a published row
_AMT = re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*(lakh|lakhs|crore|crores|cr)?", re.I)
_WARD = re.compile(r"\bward\s*(?:no\.?|number)?\s*[:\-]?\s*(\d{1,3})\b", re.I)
_TENDER = re.compile(r"\b((?:GBA|BBMP|KPPP|[A-Z]{2,6})/[A-Z0-9/_\-]{4,}/\d{2,})\b|\btender\s*(?:id|no\.?)\s*[:\-]?\s*([A-Z0-9/_\-]{5,})", re.I)
_DATE = re.compile(r"\b(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{4}-\d{2}-\d{2})\b")


def normalise_row(raw: dict, context_label: str = "") -> dict:
    """Values read from a published row. Header names decide the field where they are clear; otherwise patterns in the text."""
    out = {}
    text = " | ".join(f"{k}: {v}" for k, v in raw.items() if not k.startswith("_"))
    for k, v in raw.items():
        if k.startswith("_") or not str(v).strip():
            continue
        h = k.lower()
        if any(w in h for w in ("amount", "cost", "value", "rs", "₹", "estimate", "sanction")) and "amount" not in out:
            a = parse_amount(str(v))
            if a:
                out["amount"], out["amount_field"] = a, k
        if "date" in h:
            d = parse_date(str(v))
            if d:
                out.setdefault("dates", {})[k] = d.date().isoformat()
        if "ward" in h:
            out.setdefault("ward", str(v).strip()[:40])
        if any(w in h for w in ("tender", "nit", "bid")) and any(w in h for w in ("id", "no", "number", "ref")):
            out.setdefault("tender_id", str(v).strip()[:120])
        if any(w in h for w in ("work order", "wo no", "work code", "job")):
            out.setdefault("work_order_no", str(v).strip()[:120])
        if any(w in h for w in ("contractor", "agency", "firm", "bidder")):
            out.setdefault("contractor", str(v).strip()[:200])
        if any(w in h for w in ("name of work", "description", "subject", "title", "work name", "particulars")):
            out.setdefault("description", str(v).strip()[:500])
    if "amount" not in out:
        mm = _AMT.search(text)
        if mm:
            a = parse_amount(mm.group(1))
            if a and mm.group(2):
                a *= 1e5 if mm.group(2).lower().startswith("lakh") else 1e7
            if a:
                out["amount"] = a
    if "ward" not in out and (mm := _WARD.search(text)):
        out["ward"] = mm.group(1)
    if "tender_id" not in out and (mm := _TENDER.search(text)):
        out["tender_id"] = (mm.group(1) or mm.group(2))[:120]
    if "dates" not in out:
        ds = [parse_date(x) for x in _DATE.findall(text)[:3]]
        if any(ds):
            out["dates"] = {f"date{i + 1}": d.date().isoformat() for i, d in enumerate(ds) if d}
    out["category"] = categorise(context_label + " " + text)
    return out


def _row_hash(source, where, headers, raw):
    body = json.dumps({"s": source, "w": where, "h": headers, "r": {k: v for k, v in raw.items() if k != "_links"}}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def _upsert_record(db, run, source, origin, page_url, item_id, t_index, row_no, headers, raw, label, counts):
    h = _row_hash(source, page_url if item_id is None else f"item:{item_id}", headers, raw)
    counts["processed"] += 1
    batch = counts.setdefault("_hashes", set())
    if h in batch:  # the same row twice in one table / run (real GBA PDFs repeat rows): keep one
        counts["duplicate"] += 1
        return None, False
    batch.add(h)
    ex = db.query(m.WebRecord).filter_by(row_hash=h).first()
    if ex:
        ex.last_seen_at, ex.run_id = now(), run.id
        counts["duplicate"] += 1
        return ex, False
    norm = normalise_row(raw, label)
    r = m.WebRecord(source=source, origin=origin, page_url=page_url[:1000], item_id=item_id, table_index=t_index, row_number=row_no, raw=raw,
                    normalised=norm, category=norm.get("category"), row_hash=h, last_seen_at=now(), run_id=run.id)
    db.add(r)
    counts["inserted"] += 1
    return r, True


# ----------------------------------------------------------------------------- documents
def pdf_text_and_tables(path, max_pages=40):
    text, tables = "", []
    try:
        import fitz
        with fitz.open(path) as d:
            text = "\n".join(p.get_text() for p in list(d)[:max_pages])
    except Exception:
        pass
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            for pi, page in enumerate(pdf.pages[:max_pages]):
                for ti, tb in enumerate(page.extract_tables() or []):
                    rows = [[(c or "").replace("\n", " ").strip() for c in r] for r in tb if r and any((c or "").strip() for c in r)]
                    if len(rows) < 2 or len(rows[0]) < 2:
                        continue
                    hdr = [c or f"col{j + 1}" for j, c in enumerate(rows[0])]
                    recs = [{hdr[j] if j < len(hdr) else f"col{j + 1}": v for j, v in enumerate(r)} for r in rows[1:]]
                    tables.append({"index": pi * 100 + ti, "page": pi + 1, "headers": hdr, "rows": recs})
    except Exception:
        pass
    return text, tables


def _process_document(db, run, log, item, content, path, source, counts):
    fmt = item.file_ext
    if fmt == "pdf":
        text, tables = pdf_text_and_tables(path)
        item.text_excerpt = re.sub(r"\s+", " ", text)[:2000] or None
        try:
            from .pipeline import ingest_document
            doc, created = ingest_document(db, path, os.path.basename(path).split("_", 1)[-1], source_name=source[:100], match=True,
                                           source_page_url=item.found_on, download_url=item.url, downloaded_at=item.downloaded_at)
            item.document_id = doc.id
            if not text.strip() and doc.ocr_pages:
                log.add("info", f"{item.label[:80]}: scanned PDF, read with OCR ({doc.ocr_pages} page(s))")
        except Exception as e:
            log.add("warning", f"{item.label[:80]}: stored, but document pipeline failed ({type(e).__name__}: {str(e)[:150]})")
        n = 0
        for t in tables:
            for i, raw in enumerate(t["rows"]):
                _upsert_record(db, run, source, "pdf_table", item.found_on, item.id, t["index"], i + 1, t["headers"], raw, item.label, counts)
                n += 1
        item.records_extracted = n
    elif fmt in ("csv", "xlsx", "xls"):
        from .ingest import read_table
        try:
            df = read_table(content, os.path.basename(path))
        except Exception as e:
            raise ValueError(f"spreadsheet could not be read: {e}")
        hdr = [str(c) for c in df.columns]
        n = 0
        for i, row in enumerate(df.fillna("").astype(str).to_dict("records")):
            _upsert_record(db, run, source, "sheet", item.found_on, item.id, 0, i + 1, hdr, row, item.label, counts)
            n += 1
        item.records_extracted = n
    item.status = "processed"


def _download_item(db, run, log, fetcher, item: m.WebItem, source, counts, force=False):
    try:
        dl = fetcher.download(item.url, None if force else item.http_etag, None if force else item.http_last_modified)
    except FetchError as e:
        item.status, item.error = "download_failed", f"{e.kind}: {e}"[:2000]
        run.files_failed = (run.files_failed or 0) + 1
        log.add("error", f"Download failed ({e.kind}) {item.url}: {e}")
        return
    if dl.status == "not_modified" and item.sha256:
        run.files_unchanged = (run.files_unchanged or 0) + 1
        return
    sha = hashlib.sha256(dl.content).hexdigest()
    if sha == item.sha256 and item.status == "processed" and not force:
        run.files_unchanged = (run.files_unchanged or 0) + 1
        item.http_etag, item.http_last_modified = dl.etag, dl.last_modified
        return
    det = detect_format(dl.content, item.url)
    if det == "html":
        restricted = looks_restricted(dl.content[:20000].decode("utf-8", "replace"))
        item.status = "download_failed"
        item.error = "The link returned a web page, not a file" + (" (login or CAPTCHA page: not bypassed)" if restricted else "")
        run.files_failed = (run.files_failed or 0) + 1
        log.add("warning", f"{item.label[:80]}: {item.error}")
        return
    if item.file_ext == "pdf" and not dl.content.startswith(b"%PDF"):
        item.status, item.error = "download_failed", "the response is not a PDF"
        run.files_failed = (run.files_failed or 0) + 1
        return
    changed = item.sha256 is not None and item.sha256 != sha
    fname = _safe(os.path.basename(item.url.split("?")[0]))
    path = os.path.join(web_dir(_safe(source)), f"{sha[:12]}_{fname}")
    with open(path, "wb") as f:
        f.write(dl.content)
    item.sha256, item.size_bytes, item.content_type = sha, len(dl.content), (dl.content_type or "")[:120]
    item.http_etag, item.http_last_modified, item.stored_path = dl.etag, dl.last_modified, path
    item.downloaded_at, item.versions, item.status, item.error = now(), (item.versions or 0) + 1, "downloaded", None
    run.files_downloaded = (run.files_downloaded or 0) + 1
    log.add("info", f"{'Changed' if changed else 'Downloaded'}: {item.label[:90] or fname} ({len(dl.content):,} bytes)")
    db.flush()
    if item.file_ext not in ("pdf", "csv", "xlsx", "xls"):
        item.status = "skipped"
        item.error = f"stored; {item.file_ext} files are not parsed"
        return
    try:
        _process_document(db, run, log, item, dl.content, path, source, counts)
    except Exception as e:
        db.rollback()
        item = db.get(m.WebItem, item.id)
        item.status, item.error = "download_failed", f"processing failed: {type(e).__name__}: {str(e)[:300]}"
        run.files_failed = (run.files_failed or 0) + 1
        log.add("error", f"{item.label[:80]}: {item.error}")


# ----------------------------------------------------------------------------- one source
def _store_page(db, run, log, source, pr, counts):
    p = db.query(m.WebPage).filter_by(url=pr.url[:1000]).first()
    is_new = p is None
    if is_new:
        p = m.WebPage(source=source, url=pr.url[:1000], first_seen_at=now())
        db.add(p)
    p.final_url, p.status, p.error, p.via, p.last_run_id = pr.final_url[:1000], pr.status, pr.error or None, pr.via, run.id
    p.last_fetched_at = now()
    if pr.status == "skipped":
        return p, "skipped"
    if pr.status == "restricted":  # a login / CAPTCHA page is expected on government portals: noted, never bypassed, not an error
        log.add("info", f"Page {pr.url}: restricted ({pr.error})")
        return p, "restricted"
    if pr.status != "ok":
        log.add("warning", f"Page {pr.url}: {pr.status} ({pr.error})")
        return p, "failed"
    p.title, p.raw_sha256, p.links_found, p.tables_found = pr.title[:300], pr.sha256, len(pr.links), len(pr.tables)
    state = "unchanged"
    if is_new or not p.content_sha256:  # first successful read (an earlier visit may have failed)
        state, p.last_changed_at = "new", now()
    elif p.content_sha256 != pr.content_sha256:
        state, p.last_changed_at, p.times_changed = "modified", now(), (p.times_changed or 0) + 1
    p.content_sha256 = pr.content_sha256
    if state != "unchanged" and pr.html:
        path = os.path.join(web_dir(_safe(source), "pages"), f"{hashlib.sha1(pr.url.encode()).hexdigest()[:12]}_{pr.content_sha256[:10]}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(pr.html)
        p.stored_path = path
    for t in pr.tables:
        for i, raw in enumerate(t["rows"]):
            _upsert_record(db, run, source, "html_table", pr.final_url, None, t["index"], i + 1, t["headers"], raw, pr.title + " " + t.get("caption", ""), counts)
    return p, state


def crawl_source(db: Session, run, log, fetcher, src: dict, crawler: WebCrawler, download=True, force=False, max_docs=None):
    name = src.get("name") or src["seeds"][0]
    counts = {"processed": 0, "inserted": 0, "duplicate": 0}
    log.add("info", f"Crawling {name}: {', '.join(src['seeds'])}")
    res = crawler.crawl(src)
    states = {"new": 0, "modified": 0, "unchanged": 0, "failed": 0, "skipped": 0, "restricted": 0}
    crawled_ok = []
    for pr in res.pages:
        _, st = _store_page(db, run, log, name, pr, counts)
        states[st] += 1
        if pr.status == "ok":
            crawled_ok.append(pr.final_url)
    run.datasets_found = (run.datasets_found or 0) + states["new"] + states["modified"] + states["unchanged"]
    db.commit()

    # items: every link found (documents and pages)
    new_items, seen_urls = [], set()
    for pr in res.pages:
        for ln in pr.links:
            if ln.url in seen_urls:
                continue
            seen_urls.add(ln.url)
            it = db.query(m.WebItem).filter_by(url=ln.url[:1000]).first()
            if it is None:
                it = m.WebItem(source=name, url=ln.url[:1000], first_seen_at=now(), first_run_id=run.id, status="found" if ln.kind == "document" else "listed")
                db.add(it)
                if ln.kind == "document":
                    new_items.append(ln)
            if ln.label and len(ln.label) >= len(it.label or ""):
                it.label = ln.label
            it.category, it.kind, it.file_ext, it.found_on, it.discovered_via = ln.category, ln.kind, ln.file_ext, ln.found_on[:1000], ln.via
            it.last_seen_at, it.last_run_id, it.still_listed = now(), run.id, True
    db.flush()  # the "no longer listed" query below must see this run's updates
    # links that pages crawled now no longer list
    gone = db.query(m.WebItem).filter(m.WebItem.source == name, m.WebItem.found_on.in_(crawled_ok), m.WebItem.last_run_id != run.id,
                                      m.WebItem.still_listed.is_(True)).all()
    for it in gone:
        it.still_listed = False
    db.commit()
    run.files_found = (run.files_found or 0) + len(res.documents)
    log.add("info", f"{name}: {len(res.pages)} page(s) visited (new {states['new']}, modified {states['modified']}, unchanged {states['unchanged']}, "
                    f"failed {states['failed']}, app routes {states['skipped']}, login-only {states['restricted']}); {len(res.documents)} document link(s), {len(new_items)} new; {len(gone)} no longer listed")
    for ln in new_items[:25]:
        log.add("info", f"New [{ln.category}] {ln.label[:110] or ln.url}")

    if download:
        cats = src.get("download_categories")  # None = all
        limit = max_docs if max_docs is not None else int(src.get("max_documents", MAX_DOCS_DEFAULT))
        todo = db.query(m.WebItem).filter(m.WebItem.source == name, m.WebItem.kind == "document", m.WebItem.last_run_id == run.id).order_by(m.WebItem.id).all()
        if cats:
            todo = [t for t in todo if t.category in cats]
        skip = [x.lower() for x in src.get("skip_download_patterns", [])]
        if skip:
            todo = [t for t in todo if not any(x in t.url.lower() for x in skip)]
        todo.sort(key=lambda t: (t.status == "processed", t.id))  # never-downloaded first
        for it in todo[:limit]:
            _download_item(db, run, log, fetcher, it, name, counts, force)
            db.commit()
        if len(todo) > limit:
            log.add("info", f"{name}: {len(todo) - limit} more document(s) left for the next run (limit {limit} per run)")
    run.records_processed = (run.records_processed or 0) + counts["processed"]
    run.records_inserted = (run.records_inserted or 0) + counts["inserted"]
    run.records_duplicate = (run.records_duplicate or 0) + counts["duplicate"]
    db.commit()
    return res


def execute_web_run(run_id: int, fetcher=None, config=None, only=None, download=True, force=False, max_docs=None, renderer=None, use_playwright=None,
                    session_factory=None) -> int:
    db = (session_factory or SessionLocal)()
    try:
        run = db.get(m.CollectionRun, run_id)
        log = RunLog(run)
        try:
            cfg = config or load_sources()
            f = fetcher or FETCHER_FACTORY()
            up = (os.getenv("BBMP_USE_PLAYWRIGHT", "0").lower() in ("1", "true", "yes")) if use_playwright is None else use_playwright
            crawler = WebCrawler(f, use_playwright=up, renderer=renderer)
            srcs = [s for s in cfg.get("crawl_sources", []) if s.get("enabled", True) and (not only or s.get("name") in only)]
            if not srcs:
                log.add("warning", "No crawl sources are enabled (crawl_sources in the sources file).")
            for s in srcs:
                try:
                    crawl_source(db, run, log, f, s, crawler, download, force, max_docs)
                except Exception as e:
                    db.rollback()
                    run = db.get(m.CollectionRun, run_id)
                    log.run = run
                    run.files_failed = (run.files_failed or 0) + 1
                    log.add("error", f"{s.get('name')}: crawl stopped: {type(e).__name__}: {str(e)[:300]}")
                    db.commit()
        except Exception as e:
            db.rollback()
            run = db.get(m.CollectionRun, run_id)
            log.run = run
            run.status = "failed"
            log.add("error", f"Run stopped: {type(e).__name__}: {str(e)[:300]}")
        if run.status == "running" and not (run.datasets_found or 0):
            run.status = "failed"
            log.add("error", "No page could be read from any source.")
        _finish(db, run, log)
        return run_id
    finally:
        db.close()


def start_web_run(db, trigger="manual", user_id=None, params=None):
    run = start_run(db, trigger, user_id, params)
    run.connector = "web"
    db.commit()
    return run
