"""Optional real-browser backend: Playwright (+ playwright-stealth) for JS-heavy
or bot-protected pages.

    pip install playwright playwright-stealth
    python -m playwright install chromium      # or use installed Edge / Chrome

The Playwright sync API is bound to the thread that started it, while the
arena calls tools from several worker threads. So one dedicated thread owns the
browser; callers submit jobs through a queue. The browser starts lazily and is
closed after a period of inactivity.
"""

from __future__ import annotations

import glob
import os
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

IDLE_CLOSE = 300.0  # seconds
BLOCK_TYPES = {"image", "media", "font"}


class BrowserUnavailable(Exception):
    pass


def installed() -> dict:
    """What is available (for the settings page)."""
    info = {"playwright": False, "stealth": False, "stealth_version": ""}
    try:
        import playwright  # noqa: F401
        info["playwright"] = True
    except ImportError:
        pass
    try:
        import playwright_stealth  # noqa: F401
        info["stealth"] = True
        info["stealth_version"] = "2" if hasattr(playwright_stealth, "Stealth") else "1"
    except ImportError:
        pass
    return info


def _apply_stealth(context, page) -> bool:
    try:
        import playwright_stealth as ps
    except ImportError:
        return False
    try:
        if hasattr(ps, "Stealth"):          # playwright-stealth >= 2
            ps.Stealth().apply_stealth_sync(context)
            return True
        if hasattr(ps, "stealth_sync"):     # 1.x
            ps.stealth_sync(page)
            return True
    except Exception:  # noqa: BLE001 - stealth is best effort
        return False
    return False


class _Worker:
    def __init__(self) -> None:
        self.q: "queue.Queue[tuple[Callable, Future] | None]" = queue.Queue()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.pw = None
        self.browser = None
        self.context = None
        self.stealth = False
        self.launch_info = ""
        self.cfg_key: tuple = ()
        self.last_used = 0.0

    # ---------------------------------------------------------- thread side
    def _ensure(self, cfg: dict) -> None:
        key = (cfg.get("browser_channel") or "", cfg.get("browser_path") or "", bool(cfg.get("headless", True)))
        if self.browser and key == self.cfg_key:
            return
        self._close()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise BrowserUnavailable("A Playwright nincs telepítve. Telepítés: pip install playwright playwright-stealth "
                                     "és python -m playwright install chromium (vagy válaszd az Edge/Chrome böngészőt).")
        self.pw = sync_playwright().start()
        headless = bool(cfg.get("headless", True))
        attempts: list[tuple[str, dict]] = []
        path = (cfg.get("browser_path") or "").strip()
        channel = (cfg.get("browser_channel") or "").strip()
        if path:
            attempts.append((f"megadott fájl: {path}", {"executable_path": path}))
        if channel == "chromium":
            attempts.append(("Playwright Chromium", {}))
        elif channel and channel != "auto":
            attempts.append((f"csatorna: {channel}", {"channel": channel}))
        else:
            attempts.append(("Playwright Chromium", {}))
            for ch in ("msedge", "chrome"):
                attempts.append((f"telepített {ch}", {"channel": ch}))
            for exe in sorted(glob.glob(os.path.join(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"),
                                                     "chromium-*", "chrome-*", "chrome*"))):
                if os.path.basename(exe) in ("chrome", "chrome.exe", "chromium") and os.path.isfile(exe) \
                        and os.access(exe, os.X_OK):
                    attempts.append((f"Chromium: {exe}", {"executable_path": exe}))
        errors = []
        for label, kw in attempts:
            try:
                self.browser = self.pw.chromium.launch(headless=headless, args=[
                    "--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"], **kw)
                self.launch_info = label
                break
            except Exception as e:  # noqa: BLE001
                errors.append(f"{label}: {str(e).splitlines()[0][:160]}")
        if not self.browser:
            self._close()
            raise BrowserUnavailable("Nem sikerült böngészőt indítani. Futtasd: python -m playwright install chromium, "
                                     "vagy válaszd az Edge/Chrome csatornát. Próbálkozások: " + " | ".join(errors))
        self.context = self.browser.new_context(
            locale=cfg.get("locale") or "hu-HU", viewport={"width": 1366, "height": 900},
            java_script_enabled=True, ignore_https_errors=False)
        page = self.context.new_page()
        self.stealth = _apply_stealth(self.context, page)
        page.close()
        self.cfg_key = key

    def _close(self) -> None:
        for obj, meth in ((self.context, "close"), (self.browser, "close"), (self.pw, "stop")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:  # noqa: BLE001
                pass
        self.pw = self.browser = self.context = None

    def _loop(self) -> None:
        while True:
            try:
                item = self.q.get(timeout=30)
            except queue.Empty:
                if self.browser and time.monotonic() - self.last_used > IDLE_CLOSE:
                    self._close()
                continue
            if item is None:
                self._close()
                return
            fn, fut = item
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn(self))
                except BaseException as e:  # noqa: BLE001
                    fut.set_exception(e)
            self.last_used = time.monotonic()

    # ---------------------------------------------------------- caller side
    def submit(self, fn: Callable[["_Worker"], Any], timeout: float) -> Any:
        with self.lock:
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="arena-browser", daemon=True)
                self.thread.start()
        fut: Future = Future()
        self.q.put((fn, fut))
        return fut.result(timeout=timeout)


_WORKER = _Worker()


def fetch_html(url: str, cfg: dict, *, url_guard: Callable[[str], None] | None = None,
               timeout: float = 25.0) -> dict:
    """Render ``url`` in the stealth browser. Returns {url, title, html, status, stealth, browser}."""

    def job(w: _Worker) -> dict:
        w._ensure(cfg)
        page = w.context.new_page()
        try:
            def route(r):
                req = r.request
                if req.resource_type in BLOCK_TYPES:
                    return r.abort()
                if url_guard and req.is_navigation_request():
                    try:
                        url_guard(req.url)
                    except Exception:  # noqa: BLE001
                        return r.abort("blockedbyclient")
                return r.continue_()
            page.route("**/*", route)
            resp = page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            try:
                page.wait_for_load_state("networkidle", timeout=min(6000, int(timeout * 400)))
            except Exception:  # noqa: BLE001 - busy pages never go idle
                pass
            return {"url": page.url, "title": page.title(), "html": page.content(),
                    "status": resp.status if resp else None, "stealth": w.stealth, "browser": w.launch_info}
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    try:
        return _WORKER.submit(job, timeout=timeout + 40)
    except BrowserUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        msg = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
        raise BrowserUnavailable(f"Böngészős letöltés sikertelen: {msg}")


def status(cfg: dict, probe: bool = False) -> dict:
    info = installed()
    info.update(running=bool(_WORKER.browser), browser=_WORKER.launch_info, stealth_active=_WORKER.stealth)
    if probe and info["playwright"]:
        try:
            def job(w: _Worker) -> dict:
                w._ensure(cfg)
                page = w.context.new_page()
                try:
                    page.set_content("<html><body>ok</body></html>")
                    return {"webdriver": page.evaluate("navigator.webdriver"),
                            "ua": page.evaluate("navigator.userAgent")}
                finally:
                    page.close()
            res = _WORKER.submit(job, timeout=60)
            info.update(ok=True, running=True, browser=_WORKER.launch_info, stealth_active=_WORKER.stealth, **res)
        except Exception as e:  # noqa: BLE001
            info.update(ok=False, error=str(e)[:600])
    return info


def shutdown() -> None:
    if _WORKER.thread and _WORKER.thread.is_alive():
        _WORKER.q.put(None)
