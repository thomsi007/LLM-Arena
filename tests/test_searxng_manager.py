"""Tests for the standalone SearXNG manager (searxng/manager.py) – no Docker or network needed."""

from __future__ import annotations

import importlib.util
import json
import socket
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sx_manager", ROOT / "searxng" / "manager.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)  # type: ignore[union-attr]


class ManagerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [mock.patch.object(m, k, v) for k, v in {
            "HERE": d, "CONFIG_FILE": d / "config.json", "DATA_DIR": d / "data",
            "SETTINGS_FILE": d / "data" / "settings.yml", "NATIVE_DIR": d / "native", "LOG_DIR": d / "logs",
            "SRC_DIR": d / "native" / "src", "VENV_DIR": d / "native" / "venv", "PID_FILE": d / "data" / "pid",
        }.items()]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class ConfigTests(ManagerBase):
    def test_defaults_and_secret_persisted(self):
        cfg = m.load_config()
        self.assertEqual(cfg["port"], 8888)
        self.assertTrue(cfg["secret_key"])
        self.assertEqual(m.load_config()["secret_key"], cfg["secret_key"])

    def test_validate(self):
        cfg = m.load_config()
        out = m.validate({"port": "9001", "engines": {"youtube": True, "bogus": True}, "safe_search": 7}, cfg)
        self.assertEqual(out["port"], 9001)
        self.assertTrue(out["engines"]["youtube"])
        self.assertNotIn("bogus", out["engines"])
        self.assertEqual(out["safe_search"], 2)
        for bad in ({"port": 80}, {"port": "abc"}, {"mode": "x"}, {"bind": "8.8.8.8"}, {"instance_name": 'a"b'}):
            with self.assertRaises(m.Failure, msg=bad):
                m.validate(bad, cfg)

    def test_settings_yaml(self):
        cfg = m.load_config()
        cfg["engines"]["google"] = False
        native = m.settings_yaml(cfg)
        self.assertIn("- json", native)
        self.assertIn(f"port: {cfg['port']}", native)
        self.assertIn('bind_address: "127.0.0.1"', native)
        self.assertIn(m.MANAGED_MARK, native)
        self.assertRegex(native, r"name: google\s+disabled: true")
        docker = m.settings_yaml(cfg, port_in_container=True)
        self.assertIn("port: 8080", docker)
        self.assertIn('bind_address: "0.0.0.0"', docker)

    def test_custom_settings_not_overwritten(self):
        cfg = m.load_config()
        m.write_settings(cfg, "native")
        self.assertIn(m.MANAGED_MARK, m.SETTINGS_FILE.read_text("utf-8"))
        m.SETTINGS_FILE.write_text("use_default_settings: true\n# mine\n", "utf-8")
        cfg["custom_settings"] = True
        m.write_settings(cfg, "native")
        self.assertIn("# mine", m.SETTINGS_FILE.read_text("utf-8"))

    def test_find_free_port_skips_busy(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen()
        busy = s.getsockname()[1]
        try:
            self.assertFalse(m.port_free(busy))
            self.assertNotEqual(m.find_free_port(busy), busy)
        finally:
            s.close()


class StartTests(ManagerBase):
    def test_auto_falls_back_to_native_when_docker_fails(self):
        with mock.patch.object(m, "docker_state", return_value={"installed": True, "running": False}), \
                mock.patch.object(m, "docker_start", side_effect=m.Failure("A Docker motor nem fut.")), \
                mock.patch.object(m, "native_start", return_value="http://127.0.0.1:8888") as ns:
            self.assertEqual(m.start(), "http://127.0.0.1:8888")
            ns.assert_called_once()

    def test_explicit_docker_mode_does_not_fall_back(self):
        m.save_config(m.validate({"mode": "docker"}, m.load_config()))
        with mock.patch.object(m, "docker_start", side_effect=m.Failure("nincs")), \
                mock.patch.object(m, "native_start") as ns:
            with self.assertRaises(m.Failure):
                m.start()
            ns.assert_not_called()

    def test_docker_start_uses_next_free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen()
        busy = s.getsockname()[1]
        calls = []

        def fake_run(args, timeout=60, cwd=None):
            calls.append(args)
            return mock.Mock(returncode=0, stdout="", stderr="")
        try:
            m.save_config(m.validate({"port": busy}, m.load_config()))
            with mock.patch.object(m, "docker_install"), mock.patch.object(m, "container_state", return_value=None), \
                    mock.patch.object(m, "run", side_effect=fake_run), \
                    mock.patch.object(m, "wait_ready", side_effect=lambda u, **k: u):
                url = m.docker_start(m.load_config())
        finally:
            s.close()
        self.assertNotEqual(url, f"http://127.0.0.1:{busy}")
        new_port = int(url.rsplit(":", 1)[1])
        self.assertEqual(m.load_config()["port"], new_port)
        run_cmd = next(c for c in calls if c[:2] == ["docker", "run"])
        self.assertIn(f"127.0.0.1:{new_port}:8080", run_cmd)
        self.assertIn("GRANIAN_HOST=0.0.0.0", run_cmd)

    def test_changed_bind_recreates_container(self):
        cfg = m.validate({"bind": "0.0.0.0", "port": 18888}, m.load_config())
        calls = []

        def fake_run(args, timeout=60, cwd=None):
            calls.append(args)
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(m, "docker_install"), \
                mock.patch.object(m, "container_state", side_effect=["running", None]), \
                mock.patch.object(m, "container_binding", return_value=("127.0.0.1", 18888)), \
                mock.patch.object(m, "port_free", return_value=True), \
                mock.patch.object(m, "run", side_effect=fake_run), \
                mock.patch.object(m, "wait_ready", side_effect=lambda u, **k: u):
            m.docker_start(cfg)
        self.assertIn(["docker", "rm", "-f", m.CONTAINER], calls)
        self.assertTrue(any("0.0.0.0:18888:8080" in c for c in calls if c[:2] == ["docker", "run"]))


class GuiTests(ManagerBase):
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), m.Gui)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, path, body=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers=h)
        try:
            with self.op.open(r, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_page_and_health(self):
        code, text = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn("SearXNG kezelő", text)
        self.assertEqual(json.loads(self.req("/api/health")[1])["app"], "searxng-manager")

    def test_status(self):
        with mock.patch.object(m, "docker_state", return_value={"installed": False, "running": False,
                                                                "message": "nincs"}):
            d = json.loads(self.req("/api/status")[1])
        self.assertTrue(d["ok"])
        self.assertFalse(d["running"])
        self.assertNotIn("secret_key", d["config"])
        self.assertEqual(d["mode_resolved"], "native")

    def test_save_config_writes_settings(self):
        with mock.patch.object(m, "docker_state", return_value={"installed": False, "running": False}):
            code, text = self.req("/api/config", {"instance_name": "Teszt", "engines": {"youtube": True}})
        self.assertEqual(code, 200, text)
        yml = m.SETTINGS_FILE.read_text("utf-8")
        self.assertIn('instance_name: "Teszt"', yml)
        self.assertRegex(yml, r"name: youtube\s+disabled: false")

    def test_invalid_config_is_readable_error(self):
        code, text = self.req("/api/config", {"port": "abc"})
        self.assertEqual(code, 400)
        self.assertIn("Érvénytelen", json.loads(text)["error"])

    def test_cross_site_post_rejected(self):
        code, _ = self.req("/api/stop", {}, {"Origin": "http://evil.example"})
        self.assertEqual(code, 403)
        code, _ = self.req("/api/stop", {}, {"Content-Type": "text/plain"})
        self.assertEqual(code, 403)


class ArenaIntegrationTests(unittest.TestCase):
    def test_arena_delegates_to_manager(self):
        from llm_arena.tools import searxng as sx
        self.assertTrue(sx.manager_available())
        proc = mock.Mock(returncode=0)
        proc.stdout.read.return_value = '{"ok": true, "url": "http://127.0.0.1:18889"}\n'
        proc.stderr = iter(["Indítás – mód: docker\n", "A SearXNG fut: http://127.0.0.1:18889\n"])
        seen = []
        with mock.patch.object(sx.subprocess, "Popen", return_value=proc) as popen:
            res = sx.start("/tmp", 18888, "docker", seen.append)
        self.assertEqual(res["url"], "http://127.0.0.1:18889")
        args = popen.call_args[0][0]
        self.assertIn("--json", args)
        self.assertEqual(args[args.index("--port") + 1], "18888")
        self.assertIn("A SearXNG fut: http://127.0.0.1:18889", seen)

    def test_manager_error_is_passed_through(self):
        from llm_arena.tools import searxng as sx
        proc = mock.Mock(returncode=1)
        proc.stdout.read.return_value = '{"ok": false, "error": "A Docker motor nem fut.", "hint": "Indítsd el."}'
        proc.stderr = iter([])
        with mock.patch.object(sx.subprocess, "Popen", return_value=proc):
            with self.assertRaises(sx.SearxError) as cm:
                sx.start("/tmp", 8888, "docker", lambda _: None)
        self.assertIn("Docker motor", str(cm.exception))
        self.assertEqual(cm.exception.error["hint"], "Indítsd el.")


if __name__ == "__main__":
    unittest.main()
