import tempfile
import unittest

from llm_arena import selftest


class SelfTestTest(unittest.TestCase):
    def test_quick_selftest_passes_functional_checks(self):
        rep = selftest.run({"web_enabled": False}, {}, tempfile.mkdtemp(), quick=True)
        by_name = {r["name"]: r for r in rep["results"]}
        for name in ("Programmodulok betöltése", "Felület fájljai", "Aréna (párhuzamos válasz)", "Közös döntés",
                     "Vita (1 kör + összegzés)", "Exportok", "Fájlcsatolás", "Projekt mentése / betöltése",
                     "Tesztfuttató (sandbox)"):
            self.assertTrue(by_name[name]["ok"], by_name[name])
        # unconfigured own endpoints fail with a clear, actionable message
        own = by_name["Saját LLM A kapcsolat"]
        self.assertFalse(own["ok"])
        self.assertIn("llama-server", own["hint"])
        self.assertTrue(rep["version"]["version"])


if __name__ == "__main__":
    unittest.main()
