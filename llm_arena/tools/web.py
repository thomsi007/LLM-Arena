"""Live web access for the models: search + page fetching (standard library only).

Search backends:
  * ``duckduckgo`` – no API key, HTML endpoint (with the "lite" page as fallback)
  * ``searxng``    – your own SearXNG instance (JSON API), best for heavy use
  * ``brave``      – Brave Search API (needs an API key)

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
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36 LLM-Arena/1.0")
MAX_BYTES = 2_000_000


class WebError(Exception):
    pass


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
            raise WebError(f"HTTP {e.code} – {url}")
        except urllib.error.URLError as e:
            raise WebError(f"Nem érhető el: {url} ({e.reason})")
        except (socket.timeout, TimeoutError):
            raise WebError(f"Időtúllépés: {url}")
        except OSError as e:
            raise WebError(f"Hálózati hiba: {e}")
        with resp:
            body = resp.read(MAX_BYTES + 1)[:MAX_BYTES]
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                body = gzip.decompress(body)
            elif enc == "deflate":
                body = zlib.decompress(body)
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
# HTML → text
# --------------------------------------------------------------------------- #

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe", "aside", "button"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre",
             "blockquote", "table", "ul", "ol", "dd", "dt"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3") and not self.skip:
            self.parts.append("## ")
        if tag == "li" and not self.skip:
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def html_to_text(page: str) -> tuple[str, str]:
    p = _TextExtractor()
    try:
        p.feed(page)
        p.close()
    except Exception:  # noqa: BLE001 - malformed HTML: keep what we have
        pass
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    lines = [ln.strip() for ln in text.split("\n")]
    return p.title.strip(), "\n".join(lines).strip()


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


def search_duckduckgo(query: str, n: int, cfg: dict) -> list[dict]:
    q = urllib.parse.urlencode({"q": query, "kl": cfg.get("region") or "wt-wt"})
    errors = []
    for url, parser in ((f"https://html.duckduckgo.com/html/?{q}", parse_ddg_html),
                        (f"https://lite.duckduckgo.com/lite/?{q}", parse_ddg_lite)):
        try:
            _, ctype, body = http_get(url, timeout=cfg.get("timeout", 15), use_system_proxy=True)
            res = parser(_decode(body, ctype))
            if res:
                return res[:n]
        except WebError as e:
            errors.append(str(e))
    if errors:
        raise WebError("DuckDuckGo keresés sikertelen: " + "; ".join(errors))
    return []


def search_searxng(query: str, n: int, cfg: dict) -> list[dict]:
    base = (cfg.get("searxng_url") or "").rstrip("/")
    if not base:
        raise WebError("Nincs megadva SearXNG URL.")
    url = f"{base}/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    _, _, body = http_get(url, timeout=cfg.get("timeout", 15), allow_private=True)
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise WebError("A SearXNG nem JSON-t adott (engedélyezd a json formátumot a settings.yml-ben).")
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in data.get("results", [])[:n] if r.get("url")]


def search_brave(query: str, n: int, cfg: dict) -> list[dict]:
    key = cfg.get("brave_api_key") or ""
    if not key:
        raise WebError("Nincs megadva Brave Search API-kulcs.")
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": n})
    _, _, body = http_get(url, timeout=cfg.get("timeout", 15),
                          headers={"X-Subscription-Token": key, "Accept": "application/json"})
    data = json.loads(body.decode("utf-8", "replace"))
    return [{"title": _strip_tags(r.get("title", "")), "url": r.get("url", ""),
             "snippet": _strip_tags(r.get("description", ""))}
            for r in (data.get("web") or {}).get("results", [])[:n]]


BACKENDS = {"duckduckgo": search_duckduckgo, "searxng": search_searxng, "brave": search_brave}


def search(query: str, cfg: dict) -> list[dict]:
    query = (query or "").strip()
    if not query:
        raise WebError("Üres keresőkifejezés.")
    backend = BACKENDS.get(cfg.get("backend") or "duckduckgo", search_duckduckgo)
    return backend(query[:300], int(cfg.get("max_results") or 5), cfg)


def fetch(url: str, cfg: dict, max_chars: int = 8000) -> dict:
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    final, ctype, body = http_get(url, timeout=cfg.get("timeout", 15),
                                  allow_private=bool(cfg.get("allow_private")))
    text = _decode(body, ctype)
    title = ""
    if "html" in ctype.lower() or text.lstrip()[:200].lower().startswith(("<!doctype", "<html")):
        title, text = html_to_text(text)
    elif "json" in ctype.lower():
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
        except ValueError:
            pass
    elif not ctype.lower().startswith("text/") and ctype:
        raise WebError(f"Nem szöveges tartalom ({ctype}).")
    truncated = len(text) > max_chars
    return {"url": final, "title": title or final, "text": text[:max_chars], "truncated": truncated,
            "chars": len(text)}
