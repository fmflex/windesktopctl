"""Shared helpers for live scripts that drive an isolated Chrome instance.

SAFETY (we already damaged a user Notepad tab once - do not repeat it):

- NEVER touch the user's own Chrome/Edge profile or windows, and never visit
  real sites or real CAPTCHAs. Only local file:// pages under tests/pages/.
- Launch chrome.exe with a dedicated --user-data-dir under %TEMP%, which
  gives a separate process tree we can identify and clean up.
- Every page sets document.title to include a unique token (URL hash); the
  FOREGROUND window title must contain that token - checked via the same
  assert_safe_target() guard the Notepad scripts use - before EVERY input
  action (click/type/key/move).
- Cleanup closes only the window we opened (window close on its hwnd),
  waits for the process to exit, then deletes the temp profile dir. We
  NEVER taskkill; if Chrome refuses to close we report it and leave it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deskctl.core import api  # noqa: E402
from deskctl.core import windows as win  # noqa: E402

from _live_notepad import assert_safe_target, run  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PAGES = Path(__file__).resolve().parent / "pages"
MANIFEST = Path(tempfile.gettempdir()) / "deskctl_live_browser" / "manifest.json"


def page_url(name: str, token: str) -> str:
    """file:// URL for tests/pages/<name> with #<token> (sets doc title)."""
    return (PAGES / name).resolve().as_uri() + "#" + token


def find_chrome_window(token: str, before: set[int], timeout: float = 25.0) -> int:
    """Poll for a chrome window whose title contains our unique token."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for w in win.list_windows():
            title = (w.title or "").lower()
            proc = (w.process or "").lower()
            if token.lower() in title and "chrome" in proc:
                return w.hwnd
        time.sleep(0.4)
    return 0


class ChromeSession:
    """One isolated chrome.exe --user-data-dir=<temp> window on a test page."""

    def __init__(self, page: str, token: str, urls: list[str] | None = None):
        self.token = token  # unique substring expected in the window title
        self.page = page
        self.urls = urls or [page_url(page, token)]
        self.profile = Path(tempfile.mkdtemp(prefix="deskctl_chrome_"))
        self.hwnd = 0
        self.proc: subprocess.Popen | None = None
        self.preexisting = False

    def launch(self) -> "ChromeSession":
        before = {w.hwnd for w in win.list_windows()}
        if not Path(CHROME).exists():
            raise RuntimeError(f"chrome.exe not found at {CHROME}")
        self.proc = subprocess.Popen(
            [
                CHROME,
                f"--user-data-dir={self.profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-mode",  # exit when the last window closes
                "--new-window",
                *self.urls,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.hwnd = find_chrome_window(self.token, before)
        if not self.hwnd:
            raise RuntimeError(
                f"no chrome window titled with {self.token!r} appeared"
            )
        self.preexisting = self.hwnd in before
        info = win.window_info(self.hwnd)
        print(
            f"chrome session: hwnd={self.hwnd} pid={info.pid if info else '?'} "
            f"profile={self.profile} title={info.title if info else '?'}"
        )
        return self

    def focus(self) -> "ChromeSession":
        info = win.window_info(self.hwnd)
        if info is not None and info.iconic:
            run(api.window, "restore", str(self.hwnd))
            time.sleep(0.5)
        run(api.focus, str(self.hwnd))
        run(api.wait_for, "window", self.token, 10.0, "focused")
        time.sleep(0.6)
        assert_safe_target(self.token)
        return self

    def act(self, *args, **kwargs) -> str:
        """api.act guarded by assert_safe_target before every input action."""
        assert_safe_target(self.token)
        return run(api.act, *args, **kwargs)

    def key(self, combo: str) -> str:
        return self.act("key", None, None, combo)

    def window_spec(self):
        return str(self.hwnd)

    def close(self) -> bool:
        """Close only our window, wait for chrome to exit, delete profile.
        Never taskkill - if it won't close we report and leave it."""
        ok = True
        info = win.window_info(self.hwnd)
        if info is not None and info.iconic:
            try:
                run(api.window, "restore", str(self.hwnd))
                time.sleep(0.5)
            except Exception as ex:
                print("restore before close failed:", ex)
        try:
            run(api.window, "close", str(self.hwnd))
        except Exception as ex:
            print("window close failed:", ex)
            ok = False
        # Wait for our window to disappear.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not any(w.hwnd == self.hwnd for w in win.list_windows()):
                break
            time.sleep(0.4)
        gone = not any(w.hwnd == self.hwnd for w in win.list_windows())
        if not gone:
            print(f"WARN: chrome window hwnd={self.hwnd} still open; leaving it")
            ok = False
        # Wait for the browser process to exit (it is our Popen child).
        if self.proc is not None:
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                print(
                    f"WARN: chrome pid={self.proc.pid} did not exit; "
                    "leaving profile dir in place"
                )
                ok = False
            except Exception as ex:
                print("proc wait error:", ex)
        try:
            shutil.rmtree(self.profile, ignore_errors=True)
        except Exception as ex:
            print("profile cleanup error:", ex)
            ok = False
        return ok


def manifest_load() -> dict:
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return {}


def manifest_save(data: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print("manifest:", MANIFEST)
