"""Live web access for the models: robust search + clean page reading.

Search: a fallback chain (configured backend → DuckDuckGo HTML → DuckDuckGo lite
→ stealth browser → SearXNG / Brave when configured → Wikipedia), each with
retries on transient errors, bot-wall detection, per-host pacing and a cache.
Results are cleaned and ranked (see ``quality.py``).

Pages: plain HTTP first (fast), the Playwright + stealth browser as fallback
for JavaScript / bot-protected pages (or always, if configured). Main content
is extracted, boilerplate and injection-like text removed, and the most
relevant paragraphs kept within the budget.

``fetch_url`` refuses non-HTTP(S) schemes and, unless explicitly allowed,
any host resolving to a private / loopback / link-local address (SSRF guard),
checks every redirect hop, and caps size and time.
"""

from __future__ import annotations

import gzip
import html
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

from . import quality

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
MAX_BYTES = 3_000_000


class WebError(Exception):
    def __init__(self, message: str, *, status: int | None = None, transient: bool = False,
                 blocked: bool = False):
        super().__init__(message)
        self.status = status
        self.transient = transient
        self.blocked = blocked


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

def _check_host(url: str, allow_private: bool) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise WebError(f"Nem engedélyezett séma: {parts.scheme or '?'} (csak http/https).")
    host = parts.hostname
    if not host:
        raise WebError("Hiányzó hosztnév.")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as e:
        raise WebError(f"A hosztnév nem oldható fel: {host} ({e})")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
                or ip.is_unspecified:
            raise WebError(f"Belső hálózati cím nem kérhető le ({host} → {ip}). "
                           "Engedélyezhető a beállításokban.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # handle redirects manually (host check per hop)
        return None


def http_get(url: str, *, timeout: float = 15.0, allow_private: bool = False, headers: dict | None = None,
             data: bytes | None = None, max_redirects: int = 5, use_system_proxy: bool = True) -> tuple[str, str, bytes]:
    """GET (or POST when ``data``) with manual redirects. Returns (final_url, content_type, body)."""
    handlers: list = [_NoRedirect()]
    if not use_system_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    for _ in range(max_redirects + 1):
        _check_host(url, allow_private)
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "hu,en;q=0.8", "Accept-Encoding": "gzip, deflate", **(headers or {})})
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                if e.code == 303:
                    data = None
                continue
            raise WebError(f"HTTP {e.code} – {url}", status=e.code,
                           transient=e.code in (408, 425, 429, 500, 502, 503, 504),
                           blocked=e.code in (401, 403, 429, 451))
        except urllib.error.URLError as e:
            transient = isinstance(e.reason, (socket.timeout, TimeoutError, ConnectionError))
            raise WebError(f"Nem érhető el: {url} ({e.reason})", transient=transient)
        except (socket.timeout, TimeoutError):
            raise WebError(f"Időtúllépés: {url}", transient=True)
        except OSError as e:
            raise WebError(f"Hálózati hiba: {e}", transient=True)
        with resp:
            body = resp.read(MAX_BYTES + 1)[:MAX_BYTES]
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            try:
                if enc == "gzip":
                    body = gzip.decompress(body)
                elif enc == "deflate":
                    body = zlib.decompress(body)
            except (OSError, EOFError, zlib.error) as e:
                raise WebError(f"Hibás / csonka tömörített válasz: {url} ({e})", transient=True)
            return resp.geturl(), resp.headers.get("Content-Type") or "", body
    raise WebError("Túl sok átirányítás.")


def _decode(body: bytes, ctype: str) -> str:
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    if not m:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4000], re.I)
        charset = m.group(1).decode() if m else "utf-8"
    else:
        charset = m.group(1)
    try:
        return body.decode(charset, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# HTML → text (main content)
# --------------------------------------------------------------------------- #

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe", "aside", "button",
             "select", "template", "canvas", "dialog"}
SKIP_ATTR = re.compile(r"(^|[\s_-])(cookie|consent|gdpr|banner|popup|modal|newsletter|subscribe|sidebar|"
                       r"menu|navbar|nav|footer|breadcrumbs?|share|social|comments?|related|recommend|advert|"
                       r"ads?|sponsor|promo|skip-link|visually-hidden|sr-only)([\s_-]|$)", re.I)
BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre",
         "blockquote", "table", "ul", "ol", "dd", "dt", "main", "figcaption", "td", "th"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.main_parts: list[str] = []
        self.stack: list[str] = []
        self.skip_at: int | None = None       # stack depth where skipping started
        self.main_depth: int | None = None
        self.title = ""
        self._in_title = False

    def _emit(self, s: str) -> None:
        if self.skip_at is not None:
            return
        self.parts.append(s)
        if self.main_depth is not None:
            self.main_parts.append(s)

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
            return
        a = dict(attrs)
        if tag not in VOID:
            self.stack.append(tag)
        if self.skip_at is None:
            marker = f"{a.get('class') or ''} {a.get('id') or ''} {a.get('role') or ''}"
            hidden = a.get("aria-hidden") == "true" or "display:none" in (a.get("style") or "").replace(" ", "")
            if tag in SKIP_TAGS or hidden or (tag not in ("body", "html", "main", "article")
                                              and SKIP_ATTR.search(marker)):
                if tag not in VOID:
                    self.skip_at = len(self.stack)
                return
        if self.main_depth is None and (tag in ("main", "article") or a.get("role") == "main"):
            self.main_depth = len(self.stack)
        if tag in BLOCK:
            self._emit("\n")
        if tag in ("h1", "h2", "h3"):
            self._emit("## ")
        elif tag == "li":
            self._emit("- ")
        elif tag in ("td", "th"):
            self._emit(" | ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            return
        if tag in VOID or tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            depth = len(self.stack) + 1
            if self.skip_at is not None and depth <= self.skip_at:
                self.skip_at = None
            if self.main_depth is not None and depth <= self.main_depth:
                self.main_depth = None
            if top == tag:
                break
        if tag in BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        else:
            self._emit(data)


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return "\n".join(ln.strip() for ln in text.split("\n")).strip()


def html_to_text(page: str) -> tuple[str, str]:
    """(title, readable text) – prefers <main>/<article> when it holds the substance of the page."""
    p = _TextExtractor()
    try:
        p.feed(page)
        p.close()
    except Exception:  # noqa: BLE001 - malformed HTML: keep what we have
        pass
    full = _tidy("".join(p.parts))
    main = _tidy("".join(p.main_parts))
    text = main if len(main) >= 400 and len(main) >= 0.25 * len(full) else full
    return html.unescape(p.title.strip()), text


# --------------------------------------------------------------------------- #
# Pacing and cache
# --------------------------------------------------------------------------- #

_host_lock = threading.Lock()
_host_last: dict[str, float] = {}
_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, object]] = {}
SEARCH_TTL, FETCH_TTL, CACHE_MAX = 900.0, 1800.0, 300


def _pace(url: str, min_interval: float = 1.0) -> None:
    host = urllib.parse.urlsplit(url).hostname or ""
    with _host_lock:
        wait = _host_last.get(host, 0) + min_interval - time.monotonic()
        _host_last[host] = time.monotonic() + max(0.0, wait)
    if wait > 0:
        time.sleep(min(wait, min_interval))


def cache_get(key: tuple, ttl: float):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
    return None


def cache_put(key: tuple, value) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
        if len(_cache) > CACHE_MAX:
            for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[: len(_cache) - CACHE_MAX]:
                _cache.pop(k, None)


def cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


def _get(url: str, cfg: dict, **kw) -> tuple[str, str, bytes]:
    _pace(url)
    return http_get(url, timeout=cfg.get("timeout", 15), **kw)


def _with_retries(fn, attempts: int = 2):
    last: WebError | None = None
    for i in range(attempts + 1):
        try:
            return fn()
        except WebError as e:
            last = e
            if not e.transient or e.blocked or i == attempts:
                raise
            time.sleep(0.8 * (i + 1))
    raise last  # pragma: no cover


# --------------------------------------------------------------------------- #
# Search backends
# --------------------------------------------------------------------------- #

def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _ddg_url(href: str) -> str:
    href = html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    q = urllib.parse.urlsplit(href)
    if "duckduckgo.com" in q.netloc and q.path.startswith("/l/"):
        target = urllib.parse.parse_qs(q.query).get("uddg")
        if target:
            return target[0]
    return href


