"""Mock OpenAI-compatible / llama-server endpoint for demos and tests.

    python -m llm_arena.mock_server --port 8080 --name mock-a
    python -m llm_arena.mock_server --port 8081 --name mock-b

It produces deterministic, *structurally valid* answers for every prompt the
arena sends (JSON evaluations, debate turns, code files, tests, fixes), so the
full pipeline including the test → fix loop can be exercised offline.
Failure modes (for testing error handling) are selected with ``mode``.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BUGGY_CALC = '''"""Simple calculator module."""


def add(a, b):
    return a + b


def divide(a, b):
    return a / b


def mean(values):
    return sum(values) / len(values)


if __name__ == "__main__":
    print(divide(10, 2))
'''

FIXED_CALC = '''"""Simple calculator module."""


def add(a, b):
    return a + b


def divide(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b


def mean(values):
    if not values:
        raise ValueError("mean of empty sequence")
    return sum(values) / len(values)


if __name__ == "__main__":
    print(divide(10, 2))
'''

UNIT_TESTS = '''import unittest
import calc


class TestUnit(unittest.TestCase):
    def test_add(self):
        self.assertEqual(calc.add(2, 3), 5)

    def test_divide(self):
        self.assertEqual(calc.divide(10, 4), 2.5)


class TestIntegration(unittest.TestCase):
    def test_mean_uses_divide_semantics(self):
        self.assertAlmostEqual(calc.mean([1, 2, 3, 4]), 2.5)
'''

EDGE_TESTS = '''import unittest
import calc


class TestErrorHandling(unittest.TestCase):
    def test_divide_by_zero_raises_value_error(self):
        with self.assertRaises(ValueError):
            calc.divide(1, 0)

    def test_mean_empty_raises_value_error(self):
        with self.assertRaises(ValueError):
            calc.mean([])


class TestEdgeCases(unittest.TestCase):
    def test_negative(self):
        self.assertEqual(calc.add(-1, -1), -2)
'''


def _fence(name: str, code: str) -> str:
    return f"FILE: {name}\n```python\n{code}```\n"


def respond(messages: list[dict], name: str) -> str:
    """Pick a plausible answer from the last user message."""
    user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    if isinstance(user, list):  # multimodal parts
        user = " ".join(p.get("text", "[image]") if isinstance(p, dict) else str(p) for p in user)
    system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
    u = user
    if "single word: pong" in u:
        return "pong"
    if "could not be parsed" in u:
        return json.dumps({"ok": True})
    if "Evaluate BOTH on every criterion" in u:
        s1 = {c: 7 for c in ("correctness", "completeness", "feasibility", "security", "performance", "testability")}
        s2 = dict(s1, security=8, completeness=6)
        return "```json\n" + json.dumps({
            "scores": {"1": s1, "2": s2},
            "strengths": {"1": ["világos szerkezet"], "2": ["jobb hibakezelés"]},
            "weaknesses": {"1": ["hiányos hibakezelés"], "2": ["kevésbé teljes"]},
            "best_ideas": [{"from": "1", "idea": "moduláris felépítés"}, {"from": "2", "idea": "bemenet-validálás"}],
            "conflicts": ["kivételkezelés módja – ValueError legyen"],
        }, ensure_ascii=False) + "\n```"
    if "Build ONE combined solution" in u:
        return (f"## Közös megoldás ({name})\n\nModuláris felépítés, szigorú bemenet-validálás, "
                "ValueError hibás bemenetre.\n\n### Contributions\n- A: modulok\n- B: validálás")
    if "Review this combined solution" in u:
        return json.dumps({"approved": True, "remaining_issues": [], "improvements": ["több példa"]})
    if "neutral moderator" in u:
        return "```json\n" + json.dumps({
            "arguments_summary": ["egyszerű, moduláris megoldás"],
            "counterarguments_summary": ["hiányzó hibakezelés"],
            "facts": ["a nullával osztást kezelni kell"],
            "disputed": [{"point": "kivétel típusa", "proponent": "ZeroDivisionError", "critic": "ValueError"}],
            "solutions": ["ValueError saját üzenettel"],
            "open_questions": ["kell-e Decimal támogatás?"],
            "conclusion": "Moduláris számológép, ValueError-ral jelzett hibás bemenettel.",
        }, ensure_ascii=False) + "\n```"
    if "Check it for missing or distorted" in u:
        return json.dumps({"agree": True, "corrections": [], "missing_facts": ["az üres lista átlaga nem értelmezett"],
                           "missing_disputed": [], "additional_solutions": [], "additional_questions": [],
                           "conclusion_comment": "Egyetértek."}, ensure_ascii=False)
    if "DEBATE TOPIC" in u:
        role = "critic" if "CRITIC" in system else "proponent"
        tail = {"claims": [f"{role} érv"], "objections": ["nincs hibakezelés"] if role == "critic" else [],
                "concessions": [] if role == "critic" else ["a hibakezelés fontos"], "questions": ["Decimal?"],
                "proposals": ["ValueError"]}
        return f"{name} ({role}) álláspontja a témáról.\n\n```json\n{json.dumps(tail, ensure_ascii=False)}\n```"
    if "Implement the program" in u:
        return "Implementáció:\n\n" + _fence("calc.py", BUGGY_CALC) + "\nFuttatás: python calc.py"
    if "Fix the problems found in code review" in u:
        return "Nincs módosítás szükséges a review alapján."
    if "UNIT tests and INTEGRATION tests" in u:
        return _fence("test_unit.py", UNIT_TESTS)
    if "EDGE-CASE tests" in u:
        return _fence("test_edge_cases.py", EDGE_TESTS)
    if "Automated tests failed" in u:
        return json.dumps({"failures": [{"test": "test_divide_by_zero_raises_value_error",
                                         "root_cause": "divide nem ellenőrzi a nullát", "fix_target": "code",
                                         "location": "calc.py:divide", "suggestion": "ValueError"}],
                           "summary": "hiányzó validálás"}, ensure_ascii=False)
    if "Fix the failing tests" in u:
        return "Javítás:\n\n" + _fence("calc.py", FIXED_CALC)
    if "Review the code thoroughly" in u:
        return "Review.\n```json\n" + json.dumps({"issues": [
            {"id": "R1", "category": "edge_cases", "severity": "low", "location": "calc.py:divide",
             "description": "nullával osztás", "suggestion": "ValueError"}], "summary": "ok",
            "verdict": "needs_changes"}, ensure_ascii=False) + "\n```"
    if "final README" in u:
        return "# Calc\n\nEgyszerű számológép modul.\n\n## Tesztek\n\n`python -m unittest`"
    if "final audit" in u:
        return json.dumps({"approved": True, "scores": {"correctness": 8, "completeness": 7, "feasibility": 9,
                                                        "security": 8, "performance": 9, "testability": 9},
                           "remaining_issues": [], "strengths": ["tesztelt"], "recommendations": []})
    if "```json" in u:
        return f"{name}: elemzés.\n\n```json\n{{\"items\": [\"{name}\"]}}\n```"
    return f"**{name}** válasza.\n\nA feladat elemzése: {u[:160]}"


class MockLlama:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, name: str = "mock-model",
                 mode: str = "ok", delay: float = 0.0, token_delay: float = 0.0):
        self.name = name
        self.mode = mode
        self.delay = delay
        self.token_delay = token_delay
        self.requests: list[dict] = []
        mock = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if mock.mode == "down":
                    self.close_connection = True
                    return
                if self.path == "/health":
                    return self._json({"status": "loading model"} if mock.mode == "loading" else {"status": "ok"},
                                      503 if mock.mode == "loading" else 200)
                if self.path == "/v1/models":
                    return self._json({"object": "list", "data": [{"id": mock.name, "object": "model"}]})
                if self.path == "/props":
                    return self._json({"model_path": f"/models/{mock.name}.gguf"})
                self._json({"error": "not found"}, 404)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                mock.requests.append(body)
                if mock.delay:
                    time.sleep(mock.delay)
                mode = mock.mode
                if mode == "http500":
                    return self._json({"error": {"message": "internal boom"}}, 500)
                if mode == "http400":
                    return self._json({"error": {"message": "bad request"}}, 400)
                if mode == "invalid_json":
                    raw = b"{not json"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                text = "" if mode == "empty" else respond(body.get("messages", []), mock.name)
                think = ""
                tool_calls = None
                msgs = body.get("messages", [])
                last_user = next((m.get("content", "") for m in reversed(msgs) if m.get("role") == "user"), "")
                got_result = any(m.get("role") == "tool" for m in msgs) or "<tool_response" in last_user
                forced = "Do not call tools again" in json.dumps(msgs) or "without calling tools" in json.dumps(msgs)
                if mock.mode == "tool_loop" and not forced:
                    # A stubborn model: always asks for the same search until told to stop.
                    tool_calls = [{"id": f"call_{len(msgs)}", "type": "function", "function": {
                        "name": "web_search", "arguments": json.dumps({"query": "same query"})}}]
                    text = ""
                elif mock.mode == "tool_loop":
                    text = "Végső válasz a saját tudásom alapján (élő adat nem volt elérhető)."
                elif "LIVE_INFO" in json.dumps(msgs) and not got_result:
                    if body.get("tools") and mock.mode != "no_native_tools":
                        tool_calls = [{"id": "call_1", "type": "function", "function": {
                            "name": "web_search", "arguments": json.dumps({"query": "llama.cpp release"})}}]
                        text = ""
                    else:
                        text = '<tool_call>{"name": "web_search", "arguments": {"query": "llama.cpp release"}}</tool_call>'
                elif got_result and "LIVE_INFO" in json.dumps(msgs):
                    text = "A legfrissebb információ szerint a válasz: b9999 [1].\n\nForrások: [1] https://example.org/rel"
                if body.get("tools") and mock.mode == "no_native_tools":
                    return self._json({"error": {"message": "tools param requires --jinja flag"}}, 500)
                if mode == "thinking" and (body.get("chat_template_kwargs") or {}).get("enable_thinking") is not False:
                    think, text = "Let me think about this carefully...", ""
                if mode == "model_error" and not body.get("stream"):
                    return self._json({"error": {"message": "context size exceeded"}})
                if not body.get("stream"):
                    return self._json({
                        "model": mock.name,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": text,
                                                             "reasoning_content": think,
                                                             **({"tool_calls": tool_calls} if tool_calls else {})},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 11, "completion_tokens": max(1, len(text) // 4),
                                  "total_tokens": 11 + max(1, len(text) // 4)},
                    })
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                if tool_calls:
                    tc = tool_calls[0]
                    for part in ({"index": 0, "id": tc["id"], "type": "function",
                                  "function": {"name": "web_search", "arguments": ""}},
                                 {"index": 0, "function": {"arguments": tc["function"]["arguments"][:10]}},
                                 {"index": 0, "function": {"arguments": tc["function"]["arguments"][10:]}}):
                        chunk = {"model": mock.name, "choices": [{"index": 0, "delta": {"tool_calls": [part]},
                                                                  "finish_reason": None}]}
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    end = {"model": mock.name, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
                    self.wfile.write(f"data: {json.dumps(end)}\n\ndata: [DONE]\n\n".encode())
                    self.wfile.flush()
                    return
                pieces = [text[i:i + 12] for i in range(0, len(text), 12)] or [""]
                try:
                    for n, p in enumerate(pieces):
                        if mode == "interrupt" and n == len(pieces) // 2:
                            self.wfile.flush()
                            return  # connection closes without [DONE]
                        if mode == "model_error" and n == 1:
                            self.wfile.write(b'data: {"error": {"message": "slot crashed"}}\n\n')
                            self.wfile.flush()
                            return
                        chunk = {"model": mock.name, "choices": [{"index": 0, "delta": {"content": p},
                                                                  "finish_reason": None}]}
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                        self.wfile.flush()
                        if mock.token_delay:
                            time.sleep(mock.token_delay)
                    final = {"model": mock.name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                             "timings": {"prompt_n": 11, "predicted_n": max(1, len(text) // 4),
                                         "predicted_per_second": 42.0}}
                    self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = ThreadingHTTPServer((host, port), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://{host}:{self.port}"
        self._thread: threading.Thread | None = None

    def start(self) -> "MockLlama":
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Mock llama-server (OpenAI-compatible)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--name", default="mock-model")
    ap.add_argument("--mode", default="ok")
    ap.add_argument("--token-delay", type=float, default=0.02)
    a = ap.parse_args()
    m = MockLlama(a.host, a.port, a.name, a.mode, token_delay=a.token_delay)
    print(f"Mock llama-server: {m.url} (modell: {a.name}, mód: {a.mode})")
    try:
        m.httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
