import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from llm_arena.app import ArenaApp
from llm_arena.tools import searxng as sx
from llm_arena.tools import web


class FakeSearx(BaseHTTPRequestHandler):
    json_enabled = True

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/config"):
            return self._send(200, {"engines": [], "instance_name": "Fake SearXNG"})
        if self.path.startswith("/search"):
            if not type(self).json_enabled:
                return self._send(403, {"error": "forbidden"})
            return self._send(200, {"results": [{"title": "llama.cpp", "url": "https://github.com/ggml-org/llama.cpp",
                                                 "content": "LLM inference in C/C++"}]})
        self._send(404, {})


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class ProbeDetectTest(unittest.TestCase):
    def test_probe_json_and_forbidden(self):
        class NoJson(FakeSearx):
            json_enabled = False
        srv, url = serve(FakeSearx)
        srv2, url2 = serve(NoJson)
        try:
            p = sx.probe(url)
            self.assertTrue(p["searxng"] and p["json"])
            self.assertEqual(p["instance_name"], "Fake SearXNG")
            q = sx.probe(url2)
            self.assertTrue(q["searxng"])
            self.assertFalse(q["json"])
            self.assertIn("JSON", q["error"])
            self.assertIn(url, [f["url"] for f in sx.detect([url])])
        finally:
            srv.shutdown()
            srv2.shutdown()

    def test_probe_non_searxng_and_closed_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(sx.probe(f"http://127.0.0.1:{port}")["reachable"])

    def test_ensure_config_writes_json_format_and_secret(self):
        d = tempfile.mkdtemp()
        path = sx.ensure_config(d) / "settings.yml"
        text = path.read_text()
        self.assertIn("- json", text)
        self.assertIn("limiter: false", text)
        self.assertNotIn("ultrasecretkey", text)
        path.write_text(sx.MANAGED_MARK + "\nuse_default_settings: true\n")  # broken managed file is repaired
        self.assertIn("- json", (sx.ensure_config(d) / "settings.yml").read_text())
        path.write_text("# user file\nsearch:\n  formats: [html]\n")      # user file is left alone
        self.assertEqual((sx.ensure_config(d) / "settings.yml").read_text(), "# user file\nsearch:\n  formats: [html]\n")


class DockerFlowTest(unittest.TestCase):
    def test_docker_missing_gives_clear_error(self):
        with mock.patch.object(sx.shutil, "which", return_value=None):
            with self.assertRaises(sx.SearxError) as cm:
                sx.start_docker(tempfile.mkdtemp(), 8888, lambda m: None)
        self.assertIn("Docker", cm.exception.error["message"])
        self.assertIn("docker.com", cm.exception.error["hint"])

    def test_daemon_not_running(self):
        done = subprocess.CompletedProcess([], 1, stdout="", stderr="Cannot connect to the Docker daemon")
        with mock.patch.object(sx.shutil, "which", return_value="/usr/bin/docker"), \
                mock.patch.object(sx, "_run", return_value=done):
            st = sx.docker_status()
        self.assertFalse(st["ok"])
        self.assertIn("Docker Desktop", st["hint"])

    def test_crash_loop_detected_with_logs(self):
        srv, url = serve(FakeSearx)  # never used: port closed below
        srv.shutdown()
        srv.server_close()
        with mock.patch.object(sx, "container_state", return_value="restarting"), \
                mock.patch.object(sx, "container_logs", return_value="RuntimeError: Address family not supported"), \
                mock.patch.object(sx.time, "sleep"):
            with self.assertRaises(sx.SearxError) as cm:
                sx.wait_ready(url, 30, lambda m: None, lambda: False, container="x")
        self.assertIn("Address family", cm.exception.error["message"])
        self.assertIn("RuntimeError", cm.exception.error["detail"])


