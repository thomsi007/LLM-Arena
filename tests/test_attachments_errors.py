import base64
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile

from llm_arena import attachments as att
from llm_arena.app import ArenaApp
from llm_arena.errors import describe_exception
from llm_arena.mock_server import MockLlama
from llm_arena.server import make_server
from llm_arena.workflows.common import StepFailed


def docx_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                   f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p><w:p><w:r><w:t>Második bekezdés</w:t></w:r></w:p>'
                   '</w:body></w:document>')
    return buf.getvalue()


def xlsx_bytes() -> bytes:
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns}><sheets><sheet name="Adatok" sheetId="1"/></sheets></workbook>')
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}><si><t>Név</t></si><si><t>Érték</t></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<worksheet {ns}><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                   f'<row r="2"><c r="A2" t="inlineStr"><is><t>alma</t></is></c><c r="C2"><v>42</v></c></row></sheetData></worksheet>')
    return buf.getvalue()


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class IngestTest(unittest.TestCase):
    def test_text_utf8_and_cp1250(self):
        self.assertEqual(att.ingest("a.txt", "árvíztűrő".encode())["text"], "árvíztűrő")
        self.assertEqual(att.ingest("b.csv", "őű;x".encode("cp1250"))["text"], "őű;x")

    def test_docx_xlsx(self):
        d = att.ingest("spec.docx", docx_bytes("Követelmény egy"))
        self.assertIn("Követelmény egy", d["text"])
        self.assertIn("Második bekezdés", d["text"])
        x = att.ingest("t.xlsx", xlsx_bytes())
        self.assertIn("Adatok", x["text"])
        self.assertIn("Név\tÉrték", x["text"])
        self.assertIn("alma\t\t42", x["text"])

    def test_image(self):
        r = att.ingest("kep.png", PNG)
        self.assertEqual((r["kind"], r["mime"]), ("image", "image/png"))
        parts = att.with_attachments("Mi van a képen?", [r], 5000)
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_clear_errors(self):
        for name, raw, needle in (("ures.txt", b"", "üres"), ("regi.doc", b"x" * 10, "régi Office"),
                                  ("film.mp4", b"x" * 10, "nem értelmezhető"),
                                  ("serult.docx", b"not a zip", "sérült"),
                                  ("bin.dat", b"\x00\x01\x02" * 100, "Bináris")):
            with self.assertRaises(att.AttachmentError) as cm:
                att.ingest(name, raw)
            self.assertIn(needle, cm.exception.error["message"], name)
            self.assertTrue(cm.exception.error["hint"])

    def test_pdf_without_pypdf_explains(self):
        try:
            import pypdf  # noqa: F401
            self.skipTest("pypdf installed")
        except ImportError:
            pass
        with self.assertRaises(att.AttachmentError) as cm:
            att.ingest("a.pdf", b"%PDF-1.4 ...")
        self.assertIn("pip install pypdf", cm.exception.error["message"])

    def test_context_block_budget(self):
        recs = [att.ingest("a.txt", b"A" * 5000), att.ingest("b.txt", b"B" * 5000)]
        block = att.context_block(recs, 3000)
        self.assertIn("FILE: a.txt", block)
        self.assertIn("FILE: b.txt", block)
        self.assertLess(len(block), 4000)