DDG_BLOCK = re.compile(r"(anomaly-modal|bots use DuckDuckGo too|challenge-form|Unfortunately, bots)", re.I)


def parse_ddg_html(page: str) -> list[dict]:
    results = []
    blocks = re.split(r'<div[^>]+class="[^"]*\bresult\b', page)
    for b in blocks[1:]:
        m = re.search(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not m:
            m = re.search(r'<a[^>]+href="([^"]+)"[^>]+class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>', b, re.S)
        if not m:
            continue
        url = _ddg_url(m.group(1))
        if "duckduckgo.com/y.js" in url or not url.startswith("http"):
            continue  # ads
        sn = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div|td)>', b, re.S)
        results.append({"title": _strip_tags(m.group(2)), "url": url, "snippet": _strip_tags(sn.group(1)) if sn else ""})
    return results


def parse_ddg_lite(page: str) -> list[dict]:
    results = []
    links = list(re.finditer(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*class=[\"']result-link[\"'][^>]*>(.*?)</a>", page, re.S))
    snippets = re.findall(r"<td[^>]+class=[\"']result-snippet[\"'][^>]*>(.*?)</td>", page, re.S)
    for i, m in enumerate(links):
        url = _ddg_url(m.group(1))
        if not url.startswith("http"):
            continue
        results.append({"title": _strip_tags(m.group(2)), "url": url,
                        "snippet": _strip_tags(snippets[i]) if i < len(snippets) else ""})
    return results


def _ddg_query(query: str, cfg: dict) -> str:
    return urllib.parse.urlencode({"q": query, "kl": cfg.get("region") or "wt-wt"})


def _ddg(url: str, parser, cfg: dict) -> list[dict]:
    _, ctype, body = _get(url, cfg)
    page = _decode(body, ctype)
    if DDG_BLOCK.search(page):
        raise WebError("A DuckDuckGo robotellenőrzést kért (túl sok keresés).", blocked=True)
    return parser(page)


def search_ddg_html(query: str, n: int, cfg: dict) -> list[dict]:
    return _ddg(f"https://html.duckduckgo.com/html/?{_ddg_query(query, cfg)}", parse_ddg_html, cfg)


def search_ddg_lite(query: str, n: int, cfg: dict) -> list[dict]:
    return _ddg(f"https://lite.duckduckgo.com/lite/?{_ddg_query(query, cfg)}", parse_ddg_lite, cfg)


def search_ddg_browser(query: str, n: int, cfg: dict) -> list[dict]:
    from . import browser
    try:
        page = browser.fetch_html(f"https://html.duckduckgo.com/html/?{_ddg_query(query, cfg)}", _browser_cfg(cfg))
    except browser.BrowserUnavailable as e:
        raise WebError(str(e))
    if DDG_BLOCK.search(page["html"]):
        raise WebError("A DuckDuckGo a böngészőben is robotellenőrzést kért.", blocked=True)
    return parse_ddg_html(page["html"])


def search_duckduckgo(query: str, n: int, cfg: dict) -> list[dict]:
    """Kept for compatibility: HTML endpoint, then lite."""
    try:
        res = search_ddg_html(query, n, cfg)
        if res:
            return res
    except WebError:
        pass
    return search_ddg_lite(query, n, cfg)


def search_searxng(query: str, n: int, cfg: dict) -> list[dict]:
    base = (cfg.get("searxng_url") or "").rstrip("/")
    if not base:
        raise WebError("Nincs megadva SearXNG URL.")
    url = f"{base}/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    _, _, body = _get(url, cfg, allow_private=True)
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise WebError("A SearXNG nem JSON-t adott (engedélyezd a json formátumot a settings.yml-ben).")
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in data.get("results", [])[: n * 2] if r.get("url")]


def search_brave(query: str, n: int, cfg: dict) -> list[dict]:
    key = cfg.get("brave_api_key") or ""
    if not key:
        raise WebError("Nincs megadva Brave Search API-kulcs.")
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": min(20, n * 2)})
    _, _, body = _get(url, cfg, headers={"X-Subscription-Token": key, "Accept": "application/json"})
    data = json.loads(body.decode("utf-8", "replace"))
    return [{"title": _strip_tags(r.get("title", "")), "url": r.get("url", ""),
             "snippet": _strip_tags(r.get("description", ""))}
            for r in (data.get("web") or {}).get("results", [])]


