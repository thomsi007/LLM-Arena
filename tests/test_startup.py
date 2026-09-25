import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class OtherApp(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        body = b"<html><title>LLM-Arena (other app)</title></html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_arena(default_port: int, data: str, *args: str) -> subprocess.Popen:
    code = (f"import sys, webbrowser; webbrowser.open = lambda *a, **k: None; "
            f"import llm_arena.__main__ as m; m.DEFAULT_PORT = {default_port}; "
            f"sys.exit(m.main(['--data-dir', {data!r}, *{list(args)!r}]))")
    return subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)


def wait_health(port: int, timeout: float = 15) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with OPENER.open(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
                return json.loads(r.read())
        except OSError:
            time.sleep(0.2)
    raise AssertionError(f"no LLM Arena on port {port}")


class StartupPortTest(unittest.TestCase):
    def test_busy_port_moves_to_next_and_existing_arena_is_reused(self):
        base = free_port()
        other = ThreadingHTTPServer(("127.0.0.1", base), OtherApp)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        data = tempfile.mkdtemp()
        p1 = start_arena(base, data)
        try:
            health = wait_health(base + 1)
            self.assertIn("version", health)
            # the other app still owns the original port – we did not steal / share it
            with OPENER.open(f"http://127.0.0.1:{base}/", timeout=2) as r:
                self.assertIn(b"other app", r.read())
            # a second start finds the running arena and just opens it
            p2 = start_arena(base, data)
            out, _ = p2.communicate(timeout=20)
            self.assertEqual(p2.returncode, 0, out)
            self.assertIn("MÁSIK program", out)
            self.assertIn("már fut egy LLM Aréna", out)
        finally:
            p1.kill()
            other.shutdown()
            other.server_close()

    def test_explicit_busy_port_fails_clearly(self):
        base = free_port()
        other = ThreadingHTTPServer(("127.0.0.1", base), OtherApp)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        try:
            p = start_arena(base, tempfile.mkdtemp(), "--port", str(base))
            out, _ = p.communicate(timeout=20)
            self.assertEqual(p.returncode, 1)
            self.assertIn(f"--port {base + 1}", out)
        finally:
            other.shutdown()
            other.server_close()

    def test_service_worker_kill_switch_served(self):
        port = free_port()
        p = start_arena(port, tempfile.mkdtemp())
        try:
            wait_health(port)
            with OPENER.open(f"http://127.0.0.1:{port}/sw.js", timeout=3) as r:
                self.assertIn("javascript", r.headers["Content-Type"])
                self.assertIn(b"unregister", r.read())
        finally:
            p.kill()


if __name__ == "__main__":
    unittest.main()
