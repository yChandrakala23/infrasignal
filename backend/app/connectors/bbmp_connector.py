"""BBMP open-data connector: finds the datasets' downloadable files and fetches them. It never stores records.

Sources: the OpenCity dataset pages for BBMP work orders and payments (CKAN pages) and, as a scan for linked data files, the official
BBMP website. Download URLs are not assumed permanent: they are read from the dataset page on every run.

Behaviour you can rely on
  * Static HTML is read with BeautifulSoup. Playwright is used only when a page has no links in its static HTML and BBMP_USE_PLAYWRIGHT=1.
  * Retries with back-off on timeouts, connection errors, HTTP 429 and 5xx; a pause between requests; robots.txt is honoured.
  * 401 / 403, a login page or a CAPTCHA is reported as "access restricted" and is never worked around. Use the manual file / URL import instead.
  * A file whose ETag / Last-Modified / checksum has not changed is not downloaded or imported again.
  * Hosts are limited to BBMP_ALLOWED_HOSTS (default: data.opencity.in, site.bbmp.gov.in); private addresses are always refused.
"""
import hashlib, ipaddress, json, os, re, time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, unquote, quote_plus
from urllib import robotparser

import httpx
from bs4 import BeautifulSoup

from ..bbmp_schema import fiscal_label, regime_from_text

USER_AGENT = os.getenv("BBMP_USER_AGENT", "InfraSignal-research-collector/1.0 (public open-data; contact: see project README)")
DATA_EXT = {"csv": "csv", "xlsx": "xlsx", "xls": "xls", "xlsm": "xlsx"}
UNSUPPORTED_EXT = ("zip", "pdf", "json", "xml", "geojson", "kml", "kmz", "shp", "doc", "docx")
_RES = re.compile(r"/resource/([0-9a-fA-F][0-9a-fA-F-]{7,})")
_GENERIC = {"preview", "download", "explore", "go to resource", "more information", "view", "open", "csv", "xlsx", "xls", "more"}


class FetchError(Exception):
    """kind: timeout, network, http, blocked, robots, host, too_large, not_data"""
    def __init__(self, kind, message, status=None):
        super().__init__(message)
        self.kind, self.status = kind, status


# ----------------------------------------------------------------------------- data classes
@dataclass
class ResourceInfo:
    resource_id: str
    name: str
    download_url: str
    preview_url: str | None = None
    description: str = ""
    file_format: str | None = None       # csv / xlsx / xls / other extension seen
    filename: str | None = None
    ward_regime: str | None = None


@dataclass
class DatasetInfo:
    slug: str
    url: str
    title: str = ""
    description: str = ""
    fiscal_year: str | None = None
    meta: dict = field(default_factory=dict)
    resources: list = field(default_factory=list)
    pages: list = field(default_factory=list)
    discovered_via: str = "html"


@dataclass
class Download:
    status: str                   # ok, not_modified
    content: bytes = b""
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    final_url: str | None = None


# ----------------------------------------------------------------------------- HTTP
def _host_ok(url, allowed):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    h = p.hostname.lower()
    if h in ("localhost",) or h.endswith(".local") or h.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(h)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    except ValueError:
        pass
    return allowed is None or any(h == a or h.endswith("." + a) for a in allowed)


def allowed_hosts_from_env():
    raw = os.getenv("BBMP_ALLOWED_HOSTS", "data.opencity.in,site.bbmp.gov.in")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


MAX_CRAWL_DELAY = float(os.getenv("BBMP_MAX_CRAWL_DELAY", "10"))


def ssl_verify():
    """Certificate verification that works for the Indian government sites.

    bbmp.gov.in and accounts.bbmp.gov.in do not send their intermediate certificate. Browsers fetch it themselves, Python's own
    certificate bundle cannot, so every request failed with CERTIFICATE_VERIFY_FAILED. The operating system's trust store (Windows
    CryptoAPI, macOS Keychain) completes the chain the way a browser does; `truststore` lets httpx use it. Verification stays ON."""
    if os.getenv("BBMP_SSL_SYSTEM_STORE", "1").lower() in ("0", "false", "no"):
        return True
    try:
        import ssl, truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # not installed / unsupported platform: httpx's default (certifi)
        return True


