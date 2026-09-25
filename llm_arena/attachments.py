"""File attachments: ingest uploaded files and turn them into model context.

Text is extracted with the standard library for: plain text / source code /
JSON / CSV / HTML / XML / Markdown, DOCX, XLSX, ODT/ODS, PPTX. PDF text needs
the optional ``pypdf`` package (a clear error explains this otherwise).
Images are kept (base64) and sent to multimodal models as image parts.
"""

from __future__ import annotations

import base64
import io
import re
import time
import uuid
import zipfile
from xml.etree import ElementTree as ET

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 300_000
KEEP_RAW_BYTES = 10 * 1024 * 1024  # original kept for download up to this size

TEXT_EXT = {
    "txt", "md", "markdown", "rst", "log", "csv", "tsv", "json", "jsonl", "yaml", "yml", "toml", "ini", "cfg",
    "conf", "xml", "html", "htm", "css", "js", "mjs", "ts", "tsx", "jsx", "py", "pyi", "java", "kt", "c", "h",
    "cpp", "hpp", "cc", "cs", "go", "rs", "rb", "php", "swift", "scala", "sh", "bash", "ps1", "bat", "sql",
    "r", "m", "lua", "pl", "vue", "svelte", "dockerfile", "makefile", "gradle", "properties", "env", "tex",
    "srt", "vtt", "ipynb",
}
IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
              "webp": "image/webp", "bmp": "image/bmp"}


class AttachmentError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        from .errors import make
        self.error = make("attachment", "A fájl nem csatolható", message)


def _ext(name: str) -> str:
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if base in ("dockerfile", "makefile"):
        return base
    return base.rsplit(".", 1)[-1] if "." in base else ""


