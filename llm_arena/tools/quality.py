"""Quality control for web content given to the models.

* search results: URL normalisation, de-duplication, junk removal, relevance
  ranking, per-domain cap, snippet cleaning
* pages: boilerplate removal, prompt-injection neutralisation, blocked-page
  detection and relevance-based excerpting within a character budget
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse

STOPWORDS = set("""
a az egy és vagy de hogy is nem meg már még csak mint ami amely aki ez azt ezt ezek azok van volt lesz lehet
kell mi mit milyen melyik hol mikor hogyan miért mennyi mennyire legújabb legfrissebb aktuális jelenlegi
the a an and or of to in on for with by at from is are was were be been what which who how why when where
latest current new about vs versus does do did can could should would will this that these those it its
""".split())

TRACKING_PARAM = re.compile(r"^(utm_\w+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|igshid|yclid|_hsenc|_hsmi|"
                            r"ref|ref_src|ref_url|spm|scid|cmpid|oly_\w+|vero_\w+)$", re.I)
JUNK_URL = re.compile(r"(duckduckgo\.com/(y\.js|l/)|bing\.com/(aclick|ck/a)|google\.[a-z.]+/(url|aclk)|"
                      r"doubleclick\.net|googleadservices|/ads?/|[?&]ad_?id=)", re.I)
LOW_VALUE_DOMAINS = ("pinterest.", "facebook.com", "instagram.com", "tiktok.com", "twitter.com", "x.com",
                     "linkedin.com/posts", "quora.com", "scribd.com", "coursehero.com", "chegg.com")
AUTHORITY = (("wikipedia.org", 0.5), ("github.com", 0.45), ("docs.", 0.45), ("readthedocs", 0.45),
             ("developer.", 0.4), ("python.org", 0.4), ("mozilla.org", 0.4), ("arxiv.org", 0.35),
             ("stackoverflow.com", 0.3), (".gov", 0.4), (".edu", 0.3), ("europa.eu", 0.4), (".gov.hu", 0.4))

BOILERPLATE = re.compile(
    r"(cookie|süti|consent|hozzájárul|accept all|elfogad(om|ás)|subscribe|feliratkoz|sign ?in|log ?in|"
    r"bejelentkez|regisztr|newsletter|hírlevél|all rights reserved|minden jog fenntartva|privacy policy|"
    r"adatvédelm|terms of (use|service)|felhasználási feltételek|share (on|this)|megoszt|advertisement|"
    r"hirdetés|skip to (main )?content|ugrás a tartalomra|back to top|vissza a tetejére|javascript is disabled|"
    r"please enable javascript|loading\.\.\.|betöltés)", re.I)
INJECTION_TOKENS = re.compile(
    r"(<\|im_(start|end)\|>|<\|(system|assistant|user|eot_id|start_header_id|end_header_id)\|>|"
    r"</?tool_call>|</?tool_response[^>]*>|</?think>|\[/?INST\]|<<SYS>>|<</SYS>>)", re.I)
INJECTION_PHRASE = re.compile(
    r"(ignore (all |any )?(previous|prior|above) (instructions|prompts)|disregard (the )?(previous|above)|"
    r"you are now|new instructions:|system prompt:|hagyd figyelmen kívül a (korábbi|fenti) utasításokat)", re.I)
BLOCKED_PAGE = re.compile(
    r"(captcha|are you a (human|robot)|verify you are human|unusual traffic|access denied|"
    r"attention required|just a moment|checking your browser|enable javascript and cookies|"
    r"please turn javascript on|403 forbidden|request blocked|bot detection|ddos protection|"
    r"hozzáférés megtagadva|nem vagy robot)", re.I)


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def terms(s: str) -> list[str]:
    return [t for t in re.findall(r"[\w.+#-]{2,}", fold(s)) if t not in STOPWORDS and not t.isdigit() or
            (t.isdigit() and len(t) >= 2)]


def domain(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def normalize_url(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAM.match(k)]
    path = p.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((p.scheme.lower(), domain(url), path, urllib.parse.urlencode(query), ""))


def strip_tracking(url: str) -> str:
    """Remove tracking parameters but keep the URL otherwise intact (for display / fetching)."""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url
    query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAM.match(k)]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(query), p.fragment))


def clean_snippet(s: str, limit: int = 320) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    s = INJECTION_TOKENS.sub(" ", s)
    if len(s) > limit:
        cut = s[:limit].rsplit(" ", 1)[0]
        s = cut + " …"
    return s


def _score(r: dict, qterms: list[str]) -> float:
    if not qterms:
        return 0.0
    title, snippet, url = fold(r.get("title", "")), fold(r.get("snippet", "")), fold(r.get("url", ""))
    hit_t = sum(1 for t in qterms if t in title)
    hit_s = sum(1 for t in qterms if t in snippet)
    hit_u = sum(1 for t in qterms if t in url)
    score = (2.0 * hit_t + 1.0 * hit_s + 0.5 * hit_u) / (len(qterms) * 3.5)
    d = domain(r.get("url", ""))
    for pat, bonus in AUTHORITY:
        if pat in d or (pat.startswith(".") and d.endswith(pat)):
            score += bonus * 0.4
            break
    if any(x in d or x in fold(r.get("url", "")) for x in LOW_VALUE_DOMAINS):
        score -= 0.3
    return score


def rank_results(results: list[dict], query: str, max_results: int = 5, per_domain: int = 2) -> list[dict]:
    """Clean, de-duplicate and rank search results. Keeps provider order as a tie breaker."""
    qterms = list(dict.fromkeys(terms(query)))
    seen: set[str] = set()
    cleaned = []
    n = max(1, len(results))
    for i, r in enumerate(results):
        url = (r.get("url") or "").strip()
        if not re.match(r"^https?://", url, re.I) or JUNK_URL.search(url):
            continue
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        title = clean_snippet(r.get("title") or "", 160) or domain(url)
        item = {"title": title, "url": strip_tracking(url), "snippet": clean_snippet(r.get("snippet") or "")}
        item["score"] = round(_score(item, qterms) + 0.15 * (1 - i / n), 4)
        cleaned.append(item)
    cleaned.sort(key=lambda x: x["score"], reverse=True)
    out: list[dict] = []
    per: dict[str, int] = {}
    for item in cleaned:
        d = domain(item["url"])
        if per.get(d, 0) >= per_domain:
            continue
        per[d] = per.get(d, 0) + 1
        out.append(item)
    # Drop clearly irrelevant tail results (no term overlap at all) when enough good ones exist.
    relevant = [x for x in out if x["score"] > 0.16]
    if qterms and len(relevant) >= 3:
        out = relevant
    return out[:max_results]


def sanitize(text: str) -> str:
    """Neutralise chat-template tokens and obvious prompt-injection phrases in external content."""
    text = INJECTION_TOKENS.sub(" ", text or "")
    return INJECTION_PHRASE.sub("[eltávolított utasításszerű szöveg]", text)


def clean_page_text(text: str) -> str:
    """Drop boilerplate / navigation lines and duplicates."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        key = fold(line)
        if len(line) < 3 or (len(line) < 140 and BOILERPLATE.search(line)):
            continue
        if key in seen and len(line) < 200:
            continue
        seen.add(key)
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def blocked_reason(title: str, text: str) -> str | None:
    """Detect bot walls / error pages that must not be fed to the model as content."""
    sample = f"{title}\n{(text or '')[:3000]}"
    m = BLOCKED_PAGE.search(sample)
    if m and len(text or "") < 3000:
        return m.group(0)
    return None