def make_client(timeout=30.0) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(timeout, connect=min(15.0, timeout)), follow_redirects=True, verify=ssl_verify(),
                        headers={"User-Agent": USER_AGENT, "Accept": "*/*"})


class Fetcher:
    """httpx wrapper: timeouts, retries, politeness delay, size cap, host checks, robots.txt, conditional requests.

    A host that cannot be connected to at all (connection refused / timed out / TLS failure) is remembered for the rest of the run,
    so one site that is down does not cost minutes of retries for every one of its links."""

    def __init__(self, client: httpx.Client | None = None, *, timeout=30.0, retries=3, backoff=1.5, delay=1.0, max_bytes=150 * 1024 * 1024,
                 allowed_hosts="env", respect_robots=True, sleep=time.sleep, clock=time.monotonic):
        self.client = client or make_client(timeout)
        self.retries, self.backoff, self.delay, self.max_bytes = retries, backoff, delay, max_bytes
        self.allowed = allowed_hosts_from_env() if allowed_hosts == "env" else allowed_hosts
        self.respect_robots = respect_robots
        self._sleep, self._clock = sleep, clock
        self._last = None
        self._robots = {}
        self._crawl_delay = {}   # site root -> seconds asked for by robots.txt (Crawl-delay)
        self._dead = {}          # host -> FetchError: unreachable during this run
        self.requests = 0
        self.log: list[str] = []

    # -- politeness
    def _pause(self, url=None):
        delay = self.delay
        if url and self.delay:
            p = urlparse(url)
            delay = max(delay, self._crawl_delay.get(f"{p.scheme}://{p.netloc}", 0))
        if self._last is not None and delay:
            wait = delay - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _unreachable(self, url, err):
        host = (urlparse(url).hostname or "").lower()
        e = FetchError(err.kind, f"{err} - {host} did not answer; its other links are skipped in this run", err.status)
        self._dead[host] = e
        return e

    def _robots_ok(self, url):
        if not self.respect_robots:
            return True
        p = urlparse(url)
        root = f"{p.scheme}://{p.netloc}"
        rp = self._robots.get(root)
        if rp is None:
            rp = robotparser.RobotFileParser()
            try:
                self._pause()
                r = self.client.get(root + "/robots.txt", headers={"User-Agent": USER_AGENT})
                if r.status_code == 200 and "text" in r.headers.get("content-type", "text/plain"):
                    rp.parse(r.text.splitlines())
                    try:
                        cd = rp.crawl_delay(USER_AGENT)
                        if cd:
                            self._crawl_delay[root] = min(float(cd), MAX_CRAWL_DELAY)
                    except (TypeError, ValueError):
                        pass
                else:
                    rp = False  # no usable robots.txt: nothing forbidden
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # the site itself is unreachable: say so now instead of retrying the real request four times
                kind = "timeout" if isinstance(e, httpx.TimeoutException) else "network"
                raise self._unreachable(url, FetchError(kind, f"could not connect ({type(e).__name__}: {str(e)[:120]})"))
            except httpx.HTTPError:
                rp = False
            self._robots[root] = rp
        return True if rp is False else rp.can_fetch(USER_AGENT, url)

    # -- requests
    def _request(self, url, headers=None, stream_limit=None, strict_host=True):
        if not _host_ok(url, self.allowed if strict_host else None):
            raise FetchError("host", f"{urlparse(url).hostname or url} is not an allowed host (BBMP_ALLOWED_HOSTS) or is a private address")
        dead = self._dead.get((urlparse(url).hostname or "").lower())
        if dead is not None:
            raise FetchError(dead.kind, str(dead), dead.status)
        if not self._robots_ok(url):
            raise FetchError("robots", "robots.txt does not allow this URL for automated access")
        last_err, connect_failures = None, 0
        for attempt in range(self.retries + 1):
            self._pause(url)
            self.requests += 1
            try:
                r = self.client.get(url, headers=headers or {})
            except httpx.TimeoutException as e:
                last_err = FetchError("timeout", f"timed out ({type(e).__name__})")
                connect_failures += isinstance(e, httpx.ConnectTimeout)
            except httpx.HTTPError as e:
                last_err = FetchError("network", f"network error ({type(e).__name__}: {str(e)[:120]})")
                connect_failures += isinstance(e, httpx.ConnectError)
            else:
                if r.status_code in (401, 403, 407):
                    raise FetchError("blocked", f"HTTP {r.status_code}: access is restricted. Authentication and CAPTCHA are not bypassed; "
                                                f"download the file yourself and use the manual import.", r.status_code)
                if r.status_code == 429 or r.status_code >= 500:
                    last_err = FetchError("http", f"HTTP {r.status_code}", r.status_code)
                    ra = r.headers.get("retry-after", "")
                    if ra.isdigit():
                        self._sleep(min(int(ra), 60))
                elif r.status_code == 304:
                    return r
                elif r.status_code >= 400:
                    raise FetchError("http", f"HTTP {r.status_code}", r.status_code)
                else:
                    if not _host_ok(str(r.url), None):
                        raise FetchError("host", "redirected to a private address")
                    if stream_limit and len(r.content) > stream_limit:
                        raise FetchError("too_large", f"file is larger than the {stream_limit // (1024 * 1024)} MB limit")
                    return r
            if connect_failures >= 2:  # twice no connection at all: the host is down, more retries only waste time
                break
            if attempt < self.retries:
                self._sleep(self.backoff ** attempt)
        if connect_failures and connect_failures == attempt + 1:
            raise self._unreachable(url, last_err)
        raise last_err

    def get_text(self, url):
        r = self._request(url)
        ct = r.headers.get("content-type", "")
        if "html" not in ct and "text" not in ct and "xml" not in ct and ct:
            raise FetchError("not_data", f"expected a web page but got {ct}")
        return r.text, str(r.url)

    def download(self, url, etag=None, last_modified=None, strict_host=True) -> Download:
        h = {}
        if etag:
            h["If-None-Match"] = etag
        if last_modified:
            h["If-Modified-Since"] = last_modified
        r = self._request(url, h, stream_limit=self.max_bytes, strict_host=strict_host)
        if r.status_code == 304:
            return Download("not_modified", etag=etag, last_modified=last_modified, final_url=str(r.url))
        return Download("ok", r.content, r.headers.get("etag"), r.headers.get("last-modified"), r.headers.get("content-type"), str(r.url))


