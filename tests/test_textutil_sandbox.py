import unittest

from llm_arena import sandbox
from llm_arena.textutil import clip, extract_files, extract_json, safe_path


class TextUtilTest(unittest.TestCase):
    def test_extract_json_fenced_and_bare(self):
        self.assertEqual(extract_json('bla\n```json\n{"a": 1,}\n```'), {"a": 1})
        self.assertEqual(extract_json('text {"x": [1, 2]} more'), {"x": [1, 2]})
        self.assertEqual(extract_json('<think>{"no": 1}</think>{"yes": 2}'), {"yes": 2})
        self.assertIsNone(extract_json("no json here"))

    def test_extract_json_braces_inside_strings(self):
        self.assertEqual(extract_json('x {"s": "a } b"} y'), {"s": "a } b"})

    def test_extract_files_formats(self):
        text = ("FILE: app/main.py\n```python\nprint(1)\n```\n"
                "```python utils.py\nX = 1\n```\n"
                "```python\n# helper.py\nY = 2\n```\n")
        files = extract_files(text)
        self.assertEqual(files["app/main.py"], "print(1)\n")
        self.assertEqual(files["utils.py"], "X = 1\n")
        self.assertEqual(files["helper.py"], "Y = 2\n")

    def test_extract_files_default_name_and_skip_json(self):
        files = extract_files("```json\n{}\n```\n```python\nZ = 3\n```", default_name="solution.py")
        self.assertEqual(files, {"solution.py": "Z = 3\n"})

    def test_safe_path(self):
        self.assertIsNone(safe_path("../etc/passwd"))
        self.assertIsNone(safe_path("/abs.py"))
        self.assertEqual(safe_path("./pkg/a.py"), "pkg/a.py")

    def test_clip(self):
        s = clip("a" * 1000 + "END", 200)
        self.assertLess(len(s), 260)
        self.assertTrue(s.endswith("END"))


class SandboxTest(unittest.TestCase):
    CODE = "def add(a, b):\n    return a + b\n"

    def test_pass_and_fail(self):
        tests = ("import unittest, mod\n"
                 "class TestUnit(unittest.TestCase):\n"
                 "    def test_ok(self): self.assertEqual(mod.add(1, 2), 3)\n"
                 "    def test_bad(self): self.assertEqual(mod.add(1, 2), 4)\n"
                 "def test_plain_function():\n    assert mod.add(0, 0) == 0\n")
        r = sandbox.run_tests({"mod.py": self.CODE, "test_unit.py": tests}, timeout=30)
        self.assertEqual(r["summary"]["total"], 3)
        self.assertEqual(r["summary"]["passed"], 2)
        self.assertEqual(r["summary"]["failed"], 1)
        bad = next(t for t in r["tests"] if t["name"] == "test_bad")
        self.assertIn("AssertionError", bad["message"])

    def test_import_error_is_reported(self):
        r = sandbox.run_tests({"test_x.py": "import does_not_exist\n"}, timeout=30)
        self.assertEqual(r["summary"]["error"], 1)
        self.assertFalse(r["summary"]["all_passed"])

    def test_timeout(self):
        tests = ("import unittest\nclass T(unittest.TestCase):\n"
                 "    def test_fast(self): pass\n"
                 "    def test_slow(self):\n        while True: pass\n")
        r = sandbox.run_tests({"test_perf.py": tests}, timeout=3)
        self.assertTrue(r["timed_out"])
        statuses = {t["name"]: t["status"] for t in r["tests"]}
        self.assertEqual(statuses["test_fast"], "passed")
        self.assertEqual(statuses["test_slow"], "timeout")

    def test_no_tests_and_unsafe_names(self):
        r = sandbox.run_tests({"../evil.py": "x", "a.py": "x=1"}, timeout=10)
        self.assertIn("../evil.py", r["rejected_files"])
        self.assertTrue(r["error"])

    def test_categories(self):
        self.assertEqual(sandbox.categorize("test_edge_cases.TestErrorHandling.test_x", "test_edge_cases.py"),
                         "error_handling")
        self.assertEqual(sandbox.categorize("test_unit.TestIntegration.test_x", "test_unit.py"), "integration")
        self.assertEqual(sandbox.categorize("test_unit.TestUnit.test_add", "test_unit.py"), "unit")



class AtomicWriteTest(unittest.TestCase):
    def test_retries_when_target_locked_and_cleans_tmp(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock
        from llm_arena import project

        d = Path(tempfile.mkdtemp())
        target = d / "settings.json"
        real = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(5, "Access is denied")
            return real(src, dst)

        with mock.patch.object(project.os, "replace", side_effect=flaky):
            project._atomic_write(target, '{"a": 1}')
        self.assertEqual(target.read_text("utf-8"), '{"a": 1}')
        self.assertEqual(calls["n"], 3)
        with mock.patch.object(project.os, "replace", side_effect=PermissionError(5, "denied")):
            project._atomic_write(target, '{"b": 2}')  # falls back to in-place write
        self.assertEqual(target.read_text("utf-8"), '{"b": 2}')
        self.assertEqual(list(d.glob("*.tmp")), [])

    def test_concurrent_saves(self):
        import tempfile
        import threading
        from llm_arena.project import ProjectStore

        store = ProjectStore(tempfile.mkdtemp())
        errors = []

        def worker():
            try:
                for _ in range(20):
                    store.save_defaults()
                    store.save()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
