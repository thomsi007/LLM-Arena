"""Robust parsing helpers for free-form LLM output (JSON blocks, code files)."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.S)
_JSON_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n(\{.*?\}|\[.*?\])\s*```", re.S)
_FILE_HEADER_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*|\*\*|//[ \t]*)?(?:file|fájl|filename|path)[ \t]*[:：][ \t]*"
    r"[`*\"']*([\w./\\-]+\.\w+)[`*\"']*[ \t]*(?:\*\*)?[ \t]*$",
    re.I | re.M,
)
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def clip(text: str, limit: int, *, keep_tail: float = 0.35) -> str:
    """Shorten long text keeping head and tail (the tail often holds conclusions)."""
    text = text or ""
    if len(text) <= limit:
        return text
    tail = int(limit * keep_tail)
    head = max(0, limit - tail - 40)
    return text[:head] + "\n\n[... rövidítve ...]\n\n" + text[-tail:]


def strip_thinking(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


def _remove_trailing_commas(s: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", s)


def _balanced_candidates(text: str):
    """Yield balanced {...} / [...] substrings (string-aware)."""
    for start, ch in enumerate(text):
        if ch not in "{[":
            continue
        close = "}" if ch == "{" else "]"
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == ch:
                depth += 1
            elif c == close:
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
                    break


def _try_load(s: str) -> Any:
    for candidate in (s, _remove_trailing_commas(s)):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def extract_json(text: str, *, expect: type | None = dict) -> Any:
    """Find the most plausible JSON value in model output. Returns None if absent."""
    text = strip_thinking(text)
    found: list[Any] = []
    for m in _JSON_FENCE_RE.finditer(text):
        v = _try_load(m.group(1))
        if v is not None:
            found.append(v)
    if not found:
        whole = _try_load(text.strip())
        if whole is not None:
            found.append(whole)
    if not found:
        best = None
        for cand in _balanced_candidates(text):
            v = _try_load(cand)
            if v is not None and (best is None or len(cand) > best[0]):
                best = (len(cand), v)
        if best:
            found.append(best[1])
    if expect is not None:
        found = [f for f in found if isinstance(f, expect)]
    if not found:
        return None
    # Prefer the last fenced block (models often put the "final" JSON at the end).
    return found[-1]


def remove_json_blocks(text: str) -> str:
    """Drop fenced JSON blocks from display text."""
    return _JSON_FENCE_RE.sub("", text or "").strip()


def as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v not in (None, "", [], {})]
    if isinstance(value, (str, dict)):
        return [value] if value else []
    return [value]


def as_str_list(value: Any) -> list[str]:
    out = []
    for v in as_list(value):
        if isinstance(v, dict):
            v = v.get("text") or v.get("point") or v.get("description") or json.dumps(v, ensure_ascii=False)
        s = str(v).strip()
        if s:
            out.append(s)
    return out


def safe_path(name: str) -> str | None:
    name = (name or "").strip().strip("`'\"*").replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    if not name or name.startswith("/") or ".." in name.split("/") or not _SAFE_PATH_RE.match(name):
        return None
    if len(name) > 120:
        return None
    return name


def _name_from_info(info: str) -> str | None:
    info = info.strip()
    if not info:
        return None
    m = re.search(r"(?:title|file|filename|name)\s*=\s*[\"']?([\w./-]+\.\w+)", info, re.I)
    if m:
        return safe_path(m.group(1))
    for tok in info.split():
        if "." in tok and not tok.startswith("."):
            p = safe_path(tok)
            if p and re.search(r"\.\w{1,6}$", p):
                return p
    return None


def _name_from_first_line(code: str) -> tuple[str | None, str]:
    first, _, rest = code.partition("\n")
    m = re.match(r"^\s*(?:#|//)\s*(?:file(?:name)?\s*[:：]\s*)?([\w./-]+\.(?:py|txt|md|json|toml|cfg|ini|yaml|yml))\s*$",
                 first, re.I)
    if m:
        p = safe_path(m.group(1))
        if p:
            return p, rest
    return None, code


def extract_files(text: str, *, default_name: str = "solution.py",
                  languages: tuple[str, ...] = ("python", "py", "")) -> dict[str, str]:
    """Extract code files from markdown-ish model output.

    Understands ``FILE: name.py`` headers before a fence, filenames in the fence
    info string (```python main.py) and a ``# name.py`` first line comment.
    """
    text = strip_thinking(text)
    files: dict[str, str] = {}
    unnamed: list[str] = []
    for m in _FENCE_RE.finditer(text):
        info, code = m.group(1), m.group(2)
        lang = info.strip().split()[0].lower() if info.strip() else ""
        if lang in ("json", "bash", "sh", "shell", "console", "text", "output") and not _name_from_info(info):
            continue
        name = _name_from_info(info)
        if not name:
            # Header in the preceding lines (look back max 3 non-empty lines).
            before = text[:m.start()].rstrip().split("\n")[-3:]
            for line in reversed(before):
                hm = _FILE_HEADER_RE.match(line)
                if hm:
                    name = safe_path(hm.group(1))
                    break
                bare = re.match(r"^\s*(?:#{1,6}\s*|\*\*)?`?([\w./-]+\.py)`?(?:\*\*)?:?\s*$", line)
                if bare:
                    name = safe_path(bare.group(1))
                    break
        if not name:
            name, code = _name_from_first_line(code)
        code = code.rstrip() + "\n"
        if name:
            files[name] = code
        elif lang in languages or lang.startswith("python"):
            unnamed.append(code)
    if unnamed and not files:
        files[default_name] = unnamed[0] if len(unnamed) == 1 else "\n\n".join(unnamed)
    elif unnamed and default_name not in files and len(unnamed) == 1 and not any(
            not f.startswith("test") for f in files):
        files[default_name] = unnamed[0]
    return files


def is_test_file(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return base.startswith("test_") or base.endswith("_test.py") or base == "tests.py"


def bullet(items: list, empty: str = "(nincs)") -> str:
    items = as_str_list(items)
    return "\n".join(f"- {i}" for i in items) if items else empty


def dumps(obj: Any, limit: int | None = None) -> str:
    s = json.dumps(obj, ensure_ascii=False, indent=1)
    return clip(s, limit) if limit else s
