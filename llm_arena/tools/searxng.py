"""Run and discover a local SearXNG instance (free, open-source metasearch).

Modes:
  * docker – the official ``searxng/searxng`` image (works on Windows via Docker
    Desktop, macOS and Linux). The LLM Arena writes a settings.yml with the
    JSON output enabled and the rate limiter disabled, mounts it, and binds the
    port to 127.0.0.1 only.
  * native – ``python -m searx.webapp`` if the ``searx`` package is installed
    (advanced; officially Linux only).

After a successful start / discovery the app writes the URL into the settings
and selects SearXNG as the search backend.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

CONTAINER = "llm-arena-searxng"
IMAGE = "searxng/searxng:latest"
COMMON_PORTS = (8888, 8080, 8081, 8889, 8000, 4000, 8090)
MANAGED_MARK = "# managed by LLM Arena"

Progress = Callable[[str], None]


class SearxError(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        from ..errors import make
        self.error = make("searxng", "SearXNG hiba", message, hint=hint or None)
        if not self.error.get("hint"):
            self.error["hint"] = hint


SETTINGS_YML = """{mark} – you may edit it; delete the file to regenerate.
use_default_settings: true

general:
  instance_name: "LLM Arena SearXNG"

server:
  secret_key: "{secret}"
  limiter: false          # local use only – no bot protection needed
  image_proxy: false
  public_instance: false

search:
  safe_search: 0
  autocomplete: ""
  formats:
    - html
    - json                # required by the LLM Arena
