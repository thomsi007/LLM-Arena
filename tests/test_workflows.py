import json
import tempfile
import time
import unittest
import urllib.request

from llm_arena.app import ArenaApp, ConflictError
from llm_arena.mock_server import MockLlama
from llm_arena.server import make_server


def wait(job, timeout=60):
    t0 = time.time()
    while not job.done:
        if time.time() - t0 > timeout:
            raise AssertionError("job did not finish")
        time.sleep(0.05)
    return job


class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = MockLlama(name="mock-a").start()
        cls.b = MockLlama(name="mock-b").start()

    @classmethod
    def tearDownClass(cls):
        cls.a.stop()
        cls.b.stop()

    def setUp(self):
        self.a.mode = self.b.mode = "ok"
        self.app = ArenaApp(tempfile.mkdtemp(), autosave_interval=0)
        self.app.update_config({"llms": {"A": {"base_url": self.a.url, "retries": 0},
                                         "B": {"base_url": self.b.url, "retries": 0}},
                                "settings": {"allow_code_execution": True}})

    def test_arena_parallel_and_error_isolation(self):
        self.b.mode = "http500"
        job = wait(self.app.start_arena("Kérdés?"))
        self.assertEqual(job.status, "done")
        self.assertEqual(job.result["ok"], ["A"])
        self.assertEqual(job.result["failed"], ["B"])
        p = self.app.store.snapshot()
        msgs = {m["id"]: m for m in p["messages"]}
        rnd = p["arena"]["rounds"][0]
        self.assertEqual(msgs[rnd["responses"]["A"]]["status"], "done")
        self.assertEqual(msgs[rnd["responses"]["B"]]["error"]["kind"], "http_error")
        # Structured response object fields
        for key in ("model", "role", "content", "latency", "tokens", "round"):
            self.assertIn(key, msgs[rnd["responses"]["A"]])
        # retry B once the server is healthy
        self.b.mode = "ok"
        wait(self.app.retry_arena(1, "B"))
        p = self.app.store.snapshot()
        m = next(m for m in p["messages"] if m["id"] == p["arena"]["rounds"][0]["responses"]["B"])
        self.assertEqual(m["status"], "done")

    def test_multi_round_history(self):
        wait(self.app.start_arena("első"))
        wait(self.app.start_arena("második"))
        msgs = self.a.requests[-1]["messages"]
        self.assertEqual([m["role"] for m in msgs if m["role"] != "system"], ["user", "assistant", "user"])

    def test_consensus_on_arena_round(self):
        wait(self.app.start_arena("Kérdés?"))
        job = wait(self.app.start_consensus())
        self.assertEqual(job.status, "done", job.error)
        rec = self.app.store.snapshot()["consensus"][-1]
        self.assertTrue(rec["merged"])
        self.assertEqual(set(rec["aggregate"]["table"]), {"correctness", "completeness", "feasibility",
                                                          "security", "performance", "testability"})

    def test_debate_structured_state(self):
        job = wait(self.app.start_debate("Monolit vagy mikroszolgáltatás?", rounds=2))
        self.assertEqual(job.status, "done", job.error)
        d = self.app.store.snapshot()["debate"]
        self.assertEqual([t["phase"] for t in d["turns"]], ["position", "critique", "rebuttal", "counter"])
        self.assertTrue(d["ledger"]["objections"])
        syn = d["synthesis"]
        for key in ("facts", "disputed", "solutions", "open_questions", "conclusion"):
            self.assertIn(key, syn)
        self.assertIn("az üres lista átlaga nem értelmezett", syn["facts"])  # merged from B's review

    def test_debate_resume_after_failure(self):
        self.b.mode = "http500"
        job = wait(self.app.start_debate("Téma", rounds=1))
        self.assertEqual(job.status, "error")
        d = self.app.store.snapshot()["debate"]
        self.assertEqual(d["status"], "error")
        self.assertEqual(len([t for t in d["turns"] if t["status"] == "done"]), 1)
        self.b.mode = "ok"
        job = wait(self.app.start_debate(resume=True))
        self.assertEqual(job.status, "done", job.error)
        self.assertEqual(len(self.app.store.snapshot()["debate"]["turns"]), 2)

    def test_pipeline_end_to_end_with_fix_loop(self):
        job = wait(self.app.start_pipeline("Számológép modul hibakezeléssel."), timeout=120)
        self.assertEqual(job.status, "done", job.error)
        p = self.app.store.snapshot()
        rep = p["testing"]["report"]
        self.assertTrue(rep["all_passed"])
        self.assertGreaterEqual(len(rep["fixed_bugs"]), 1)
        self.assertIn("calc.py", p["code"]["files"])
        self.assertIn("README.md", p["code"]["files"])
        self.assertEqual(p["final"]["source"], "pipeline")
        self.assertTrue(all(s["status"] == "done" for s in p["design"]["stages"].values()))

    def test_design_without_execution_skips_tests(self):
        self.app.update_config({"settings": {"allow_code_execution": False}})
        job = wait(self.app.start_design("Számológép"), timeout=90)
        self.assertEqual(job.status, "done", job.error)
        fix = self.app.store.snapshot()["design"]["stages"]["fix"]
        self.assertIn("kihagyva", fix["text"])

    def test_cancel(self):
        self.a.token_delay = self.b.token_delay = 0.05
        try:
            job = self.app.start_debate("Hosszú vita", rounds=3)
            time.sleep(0.4)
            self.app.jobs.cancel(job.id)
            wait(job)
            self.assertEqual(job.status, "cancelled")
            self.assertEqual(self.app.store.snapshot()["debate"]["status"], "cancelled")
        finally:
            self.a.token_delay = self.b.token_delay = 0

    def test_conflicting_jobs_rejected(self):
        self.a.token_delay = 0.05
        try:
            job = self.app.start_debate("Téma", rounds=1)
            with self.assertRaises(ConflictError):
                self.app.start_debate("Másik")
            self.app.jobs.cancel(job.id)
            wait(job)
        finally:
            self.a.token_delay = 0

    def test_html_reports_all_sections(self):
        from llm_arena import report
        job = wait(self.app.start_pipeline("Számológép <script>alert(1)</script>"), timeout=120)
        self.assertEqual(job.status, "done", job.error)
        p = self.app.store.snapshot()
        p["messages"][0]["content"] = "<img src=x onerror=alert(1)> **bold**\n```python\nx = 1\n```"
        for section in report.SECTIONS:
            doc = report.render(p, section)
            self.assertTrue(doc.startswith("<!doctype html>"), section)
            self.assertNotIn("<script", doc.lower(), section)
            self.assertNotIn("<img", doc, section)
        full = report.render(p, "all")
        for sid in ("s-arena", "s-debate", "s-design", "s-testing", "s-consensus", "s-code", "s-pipeline"):
            self.assertIn(sid, full)
        self.assertIn("Közös ténylista", full)
        self.assertIn("calc.py", full)
        with self.assertRaises(ValueError):
            report.render(p, "nope")

    def test_markdown_renderer(self):
        from llm_arena.report import markdown
        out = markdown("# Cím\n- a\n- **b**\n\n| x | y |\n|---|---|\n| 1 | 2 |\n\n```py\n<tag>\n```")
        self.assertIn("<h3>Cím</h3>", out)
        self.assertIn("<li><strong>b</strong></li>", out)
        self.assertIn("<td>1</td>", out)
        self.assertIn("&lt;tag&gt;", out)
        self.assertNotIn("javascript:", markdown("[x](javascript:alert(1))").replace("[x](javascript:alert(1))", ""))

    def test_save_export_import_roundtrip(self):
        self.app.update_config({"llms": {"A": {"api_key": "secret"}}})
        wait(self.app.start_arena("Kérdés?"))
        self.app.store.save()
        exported = self.app.store.export(include_keys=False)
        self.assertEqual(exported["llms"]["A"]["api_key"], "")
        pid = exported["id"]
        self.app.store.new("masik")
        self.assertEqual(self.app.store.snapshot()["arena"]["rounds"], [])
        self.app.store.import_project(exported)
        snap = self.app.store.snapshot()
        self.assertEqual(snap["id"], pid)
        self.assertEqual(len(snap["arena"]["rounds"]), 1)
        self.assertEqual(snap["llms"]["A"]["api_key"], "secret")  # local key kept
        self.assertTrue(any(p["id"] == pid for p in self.app.store.list_saved()))
        self.app.store.load(pid)
        self.assertIn("Kérdés?", self.app.conversation_markdown())


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = MockLlama(name="srv-model").start()
        cls.app = ArenaApp(tempfile.mkdtemp(), autosave_interval=0)
        cls.srv = make_server(cls.app, "127.0.0.1", 0)
        import threading
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.mock.stop()

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(r, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_api_flow_with_sse(self):
        st, _ = self.req("POST", "/api/config", {"llms": {"A": {"base_url": self.mock.url, "api_key": "k"},
                                                          "B": {"base_url": self.mock.url}}})
        self.assertEqual(st, 200)
        st, body = self.req("GET", "/api/config")
        self.assertEqual(json.loads(body)["llms"]["A"]["api_key"], "***")
        st, body = self.req("POST", "/api/llm/A/test", {})
        self.assertTrue(json.loads(body)["result"]["ok"])
        st, body = self.req("POST", "/api/arena/run", {"prompt": "Hello"})
        job = json.loads(body)["job"]
        st, body = self.req("GET", f"/api/jobs/{job['id']}/events")
        events = [json.loads(line[5:]) for line in body.decode().splitlines() if line.startswith("data:")]
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "job_start")
        self.assertEqual(types[-1], "job_end")
        self.assertIn("token", types)
        st, body = self.req("GET", "/api/project")
        self.assertEqual(len(json.loads(body)["project"]["arena"]["rounds"]), 1)
        st, body = self.req("GET", "/api/export/conversation?format=md")
        self.assertIn(b"Hello", body)

    def test_errors_and_static(self):
        self.assertEqual(self.req("POST", "/api/arena/run", {"prompt": ""})[0], 200)  # job fails, server fine
        self.assertEqual(self.req("GET", "/api/nope")[0], 404)
        self.assertEqual(self.req("GET", "/../../etc/passwd")[0], 404)
        self.assertEqual(self.req("GET", "/api/code/download")[0], 400)
        st, body = self.req("GET", "/api/export/html?section=arena")
        self.assertEqual(st, 200)
        self.assertIn(b"<!doctype html>", body)
        self.assertEqual(self.req("GET", "/api/export/html?section=bogus")[0], 400)
        st, body = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"LLM Ar", body)

    def test_static_js_mime_type_ignores_system_registry(self):
        import mimetypes
        mimetypes.add_type("text/plain", ".js")  # what a broken Windows registry reports
        try:
            with self.opener.open(self.base + "/js/app.js", timeout=10) as resp:
                self.assertTrue(resp.headers["Content-Type"].startswith("text/javascript"))
        finally:
            mimetypes.add_type("text/javascript", ".js")


if __name__ == "__main__":
    unittest.main()