def search_wikipedia(query: str, n: int, cfg: dict) -> list[dict]:
    lang = "hu" if re.search(r"[áéíóöőúüű]", query, re.I) else "en"
    url = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": n, "utf8": 1})
    _, _, body = _get(url, cfg, headers={"Accept": "application/json"})
    data = json.loads(body.decode("utf-8", "replace"))
    return [{"title": r["title"], "snippet": _strip_tags(r.get("snippet", "")),
             "url": f"https://{lang}.wikipedia.org/wiki/" + urllib.parse.quote(r["title"].replace(" ", "_"))}
            for r in (data.get("query") or {}).get("search", [])]


BACKENDS = {"duckduckgo": search_duckduckgo, "searxng": search_searxng, "brave": search_brave,
            "wikipedia": search_wikipedia}
CHAIN_FUNCS = {"ddg_html": search_ddg_html, "ddg_lite": search_ddg_lite, "ddg_browser": search_ddg_browser,
               "searxng": search_searxng, "brave": search_brave, "wikipedia": search_wikipedia}
CHAIN_LABELS = {"ddg_html": "DuckDuckGo", "ddg_lite": "DuckDuckGo lite", "ddg_browser": "DuckDuckGo (böngésző)",
                "searxng": "SearXNG", "brave": "Brave", "wikipedia": "Wikipedia"}


def chain(cfg: dict) -> list[str]:
    first = cfg.get("backend") or "duckduckgo"
    order: list[str] = []
    if first == "searxng" and cfg.get("searxng_url"):
        order.append("searxng")
    if first == "brave" and cfg.get("brave_api_key"):
        order.append("brave")
    order += ["ddg_html", "ddg_lite"]
    if cfg.get("browser_mode", "fallback") != "off" and _browser_installed():
        order.append("ddg_browser")
    if cfg.get("searxng_url") and "searxng" not in order:
        order.append("searxng")
    if cfg.get("brave_api_key") and "brave" not in order:
        order.append("brave")
    order.append("wikipedia")
    return order


class SearchResults(list):
    """List of results with provenance (backend used, failed attempts, cache hit)."""
    backend = ""
    attempts: list = []
    cached = False