"""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _run(args: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kw)


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local instance: never via proxy


def config_dir(data_dir: str | os.PathLike) -> Path:
    return Path(data_dir).resolve() / "searxng"


def ensure_config(data_dir: str | os.PathLike) -> Path:
    """Create (or repair) the managed settings.yml. Returns the directory to mount."""
    d = config_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / "settings.yml"
    if f.exists():
        text = f.read_text("utf-8", "replace")
        if MANAGED_MARK in text and "- json" not in text:
            f.write_text(SETTINGS_YML.format(mark=MANAGED_MARK, secret=secrets.token_hex(24)), "utf-8")
    else:
        f.write_text(SETTINGS_YML.format(mark=MANAGED_MARK, secret=secrets.token_hex(24)), "utf-8")
    return d


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def probe(url: str, timeout: float = 3.0) -> dict:
    """Is ``url`` a SearXNG instance, and does it answer JSON searches?"""
    url = url.rstrip("/")
    out = {"url": url, "reachable": False, "searxng": False, "json": False, "error": ""}
    op = _opener()
    try:
        with op.open(urllib.request.Request(url + "/config", headers={"Accept": "application/json"}),
                     timeout=timeout) as r:
            out["reachable"] = True
            data = json.loads(r.read(2_000_000).decode("utf-8", "replace"))
            out["searxng"] = isinstance(data, dict) and "engines" in data
            out["instance_name"] = data.get("instance_name", "") if isinstance(data, dict) else ""
    except urllib.error.HTTPError as e:
        out["reachable"] = True
        out["error"] = f"/config: HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        out["error"] = str(getattr(e, "reason", e))
        return out
    try:
        q = urllib.parse.urlencode({"q": "searxng", "format": "json"})
        with op.open(urllib.request.Request(f"{url}/search?{q}", headers={"Accept": "application/json"}),
                     timeout=max(timeout, 8)) as r:
            data = json.loads(r.read(2_000_000).decode("utf-8", "replace"))
            out["json"] = isinstance(data, dict) and "results" in data
            out["searxng"] = out["searxng"] or out["json"]
    except urllib.error.HTTPError as e:
        out["error"] = ("A JSON formátum tiltva (HTTP 403) – settings.yml: search.formats: [html, json]"
                        if e.code == 403 else f"/search: HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        out["error"] = f"/search: {getattr(e, 'reason', e)}"
    return out


def detect(extra_urls: list[str] | None = None) -> list[dict]:
    """Look for running SearXNG instances on localhost (and the given URLs)."""
    urls = [u for u in (extra_urls or []) if u] + [f"http://127.0.0.1:{p}" for p in COMMON_PORTS]
    found, seen = [], set()
    for u in urls:
        u = u.rstrip("/")
        if u in seen:
            continue
        seen.add(u)
        parts = urllib.parse.urlsplit(u)
        if parts.hostname in ("127.0.0.1", "localhost") and port_free(parts.port or 80):
            continue  # nothing listening – skip quickly
        p = probe(u, timeout=2.0)
        if p["searxng"]:
            found.append(p)
    return found


# --------------------------------------------------------------------------- #
# docker
# --------------------------------------------------------------------------- #

def docker_status() -> dict:
    exe = shutil.which("docker")
    if not exe:
        return {"ok": False, "installed": False, "message": "A Docker nincs telepítve.",
                "hint": "Telepítsd az ingyenes Docker Desktopot (https://www.docker.com/products/docker-desktop/), "
                        "indítsd el, majd próbáld újra."}
    try:
        r = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=15)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "installed": True, "message": f"A Docker nem válaszol: {e}",
                "hint": "Indítsd el a Docker Desktopot, várd meg, amíg „Running” lesz, majd próbáld újra."}
    if r.returncode != 0 or not r.stdout.strip():
        return {"ok": False, "installed": True, "message": "A Docker telepítve van, de a motor nem fut.",
                "hint": "Indítsd el a Docker Desktopot (Windows/macOS) vagy a docker szolgáltatást (Linux).",
                "detail": (r.stderr or r.stdout)[-800:]}
    return {"ok": True, "installed": True, "version": r.stdout.strip()}


def container_state(name: str = CONTAINER) -> str | None:
    try:
        r = _run(["docker", "inspect", "-f", "{{.State.Status}}", name], timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def container_port(name: str = CONTAINER) -> int | None:
    try:
        r = _run(["docker", "port", name, "8080/tcp"], timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return None
    for line in r.stdout.splitlines():
        try:
            return int(line.rsplit(":", 1)[-1])
        except ValueError:
            continue
    return None


def _image_present(image: str) -> bool:
    try:
        return _run(["docker", "image", "inspect", image], timeout=20).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _pull(image: str, progress: Progress, cancelled: Callable[[], bool]) -> None:
    progress(f"A SearXNG image letöltése ({image}) – első alkalommal ez néhány percig tarthat…")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(["docker", "pull", image], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, **kw)
    last = ""
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if line and line != last:
                last = line
                if "Pull complete" in line or "Downloaded" in line or "Status:" in line or "Digest" in line:
                    progress(line[:120])
            if cancelled():
                proc.kill()
                raise SearxError("A letöltés megszakítva.")
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        raise SearxError(f"Az image letöltése nem sikerült: {last}",
                         "Ellenőrizd az internetkapcsolatot és hogy a Docker fut. Proxy mögött állítsd be a Docker proxyt.")


def container_logs(name: str = CONTAINER, lines: int = 30) -> str:
    try:
        r = _run(["docker", "logs", "--tail", str(lines), name], timeout=15)
        return (r.stdout + r.stderr)[-4000:]
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _last_error_line(logs: str) -> str:
    for line in reversed(logs.strip().splitlines()):
        if re.search(r"(Error|Exception|error:|ERROR)", line):
            return line.strip()[:300]
    return logs.strip().splitlines()[-1][:300] if logs.strip() else ""


def wait_ready(url: str, timeout: float, progress: Progress, cancelled: Callable[[], bool],
               container: str | None = None) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    n = 0
    while time.monotonic() < deadline:
        if cancelled():
            raise SearxError("Megszakítva.")
        last = probe(url, timeout=2.0)
        if last["searxng"] and last["json"]:
            return last
        if last["searxng"] and not last["json"] and "403" in last.get("error", ""):
            raise SearxError(last["error"], "A settings.yml-ben kapcsold be a JSON formátumot "
                                            "(search.formats: [html, json]), majd indítsd újra a SearXNG-t.")
        if container and n >= 2:
            state = container_state(container)
            if state in ("restarting", "exited", "dead"):
                logs = container_logs(container)
                err = SearxError(f"A SearXNG konténer leállt / újraindul ({state}): {_last_error_line(logs)}",
                                 "Nézd meg a Részleteket (konténernapló). Gyakori ok: foglalt port vagy hibás "
                                 "settings.yml – töröld a data/searxng/settings.yml fájlt, és indítsd újra.")
                err.error["detail"] = logs
                raise err
        n += 1
        if n % 5 == 0:
            progress(f"Várakozás a SearXNG indulására… ({last.get('error') or 'indul'})")
        time.sleep(1.5)
    raise SearxError(f"A SearXNG nem indult el {int(timeout)} másodperc alatt ({last.get('error', '')}).",
                     "Nézd meg a konténer naplóját: docker logs llm-arena-searxng")


def start_docker(data_dir: str | os.PathLike, port: int, progress: Progress,
                 cancelled: Callable[[], bool] = lambda: False, image: str = IMAGE) -> str:
    st = docker_status()
    if not st["ok"]:
        raise SearxError(st["message"], st.get("hint", ""))
    cfg = ensure_config(data_dir)
    state = container_state()
    if state == "running":
        p = container_port() or port
        url = f"http://127.0.0.1:{p}"
        progress(f"A SearXNG konténer már fut ({url}).")
        return wait_ready(url, 60, progress, cancelled)["url"]
    if state:  # exists but stopped
        cport = container_port()
        progress("A meglévő SearXNG konténer indítása…")
        r = _run(["docker", "start", CONTAINER], timeout=60)
        if r.returncode != 0:
            raise SearxError(f"A konténer nem indult: {r.stderr.strip()[-400:]}",
                             "Próbáld: docker rm -f llm-arena-searxng, majd indítsd újra innen.")
        url = f"http://127.0.0.1:{container_port() or cport or port}"
        return wait_ready(url, 90, progress, cancelled, CONTAINER)["url"]
    if not port_free(port):
        p = probe(f"http://127.0.0.1:{port}", 2.0)
        if p["searxng"]:
            raise SearxError(f"A {port}-es porton már fut egy SearXNG – használd a „Keresés helyi SearXNG után” gombot.")
        raise SearxError(f"A {port}-es port foglalt.", "Adj meg másik portot (pl. 8889) a SearXNG port mezőben.")
    if not _image_present(image):
        _pull(image, progress, cancelled)
    progress("SearXNG konténer létrehozása…")
    args = ["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
            "-p", f"127.0.0.1:{port}:8080",
            "-v", f"{cfg}:/etc/searxng",
            "-e", f"SEARXNG_BASE_URL=http://127.0.0.1:{port}/",
            # IPv4 bind: the image defaults to "::", which fails on hosts without IPv6.
            "-e", "GRANIAN_HOST=0.0.0.0",
            image]
    r = _run(args, timeout=120)
    if r.returncode != 0:
        raise SearxError(f"A konténer létrehozása nem sikerült: {(r.stderr or r.stdout).strip()[-500:]}",
                         "Windowson engedélyezd a Docker Desktopban a mappamegosztást (Settings → Resources → "
                         "File sharing) az LLM Aréna adatmappájára.")
    url = f"http://127.0.0.1:{port}"
    return wait_ready(url, 120, progress, cancelled, CONTAINER)["url"]


def stop_docker() -> bool:
    if not container_state():
        return False
    r = _run(["docker", "stop", CONTAINER], timeout=60)
    return r.returncode == 0


def remove_docker() -> bool:
    r = _run(["docker", "rm", "-f", CONTAINER], timeout=60)
    return r.returncode == 0


# --------------------------------------------------------------------------- #
# native (python -m searx.webapp)
# --------------------------------------------------------------------------- #

_native: subprocess.Popen | None = None


def native_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("searx") is not None
    except (ImportError, ValueError):
        return False


def start_native(data_dir: str | os.PathLike, port: int, progress: Progress,
                 cancelled: Callable[[], bool] = lambda: False) -> str:
    global _native
    if not native_available():
        raise SearxError("A SearXNG Python csomag nincs telepítve.",
                         "Ajánlott a Docker mód. Haladóknak: pip install git+https://github.com/searxng/searxng "
                         "(hivatalosan csak Linuxon támogatott).")
    if _native and _native.poll() is None:
        return wait_ready(f"http://127.0.0.1:{port}", 60, progress, cancelled)["url"]
    if not port_free(port):
        raise SearxError(f"A {port}-es port foglalt.", "Adj meg másik portot.")
    cfg = ensure_config(data_dir) / "settings.yml"
    env = dict(os.environ, SEARXNG_SETTINGS_PATH=str(cfg), SEARXNG_PORT=str(port), SEARXNG_BIND_ADDRESS="127.0.0.1")
    progress("SearXNG indítása (python -m searx.webapp)…")
    log = open(config_dir(data_dir) / "native.log", "a", encoding="utf-8")  # noqa: SIM115
    _native = subprocess.Popen([sys.executable, "-m", "searx.webapp"], env=env, stdout=log, stderr=log)
    return wait_ready(f"http://127.0.0.1:{port}", 90, progress, cancelled)["url"]


def stop_native() -> bool:
    global _native
    if _native and _native.poll() is None:
        _native.terminate()
        try:
            _native.wait(10)
        except subprocess.TimeoutExpired:
            _native.kill()
        _native = None
        return True
    return False


# --------------------------------------------------------------------------- #
# facade
# --------------------------------------------------------------------------- #

def start(data_dir: str | os.PathLike, port: int, mode: str, progress: Progress,
          cancelled: Callable[[], bool] = lambda: False) -> dict:
    if mode == "native" or (mode == "auto" and not shutil.which("docker") and native_available()):
        return {"url": start_native(data_dir, port, progress, cancelled), "mode": "native"}
    return {"url": start_docker(data_dir, port, progress, cancelled), "mode": "docker"}


def stop() -> bool:
    a = stop_native()
    b = stop_docker() if shutil.which("docker") else False
    return a or b


def status(configured_url: str = "") -> dict:
    d = docker_status()
    info = {"docker": d, "native_available": native_available(),
            "native_running": bool(_native and _native.poll() is None),
            "container": container_state() if d.get("ok") else None, "configured_url": configured_url}
    if info["container"] == "running":
        info["container_url"] = f"http://127.0.0.1:{container_port() or 8888}"
    target = configured_url or info.get("container_url")
    info["probe"] = probe(target, 2.0) if target else None
    return info
