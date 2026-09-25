import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from llm_arena import tools
from llm_arena.app import ArenaApp
from llm_arena.mock_server import MockLlama
from llm_arena.tools import web
from llm_arena.workflows import common

DDG_HTML = """
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title"><a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Fggml-org%2Fllama.cpp%2Freleases&amp;rut=x">llama.cpp <b>releases</b></a></h2>
  <a class="result__snippet" href="#">Latest release <b>b9999</b> &amp; notes</a>
</div>
<div class="result result--ad"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a></div>
<div class="result results_links"><a class="result__a" href="https://example.org/page">Example</a>
  <div class="result__snippet">Snippet two</div></div>
"""

DDG_LITE = """<table><tr><td><a rel="nofollow" href="https://a.example/x" class='result-link'>A title</a></td></tr>
<tr><td class='result-snippet'>A snippet</td></tr></table>"""


class ParserTest(unittest.TestCase):
    def test_ddg_html(self):
        res = web.parse_ddg_html(DDG_HTML)
        self.assertEqual(res[0]["url"], "https://github.com/ggml-org/llama.cpp/releases")
        self.assertEqual(res[0]["title"], "llama.cpp releases")
        self.assertEqual(res[0]["snippet"], "Latest release b9999 & notes")
        self.assertEqual([r["url"] for r in res], ["https://github.com/ggml-org/llama.cpp/releases",
                                                  "https://example.org/page"])

    def test_ddg_lite(self):
        self.assertEqual(web.parse_ddg_lite(DDG_LITE), [{"title": "A title", "url": "https://a.example/x",
                                                         "snippet": "A snippet"}])

    def test_html_to_text(self):
        title, text = web.html_to_text("<html><head><title>T</title><script>var x=1</script></head>"
                                       "<body><nav>menu</nav><h1>Head</h1><p>Hello <b>world</b></p></body></html>")
        self.assertEqual(title, "T")
        self.assertIn("Hello world", text)
        self.assertNotIn("var x", text)
        self.assertNotIn("menu", text)

    def test_text_protocol_parse(self):
        calls, cleaned = tools.parse_text_calls(
            'ok <tool_call>{"name": "web_search", "arguments": {"query": "x"}}</tool_call>'
            '<tool_call>{"name": "rm_rf", "arguments": {}}</tool_call>')
        self.assertEqual([(c["name"], c["arguments"]) for c in calls], [("web_search", {"query": "x"})])
        self.assertEqual(cleaned, "ok")

    def test_native_normalize(self):
        calls = tools.normalize_native([{"id": "c1", "function": {"name": "fetch_url",
                                                                  "arguments": '{"url": "https://x.org"}'}}])
        self.assertEqual(calls, [{"id": "c1", "name": "fetch_url", "arguments": {"url": "https://x.org"}}])


