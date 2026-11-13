"""Live web crawler for BBMP / GBA and other configured government sites.

What it does on each run, starting from the seed URLs of every enabled source in data/bbmp_sources.json ("crawl_sources"):

  1. Visits the page (httpx, same politeness rules as the dataset connector: robots.txt, delay, retries, host allow-list).
  2. Discovers links three ways, in this order:
       a. static HTML anchors (<a href>, <iframe src>, <embed src>);
       b. links compiled into the page's own JavaScript bundles. The GBA site (bbmp.gov.in) is a React app whose static HTML
          has no links at all; its notices, tenders and PDFs are string literals inside /static/js/main.*.js. Reading the bundle
          needs no browser and gets the same links the rendered page shows;
       c. Playwright rendering, only when (a) and (b) found nothing and BBMP_USE_PLAYWRIGHT=1 (or the source says render: always).
  3. Keeps links on allowed hosts. Pages are followed up to max_depth / max_pages; documents (PDF, CSV, XLS/XLSX, DOC/DOCX)
     are listed with the link text, the page they were found on and a category guessed from the words (tender, work order,
     payment, notice ...).
  4. Extracts every HTML <table> with a header row into records (one dict per row, keyed by the published header).

Nothing here touches the database: app/web_collection.py stores pages, items, tables and documents and decides what is new.
"""
import hashlib, json, re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urldefrag, unquote

from bs4 import BeautifulSoup

from .bbmp_connector import Fetcher, FetchError, render_with_playwright, looks_restricted, _host_ok

DOC_EXT = {"pdf": "pdf", "csv": "csv", "xlsx": "xlsx", "xls": "xls", "xlsm": "xlsx", "doc": "doc", "docx": "docx", "zip": "zip"}
GENERIC_LABELS = {"", "download", "preview", "explore", "view", "open", "more information", "go to resource", "click here", "here", "csv", "xlsx", "xls", "pdf"}
SKIP_EXT = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "css", "js", "mp4", "mp3", "woff", "woff2", "ttf", "apk"}

CATEGORIES = [  # first match wins; checked against link text + URL, lower-case
    ("tender", ("tender", "nit ", "nit-", "bid ", "e-procurement", "eoi", "expression of interest", "rfp", "request for proposal", "quotation")),
    ("work_order", ("work order", "workorder", "work-order", "letter of acceptance", "loa", "agreement")),
    ("payment", ("payment", "bill", "pending bills", "paid", "release of funds", "lop")),
    ("budget", ("budget", "estimate", "action plan", "grant", "allocation")),
    ("works", ("road", "white topping", "pothole", "storm water", "drain", "footpath", "flyover", "infrastructure", "works", "project")),
    ("circular", ("circular", "order no", "government order", "g.o", "office order")),
    ("notice", ("notification", "notice", "invitation", "applications", "announcement", "press release", "list")),
]


def categorise(text: str) -> str:
    t = " " + (text or "").lower().replace("_", " ") + " "
    for cat, words in CATEGORIES:
        if any(w in t for w in words):
            return cat
    return "other"


def _ext(url):
    m = re.search(r"\.([A-Za-z0-9]{2,5})$", unquote(urlparse(url).path))
    return m.group(1).lower() if m else None


def _clean_label(s):
    s = re.sub(r"\s+", " ", s or "").strip()
    s = re.sub(r"^[^\w(]+", "", s)  # leading emoji / bullets used on the GBA site
    return s[:400]


@dataclass
class Link:
    url: str
    label: str
    found_on: str
    kind: str            # page | document
    file_ext: str | None
    category: str
    via: str             # html | js_bundle | playwright


@dataclass
class PageResult:
    url: str
    final_url: str = ""
    status: str = "ok"   # ok, failed, restricted, skipped
    error: str = ""
    title: str = ""
    sha256: str = ""
    content_sha256: str = ""   # hash of the discovered links + tables (stable when only scripts / timestamps change)
    via: str = "html"
    depth: int = 0
    links: list = field(default_factory=list)
    tables: list = field(default_factory=list)   # [{"index", "caption", "headers", "rows": [dict]}]
    html: str = ""


