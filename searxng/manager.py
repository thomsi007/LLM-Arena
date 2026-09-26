#!/usr/bin/env python3
"""SearXNG kezelő – önálló, telepítő + indító + grafikus beállító felület.

Csak a Python standard könyvtárát használja (3.9+). Windows és Linux.

    python manager.py              # grafikus felület (böngészőben), SearXNG indítása ha kell
    python manager.py start|stop|restart|status|install|test|gui

Módok:
  docker – a hivatalos searxng/searxng image (Windows: Docker Desktop)
  native – Docker nélkül: a SearXNG forrás letöltése (git vagy zip) ebbe a mappába,
           saját Python virtuális környezet, `python -m searx.webapp`
  auto   – Docker, ha elérhető és fut; különben natív

Minden adat ebben a mappában marad: config.json (beállítások), data/settings.yml
(a SearXNG generált konfigurációja), native/ (natív telepítés), logs/.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
DATA_DIR = HERE / "data"
SETTINGS_FILE = DATA_DIR / "settings.yml"
NATIVE_DIR = HERE / "native"
SRC_DIR = NATIVE_DIR / "searxng-src"
VENV_DIR = NATIVE_DIR / "venv"
LOG_DIR = HERE / "logs"
PID_FILE = DATA_DIR / "native.pid"
CONTAINER = "llm-arena-searxng"          # shared with the LLM Arena
IMAGE = "searxng/searxng:latest"
REPO_GIT = "https://github.com/searxng/searxng.git"
REPO_ZIP = "https://github.com/searxng/searxng/archive/refs/heads/master.zip"
MANAGED_MARK = "# managed by LLM Arena"
IS_WIN = os.name == "nt"

ENGINES = [  # (name, label, default enabled)
    ("google", "Google", True), ("bing", "Bing", True), ("duckduckgo", "DuckDuckGo", True),
    ("brave", "Brave", True), ("qwant", "Qwant", False), ("yahoo", "Yahoo", False),
    ("wikipedia", "Wikipedia", True), ("wikidata", "Wikidata", True), ("github", "GitHub", True),
    ("stackoverflow", "Stack Overflow", True), ("mdn", "MDN (web-dokumentáció)", True),
    ("pypi", "PyPI", True), ("docker hub", "Docker Hub", False), ("arxiv", "arXiv", True),
    ("semantic scholar", "Semantic Scholar", False), ("arch linux wiki", "Arch Linux Wiki", False),
    ("google news", "Google News", False), ("bing news", "Bing News", False),
    ("duckduckgo news", "DuckDuckGo News", False), ("youtube", "YouTube", False),
]

DEFAULT_CONFIG = {
    "mode": "auto",                # auto | docker | native
    "port": 8888,
    "bind": "127.0.0.1",           # 127.0.0.1 = csak ez a gép; 0.0.0.0 = helyi hálózat is
    "instance_name": "LLM Arena SearXNG",
    "default_lang": "auto",
    "safe_search": 0,              # 0 ki, 1 mérsékelt, 2 szigorú
    "autocomplete": "",
    "request_timeout": 4.0,
    "max_request_timeout": 10.0,
    "limiter": False,
    "image_proxy": False,
    "engines": {name: on for name, _, on in ENGINES},
    "custom_settings": False,      # True: a settings.yml-t kézzel szerkesztik, nem generáljuk újra
    "gui_port": 8899,
    "open_browser": True,
    "secret_key": "",
    "last_url": "",
}


# =========================================================================== #
# helpers
# =========================================================================== #

def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        with open(LOG_DIR / "manager.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    TASK.add(msg)
    print(msg, file=sys.stderr, flush=True)


def run(args: list[str], timeout: float = 60, cwd: Path | None = None) -> subprocess.CompletedProcess:
    kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if IS_WIN else {}
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd, encoding="utf-8",
                          errors="replace", **kw)


def stream(args: list[str], cwd: Path | None = None, env: dict | None = None, filt=None) -> int:
    """Run a command, forwarding (filtered) output lines to the task log."""
    kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if IS_WIN else {}
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=cwd, env=env,
                         encoding="utf-8", errors="replace", **kw)
    last = ""
    for line in p.stdout:  # type: ignore[union-attr]
        line = line.rstrip()
        if line and line != last and (filt is None or filt(line)):
            TASK.add(line[:200])
            print("  " + line[:200], file=sys.stderr, flush=True)
        last = line
        if TASK.cancel.is_set():
            p.kill()
            raise Failure("Megszakítva.")
    return p.wait()


class Failure(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) != 0


def find_free_port(start: int, tries: int = 40) -> int:
    for p in range(start, min(start + tries, 65535)):
        if port_free(p):
            return p
    raise Failure(f"Nem találtam szabad portot {start} és {start + tries} között.")


def http_json(url: str, timeout: float = 5) -> tuple[int, object]:
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
            return r.status, json.loads(r.read(3_000_000).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def probe(url: str) -> dict:
    """Is a SearXNG answering at url, with JSON output?"""
    url = url.rstrip("/")
    st, cfg = http_json(url + "/config", 3)
    out = {"url": url, "searxng": isinstance(cfg, dict) and "engines" in cfg, "json": False, "error": ""}
    if st == 0:
        out["error"] = "nem válaszol"
        return out
    st, data = http_json(f"{url}/search?" + urllib.parse.urlencode({"q": "searxng", "format": "json"}), 12)
    if st == 403:
        out["error"] = "a JSON formátum tiltva"
    elif isinstance(data, dict) and "results" in data:
        out["json"] = out["searxng"] = True
    elif st:
        out["error"] = f"HTTP {st}"
    return out


# =========================================================================== #
# configuration
# =========================================================================== #

def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        saved = json.loads(CONFIG_FILE.read_text("utf-8"))
        if isinstance(saved, dict):
            engines = {**cfg["engines"], **(saved.get("engines") or {})}
            cfg.update(saved)
            cfg["engines"] = engines
    except (OSError, ValueError):
        pass
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_hex(24)
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, CONFIG_FILE)


def validate(new: dict, cfg: dict) -> dict:
    try:
        return _validate(new, cfg)
    except (TypeError, ValueError) as e:
        raise Failure(f"Érvénytelen érték: {e}", "Számmezőbe számot írj.") from None


def _validate(new: dict, cfg: dict) -> dict:
    out = dict(cfg)
    if "mode" in new:
        if new["mode"] not in ("auto", "docker", "native"):
            raise Failure("Érvénytelen mód.")
        out["mode"] = new["mode"]
    if "port" in new:
        p = int(new["port"])
        if not 1024 <= p <= 65535:
            raise Failure("A port 1024 és 65535 közötti szám legyen.")
        out["port"] = p
    if "gui_port" in new:
        out["gui_port"] = max(1024, min(int(new["gui_port"]), 65535))
    if "bind" in new:
        if new["bind"] not in ("127.0.0.1", "0.0.0.0"):
            raise Failure("A figyelési cím 127.0.0.1 vagy 0.0.0.0 lehet.")
        out["bind"] = new["bind"]
    for k in ("instance_name", "default_lang", "autocomplete"):
        if k in new:
            v = str(new[k]).strip()[:80]
            if re.search(r"[\"\\\n\r]", v):
                raise Failure(f"Érvénytelen karakter: {k}")
            out[k] = v
    if "safe_search" in new:
        out["safe_search"] = max(0, min(int(new["safe_search"]), 2))
    for k in ("request_timeout", "max_request_timeout"):
        if k in new:
            out[k] = max(1.0, min(float(new[k]), 60.0))
    for k in ("limiter", "image_proxy", "open_browser", "custom_settings"):
        if k in new:
            out[k] = bool(new[k])
    if isinstance(new.get("engines"), dict):
        known = {n for n, _, _ in ENGINES}
        out["engines"] = {**out["engines"], **{k: bool(v) for k, v in new["engines"].items() if k in known}}
    return out


def settings_yaml(cfg: dict, port_in_container: bool = False) -> str:
    """The SearXNG settings.yml generated from config.json (json format always enabled)."""
    port = 8080 if port_in_container else cfg["port"]
    lines = [
        f"{MANAGED_MARK} – generálta a searxng/manager.py a config.json alapján.",
        "# Kézi szerkesztéshez a grafikus felületen kapcsold be a „Saját settings.yml” módot.",
        "use_default_settings: true",
        "",
        "general:",
        f'  instance_name: "{cfg["instance_name"]}"',
        "",
        "server:",
        f'  secret_key: "{cfg["secret_key"]}"',
        f"  port: {port}",
        f'  bind_address: "{"0.0.0.0" if port_in_container else cfg["bind"]}"',
        f"  limiter: {str(cfg['limiter']).lower()}",
        f"  image_proxy: {str(cfg['image_proxy']).lower()}",
        "  public_instance: false",
        "",
        "search:",
        f"  safe_search: {cfg['safe_search']}",
        f'  autocomplete: "{cfg["autocomplete"]}"',
        f'  default_lang: "{cfg["default_lang"]}"',
        "  formats:",
        "    - html",
        "    - json                # az LLM Aréna ezt használja",
        "",
        "outgoing:",
        f"  request_timeout: {cfg['request_timeout']}",
        f"  max_request_timeout: {max(cfg['max_request_timeout'], cfg['request_timeout'])}",
        "",
        "engines:",
    ]
    for name, _, _ in ENGINES:
        lines += [f"  - name: {name}", f"    disabled: {str(not cfg['engines'].get(name, False)).lower()}"]
    return "\n".join(lines) + "\n"


def write_settings(cfg: dict, mode: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if cfg.get("custom_settings") and SETTINGS_FILE.exists():
        text = SETTINGS_FILE.read_text("utf-8", "replace")
        if "json" not in text:
            log("FIGYELEM: a saját settings.yml nem engedélyezi a json formátumot – az LLM Aréna így nem tud keresni.")
        return
    SETTINGS_FILE.write_text(settings_yaml(cfg, port_in_container=(mode == "docker")), "utf-8")


# =========================================================================== #
# docker
# =========================================================================== #

def docker_state() -> dict:
    if not shutil.which("docker"):
        return {"installed": False, "running": False,
                "message": "A Docker nincs telepítve."}
    try:
        r = run(["docker", "version", "--format", "{{.Server.Version}}"], 15)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"installed": True, "running": False, "message": f"A Docker nem válaszol: {e}"}
    if r.returncode != 0 or not r.stdout.strip():
        return {"installed": True, "running": False, "message": "A Docker telepítve van, de a motor nem fut."}
    return {"installed": True, "running": True, "version": r.stdout.strip()}


def container_state() -> str | None:
    try:
        r = run(["docker", "inspect", "-f", "{{.State.Status}}", CONTAINER], 15)
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def container_binding() -> tuple[str, int] | None:
    """(host ip, host port) the container publishes SearXNG on."""
    try:
        r = run(["docker", "inspect", "-f", "{{json .HostConfig.PortBindings}}", CONTAINER], 15)
        data = json.loads(r.stdout or "null") if r.returncode == 0 else None
        for b in (data or {}).get("8080/tcp") or []:
            if b.get("HostPort"):
                return (b.get("HostIp") or "0.0.0.0"), int(b["HostPort"])
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return None


def container_port() -> int | None:
    b = container_binding()
    return b[1] if b else None


def container_mount() -> str:
    """Host folder mounted as /etc/searxng ('' if unknown)."""
    try:
        r = run(["docker", "inspect", "-f", "{{json .Mounts}}", CONTAINER], 15)
        for mnt in (json.loads(r.stdout or "null") if r.returncode == 0 else None) or []:
            if mnt.get("Destination") == "/etc/searxng":
                return mnt.get("Source") or ""
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return ""


def _same_dir(a: str, b: Path) -> bool:
    norm = lambda x: re.sub(r"^/(?:run/desktop/mnt/host/|host_mnt/|mnt/)?([a-z])/", r"\1:/",  # noqa: E731
                            str(x).replace("\\", "/").rstrip("/").lower())
    return bool(a) and norm(a) == norm(b.resolve())


def _docker_bind(cfg: dict) -> str:
    return "0.0.0.0" if cfg["bind"] == "0.0.0.0" else "127.0.0.1"


def image_present() -> bool:
    try:
        return run(["docker", "image", "inspect", IMAGE], 20).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def start_docker_desktop(timeout: float = 180) -> bool:
    cmds = []
    if IS_WIN:
        for root in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramW6432", ""),
                     os.environ.get("LOCALAPPDATA", "")):
            exe = Path(root) / "Docker" / "Docker" / "Docker Desktop.exe"
            if root and exe.is_file():
                cmds.append([str(exe)])
    elif sys.platform == "darwin":
        cmds.append(["open", "-a", "Docker"])
    else:
        cmds += [["systemctl", "--user", "start", "docker-desktop"]]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            cmds.append(["systemctl", "start", "docker"])
    for c in cmds:
        try:
            kw = {"creationflags": 0x00000008 | 0x00000200} if IS_WIN else {"start_new_session": True}
            subprocess.Popen(c, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
            log("A Docker motor nem futott – indítás… (akár 1–2 perc)")
            break
        except OSError:
            continue
    else:
        return False
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if TASK.cancel.is_set():
            raise Failure("Megszakítva.")
        time.sleep(3)
        if docker_state()["running"]:
            log("A Docker motor elindult.")
            return True
    return False


def install_docker_engine() -> None:
    """Install Docker itself (Windows: winget Docker Desktop, Linux as root: get.docker.com)."""
    if IS_WIN:
        if not shutil.which("winget"):
            raise Failure("A winget nem érhető el.", "Töltsd le kézzel: https://www.docker.com/products/docker-desktop/")
        log("Docker Desktop telepítése winget-tel… (rendszergazdai jóváhagyást kérhet)")
        code = stream(["winget", "install", "-e", "--id", "Docker.DockerDesktop",
                       "--accept-package-agreements", "--accept-source-agreements"])
        if code != 0:
            raise Failure(f"A winget telepítés hibával zárult ({code}).",
                          "Telepítsd kézzel: https://www.docker.com/products/docker-desktop/")
        log("A Docker Desktop települt. Indítsd el egyszer, fogadd el a feltételeket, majd nyomd meg újra az Indítást.")
        return
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        log("Docker telepítése a hivatalos get.docker.com szkripttel…")
        script = NATIVE_DIR / "get-docker.sh"
        NATIVE_DIR.mkdir(parents=True, exist_ok=True)
        op = urllib.request.build_opener()
        script.write_bytes(op.open("https://get.docker.com", timeout=60).read())
        if stream(["sh", str(script)]) != 0:
            raise Failure("A Docker telepítése nem sikerült.")
        return
    raise Failure("A Docker telepítéséhez rendszergazdai jog kell.",
                  "Futtasd terminálban: curl -fsSL https://get.docker.com | sudo sh   – vagy válaszd a Natív módot "
                  "(nem kell hozzá Docker).")


def docker_install() -> None:
    st = docker_state()
    if not st["installed"]:
        install_docker_engine()
        st = docker_state()
    if not st["running"] and not start_docker_desktop():
        raise Failure(st.get("message") or "A Docker motor nem fut.",
                      "Indítsd el a Docker Desktopot (Windows) vagy a docker szolgáltatást (Linux), vagy válaszd a "
                      "Natív módot.")
    if not image_present():
        log(f"A SearXNG image letöltése ({IMAGE})…")
        if stream(["docker", "pull", IMAGE], filt=lambda l: any(k in l for k in ("Pull complete", "Status", "Digest",
                                                                                   "Error", "error"))) != 0:
            raise Failure("Az image letöltése nem sikerült.", "Ellenőrizd az internetkapcsolatot.")
    log("Docker mód: telepítve.")


def docker_start(cfg: dict) -> str:
    docker_install()
    write_settings(cfg, "docker")
    state = container_state()
    if state:
        mount = container_mount()
        same = container_binding() == (_docker_bind(cfg), cfg["port"]) and (not mount or _same_dir(mount, DATA_DIR))
        if not same:
            log("A konténer beállításai (port / elérhetőség / konfigurációs mappa) változtak – újralétrehozás…")
        elif state == "running":
            return wait_ready(f"http://127.0.0.1:{cfg['port']}", container=True)
        elif run(["docker", "start", CONTAINER], 60).returncode == 0:
            return wait_ready(f"http://127.0.0.1:{cfg['port']}", container=True)
        run(["docker", "rm", "-f", CONTAINER], 60)
    port = cfg["port"]
    if not port_free(port):
        p = probe(f"http://127.0.0.1:{port}")
        if p["json"]:
            log(f"A {port}-es porton már fut egy használható SearXNG – azt használom.")
            return p["url"]
        port = find_free_port(port + 1)
        log(f"A {cfg['port']}-es port foglalt – a SearXNG a {port}-es porton indul.")
        cfg["port"] = port
        save_config(cfg)
    bind = _docker_bind(cfg)
    for _ in range(3):
        log(f"SearXNG konténer indítása (port: {port})…")
        r = run(["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
                 "-p", f"{bind}:{port}:8080", "-v", f"{DATA_DIR}:/etc/searxng",
                 "-e", f"SEARXNG_BASE_URL=http://127.0.0.1:{port}/", "-e", "GRANIAN_HOST=0.0.0.0", IMAGE], 120)
        if r.returncode == 0:
            return wait_ready(f"http://127.0.0.1:{port}", container=True)
        out = (r.stderr or r.stdout).strip()
        run(["docker", "rm", "-f", CONTAINER], 60)
        if re.search(r"port is already allocated|address already in use|Ports are not available|access permissions",
                     out, re.I):
            port = find_free_port(port + 1)
            cfg["port"] = port
            save_config(cfg)
            continue
        raise Failure(f"A konténer nem indult: {out[-400:]}",
                      "Windowson engedélyezd a Docker Desktopban ennek a mappának a megosztását "
                      "(Settings → Resources → File sharing).")
    raise Failure("Nem találtam használható portot.")


def docker_stop() -> bool:
    if shutil.which("docker") and container_state():
        return run(["docker", "stop", CONTAINER], 60).returncode == 0
    return False


def docker_logs() -> str:
    try:
        r = run(["docker", "logs", "--tail", "80", CONTAINER], 20)
        return (r.stdout + r.stderr)[-8000:]
    except (subprocess.TimeoutExpired, OSError):
        return ""


# =========================================================================== #
# native
# =========================================================================== #

def venv_python() -> Path:
    return VENV_DIR / ("Scripts/python.exe" if IS_WIN else "bin/python")


def native_installed() -> bool:
    return (SRC_DIR / "searx" / "webapp.py").is_file() and venv_python().is_file()


def native_install(update: bool = False) -> None:
    NATIVE_DIR.mkdir(parents=True, exist_ok=True)
    if not (SRC_DIR / "searx").is_dir() or update:
        if shutil.which("git"):
            if (SRC_DIR / ".git").is_dir():
                log("A SearXNG forrás frissítése (git pull)…")
                stream(["git", "-C", str(SRC_DIR), "pull", "--ff-only"])
            else:
                if SRC_DIR.exists():
                    shutil.rmtree(SRC_DIR, ignore_errors=True)
                log("A SearXNG forrás letöltése (git clone)…")
                if stream(["git", "clone", "--depth", "1", REPO_GIT, str(SRC_DIR)]) != 0:
                    raise Failure("A git clone nem sikerült.", "Ellenőrizd az internetkapcsolatot.")
        else:
            log("A SearXNG forrás letöltése (zip)…")
            op = urllib.request.build_opener()
            data = op.open(REPO_ZIP, timeout=120).read()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                top = z.namelist()[0].split("/")[0]
                tmp = NATIVE_DIR / "_unzip"
                shutil.rmtree(tmp, ignore_errors=True)
                z.extractall(tmp)
                shutil.rmtree(SRC_DIR, ignore_errors=True)
                shutil.move(str(tmp / top), str(SRC_DIR))
                shutil.rmtree(tmp, ignore_errors=True)
    if not venv_python().is_file():
        log("Python virtuális környezet létrehozása…")
        if stream([sys.executable, "-m", "venv", str(VENV_DIR)]) != 0:
            raise Failure("A virtuális környezet nem jött létre.",
                          "Linuxon telepítsd: sudo apt install python3-venv")
    log("Függőségek telepítése (pip) – első alkalommal néhány perc…")
    py = str(venv_python())
    stream([py, "-m", "pip", "install", "-q", "--upgrade", "pip", "setuptools", "wheel"])
    code = stream([py, "-m", "pip", "install", "-q", "-r", str(SRC_DIR / "requirements.txt")],
                  filt=lambda l: "notice" not in l.lower())
    if code != 0:
        raise Failure("A függőségek telepítése nem sikerült.",
                      "Nézd meg a naplót. Windowson a Docker mód a megbízhatóbb.")
    log("Natív mód: telepítve.")


PWD_SHIM = '''"""Windows shim for the POSIX-only pwd module (SearXNG imports it in searx/valkeydb.py)."""
import getpass
import os


class struct_passwd(tuple):
    pw_name = property(lambda self: self[0])
    pw_uid = property(lambda self: self[2])


def getpwuid(uid):
    return struct_passwd((getpass.getuser(), "x", uid, 0, "", os.path.expanduser("~"), ""))
'''


def native_windows_fixes() -> None:
    """SearXNG is developed for Linux; patch the few POSIX-only bits so it also runs on Windows."""
    if not IS_WIN:
        return
    site = VENV_DIR / "Lib" / "site-packages"
    if site.is_dir() and not (site / "pwd.py").exists():
        (site / "pwd.py").write_text(PWD_SHIM, "utf-8")


def native_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    if IS_WIN:
        r = run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], 15)
        return pid if str(pid) in r.stdout else None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def native_start(cfg: dict) -> str:
    if not native_installed():
        native_install()
    native_windows_fixes()
    if native_pid():
        url = cfg.get("last_url") or f"http://127.0.0.1:{cfg['port']}"
        p = probe(url)
        if p["json"]:
            return url
        native_stop()
    port = cfg["port"]
    if not port_free(port):
        p = probe(f"http://127.0.0.1:{port}")
        if p["json"]:
            log(f"A {port}-es porton már fut egy használható SearXNG – azt használom.")
            return p["url"]
        port = find_free_port(port + 1)
        log(f"A {cfg['port']}-es port foglalt – a SearXNG a {port}-es porton indul.")
        cfg["port"] = port
        save_config(cfg)
    write_settings(cfg, "native")
    env = dict(os.environ, SEARXNG_SETTINGS_PATH=str(SETTINGS_FILE), SEARXNG_PORT=str(port),
               SEARXNG_BIND_ADDRESS=cfg["bind"], PYTHONUTF8="1")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logf = open(LOG_DIR / "searxng-native.log", "a", encoding="utf-8")  # noqa: SIM115
    kw = {"creationflags": 0x00000008 | 0x00000200 | getattr(subprocess, "CREATE_NO_WINDOW", 0)} if IS_WIN \
        else {"start_new_session": True}
    log(f"SearXNG indítása natívan (port: {port})…")
    p = subprocess.Popen([str(venv_python()), "-m", "searx.webapp"], cwd=SRC_DIR, env=env, stdout=logf,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)
    PID_FILE.write_text(str(p.pid))
    return wait_ready(f"http://127.0.0.1:{port}", native_proc=p)


def native_stop() -> bool:
    pid = native_pid()
    if not pid:
        return False
    if IS_WIN:
        run(["taskkill", "/PID", str(pid), "/T", "/F"], 30)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for _ in range(20):
            if not native_pid():
                break
            time.sleep(0.25)
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return True


def native_logs() -> str:
    try:
        return (LOG_DIR / "searxng-native.log").read_text("utf-8", "replace")[-8000:]
    except OSError:
        return ""


# =========================================================================== #
# high level
# =========================================================================== #

def resolve_mode(cfg: dict) -> str:
    if cfg["mode"] != "auto":
        return cfg["mode"]
    st = docker_state()
    if st["running"] or (st["installed"] and not native_installed()):
        return "docker"
    return "native"


def wait_ready(url: str, timeout: float = 120, container: bool = False, native_proc=None) -> str:
    t0 = time.monotonic()
    n = 0
    while time.monotonic() - t0 < timeout:
        if TASK.cancel.is_set():
            raise Failure("Megszakítva.")
        p = probe(url)
        if p["json"]:
            log(f"A SearXNG fut: {url}")
            cfg = load_config()
            cfg["last_url"] = url
            save_config(cfg)
            return url
        if p["searxng"] and "tiltva" in p["error"]:
            raise Failure("A SearXNG fut, de a JSON formátum tiltva van.",
                          "Kapcsold ki a „Saját settings.yml” módot, vagy vedd fel a json-t a formats közé.")
        if container and n >= 3 and container_state() in ("restarting", "exited", "dead"):
            raise Failure("A SearXNG konténer leállt: " + docker_logs()[-500:],
                          "Nézd meg a Napló részt. Gyakori ok: hibás saját settings.yml.")
        if native_proc is not None and native_proc.poll() is not None:
            raise Failure("A SearXNG folyamat leállt: " + native_logs()[-600:],
                          "Nézd meg a Napló részt (hiányzó függőség? foglalt port?).")
        n += 1
        if n % 6 == 0:
            log("Várakozás a SearXNG indulására…")
        time.sleep(1.5)
    raise Failure(f"A SearXNG {int(timeout)} s alatt sem indult el.", "Nézd meg a Napló részt.")


def start() -> str:
    cfg = load_config()
    mode = resolve_mode(cfg)
    log(f"Indítás – mód: {mode}")
    if mode == "native":
        return native_start(cfg)
    try:
        return docker_start(cfg)
    except Failure as e:
        if cfg["mode"] != "auto" or TASK.cancel.is_set():
            raise
        log(f"Docker mód nem sikerült ({e}) – átváltás natív módra…")
        return native_start(load_config())


def stop() -> bool:
    a = native_stop()
    b = docker_stop()
    log("A SearXNG leállítva." if (a or b) else "A SearXNG nem futott.")
    return a or b


def install(mode: str | None = None) -> None:
    cfg = load_config()
    mode = mode or resolve_mode(cfg)
    docker_install() if mode == "docker" else native_install()


def status() -> dict:
    cfg = load_config()
    d = docker_state()
    cont = container_state() if d["running"] else None
    url = ""
    if cont == "running":
        url = f"http://127.0.0.1:{container_port() or cfg['port']}"
    elif native_pid():
        url = cfg.get("last_url") or f"http://127.0.0.1:{cfg['port']}"
    running = bool(url) and probe(url)["json"]
    return {"config": {k: v for k, v in cfg.items() if k != "secret_key"}, "docker": d, "container": cont,
            "native_installed": native_installed(), "native_pid": native_pid(), "url": url, "running": running,
            "mode_resolved": resolve_mode(cfg), "engines": [{"name": n, "label": l} for n, l, _ in ENGINES],
            "settings_file": str(SETTINGS_FILE), "folder": str(HERE), "task": TASK.snapshot(),
            "platform": f"{sys.platform} · Python {sys.version.split()[0]}"}


def test_search(query: str) -> dict:
    st = status()
    if not st["url"]:
        raise Failure("A SearXNG nem fut.", "Indítsd el a „▶ Indítás” gombbal.")
    code, data = http_json(f"{st['url']}/search?" + urllib.parse.urlencode({"q": query, "format": "json"}), 25)
    if not isinstance(data, dict):
        raise Failure(f"A keresés nem sikerült (HTTP {code}).")
    return {"count": len(data.get("results", [])),
            "results": [{"title": r.get("title"), "url": r.get("url"), "engine": r.get("engine")}
                        for r in data.get("results", [])[:8]],
            "unresponsive": data.get("unresponsive_engines", [])}


def uninstall(what: str) -> None:
    stop()
    if what == "docker" and shutil.which("docker"):
        run(["docker", "rm", "-f", CONTAINER], 60)
        run(["docker", "rmi", IMAGE], 120)
        log("A Docker konténer és image eltávolítva.")
    elif what == "native":
        shutil.rmtree(NATIVE_DIR, ignore_errors=True)
        log("A natív telepítés eltávolítva.")


# =========================================================================== #
# background task (one at a time) for the GUI
# =========================================================================== #

class Task:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.name = ""
        self.running = False
        self.lines: list[str] = []
        self.result = None
        self.error = None
        self.cancel = threading.Event()

    def add(self, line: str) -> None:
        with self.lock:
            self.lines.append(time.strftime("%H:%M:%S ") + line)
            self.lines = self.lines[-300:]

    def start(self, name: str, fn) -> None:
        with self.lock:
            if self.running:
                raise Failure(f"Már fut egy művelet: {self.name}")
            self.name, self.running, self.result, self.error = name, True, None, None
            self.lines = []
            self.cancel.clear()

        def runner():
            try:
                self.result = fn()
            except Failure as e:
                self.error = {"message": str(e), "hint": e.hint}
                self.add("HIBA: " + str(e))
            except Exception as e:  # noqa: BLE001
                self.error = {"message": f"{type(e).__name__}: {e}", "hint": "", "trace": traceback.format_exc()}
                self.add("HIBA: " + str(e))
            finally:
                self.running = False
        threading.Thread(target=runner, daemon=True).start()

    def snapshot(self) -> dict:
        with self.lock:
            return {"name": self.name, "running": self.running, "lines": self.lines[-120:], "result": self.result,
                    "error": self.error}


TASK = Task()


# =========================================================================== #
# GUI (local web page)
# =========================================================================== #

GUI = r"""<!doctype html><html lang="hu"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,"><title>SearXNG kezelő</title><style>
:root{--bg:#0f131b;--panel:#1a2030;--p2:#202739;--b:#2a3247;--t:#e6e9f0;--m:#8d97ad;--a:#7c6cff;--ok:#3ccf8e;--err:#ff5d6c;--warn:#f5b942}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--t);font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:12px 20px;background:#151a24;border-bottom:1px solid var(--b)}
h1{font-size:18px;margin:0}main{max-width:1150px;margin:0 auto;padding:18px}
.card{background:var(--panel);border:1px solid var(--b);border-radius:12px;padding:14px 18px;margin-bottom:14px}
h2{font-size:15px;margin:0 0 10px}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.sp{flex:1}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
label.f span{display:block;font-size:12px;color:var(--m);margin-bottom:3px}
input,select,textarea{width:100%;background:var(--bg);color:var(--t);border:1px solid var(--b);border-radius:8px;padding:7px 9px;font:inherit}
textarea{font-family:ui-monospace,Consolas,monospace;font-size:12.5px;min-height:260px}
.btn{border:1px solid var(--b);background:var(--p2);color:var(--t);padding:7px 14px;border-radius:8px;cursor:pointer;font:inherit}
.btn:hover{border-color:var(--a)}.btn.p{background:var(--a);border-color:var(--a);color:#fff;font-weight:600}.btn.d{color:var(--err)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;border:1px solid var(--b);font-size:12px}
.ok{color:var(--ok);border-color:var(--ok)}.bad{color:var(--err);border-color:var(--err)}.wa{color:var(--warn);border-color:var(--warn)}
.chk{display:inline-flex;gap:6px;align-items:center;margin:3px 14px 3px 0;cursor:pointer;white-space:nowrap}
.chk input{width:auto;margin:0;flex:none}
pre{background:#0b0e14;border:1px solid var(--b);border-radius:8px;padding:10px;max-height:320px;overflow:auto;white-space:pre-wrap;font-size:12px;margin:0}
.hint{color:var(--m);font-size:12.5px}.err{border:1px solid var(--err);background:rgba(255,93,108,.08);border-radius:8px;padding:8px 12px;margin-top:8px}
a{color:var(--a)}
</style></head><body>
<header><h1>🔎 SearXNG kezelő</h1><span id="st" class="badge">…</span><span class="sp"></span><span class="hint" id="plat"></span></header>
<main>
<div class="card"><div class="row"><h2 style="margin:0">Állapot</h2><span class="sp"></span>
<button class="btn p" data-a="start">▶ Indítás</button><button class="btn" data-a="restart">↻ Újraindítás</button>
<button class="btn d" data-a="stop">■ Leállítás</button><button class="btn" data-a="install">⤓ Telepítés / frissítés</button>
<a class="btn" id="open" target="_blank" rel="noopener">Megnyitás ↗</a></div>
<div id="info" class="hint" style="margin-top:8px"></div><div id="task"></div></div>

<div class="card"><h2>Beállítások</h2><div class="grid">
<label class="f"><span>Futtatás módja</span><select id="mode"><option value="auto">Automatikus (Docker, ha fut; különben natív)</option><option value="docker">Docker</option><option value="native">Natív (Docker nélkül, Python)</option></select></label>
<label class="f"><span>Port (foglaltság esetén a következő szabad)</span><input id="port" type="number" min="1024" max="65535"></label>
<label class="f"><span>Elérhetőség</span><select id="bind"><option value="127.0.0.1">Csak ez a gép (ajánlott)</option><option value="0.0.0.0">Helyi hálózat is</option></select></label>
<label class="f"><span>Példány neve</span><input id="instance_name"></label>
<label class="f"><span>Alapértelmezett nyelv (auto, hu, en-US…)</span><input id="default_lang"></label>
<label class="f"><span>Biztonságos keresés</span><select id="safe_search"><option value="0">Ki</option><option value="1">Mérsékelt</option><option value="2">Szigorú</option></select></label>
<label class="f"><span>Automatikus kiegészítés</span><select id="autocomplete"><option value="">Ki</option><option value="duckduckgo">DuckDuckGo</option><option value="google">Google</option><option value="wikipedia">Wikipedia</option><option value="brave">Brave</option></select></label>
<label class="f"><span>Kérés időkorlát (s)</span><input id="request_timeout" type="number" step="0.5" min="1" max="60"></label>
<label class="f"><span>Max. időkorlát (s)</span><input id="max_request_timeout" type="number" step="0.5" min="1" max="60"></label>
</div>
<div class="row" style="margin-top:8px"><label class="chk"><input type="checkbox" id="limiter"> Korlátozó (limiter – nyilvános példányhoz)</label>
<label class="chk"><input type="checkbox" id="image_proxy"> Képproxy</label>
<label class="chk"><input type="checkbox" id="open_browser"> Kezelő megnyitása indításkor</label></div>
<h2 style="margin-top:14px">Keresőmotorok</h2><div id="engines"></div>
<div class="row" style="margin-top:12px"><span class="hint">Mentés után a futó SearXNG automatikusan újraindul.</span><span class="sp"></span>
<button class="btn p" id="save">💾 Mentés</button></div></div>

<div class="card"><div class="row"><h2 style="margin:0">Próbakeresés</h2><span class="sp"></span></div>
<div class="row" style="margin-top:8px"><input id="q" placeholder="pl. llama.cpp latest release" style="flex:1"><button class="btn" id="test">🔎 Keresés</button></div>
<div id="tres"></div></div>

<div class="card"><div class="row"><h2 style="margin:0">Haladó: saját settings.yml</h2><span class="sp"></span>
<label class="chk"><input type="checkbox" id="custom_settings"> Saját settings.yml (nem generálom újra)</label></div>
<p class="hint">Csak akkor kapcsold be, ha a fenti mezőkön túli beállítás kell. A <code>search.formats</code> alatt maradjon benne a <code>json</code>, különben az LLM Aréna nem tud keresni.</p>
<textarea id="yaml" spellcheck="false"></textarea><div class="row" style="margin-top:8px"><span class="sp"></span>
<button class="btn" id="yreset">Generált változat visszaállítása</button><button class="btn p" id="ysave">Saját settings.yml mentése</button></div></div>

<div class="card"><div class="row"><h2 style="margin:0">Napló</h2><span class="sp"></span><button class="btn" id="logs">Frissítés</button></div><pre id="log" style="margin-top:8px"></pre></div>

<div class="card"><h2>Eltávolítás</h2><div class="row"><button class="btn d" data-u="docker">Docker konténer + image törlése</button>
<button class="btn d" data-u="native">Natív telepítés törlése</button><span class="hint">A beállítások (config.json) megmaradnak.</span></div></div>
</main><script>
const $=s=>document.querySelector(s);let S=null,dirty=false;
async function api(p,b){const r=await fetch(p,{method:b?"POST":"GET",headers:{"Content-Type":"application/json"},body:b?JSON.stringify(b):undefined});
const d=await r.json().catch(()=>({ok:false,error:"hibás válasz"}));if(!d.ok)throw d;return d}
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function errHtml(e){return `<div class="err"><b>${esc(e.message||e.error||"Hiba")}</b>${e.hint?`<div class="hint">💡 ${esc(e.hint)}</div>`:""}</div>`}
function fill(){const c=S.config;for(const k of["mode","port","bind","instance_name","default_lang","safe_search","autocomplete","request_timeout","max_request_timeout"])$("#"+k).value=c[k];
for(const k of["limiter","image_proxy","open_browser","custom_settings"])$("#"+k).checked=!!c[k];
$("#engines").innerHTML=S.engines.map(e=>`<label class="chk"><input type="checkbox" data-e="${esc(e.name)}" ${c.engines[e.name]?"checked":""}> ${esc(e.label)}</label>`).join("")}
function render(){const b=$("#st");b.textContent=S.running?"fut":S.task.running?"folyamatban…":"nem fut";b.className="badge "+(S.running?"ok":S.task.running?"wa":"bad");
$("#plat").textContent=S.platform;const o=$("#open");o.href=S.url||"#";o.style.pointerEvents=S.running?"":"none";o.style.opacity=S.running?1:.45;
const d=S.docker;$("#info").innerHTML=`${S.running?`✅ Elérhető: <a href="${esc(S.url)}" target="_blank">${esc(S.url)}</a> · `:""}mód: <b>${esc(S.config.mode)}</b> (most: ${esc(S.mode_resolved)}) · Docker: ${d.running?"fut v"+esc(d.version):esc(d.message)} · natív: ${S.native_installed?"telepítve":"nincs telepítve"}${S.native_pid?" (PID "+S.native_pid+")":""}<br>Mappa: <code>${esc(S.folder)}</code>`;
const t=S.task;$("#task").innerHTML=(t.name?`<div class="hint" style="margin-top:8px">Művelet: <b>${esc(t.name)}</b> ${t.running?"⏳":t.error?"❌":"✅"}</div>`:"")+(t.error?errHtml(t.error):"")+(t.lines.length?`<pre style="margin-top:6px">${esc(t.lines.join("\n"))}</pre>`:"");
document.querySelectorAll("[data-a],[data-u]").forEach(x=>x.disabled=t.running)}
async function refresh(first){try{S=await api("/api/status");if(first||!dirty)fill();render()}catch(e){$("#info").innerHTML=errHtml(e)}}
function collect(){const c={};for(const k of["mode","bind","instance_name","default_lang","autocomplete"])c[k]=$("#"+k).value;
for(const k of["port","safe_search","request_timeout","max_request_timeout"])c[k]=+$("#"+k).value;
for(const k of["limiter","image_proxy","open_browser","custom_settings"])c[k]=$("#"+k).checked;
c.engines={};document.querySelectorAll("[data-e]").forEach(x=>c.engines[x.dataset.e]=x.checked);return c}
document.addEventListener("input",e=>{if(e.target.closest(".card")&&e.target.id!=="q"&&e.target.id!=="yaml")dirty=true});
document.addEventListener("click",async e=>{const a=e.target.closest("[data-a]"),u=e.target.closest("[data-u]");
try{if(a){await api("/api/"+a.dataset.a,{});refresh()}
if(u&&confirm("Biztosan törlöd?")){await api("/api/uninstall",{what:u.dataset.u});refresh()}}catch(err){$("#task").innerHTML=errHtml(err)}});
$("#save").onclick=async()=>{try{const r=await api("/api/config",collect());dirty=false;S.config=r.config;fill();loadYaml();refresh()}catch(e){$("#task").innerHTML=errHtml(e)}};
$("#test").onclick=async()=>{$("#tres").innerHTML='<p class="hint">Keresés…</p>';try{const r=await api("/api/test",{q:$("#q").value||"SearXNG"});
$("#tres").innerHTML=`<p class="hint">${r.count} találat${r.unresponsive.length?" · nem válaszolt: "+esc(r.unresponsive.map(x=>x[0]+" ("+x[1]+")").join(", ")):""}</p>`+r.results.map(x=>`<div><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a> <span class="hint">${esc(x.engine)}</span></div>`).join("")}catch(e){$("#tres").innerHTML=errHtml(e)}};
async function loadYaml(){try{$("#yaml").value=(await api("/api/settings-yml")).text}catch(e){}}
$("#ysave").onclick=async()=>{try{await api("/api/settings-yml",{text:$("#yaml").value});$("#custom_settings").checked=true;refresh()}catch(e){$("#task").innerHTML=errHtml(e)}};
$("#yreset").onclick=async()=>{try{await api("/api/settings-yml",{reset:true});loadYaml();refresh(true)}catch(e){$("#task").innerHTML=errHtml(e)}};
$("#logs").onclick=async()=>{try{$("#log").textContent=(await api("/api/logs")).text||"(üres)"}catch(e){}};
refresh(true);loadYaml();$("#logs").click();setInterval(()=>refresh(false),2000);
</script></body></html>"""


class Gui(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return {}

    def do_GET(self):  # noqa: N802
        try:
            if self.path in ("/", "/index.html"):
                body = GUI.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/status":
                self._json({"ok": True, **status()})
            elif self.path == "/api/logs":
                mode = resolve_mode(load_config())
                own = ""
                try:
                    own = (LOG_DIR / "manager.log").read_text("utf-8", "replace")[-3000:]
                except OSError:
                    pass
                self._json({"ok": True, "text": ("=== SearXNG ===\n" + (docker_logs() if mode == "docker"
                                                                       else native_logs()) + "\n=== kezelő ===\n" + own)})
            elif self.path == "/api/settings-yml":
                cfg = load_config()
                text = SETTINGS_FILE.read_text("utf-8", "replace") if SETTINGS_FILE.exists() else \
                    settings_yaml(cfg, resolve_mode(cfg) == "docker")
                self._json({"ok": True, "text": text})
            elif self.path == "/api/health":
                self._json({"ok": True, "app": "searxng-manager"})
            else:
                self._json({"ok": False, "error": "nem található"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": str(e)}, 500)

    def _local_request(self) -> bool:
        """Reject cross-site requests (another web page POSTing to this localhost GUI)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            return False
        return "application/json" in (self.headers.get("Content-Type") or "")

    def do_POST(self):  # noqa: N802
        if not self._local_request():
            self._json({"ok": False, "error": "Csak a helyi kezelőfelületről érkező kérés engedélyezett."}, 403)
            return
        b = self._body()
        try:
            if self.path == "/api/start":
                TASK.start("Indítás", start)
            elif self.path == "/api/stop":
                TASK.start("Leállítás", stop)
            elif self.path == "/api/restart":
                TASK.start("Újraindítás", lambda: (stop(), start())[1])
            elif self.path == "/api/install":
                TASK.start("Telepítés / frissítés", lambda: install())
            elif self.path == "/api/uninstall":
                what = b.get("what")
                if what not in ("docker", "native"):
                    raise Failure("Ismeretlen eltávolítás.")
                TASK.start("Eltávolítás", lambda: uninstall(what))
            elif self.path == "/api/config":
                old = load_config()
                cfg = validate(b, old)
                save_config(cfg)
                was_running = status()["running"]
                write_settings(cfg, resolve_mode(cfg))
                if was_running and not TASK.running:
                    TASK.start("Újraindítás (új beállítások)", lambda: (stop(), start())[1])
                return self._json({"ok": True, "config": {k: v for k, v in cfg.items() if k != "secret_key"}})
            elif self.path == "/api/settings-yml":
                cfg = load_config()
                if b.get("reset"):
                    cfg["custom_settings"] = False
                    save_config(cfg)
                    write_settings(cfg, resolve_mode(cfg))
                else:
                    text = str(b.get("text") or "")
                    if "use_default_settings" not in text and "server:" not in text:
                        raise Failure("Ez nem tűnik SearXNG settings.yml-nek.")
                    DATA_DIR.mkdir(parents=True, exist_ok=True)
                    SETTINGS_FILE.write_text(text, "utf-8")
                    cfg["custom_settings"] = True
                    save_config(cfg)
                if status()["running"] and not TASK.running:
                    TASK.start("Újraindítás (settings.yml)", lambda: (stop(), start())[1])
            elif self.path == "/api/test":
                return self._json({"ok": True, **test_search(str(b.get("q") or "SearXNG"))})
            else:
                return self._json({"ok": False, "error": "nem található"}, 404)
            self._json({"ok": True})
        except Failure as e:
            self._json({"ok": False, "message": str(e), "error": str(e), "hint": e.hint}, 400)
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "message": f"{type(e).__name__}: {e}", "error": str(e)}, 500)


def gui(autostart: bool, open_browser: bool | None = None) -> None:
    cfg = load_config()
    port = cfg["gui_port"]
    for _ in range(30):
        if port_free(port):
            break
        st, data = http_json(f"http://127.0.0.1:{port}/api/health", 2)
        if isinstance(data, dict) and data.get("app") == "searxng-manager":
            print(f"A kezelő már fut: http://127.0.0.1:{port}/")
            webbrowser.open(f"http://127.0.0.1:{port}/")
            return
        port += 1
    srv = ThreadingHTTPServer(("127.0.0.1", port), Gui)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"SearXNG kezelő: {url}   (bezárás: Ctrl+C – a SearXNG tovább fut)")
    if autostart:
        TASK.start("Indítás", start)
    if open_browser if open_browser is not None else cfg.get("open_browser", True):
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nA kezelő bezárva (a SearXNG tovább fut; leállítás: manager.py stop).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SearXNG kezelő (telepítés, indítás, beállítások)")
    ap.add_argument("command", nargs="?", default="gui",
                    choices=["gui", "start", "stop", "restart", "status", "install", "test", "url"])
    ap.add_argument("--autostart", action="store_true", help="gui: a SearXNG indítása (és telepítése) azonnal")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--mode", choices=["auto", "docker", "native"])
    ap.add_argument("--port", type=int)
    ap.add_argument("--json", action="store_true", help="gépi olvasható kimenet")
    a = ap.parse_args(argv)
    for f in (sys.stdout, sys.stderr):  # Windows console code pages cannot print every character
        try:
            f.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    if a.mode or a.port:
        cfg = validate({k: v for k, v in (("mode", a.mode), ("port", a.port)) if v}, load_config())
        save_config(cfg)
    try:
        if a.command == "gui":
            gui(a.autostart, False if a.no_browser else None)
            return 0
        if a.command in ("start", "restart"):
            if a.command == "restart":
                stop()
            url = start()
            if a.json:
                print(json.dumps({"ok": True, "url": url}))
        elif a.command == "stop":
            stop()
        elif a.command == "install":
            install(a.mode)
        elif a.command == "status":
            st = status()
            st.pop("task", None)
            print(json.dumps(st, ensure_ascii=False, indent=None if a.json else 2))
        elif a.command == "url":
            st = status()
            print(st["url"] if st["running"] else "")
            return 0 if st["running"] else 1
        elif a.command == "test":
            print(json.dumps(test_search("SearXNG"), ensure_ascii=False, indent=2))
        return 0
    except Failure as e:
        msg = {"ok": False, "error": str(e), "hint": e.hint}
        print(json.dumps(msg, ensure_ascii=False) if a.json else f"HIBA: {e}\n{('Tipp: ' + e.hint) if e.hint else ''}",
              file=sys.stderr if not a.json else sys.stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