class AutoPortAndDockerDesktopTest(unittest.TestCase):
    def test_busy_port_moves_to_next_free(self):
        srv, url = serve(OtherApp)
        busy = srv.server_address[1]
        try:
            created = {}

            def fake_create(cfg, port, image):
                created["port"] = port
                return subprocess.CompletedProcess([], 0, "id", "")

            with mock.patch.object(sx, "ensure_docker_running", return_value={"ok": True}), \
                    mock.patch.object(sx, "container_state", return_value=None), \
                    mock.patch.object(sx, "_image_present", return_value=True), \
                    mock.patch.object(sx, "_create_container", side_effect=fake_create), \
                    mock.patch.object(sx, "wait_ready", side_effect=lambda u, *a, **k: {"url": u}):
                url = sx.start_docker(tempfile.mkdtemp(), busy, lambda m: None)
            self.assertNotEqual(created["port"], busy)
            self.assertEqual(url, f"http://127.0.0.1:{created['port']}")
        finally:
            srv.shutdown()

    def test_busy_port_with_working_searxng_is_taken_over(self):
        srv, url = serve(FakeSearx)
        try:
            with mock.patch.object(sx, "ensure_docker_running", return_value={"ok": True}), \
                    mock.patch.object(sx, "container_state", return_value=None):
                self.assertEqual(sx.start_docker(tempfile.mkdtemp(), srv.server_address[1], lambda m: None), url)
        finally:
            srv.shutdown()

    def test_docker_desktop_is_started_when_engine_down(self):
        states = [{"ok": False, "installed": True, "message": "nem fut"}] * 2 + [{"ok": True, "version": "29"}]
        msgs = []
        with mock.patch.object(sx, "docker_status", side_effect=states), \
                mock.patch.object(sx, "_docker_desktop_launchers", return_value=[["docker-desktop"]]), \
                mock.patch.object(sx.subprocess, "Popen") as popen, mock.patch.object(sx.time, "sleep"):
            st = sx.ensure_docker_running(msgs.append)
        self.assertTrue(st["ok"])
        popen.assert_called_once()
        self.assertTrue(any("Docker Desktop" in m for m in msgs))


class OtherApp(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"another program")


class AppIntegrationTest(unittest.TestCase):
    def test_detect_takes_over_settings_and_search_works(self):
        srv, url = serve(FakeSearx)
        try:
            app = ArenaApp(tempfile.mkdtemp(), autosave_interval=0)
            app.update_config({"settings": {"web_searxng_url": url, "web_backend": "duckduckgo"}})
            r = app.detect_searxng()
            self.assertEqual(r["applied"], url)
            s = app.store.settings()
            self.assertEqual((s["web_backend"], s["web_searxng_url"]), ("searxng", url))
            web.cache_clear()
            from llm_arena import tools
            res = tools.execute("web_search", {"query": "llama.cpp"}, tools.web_config(s))
            self.assertTrue(res["ok"], res)
            self.assertIn("SearXNG", res["summary"])
        finally:
            srv.shutdown()

    def test_start_job_applies_settings(self):
        app = ArenaApp(tempfile.mkdtemp(), autosave_interval=0)
        with mock.patch.object(sx, "start", return_value={"url": "http://127.0.0.1:18888", "mode": "docker"}):
            job = app.start_searxng()
            for _ in range(100):
                if job.done:
                    break
                time.sleep(0.02)
        self.assertEqual(job.status, "done", job.error)
        s = app.store.settings()
        self.assertEqual((s["web_backend"], s["web_searxng_url"]), ("searxng", "http://127.0.0.1:18888"))

    def test_start_job_error_is_readable(self):
        app = ArenaApp(tempfile.mkdtemp(), autosave_interval=0)
        with mock.patch.object(sx, "start", side_effect=sx.SearxError("A Docker nincs telepítve.", "Telepítsd…")):
            job = app.start_searxng()
            for _ in range(100):
                if job.done:
                    break
                time.sleep(0.02)
        self.assertEqual(job.status, "error")
        self.assertEqual(job.error["label"], "SearXNG hiba")
        self.assertEqual(job.error["hint"], "Telepítsd…")


@unittest.skipUnless(os.environ.get("LLM_ARENA_DOCKER_TESTS") == "1" and shutil.which("docker"),
                     "set LLM_ARENA_DOCKER_TESTS=1 to run the real Docker test")
class RealDockerTest(unittest.TestCase):
    def test_start_real_container(self):
        subprocess.run(["docker", "rm", "-f", sx.CONTAINER], capture_output=True)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        try:
            url = sx.start_docker(tempfile.mkdtemp(), port, lambda m: None)
            p = sx.probe(url)
            self.assertTrue(p["searxng"] and p["json"])
            self.assertEqual(sx.start_docker(tempfile.mkdtemp(), port, lambda m: None), url)  # idempotent
            self.assertTrue(sx.stop_docker())
            self.assertEqual(sx.container_port(), port)  # known even while stopped
            # the old port gets taken by another program -> recreated on a free port
            other = ThreadingHTTPServer(("127.0.0.1", port), OtherApp)
            threading.Thread(target=other.serve_forever, daemon=True).start()
            try:
                url2 = sx.start_docker(tempfile.mkdtemp(), port, lambda m: None)
                self.assertNotEqual(url2, url)
                self.assertTrue(sx.probe(url2)["json"])
            finally:
                other.shutdown()
        finally:
            subprocess.run(["docker", "rm", "-f", sx.CONTAINER], capture_output=True)


if __name__ == "__main__":
    unittest.main()