def _xml_text(data: bytes, para_tags: tuple[str, ...], text_tag: str) -> str:
    root = ET.fromstring(data)
    out: list[str] = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == text_tag and el.text:
            out.append(el.text)
        elif tag in ("tab",):
            out.append("\t")
        elif tag in ("br", "cr"):
            out.append("\n")
        if tag in para_tags:
            out.append("\n")
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def _docx(raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        parts = [n for n in z.namelist() if re.match(r"word/(document|header\d*|footer\d*|footnotes)\.xml$", n)]
        parts.sort(key=lambda n: (not n.endswith("document.xml"), n))
        texts = []
        for n in parts:
            texts.append(_xml_text(z.read(n), ("p",), "t"))
        # paragraphs close with </w:p>: add newline after each paragraph end (handled via iteration order)
        return "\n\n".join(t for t in texts if t)


def _pptx(raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        return "\n\n".join(f"--- Dia {i} ---\n" + _xml_text(z.read(n), ("p",), "t") for i, n in enumerate(slides, 1))


def _odf(raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        return _xml_text(z.read("content.xml"), ("p", "h", "table-row"), "span") or \
            re.sub(r"<[^>]+>", " ", z.read("content.xml").decode("utf-8", "replace"))


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref or "A")
    n = 0
    for ch in (letters.group(0) if letters else "A"):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _xlsx(raw: bytes, max_rows: int = 2000) -> str:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
        names: dict[str, str] = {}
        try:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            for i, sh in enumerate(wb.iter(f"{{{ns['m']}}}sheet"), 1):
                names[f"xl/worksheets/sheet{i}.xml"] = sh.get("name") or f"Munkalap {i}"
        except KeyError:
            pass
        sheets = sorted((n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        out = []
        for sname in sheets:
            root = ET.fromstring(z.read(sname))
            lines = []
            for row in root.iter(f"{{{ns['m']}}}row"):
                cells: dict[int, str] = {}
                for c in row.findall("m:c", ns):
                    v = c.find("m:v", ns)
                    t = c.get("t")
                    if t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
                    elif v is None:
                        continue
                    elif t == "s":
                        idx = int(v.text or 0)
                        val = shared[idx] if idx < len(shared) else ""
                    else:
                        val = v.text or ""
                    cells[_col_index(c.get("r", ""))] = val.replace("\t", " ").replace("\n", " ")
                if cells:
                    lines.append("\t".join(cells.get(i, "") for i in range(max(cells) + 1)))
                if len(lines) >= max_rows:
                    lines.append(f"... (további sorok levágva, max {max_rows})")
                    break
            out.append(f"--- {names.get(sname, sname)} ---\n" + "\n".join(lines))
        return "\n\n".join(out)


def _pdf(raw: bytes) -> str:
    try:
        from pypdf import PdfReader  # optional dependency
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            raise AttachmentError("PDF-ből szöveget csak a 'pypdf' csomaggal tudok kinyerni. Telepítés: "
                                  "pip install pypdf  – vagy mentsd a PDF-et szövegként (.txt) / Word-ként (.docx).")
    reader = PdfReader(io.BytesIO(raw))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            raise AttachmentError("A PDF jelszóval védett.")
    pages = []
    for i, page in enumerate(reader.pages, 1):
        pages.append(f"--- {i}. oldal ---\n{(page.extract_text() or '').strip()}")
    text = "\n\n".join(pages)
    if not re.sub(r"--- \d+\. oldal ---", "", text).strip():
        raise AttachmentError("A PDF nem tartalmaz kinyerhető szöveget (valószínűleg szkennelt kép). "
                              "Csatold képként, vagy használj OCR-t.")
    return text


def _decode_text(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    for enc in ("utf-8", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8",):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    try:
        return raw.decode("cp1250")  # common for Hungarian Windows text files
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _looks_binary(raw: bytes) -> bool:
    sample = raw[:4096]
    return b"\x00" in sample and not sample.startswith((b"\xff\xfe", b"\xfe\xff"))


def ingest(name: str, raw: bytes, mime: str = "") -> dict:
    """Validate and convert an uploaded file into an attachment record."""
    name = (name or "fajl").replace("\\", "/").rsplit("/", 1)[-1][:160] or "fajl"
    size = len(raw)
    if size == 0:
        raise AttachmentError(f"A fájl üres: {name}")
    if size > MAX_FILE_BYTES:
        raise AttachmentError(f"A fájl túl nagy: {name} ({size / 1048576:.1f} MB, max {MAX_FILE_BYTES // 1048576} MB).")
    ext = _ext(name)
    rec = {"id": uuid.uuid4().hex[:12], "name": name, "size": size, "mime": mime or "", "ext": ext,
           "created": time.time(), "kind": "text", "text": "", "truncated": False, "note": "", "data": None}
    try:
        if ext in IMAGE_MIME or (mime or "").startswith("image/"):
            if size > MAX_IMAGE_BYTES:
                raise AttachmentError(f"A kép túl nagy: {name} (max {MAX_IMAGE_BYTES // 1048576} MB).")
            rec.update(kind="image", mime=IMAGE_MIME.get(ext, mime or "image/png"),
                       note="Kép – csak képet értő (multimodális) modell látja.")
        elif ext == "docx":
            rec["text"] = _docx(raw)
        elif ext == "xlsx":
            rec["text"] = _xlsx(raw)
        elif ext == "pptx":
            rec["text"] = _pptx(raw)
        elif ext in ("odt", "ods", "odp"):
            rec["text"] = _odf(raw)
        elif ext == "pdf":
            rec["text"] = _pdf(raw)
        elif ext in ("doc", "xls", "ppt"):
            raise AttachmentError(f"A régi Office formátum ({ext}) nem támogatott – mentsd el "
                                  f"{ext}x formátumban, vagy szövegként.")
        elif ext in ("zip", "7z", "rar", "exe", "dll", "bin", "iso", "mp3", "mp4", "wav", "avi", "mov", "mkv"):
            raise AttachmentError(f"Ez a fájltípus ({ext}) nem értelmezhető szövegként.")
        elif ext in TEXT_EXT or not _looks_binary(raw):
            rec["text"] = _decode_text(raw)
        else:
            raise AttachmentError(f"Bináris fájl, amiből nem nyerhető ki szöveg: {name}")
    except AttachmentError:
        raise
    except (zipfile.BadZipFile, ET.ParseError, KeyError) as e:
        raise AttachmentError(f"A fájl sérült vagy nem a várt formátumú ({ext}): {e}")
    except Exception as e:  # noqa: BLE001
        raise AttachmentError(f"A fájl feldolgozása nem sikerült: {type(e).__name__}: {e}")
    if rec["kind"] == "text":
        text = rec["text"].replace("\r\n", "\n")
        if not text.strip():
            raise AttachmentError(f"Nem találtam szöveget a fájlban: {name}")
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS]
            rec["truncated"] = True
            rec["note"] = f"A szöveg {MAX_TEXT_CHARS} karakternél levágva."
        rec["text"] = text
    if rec["kind"] == "image" or size <= KEEP_RAW_BYTES:
        rec["data"] = base64.b64encode(raw).decode("ascii")
    return rec


def meta(rec: dict) -> dict:
    """Lightweight description for the UI (no payload)."""
    return {k: rec.get(k) for k in ("id", "name", "size", "mime", "ext", "kind", "created", "truncated", "note")} | {
        "chars": len(rec.get("text") or ""), "preview": (rec.get("text") or "")[:400],
        "has_data": bool(rec.get("data"))}


def context_block(records: list[dict], budget: int) -> str:
    """Attached text files as a prompt section, split fairly within ``budget`` characters."""
    texts = [r for r in records if r.get("kind") == "text" and r.get("text")]
    images = [r for r in records if r.get("kind") == "image"]
    if not texts and not images:
        return ""
    per = max(800, budget // max(1, len(texts))) if texts else 0
    parts = ["ATTACHED FILES (provided by the user – use them as source material):"]
    for r in texts:
        t = r["text"]
        cut = len(t) > per
        body = t[:per] + ("\n[... a fájl további része levágva ...]" if cut else "")
        fence = "```"
        parts.append(f"### FILE: {r['name']}\n{fence}\n{body}\n{fence}")
    for r in images:
        parts.append(f"### IMAGE: {r['name']} (attached as an image)")
    return "\n\n".join(parts)


def image_parts(records: list[dict]) -> list[dict]:
    return [{"type": "image_url", "image_url": {"url": f"data:{r['mime']};base64,{r['data']}"}}
            for r in records if r.get("kind") == "image" and r.get("data")]


def with_attachments(text: str, records: list[dict], budget: int, images: bool = True):
    """User message content: plain string, or multimodal parts when images are attached."""
    block = context_block(records, budget)
    full = f"{text}\n\n{block}" if block else text
    parts = image_parts(records) if images else []
    if not parts:
        return full
    return [{"type": "text", "text": full}, *parts]