class _Page(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        body = b"<html><title>Local</title><body><p>secret intranet data</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FetchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_private_address_blocked_by_default(self):
        r = tools.execute("fetch_url", {"url": self.url + "/page"}, {"allow_private": False})
        self.assertFalse(r["ok"])
        self.assertIn("Belső hálózati", r["error"])

    def test_private_allowed_and_redirect_followed(self):
        with mock.patch.dict("os.environ", {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
            r = tools.execute("fetch_url", {"url": self.url + "/redirect"}, {"allow_private": True})
        self.assertTrue(r["ok"], r)
        self.assertIn("secret intranet data", r["content"])
        self.assertEqual(r["sources"][0]["title"], "Local")

    def test_bad_scheme(self):
        r = tools.execute("fetch_url", {"url": "file:///etc/passwd"}, {})
        self.assertFalse(r["ok"])

    def test_unknown_tool(self):
        self.assertFalse(tools.execute("shell", {}, {})["ok"])


FAKE_RESULTS = [{"title": "llama.cpp releases", "url": "https://example.org/rel", "snippet": "b9999"}]


def wait(job, timeout=30):
    t0 = time.time()
    while not job.done:
        if time.time() - t0 > timeout:
            raise AssertionError("timeout")
        time.sleep(0.05)
    return job


class ToolLoopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = MockLlama(name="tool-a").start()
        cls.b = MockLlama(name="tool-b").start()

    @classmethod
    def tearDownClass(cls):
        cls.a.stop()
        cls.b.stop()

    def setUp(self):
        common._NATIVE_TOOLS_UNSUPPORTED.clear()
        self.a.mode = self.b.mode = "ok"
        self.app = ArenaApp(tempfile.mkdtemp(), autosave_interval=0)
        self.app.update_config({"llms": {"A": {"base_url": self.a.url, "retries": 0},
                                         "B": {"base_url": self.b.url, "retries": 0, "stream": False}}})
        patcher = mock.patch.object(web, "search", return_value=FAKE_RESULTS)
        self.search = patcher.start()
        self.addCleanup(patcher.stop)

    def arena_msgs(self):
        p = self.app.store.snapshot()
        msgs = {m["id"]: m for m in p["messages"]}
        rnd = p["arena"]["rounds"][-1]["responses"]
        return msgs[rnd["A"]], msgs[rnd["B"]]

    def test_native_tool_calls_stream_and_plain(self):
        job = wait(self.app.start_arena("LIVE_INFO: mi a legújabb llama.cpp verzió?"))
        self.assertEqual(job.status, "done", job.error)
        for m in self.arena_msgs():  # A streams, B does not
            self.assertEqual(m["status"], "done")
            self.assertIn("b9999", m["content"])
            self.assertEqual(len(m["tool_calls"]), 1)
            tc = m["tool_calls"][0]
            self.assertEqual((tc["name"], tc["status"], tc["arguments"]), ("web_search", "done", {"query": "llama.cpp release"}))
            self.assertEqual(tc["sources"][0]["url"], "https://example.org/rel")
        self.assertTrue(any(r.get("tools") for r in self.a.requests))
        self.assertTrue(any(m.get("role") == "tool" for m in self.a.requests[-1]["messages"]))
        self.assertIn("tool", [e["type"] for e in job.events])

    def test_fallback_to_text_protocol(self):
        self.a.mode = "no_native_tools"
        job = wait(self.app.start_arena("LIVE_INFO kérdés"))
        self.assertEqual(job.status, "done", job.error)
        a, _ = self.arena_msgs()
        self.assertIn("b9999", a["content"])
        self.assertNotIn("<tool_call>", a["content"])
        self.assertEqual(len(a["tool_calls"]), 1)
        self.assertTrue(common._NATIVE_TOOLS_UNSUPPORTED.get(self.a.url))
        self.assertIn("<tool_call>", self.a.requests[-1]["messages"][0]["content"])  # protocol in system prompt

    def test_text_mode_setting(self):
        self.app.update_config({"settings": {"web_tool_mode": "text"}})
        wait(self.app.start_arena("LIVE_INFO kérdés"))
        a, _ = self.arena_msgs()
        self.assertIn("b9999", a["content"])
        self.assertFalse(any(r.get("tools") for r in self.a.requests[-2:]))

    def test_disabled_or_zero_budget_sends_no_tools(self):
        for settings in ({"web_enabled": False}, {"web_enabled": True, "web_max_calls": 0}):
            self.app.update_config({"settings": settings})
            n = len(self.a.requests)
            wait(self.app.start_arena("Sima kérdés"))
            self.assertFalse(any(r.get("tools") for r in self.a.requests[n:]), settings)
        self.search.assert_not_called()

    def test_search_failure_does_not_break_answer(self):
        self.search.side_effect = web.WebError("offline")
        job = wait(self.app.start_arena("LIVE_INFO kérdés"))
        self.assertEqual(job.status, "done", job.error)
        a, _ = self.arena_msgs()
        self.assertEqual(a["tool_calls"][0]["status"], "error")
        self.assertTrue(a["content"])

    def test_brave_key_masked(self):
        self.app.update_config({"settings": {"web_brave_api_key": "secret"}})
        self.assertEqual(self.app.public_config()["settings"]["web_brave_api_key"], "***")
        self.app.update_config({"settings": {"web_brave_api_key": "***"}})
        self.assertEqual(self.app.store.settings()["web_brave_api_key"], "secret")
        self.assertEqual(self.app.store.export()["settings"]["web_brave_api_key"], "")


if __name__ == "__main__":
    unittest.main()
