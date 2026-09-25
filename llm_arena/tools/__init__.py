"""Tools the models may call (OpenAI function-calling format + text fallback).

New tools: add a spec to ``TOOL_SPECS`` and a branch in :func:`execute`.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from . import web

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web for current / live information (news, prices, versions, recent events, "
                       "documentation). Returns titles, URLs and snippets.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "search query, in the most suitable language"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "Download a web page and return its readable text. Use it to read a search result in detail.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "absolute http(s) URL"}},
            "required": ["url"]}}},
]
TOOL_NAMES = {t["function"]["name"] for t in TOOL_SPECS}

SYSTEM_HINT = """You have live web access through tools: web_search(query) and fetch_url(url).
Current date: {date}. Use the tools ONLY when the task needs current/live information or facts you are unsure about;
otherwise answer directly. Prefer 1-2 targeted searches, read the most relevant page if the snippet is not enough.
When you use web information, cite sources inline as [1], [2] and list the URLs at the end under "Források"."""

TEXT_PROTOCOL = """
To call a tool, output ONLY this (nothing else) and stop:
<tool_call>{{"name": "web_search", "arguments": {{"query": "..."}}}}</tool_call>
or
<tool_call>{{"name": "fetch_url", "arguments": {{"url": "https://..."}}}}</tool_call>
The result will be sent back to you inside <tool_response>...</tool_response>. Then continue or give the final answer."""

_TEXT_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_FUNC_CALL_RE = re.compile(r"<function=(\w+)>\s*(.*?)\s*</function>", re.S)


def system_hint(text_mode: bool) -> str:
    hint = SYSTEM_HINT.format(date=time.strftime("%Y-%m-%d"))
    return hint + (TEXT_PROTOCOL.format() if text_mode else "")


def parse_text_calls(content: str) -> tuple[list[dict], str]:
    """Find tool calls written as text (<tool_call>{json}</tool_call>, Qwen/Hermes style)."""
    calls = []
    for m in _TEXT_CALL_RE.finditer(content or ""):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        name = data.get("name")
        args = data.get("arguments") or data.get("parameters") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"query": args}
        if name in TOOL_NAMES:
            calls.append({"id": "call_" + uuid.uuid4().hex[:8], "name": name, "arguments": args})
    cleaned = _TEXT_CALL_RE.sub("", content or "").strip()
    return calls, cleaned


def normalize_native(tool_calls: list[dict]) -> list[dict]:
    out = []
    for tc in tool_calls or []:
        fn = tc.get("function") or {}
        name = fn.get("name") or tc.get("name")
        raw = fn.get("arguments", tc.get("arguments", {}))
        if isinstance(raw, str):
            try:
                args = json.loads(raw) if raw.strip() else {}
            except ValueError:
                args = {"query": raw} if name == "web_search" else {"url": raw}
        else:
            args = raw or {}
        if name:
            out.append({"id": tc.get("id") or "call_" + uuid.uuid4().hex[:8], "name": name, "arguments": args})
    return out


def execute(name: str, args: dict, cfg: dict, max_chars: int = 8000) -> dict:
    """Run a tool. Returns {ok, content (for the model), summary, sources, error}."""
    t0 = time.monotonic()
    try:
        if name == "web_search":
            query = str(args.get("query") or args.get("q") or "").strip()
            results = web.search(query, cfg)
            if not results:
                content = f"No results for: {query}"
            else:
                content = "\n\n".join(f"[{i}] {r['title']}\nURL: {r['url']}\n{r['snippet']}"
                                      for i, r in enumerate(results, 1))
            return {"ok": True, "content": content, "summary": f"„{query}” – {len(results)} találat",
                    "sources": [{"title": r["title"], "url": r["url"]} for r in results],
                    "duration": round(time.monotonic() - t0, 2)}
        if name == "fetch_url":
            page = web.fetch(str(args.get("url") or ""), cfg, max_chars=max_chars)
            content = (f"Title: {page['title']}\nURL: {page['url']}\n\n{page['text']}"
                       + ("\n\n[... truncated ...]" if page["truncated"] else ""))
            return {"ok": True, "content": content, "summary": f"{page['title'][:80]} ({page['chars']} karakter)",
                    "sources": [{"title": page["title"], "url": page["url"]}],
                    "duration": round(time.monotonic() - t0, 2)}
        raise web.WebError(f"Ismeretlen eszköz: {name}")
    except web.WebError as e:
        return {"ok": False, "content": f"Tool error: {e}", "summary": str(e), "sources": [], "error": str(e),
                "duration": round(time.monotonic() - t0, 2)}
    except Exception as e:  # noqa: BLE001 - a tool must never break the workflow
        return {"ok": False, "content": f"Tool error: {e}", "summary": f"Váratlan hiba: {e}", "sources": [],
                "error": str(e), "duration": round(time.monotonic() - t0, 2)}


def web_config(settings: dict) -> dict:
    return {
        "backend": settings.get("web_backend") or "duckduckgo",
        "searxng_url": settings.get("web_searxng_url") or "",
        "brave_api_key": settings.get("web_brave_api_key") or "",
        "max_results": int(settings.get("web_max_results") or 5),
        "allow_private": bool(settings.get("web_allow_private")),
        "timeout": 15,
    }


def as_tool_messages(calls: list[dict], results: list[dict], native: bool, content: str) -> list[dict]:
    """Conversation messages to append after a round of tool calls."""
    if native:
        msgs: list[dict[str, Any]] = [{"role": "assistant", "content": content or "", "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
            for c in calls]}]
        for c, r in zip(calls, results):
            msgs.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"], "content": r["content"]})
        return msgs
    call_text = "\n".join(f'<tool_call>{json.dumps({"name": c["name"], "arguments": c["arguments"]}, ensure_ascii=False)}</tool_call>'
                          for c in calls)
    resp_text = "\n\n".join(f"<tool_response name=\"{c['name']}\">\n{r['content']}\n</tool_response>"
                            for c, r in zip(calls, results))
    return [{"role": "assistant", "content": (content + "\n" if content else "") + call_text},
            {"role": "user", "content": resp_text + "\n\nContinue: call another tool if really needed, "
                                                    "otherwise give the final answer now."}]