def is_thin(text: str) -> bool:
    """Very little readable text – probably rendered by JavaScript (worth a browser retry)."""
    return len((text or "").strip()) < 200


def select_relevant(text: str, focus: str, budget: int) -> tuple[str, bool]:
    """Keep the page within ``budget`` chars, preferring paragraphs that match ``focus``."""
    if len(text) <= budget:
        return text, False
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) < 3:
        paras = [p for p in text.split("\n") if p.strip()]
    fterms = list(dict.fromkeys(terms(focus)))
    head_budget = int(budget * 0.2)
    chosen: set[int] = set()
    used = 0
    for i, p in enumerate(paras):  # always keep the beginning (title, lead)
        if used + len(p) > head_budget:
            break
        chosen.add(i)
        used += len(p) + 2
    if fterms:
        scored = sorted(((sum(fold(p).count(t) for t in fterms) / (1 + len(p) / 800), i)
                         for i, p in enumerate(paras) if i not in chosen), reverse=True)
        for s, i in scored:
            if s <= 0:
                break
            p = paras[i]
            if used + len(p) > budget:
                if used + 300 < budget:
                    p = p[: budget - used - 20] + " …"
                    paras[i] = p
                else:
                    continue
            chosen.add(i)
            used += len(p) + 2
    for i, p in enumerate(paras):  # fill the rest in reading order
        if i in chosen:
            continue
        if used + len(p) > budget:
            break
        chosen.add(i)
        used += len(p) + 2
    out, last = [], -1
    for i in sorted(chosen):
        if last >= 0 and i != last + 1:
            out.append("[…]")
        out.append(paras[i])
        last = i
    return "\n\n".join(out), True
