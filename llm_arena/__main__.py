"""Entry point: ``python -m llm_arena [--host 127.0.0.1] [--port 8765]``."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from . import __version__
from .app import ArenaApp
from .server import make_server


DEFAULT_PORT = 8765


def port_owner(host: str, port: int) -> str:
    """'free', 'arena' (an LLM Arena already runs there) or 'other'."""
    import json
    import socket
    import urllib.request
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex((host, port)) != 0:
            return "free"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{host}:{port}/api/health", timeout=2) as r:
            data = json.loads(r.read(20000) or b"{}")
            if isinstance(data, dict) and isinstance(data.get("version"), dict):
                return "arena"
    except Exception:  # noqa: BLE001
        pass
    return "other"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="llm_arena", description="Helyi LLM Aréna – két Llama Server együttműködése")
    ap.add_argument("--host", default="127.0.0.1", help="figyelési cím (alapértelmezés: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=None, help="port (alapértelmezés: 8765, foglaltság esetén a következő szabad)")
    ap.add_argument("--data-dir", default="data", help="projektek és beállítások mappája")
    ap.add_argument("--open", action="store_true", help="böngésző megnyitása indításkor")
    ap.add_argument("--selftest", action="store_true", help="önellenőrzés futtatása a konzolon, majd kilépés")
    args = ap.parse_args(argv)

    if args.selftest:
        from . import selftest
        from .project import ProjectStore
        store = ProjectStore(args.data_dir)
        rep = selftest.run(store.settings(), store.snapshot()["llms"], args.data_dir)
        selftest.print_report(rep)
        return 0 if not rep["failed"] else 2

    explicit = args.port is not None
    port = args.port or DEFAULT_PORT
    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    for _ in range(20):
        who = port_owner(shown_host, port)
        if who == "free":
            break
        if who == "arena":
            url = f"http://{shown_host}:{port}/"
            print(f"Ezen a porton már fut egy LLM Aréna: {url} – azt nyitom meg.")
            webbrowser.open(url)
            return 0
        msg = f"A {port}-es portot egy MÁSIK program használja."
        if explicit:
            print(f"{msg} Adj meg másik portot: python -m llm_arena --port {port + 1}", file=sys.stderr)
            return 1
        print(f"{msg} Az LLM Aréna a következő szabad portot próbálja…", file=sys.stderr)
        port += 1
    app = ArenaApp(args.data_dir)
    try:
        server = make_server(app, args.host, port)
    except OSError as e:
        print(f"Nem sikerült elindítani a szervert {args.host}:{port} – {e}\n"
              f"Tipp: egy másik program foglalja a portot. Próbáld: python -m llm_arena --port {port + 1}",
              file=sys.stderr)
        return 1
    url = f"http://{shown_host}:{port}/"
    print(f"LLM Aréna {__version__} fut: {url}  (leállítás: Ctrl+C)")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("FIGYELEM: a szerver nem csak helyben érhető el; nincs hitelesítés!", file=sys.stderr)
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nLeállítás…")
    finally:
        app.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