@dataclass
class CrawlResult:
    source: str
    pages: list = field(default_factory=list)
    documents: list = field(default_factory=list)    # unique Link objects with kind == document

    def page_urls(self):
        return [p.url for p in self.pages]


# ----------------------------------------------------------------------------- parsing
def extract_html_links(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tag, attr in (("a", "href"), ("iframe", "src"), ("embed", "src"), ("object", "data")):
        for el in soup.find_all(tag):
            raw = (el.get(attr) or "").strip()
            if not raw or raw.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            url = urldefrag(urljoin(base_url, raw))[0]
            label = el.get("title") or el.get_text(" ", strip=True) or el.get("aria-label") or ""
            out.append((url, _clean_label(label)))
    return out


_STR = r'"((?:[^"\\\n]|\\.){1,600})"'
_URLISH = re.compile(r"""["'`]((?:https?://|/)[^"'`<>{}\n]{2,400}?\.(?:pdf|csv|xlsx|xls|xlsm|docx?|zip))["'`]""", re.I)  # real GBA paths contain spaces
_ABS_URL = re.compile(r'''["'`](https?://[^"'`\s<>{}]{4,400})["'`]''')
_ROUTE = re.compile(r'''(?:to|path|href)\s*:\s*["'](/[a-z0-9][a-z0-9/_-]{1,80})["']''', re.I)
_CHILD = re.compile(r'children\s*:\s*' + _STR)


def _js_unescape(s):
    try:
        return json.loads('"' + s + '"')
    except Exception:
        return s


_KEY_BEFORE = re.compile(r'([A-Za-z_$][\w$]*)\s*:\s*(?:""\.concat\([^)]*?,\s*)?$')
_GROUP = re.compile(r'([A-Za-z_$][\w$]*)\s*:\s*\{[^{}]*$')
_NEXT_URL = re.compile(r"[\"'`](?:https?://|/)[^\"'`]{3,}")


def _humanise(key):
    k = re.sub(r"([a-z])([A-Z])", r"\1 \2", key.replace("_", " "))
    k = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", k)
    return re.sub(r"\s+", " ", k).strip()


def extract_bundle_links(js: str, base_url: str):
    """Links compiled into a JavaScript bundle (React / Vue build).

    Label, in order of preference:
      * JSX text: {href:"...", children:"text"} -> "text" (only when it follows the URL, before any other URL);
      * the configuration key and its group: csr:{CSRTender:"/csr/TENDER.pdf"} -> "Csr: CSR Tender" (how the GBA bundle stores links);
      * nothing (the file name is still in the URL)."""
    out, seen = [], set()

    def label_for(start, end):
        after = js[end:end + 300]
        nxt = _NEXT_URL.search(after)
        c = _CHILD.search(after[:nxt.start()] if nxt else after)
        if c:
            return _clean_label(_js_unescape(c.group(1)))
        k = _KEY_BEFORE.search(js[max(0, start - 120):start])
        if k:
            g = _GROUP.search(js[max(0, start - 3000):start])
            key = _humanise(k.group(1))
            grp = _humanise(g.group(1)) if g and g.group(1) != k.group(1) else ""
            return _clean_label(f"{grp}: {key}" if grp and grp.lower() != key.lower() else key)
        return ""

    for rx in (_URLISH, _ABS_URL):
        for m_ in rx.finditer(js):
            raw = _js_unescape(m_.group(1))
            url = urldefrag(urljoin(base_url, raw.replace(" ", "%20")))[0]
            if url in seen or len(url) > 900:
                continue
            seen.add(url)
            out.append((url, label_for(m_.start(), m_.end())))
    for m_ in _ROUTE.finditer(js):
        url = urljoin(base_url, m_.group(1))
        if url not in seen:
            seen.add(url)
            out.append((url, label_for(m_.start(1) - 1, m_.end(1) + 1)))
    return out



def bundle_urls(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    return [urljoin(base_url, s["src"]) for s in soup.find_all("script", src=True)
            if urlparse(urljoin(base_url, s["src"])).hostname == urlparse(base_url).hostname]


def is_app_shell(html: str) -> bool:
    """A single-page-app shell: almost no anchors, a mount point and a script bundle."""
    soup = BeautifulSoup(html, "html.parser")
    anchors = len(soup.find_all("a", href=True))
    mount = soup.find(id=re.compile(r"^(root|app|__next|__nuxt)$"))
    return anchors < 3 and (mount is not None or bool(soup.find_all("script", src=True)))


def extract_tables(html: str, max_rows=2000):
    """Every <table> that has a header row -> {"headers": [...], "rows": [{header: cell}]}. Layout tables (no header, one column) are skipped."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for i, t in enumerate(soup.find_all("table")):
        rows = t.find_all("tr")
        if len(rows) < 2:
            continue
        head_cells = rows[0].find_all(["th", "td"])
        has_th = bool(rows[0].find_all("th")) or t.find("thead") is not None
        headers = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) or f"col{j + 1}" for j, c in enumerate(head_cells)]
        if len(headers) < 2 or not has_th:
            continue
        recs = []
        for r in rows[1:max_rows + 1]:
            cells = r.find_all(["td", "th"])
            if not cells:
                continue
            vals = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in cells]
            if not any(vals):
                continue
            rec = {headers[j] if j < len(headers) else f"col{j + 1}": v for j, v in enumerate(vals)}
            links = [a["href"] for a in r.find_all("a", href=True)]
            if links:
                rec["_links"] = links
            recs.append(rec)
        if recs:
            cap = t.find("caption")
            out.append({"index": i, "caption": cap.get_text(" ", strip=True) if cap else "", "headers": headers, "rows": recs})
    return out


def _title(html):
    soup = BeautifulSoup(html, "html.parser")
    h = soup.find("h1") or soup.title
    return re.sub(r"\s+", " ", h.get_text(" ", strip=True))[:300] if h else ""


# ----------------------------------------------------------------------------- crawler
class WebCrawler:
    def __init__(self, fetcher: Fetcher, *, use_playwright=False, renderer=None):
        self.fetcher = fetcher
        self.use_playwright = use_playwright
        self.renderer = renderer or render_with_playwright

    def _fetch_page(self, url):
        r = self.fetcher._request(url)
        ct = r.headers.get("content-type", "")
        return r, ct

    def crawl(self, source: dict) -> CrawlResult:
        name = source.get("name") or source["seeds"][0]
        hosts = [h.lower() for h in source.get("allowed_hosts") or [urlparse(source["seeds"][0]).hostname]]
        doc_hosts = hosts + [h.lower() for h in source.get("document_hosts", [])]
        max_depth, max_pages = int(source.get("max_depth", 2)), int(source.get("max_pages", 40))
        render = source.get("render", "auto")
        include = [k.lower() for k in source.get("follow_keywords", [])]
        exclude = [k.lower() for k in source.get("exclude_patterns", [])]
        self.fetcher.allowed = sorted(set(doc_hosts))

        # pages are followed on the listed hosts themselves; their sub-domains (hrms., accounts., sahaaya2. ... on bbmp.gov.in are separate
        # login-only applications) only when the source says follow_subdomains. Documents may come from any listed host or sub-domain.
        follow_sub = bool(source.get("follow_subdomains"))
        page_hosts = None if follow_sub else set(hosts)

        res = CrawlResult(source=name)
        seen_pages, docs, spa_routes = set(), {}, set()
        queue = [(u, 0, "") for u in source["seeds"]]
        while queue and len(res.pages) < max_pages:
            url, depth, _ = queue.pop(0)
            url = urldefrag(url)[0]
            if url in seen_pages:
                continue
            seen_pages.add(url)
            pr = PageResult(url=url, depth=depth)
            res.pages.append(pr)
            try:
                r, ct = self._fetch_page(url)
            except FetchError as e:
                pr.status = "restricted" if e.kind in ("blocked", "robots") else "failed"
                pr.error = f"{e.kind}: {e}"
                if url in spa_routes and e.status == 404:
                    # a route of the React app (e.g. /about-gba): the server has no page there, the app draws it in the browser.
                    # Its links were already read from the bundle, so this is not a failure.
                    pr.status, pr.error = "skipped", "client-side route of the web app (no server page; its links come from the JavaScript bundle)"
                continue
            if "html" not in ct and ct:
                pr.status, pr.error = "skipped", f"not a web page ({ct})"
                continue
            html, pr.final_url = r.text, str(r.url)
            pr.sha256 = hashlib.sha256(r.content).hexdigest()
            if looks_restricted(html[:30000]) and len(html) < 30000:
                pr.status, pr.error = "restricted", "login or CAPTCHA page: not bypassed"
                continue
            pr.title = _title(html)
            found = [(u, l, "html") for u, l in extract_html_links(html, pr.final_url)]
            shell = is_app_shell(html)
            if shell or render == "bundle":
                for b in bundle_urls(html, pr.final_url)[:6]:
                    try:
                        br = self.fetcher._request(b)
                        found += [(u, l, "js_bundle") for u, l in extract_bundle_links(br.text, pr.final_url)]
                        pr.via = "js_bundle"
                    except FetchError as e:
                        pr.error = f"bundle {b}: {e.kind}"
            if render == "always" or (shell and self.use_playwright and not found and render != "never"):
                try:
                    html = self.renderer(pr.final_url)
                    found += [(u, l, "playwright") for u, l in extract_html_links(html, pr.final_url)]
                    pr.via = "playwright"
                except Exception as e:
                    pr.error = (pr.error + "; " if pr.error else "") + f"render failed: {type(e).__name__}: {str(e)[:150]}"
            pr.html = html
            pr.tables = extract_tables(html)

            uniq = {}
            for u, label, via in found:
                if not u.startswith(("http://", "https://")):
                    continue
                ext = _ext(u)
                if ext in SKIP_EXT:
                    continue
                prev = uniq.get(u)
                if prev is None or (len(label) > len(prev[0])):
                    uniq[u] = (label, via)
            for u, (label, via) in uniq.items():
                ext = _ext(u)
                if label.strip().lower() in GENERIC_LABELS:  # CKAN "Download" buttons: say what the file is instead
                    fname = unquote(urlparse(u).path.rstrip("/").rsplit("/", 1)[-1])
                    label = _clean_label(f"{pr.title}: {fname}" if pr.title and fname else (fname or pr.title or label))
                is_doc = ext in DOC_EXT
                host_list = doc_hosts if is_doc else hosts
                if not _host_ok(u, host_list):
                    continue
                if any(x in u.lower() for x in exclude):
                    continue
                cat = categorise(label + " " + unquote(urlparse(u).path))
                link = Link(url=u, label=label, found_on=pr.final_url, kind="document" if is_doc else "page", file_ext=DOC_EXT.get(ext) if is_doc else None,
                            category=cat, via=via)
                pr.links.append(link)
                if is_doc:
                    if u not in docs or (len(label) > len(docs[u].label)):
                        docs[u] = link
                elif depth < max_depth and u not in seen_pages:
                    if page_hosts is not None and (urlparse(u).hostname or "").lower() not in page_hosts:
                        continue  # listed as a link, not crawled
                    text = (label + " " + u).lower()
                    if not include or any(k in text for k in include) or depth == 0:
                        if via == "js_bundle":
                            spa_routes.add(u)
                        queue.append((u, depth + 1, url))
            sig = json.dumps({"links": sorted(f"{l.url}|{l.label}" for l in pr.links),
                              "tables": [[t["headers"], t["rows"]] for t in pr.tables]}, sort_keys=True, default=str)
            pr.content_sha256 = hashlib.sha256(sig.encode()).hexdigest()
        res.documents = list(docs.values())
        return res
