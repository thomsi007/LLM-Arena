"""Entry point: ``python -m llm_arena [--host 127.0.0.1] [--port 8765]``."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from . import __version__
from .app import ArenaApp
from .server import make_server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="llm_arena", description="Helyi LLM Aréna – két Llama Server együttműködése")
    ap.add_argument("--host", default="127.0.0.1", help="figyelési cím (alapértelmezés: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8765)
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

    app = ArenaApp(args.data_dir)
    try:
        server = make_server(app, args.host, args.port)
    except OSError as e:
        print(f"Nem sikerült elindítani a szervert {args.host}:{args.port} – {e}", file=sys.stderr)
        return 1
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}/"
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