# ----------------------------------------------------------------------------- file type
def detect_format(content: bytes, filename: str = "", content_type: str = ""):
    """csv / xlsx / xls from the bytes themselves; 'html' for a web page (e.g. a login or CAPTCHA page); None if unreadable."""
    if content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "xls"
    if content.startswith(b"PK\x03\x04"):
        return "xlsx" if b"xl/" in content[:4096] or filename.lower().endswith((".xlsx", ".xlsm")) else "zip"
    head = content[:2000].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html", b"<head", b"<?xml")) or b"<html" in head[:300]:
        return "html"
    if not content.strip():
        return None
    try:
        sample = content[:20000].decode("utf-8-sig")
    except UnicodeDecodeError:
        sample = content[:20000].decode("cp1252", errors="replace")
    if "\x00" in sample:
        return None
    return "csv" if ("," in sample or "\t" in sample or ";" in sample or "\n" in sample) else None


def looks_restricted(html_text: str):
    t = html_text.lower()
    return any(k in t for k in ("captcha", "recaptcha", "hcaptcha", "access denied", "please log in", "sign in to continue", "login required"))


# ----------------------------------------------------------------------------- page parsing
def _text(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


def _ext(url):
    path = urlparse(url).path
    m = re.search(r"\.([A-Za-z0-9]{2,5})$", path)
    return m.group(1).lower() if m else None


def _filename(url):
    seg = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    return seg or None


def slug_of(url):
    m = re.search(r"/dataset/([^/?#]+)", urlparse(url).path)
    return m.group(1) if m else None


def parse_dataset_page(html: str, page_url: str) -> tuple[DatasetInfo, list[str]]:
    """-> (dataset info with the resources found on this page, URLs of further pages of the same list)."""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    title = _text(h1) or (soup.find("meta", property="og:title") or {}).get("content", "") or _text(soup.title)
    desc_el = soup.select_one(".notes, .dataset-notes, [itemprop=description]")
    desc = _text(desc_el) or (soup.find("meta", attrs={"name": "description"}) or {}).get("content", "") \
        or (soup.find("meta", property="og:description") or {}).get("content", "")
    meta = {}
    for row in soup.select("table tr"):
        th, td = row.find("th"), row.find("td")
        if th and td:
            k = _text(th).rstrip(":")
            if k and len(k) < 40:
                meta[k] = _text(td)[:300]
    lic = soup.select_one("#dataset-license, .module-license, section.license, [class*=license]")
    if lic and _text(lic):
        meta.setdefault("License", re.sub(r"^License\s*", "", _text(lic))[:200])
    slug = slug_of(page_url) or ""
    info = DatasetInfo(slug=slug, url=page_url, title=title, description=desc[:1000], meta=meta,
                       fiscal_year=fiscal_label(slug) or fiscal_label(title))

    by_id: dict[str, dict] = {}
    order = []
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"].strip())
        if href.startswith("#") or not href.startswith(("http://", "https://")):
            continue
        low = urlparse(href).path.lower()
        rid = (_RES.search(low) or [None, None])[1]
        is_download = "/download/" in low or _ext(href) in DATA_EXT or _ext(href) in UNSUPPORTED_EXT
        if not rid and not is_download:
            continue
        key = rid or "url-" + hashlib.sha1(href.encode()).hexdigest()[:12]
        r = by_id.get(key)
        if r is None:
            r = by_id[key] = {"resource_id": key, "name": "", "download_url": None, "preview_url": None, "description": "", "format": None}
            order.append(key)
        txt = a.get("title") or _text(a)
        if "/download/" in low or (is_download and not rid):
            r["download_url"] = r["download_url"] or href
        elif rid:
            r["preview_url"] = r["preview_url"] or href
        if txt and txt.strip().lower() not in _GENERIC and len(txt) > len(r["name"]) and "download" not in txt.lower()[:9]:
            r["name"] = txt.strip()[:300]
        li = a.find_parent("li") or a.find_parent("div", class_=re.compile("resource"))
        if li is not None:
            d = li.select_one("p.description, .description, .resource-description")
            if d and not r["description"]:
                r["description"] = _text(d)[:500]
            fl = li.select_one("[data-format], .format-label")
            if fl and not r["format"]:
                r["format"] = (fl.get("data-format") or _text(fl)).lower().strip()
    for key in order:
        r = by_id[key]
        if not r["download_url"]:
            continue  # a resource page with no direct file (API-only or external link)
        ext = _ext(r["download_url"])
        fmt = DATA_EXT.get(ext) or (r["format"] if r["format"] in DATA_EXT else None) or ext or r["format"]
        name = r["name"] or _filename(r["download_url"]) or key
        info.resources.append(ResourceInfo(resource_id=key, name=name, download_url=r["download_url"], preview_url=r["preview_url"],
                                           description=r["description"], file_format=fmt, filename=_filename(r["download_url"]),
                                           ward_regime=regime_from_text(name) or regime_from_text(r["description"])))
    nxt = []
    for a in soup.select("a[rel=next], .pagination li.next a, .pagination a[aria-label*=Next], .pagination a.next"):
        u = urljoin(page_url, a.get("href", ""))
        if u and u != page_url and u not in nxt:
            nxt.append(u)
    return info, nxt


# ----------------------------------------------------------------------------- configuration
def load_sources(path: str | None = None) -> dict:
    p = path or os.getenv("BBMP_SOURCES_FILE") or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "bbmp_sources.json")
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("datasets", [])
    cfg.setdefault("site_pages", [])
    return cfg