def search(query: str, cfg: dict) -> SearchResults:
    query = re.sub(r"\s+", " ", (query or "")).strip()[:300]
    if not query:
        raise WebError("Üres keresőkifejezés.")
    n = int(cfg.get("max_results") or 5)
    key = ("search", quality.fold(query), tuple(chain(cfg)))
    hit = cache_get(key, SEARCH_TTL)
    if hit is not None:
        res = SearchResults(hit)
        res.backend, res.cached, res.attempts = getattr(hit, "backend", ""), True, []
        return res
    attempts = []
    for name in chain(cfg):
        fn = CHAIN_FUNCS[name]
        try:
            found = _with_retries(lambda fn=fn: fn(query, n, cfg))
        except WebError as e:
            attempts.append({"backend": CHAIN_LABELS[name], "error": str(e)[:200], "blocked": e.blocked})
            continue
        except Exception as e:  # noqa: BLE001 - a broken backend must not stop the chain
            attempts.append({"backend": CHAIN_LABELS[name], "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        if found:
            res = SearchResults(found)
            res.backend, res.attempts = CHAIN_LABELS[name], attempts
            cache_put(key, res)
            return res
        attempts.append({"backend": CHAIN_LABELS[name], "error": "nincs találat"})
    if all(a["error"] == "nincs találat" for a in attempts):
        res = SearchResults()
        res.backend, res.attempts = "", attempts
        return res
    raise WebError("Egyik keresőszolgáltatás sem válaszolt: " +
                   "; ".join(f"{a['backend']}: {a['error']}" for a in attempts), transient=True)


# --------------------------------------------------------------------------- #
# Page fetching
# --------------------------------------------------------------------------- #

def _browser_installed() -> bool:
    try:
        from . import browser
        return browser.installed()["playwright"]
    except Exception:  # noqa: BLE001
        return False


def _browser_cfg(cfg: dict) -> dict:
    return {"browser_channel": cfg.get("browser_channel") or "auto", "browser_path": cfg.get("browser_path") or "",
            "headless": cfg.get("browser_headless", True), "locale": "hu-HU"}


def _page_from_html(raw_html: str, final: str) -> tuple[str, str]:
    title, text = html_to_text(raw_html)
    return title or final, text


def _pdf_text(body: bytes) -> str:
    from ..attachments import AttachmentError, _pdf
    try:
        return _pdf(body)
    except AttachmentError as e:
        raise WebError(e.error["message"])


def _fetch_http(url: str, cfg: dict) -> tuple[str, str, str, str]:
    final, ctype, body = _with_retries(lambda: _get(url, cfg, allow_private=bool(cfg.get("allow_private"))), 1)
    low = (ctype or "").lower()
    if "pdf" in low or body[:5] == b"%PDF-":
        return final, final.rsplit("/", 1)[-1], _pdf_text(body), "http-pdf"
    text = _decode(body, ctype)
    if "html" in low or text.lstrip()[:300].lower().startswith(("<!doctype", "<html")):
        title, text = _page_from_html(text, final)
        return final, title, text, "http"
    if "json" in low:
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
        except ValueError:
            pass
        return final, final, text, "http"
    if low and not low.startswith("text/") and "xml" not in low:
        raise WebError(f"Nem szöveges tartalom ({ctype}).")
    return final, final, text, "http"


def _fetch_browser(url: str, cfg: dict) -> tuple[str, str, str, str]:
    from . import browser
    allow = bool(cfg.get("allow_private"))
    try:
        page = browser.fetch_html(url, _browser_cfg(cfg), url_guard=lambda u: _check_host(u, allow),
                                  timeout=float(cfg.get("browser_timeout", 25)))
    except browser.BrowserUnavailable as e:
        raise WebError(str(e))
    title, text = _page_from_html(page["html"], page["url"])
    return page["url"], page["title"] or title, text, "browser" + ("+stealth" if page.get("stealth") else "")


def fetch(url: str, cfg: dict, max_chars: int = 8000, focus: str = "") -> dict:
    url = (url or "").strip().strip("<>\"'")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    url = quality.strip_tracking(url)
    _check_host(url, bool(cfg.get("allow_private")))
    mode = cfg.get("browser_mode", "fallback")
    can_browse = mode != "off" and _browser_installed()
    key = ("fetch", quality.normalize_url(url), mode)
    page = cache_get(key, FETCH_TTL)
    notes: list[str] = []
    if page is None:
        page = None
        errors: list[str] = []
        order = ["browser", "http"] if (mode == "always" and can_browse) else ["http"] + (["browser"] if can_browse else [])
        for how in order:
            try:
                final, title, text, via = _fetch_http(url, cfg) if how == "http" else _fetch_browser(url, cfg)
            except WebError as e:
                errors.append(f"{how}: {e}")
                if how == "http" and not can_browse:
                    raise
                continue
            text = quality.clean_page_text(text)
            reason = quality.blocked_reason(title, text)
            if how != order[-1] and (reason or quality.is_thin(text)):
                errors.append(f"{how}: {reason or 'kevés olvasható szöveg (JavaScript-es oldal?)'}")
                continue
            page = {"url": final, "title": title, "text": text, "via": via, "blocked": reason}
            break
        if page is None:
            raise WebError("Az oldal nem olvasható: " + "; ".join(errors), transient=True)
        if errors:
            notes.append("tartalék út: " + errors[0][:120])
        if not page["blocked"]:
            cache_put(key, page)
    else:
        notes.append("gyorsítótárból")
    if page["blocked"]:
        raise WebError(f"Az oldal nem adott érdemi tartalmat ({page['blocked']}).", blocked=True)
    text = quality.sanitize(page["text"])
    excerpt, truncated = quality.select_relevant(text, focus, max_chars)
    return {"url": page["url"], "title": quality.sanitize(page["title"])[:200], "text": excerpt,
            "truncated": truncated, "chars": len(text), "via": page["via"], "notes": notes}