class ErrorDescriptionTest(unittest.TestCase):
    def test_step_failed_keeps_label_and_gets_hint(self):
        e = StepFailed("analysis", {"kind": "analysis_failed", "label": "Elemzés sikertelen", "message": "x"})
        d = describe_exception(e)
        self.assertEqual(d["label"], "Elemzés sikertelen")
        self.assertTrue(d["hint"])
        self.assertEqual(d["stage"], "analysis")

    def test_generic_exceptions(self):
        self.assertEqual(describe_exception(ValueError("rossz"))["label"], "Hibás bemenet")
        self.assertEqual(describe_exception(PermissionError("x"))["kind"], "permission")
        internal = describe_exception(RuntimeError("boom"))
        self.assertEqual(internal["kind"], "internal")
        self.assertIn("RuntimeError", internal["detail"])


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = MockLlama(name="att-model").start()
        cls.app = ArenaApp(tempfile.mkdtemp(), autosave_interval=0)
        cls.app.update_config({"llms": {"A": {"base_url": cls.mock.url, "retries": 0},
                                        "B": {"base_url": "http://127.0.0.1:9", "retries": 0, "timeout": 2}},
                               "settings": {"web_enabled": False}})
        cls.srv = make_server(cls.app, "127.0.0.1", 0)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.mock.stop()

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def wait(self, job_id):
        for _ in range(300):
            j = self.app.jobs.get(job_id)
            if j.done:
                return j
            time.sleep(0.05)
        raise AssertionError("timeout")

    def upload(self, name, raw):
        return self.req("POST", "/api/attachments", {"name": name, "data": base64.b64encode(raw).decode()})

    def test_upload_and_use_in_arena(self):
        st, body = self.upload("spec.docx", docx_bytes("TITKOS_KÖVETELMÉNY"))
        self.assertEqual(st, 200, body)
        a = body["attachment"]
        self.assertGreater(a["chars"], 10)
        st, img = self.upload("kep.png", PNG)
        st, body = self.req("POST", "/api/arena/run", {"prompt": "", "attachments": [a["id"], img["attachment"]["id"]]})
        self.assertEqual(st, 200, body)
        job = self.wait(body["job"]["id"])
        self.assertEqual(job.status, "done")
        sent = self.mock.requests[-1]["messages"][-1]["content"]
        self.assertIsInstance(sent, list)  # multimodal: text + image
        self.assertIn("TITKOS_KÖVETELMÉNY", sent[0]["text"])
        self.assertEqual(sent[1]["type"], "image_url")
        st, proj = self.req("GET", "/api/project")
        self.assertEqual(proj["project"]["arena"]["rounds"][-1]["attachments"], [a["id"], img["attachment"]["id"]])
        self.assertTrue(all("data" not in x and "text" not in x for x in proj["project"]["attachments"]))
        # B is unreachable: its error must be understandable
        msgs = {m["id"]: m for m in proj["project"]["messages"]}
        err = msgs[proj["project"]["arena"]["rounds"][-1]["responses"]["B"]]["error"]
        self.assertEqual(err["kind"], "unreachable")
        self.assertIn("llama-server", err["hint"])

    def test_errors_are_structured(self):
        st, body = self.req("POST", "/api/arena/run", {"prompt": ""})
        self.assertEqual(st, 400)
        self.assertEqual(body["error_info"]["label"], "Hibás bemenet")
        self.assertTrue(body["error_info"]["hint"])
        st, body = self.req("POST", "/api/arena/run", {"prompt": "x", "attachments": ["nincsilyen"]})
        self.assertEqual(st, 400)
        self.assertIn("nem található", body["error_info"]["message"])
        st, body = self.upload("film.mp4", b"x" * 10)
        self.assertEqual(st, 400)
        self.assertEqual(body["error_info"]["label"], "A fájl nem csatolható")
        st, body = self.req("GET", "/api/nincs")
        self.assertEqual(st, 404)
        self.assertEqual(body["error_info"]["kind"], "not_found")
        st, body = self.req("POST", "/api/debate/start", {"resume": True})
        self.assertEqual(st, 400)

    def test_job_error_is_not_internal(self):
        self.app.update_config({"llms": {"A": {"base_url": "http://127.0.0.1:9", "timeout": 2}}})
        try:
            job = self.app.start_debate("Téma", rounds=1)
            self.wait(job.id)
            self.assertEqual(job.status, "error")
            self.assertEqual(job.error["kind"], "unreachable")
            self.assertTrue(job.error["hint"])
        finally:
            self.app.update_config({"llms": {"A": {"base_url": self.mock.url}}})

    def test_code_upload_and_attachment_persistence(self):
        st, body = self.req("POST", "/api/code/upload", {"name": "sajat_modul.py",
                                                         "data": base64.b64encode(b"X = 1\n").decode()})
        self.assertEqual(st, 200, body)
        self.assertIn("sajat_modul.py", self.app.store.snapshot()["code"]["files"])
        st, body = self.upload("jegyzet.md", "# Jegyzet ő".encode())
        aid = body["attachment"]["id"]
        self.app.store.save()
        pid = self.app.store.project["id"]
        exported = self.app.store.export()
        self.assertIn(aid, exported["attachments"])
        self.app.store.new("x")
        self.assertEqual(self.app.store.attachments, {})
        self.app.store.load(pid)
        self.assertIn(aid, self.app.store.attachments)
        self.app.store.new("y")
        self.app.store.import_project(exported)
        self.assertEqual(self.app.store.attachments[aid]["text"], "# Jegyzet ő")

    def test_accented_download_name(self):
        with self.app.store.mutate() as p:
            p["name"] = "Számítógép őrület"
        with self.opener.open(self.base + "/api/export/html?section=arena", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("filename*=UTF-8''", resp.headers["Content-Disposition"])


if __name__ == "__main__":
    unittest.main()