# ----------------------------------------------------------------------------- connector
def render_with_playwright(url: str, timeout_ms=30000) -> str:  # pragma: no cover - needs a browser
    """Last resort for a page whose links are added by JavaScript. Does not log in, solve CAPTCHAs or click through consent walls."""
    import importlib
    try:  # optional: not in requirements.txt because it needs a downloaded browser (pip install playwright && playwright install chromium)
        sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
    except ImportError as e:
        raise FetchError("network", "Playwright is not installed, so a page that needs JavaScript cannot be read. Install it or use the manual import.") from e
    with sync_playwright() as p:
        b = p.chromium.launch()
        try:
            page = b.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            return page.content()
        finally:
            b.close()


class BbmpConnector:
    def __init__(self, fetcher: Fetcher | None = None, *, use_playwright: bool | None = None, renderer=None, max_pages=10):
        self.fetcher = fetcher or Fetcher()
        self.use_playwright = (os.getenv("BBMP_USE_PLAYWRIGHT", "0").lower() in ("1", "true", "yes")) if use_playwright is None else use_playwright
        self.renderer = renderer or render_with_playwright
        self.max_pages = max_pages

    def discover_dataset(self, url: str, fiscal_year: str | None = None) -> DatasetInfo:
        """List every downloadable file of a dataset. Uses the portal's CKAN API when it answers (exact resource ids, formats and
        modification times); otherwise reads the dataset web page. BBMP_USE_CKAN_API=0 forces the web page."""
        if os.getenv("BBMP_USE_CKAN_API", "1").lower() in ("1", "true", "yes") and slug_of(url):
            try:
                return self.discover_dataset_ckan(url, fiscal_year)
            except FetchError as e:
                # robots.txt on data.opencity.in disallows /api/ (Nov 2026) but allows /dataset/: the web page is the permitted route
                if e.kind in ("blocked", "host"):
                    raise
                self.fetcher.log.append(f"CKAN API not usable for {url} ({e.kind}: {e}); reading the web page instead")
        return self.discover_dataset_html(url, fiscal_year)

    def discover_dataset_ckan(self, url: str, fiscal_year: str | None = None) -> DatasetInfo:
        p = urlparse(url)
        slug = slug_of(url)
        api = f"{p.scheme}://{p.netloc}/api/3/action/package_show?id={slug}"
        r = self.fetcher._request(api)
        try:
            data = r.json()
        except ValueError:
            raise FetchError("not_data", "the CKAN API did not return JSON")
        if not data.get("success") or not isinstance(data.get("result"), dict):
            raise FetchError("not_data", f"the CKAN API reported an error: {str(data.get('error'))[:200]}")
        pkg = data["result"]
        org = (pkg.get("organization") or {}).get("title")
        meta = {k: v for k, v in {"Organisation": org, "License": pkg.get("license_title"), "Author": pkg.get("author"),
                                  "Created": pkg.get("metadata_created"), "Last Updated": pkg.get("metadata_modified"),
                                  "CKAN package id": pkg.get("id"), "Source": pkg.get("url")}.items() if v}
        info = DatasetInfo(slug=slug, url=url, title=pkg.get("title") or slug, description=(pkg.get("notes") or "")[:1000], meta=meta,
                           fiscal_year=fiscal_label(slug) or fiscal_label(pkg.get("title")) or (fiscal_label(fiscal_year) if fiscal_year else None),
                           discovered_via="ckan_api")
        for res in pkg.get("resources") or []:
            dl = (res.get("url") or "").strip()
            if not dl.startswith(("http://", "https://")):
                continue
            fmt = (res.get("format") or "").strip().lower() or _ext(dl)
            fmt = DATA_EXT.get(fmt, fmt)
            name = res.get("name") or _filename(dl) or res.get("id")
            info.resources.append(ResourceInfo(resource_id=res.get("id") or "url-" + hashlib.sha1(dl.encode()).hexdigest()[:12], name=name[:300],
                                               download_url=dl, preview_url=f"{p.scheme}://{p.netloc}/dataset/{slug}/resource/{res.get('id')}",
                                               description=(res.get("description") or "")[:500], file_format=fmt, filename=_filename(dl),
                                               ward_regime=regime_from_text(name) or regime_from_text(res.get("description"))))
        info.pages.append(api)
        if not info.resources:
            raise FetchError("not_data", "the dataset lists no downloadable files")
        return info

    def search_datasets(self, site: str, query: str, rows: int = 100) -> list[dict]:
        """CKAN package_search: datasets on the portal matching `query` (used to report BBMP datasets that are not configured yet).
        Falls back to the portal's search page when the API is not allowed (robots.txt) or not available."""
        try:
            r = self.fetcher._request(f"{site.rstrip('/')}/api/3/action/package_search?q={quote_plus(query)}&rows={rows}")
        except FetchError as e:
            if e.kind in ("blocked", "host"):
                raise
            return self.search_datasets_html(site, query)
        try:
            res = r.json().get("result") or {}
        except ValueError:
            raise FetchError("not_data", "the CKAN API did not return JSON")
        return [{"slug": d.get("name"), "title": d.get("title"), "modified": d.get("metadata_modified"),
                 "resources": len(d.get("resources") or [])} for d in res.get("results") or []]

    def search_datasets_html(self, site: str, query: str, max_pages: int = 5) -> list[dict]:
        """The same search through the CKAN web page /dataset/?q=... (allowed by robots.txt)."""
        out, seen = [], set()
        url = f"{site.rstrip('/')}/dataset/?q={quote_plus(query)}"
        for _ in range(max_pages):
            html, final = self.fetcher.get_text(url)
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.select(".dataset-heading a[href], .dataset-item h2 a[href], .dataset-item h3 a[href]"):
                slug = slug_of(urljoin(final, a["href"]))
                if slug and slug not in seen:
                    seen.add(slug)
                    item = a.find_parent(class_=re.compile("dataset-item")) or a.parent
                    out.append({"slug": slug, "title": _text(a), "modified": None,
                                "resources": len(item.select(".dataset-resources li, [data-format]")) if item else None})
            nxt = soup.select_one(".pagination li.next a, .pagination a[rel=next], .pagination a[aria-label*=Next]")
            if not nxt or not nxt.get("href"):
                break
            url = urljoin(final, nxt["href"])
        return out

    def discover_dataset_html(self, url: str, fiscal_year: str | None = None) -> DatasetInfo:
        """Open a dataset page (and any further pages of its resource list) and list every downloadable file with its metadata."""
        seen, queue, info = set(), [url], None
        while queue and len(seen) < self.max_pages:
            u = queue.pop(0)
            if u in seen:
                continue
            seen.add(u)
            html, final = self.fetcher.get_text(u)
            page_info, more = parse_dataset_page(html, final)
            via = "html"
            if not page_info.resources and self.use_playwright and not looks_restricted(html):
                try:
                    page_info, more = parse_dataset_page(self.renderer(final), final)
                    via = "playwright"
                except Exception as e:
                    raise FetchError("network", f"page has no static links and rendering it failed ({type(e).__name__})")
            if info is None:
                info = page_info
                info.url = url
                info.slug = slug_of(url) or page_info.slug
                info.discovered_via = via
            else:
                known = {r.resource_id for r in info.resources}
                info.resources += [r for r in page_info.resources if r.resource_id not in known]
            info.pages.append(final)
            queue += [x for x in more if x not in seen]
        if info is None:
            raise FetchError("not_data", "no page could be read")
        if fiscal_year:
            info.fiscal_year = info.fiscal_year or fiscal_label(fiscal_year) or fiscal_year
        if not info.resources:
            raise FetchError("not_data", "the page lists no downloadable files (the layout may have changed, or the dataset has no files)")
        return info

    def discover_site(self, url: str) -> DatasetInfo:
        """Scan a general web page (the official BBMP site) for links to structured data files."""
        html, final = self.fetcher.get_text(url)
        info, _ = parse_dataset_page(html, final)
        if not info.resources and self.use_playwright and not looks_restricted(html):
            info, _ = parse_dataset_page(self.renderer(final), final)
            info.discovered_via = "playwright"
        info.slug = (urlparse(final).hostname or "site") + "-site"
        info.url = url
        info.resources = [r for r in info.resources if (r.file_format or "") in DATA_EXT.values() or (r.file_format or "") in UNSUPPORTED_EXT]
        return info
