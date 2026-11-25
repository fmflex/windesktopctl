"""Live check for browser() + the challenge relay - ISOLATED Chrome only.

SAFETY: only local file:// pages under tests/pages/, unique title tokens,
assert_safe_target before every input, close only our own window, never
taskkill, delete the temp profile afterwards. Never touches the user's
Chrome/Edge or real sites.

Phases (chrome windows stay open between them so challenge ids stay valid):

  python tests/live_browser_check.py capture
      article.html browser info/tabs/tab/text/navigate checks, then launches
      the three fake challenges, verifies observe flags them and act/window/
      browser-tab refuse, runs challenge() and writes a manifest + PNGs.

  python tests/live_browser_check.py answer PATH/TO/answers.json
      for each manifest entry calls challenge_answer() with the answers YOU
      chose from the saved PNGs (you are "the user"), verifies clearing,
      closes the chrome windows and deletes the temp profiles.

answers.json shape:
  {"grid": {"cells": "B3 C4"}, "text": {"text": "K7Q2-X9"},
   "checkbox": {"cells": "B1"}}
"""

from __future__ import annotations

import json
import random
import string
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _live_browser import (  # noqa: E402
    CHROME,
    MANIFEST,
    ChromeSession,
    manifest_load,
    manifest_save,
    page_url,
)
from _live_notepad import assert_safe_target, run  # noqa: E402
from deskctl.core import DeskctlError, api, windows as win  # noqa: E402


def tok() -> str:
    return "t" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))


def expect_error(fn, needle: str) -> str:
    try:
        out = fn()
    except DeskctlError as ex:
        return str(ex)
    except Exception as ex:
        return f"{type(ex).__name__}: {ex}"
    return f"NO ERROR (returned {out!r})"


# ------------------------------------------------------------------ article --

def article_checks() -> None:
    print("\n=== article.html: browser info/tabs/tab/text/navigate ===")
    t1, t2 = tok(), tok()
    s = ChromeSession(
        "article.html",
        t1,
        urls=[page_url("article.html", t1), page_url("article.html", t2)],
    ).launch()
    try:
        s.focus()
        hwnd = s.window_spec()
        info = run(api.browser, "info", None, hwnd)
        print("--- browser info ---\n" + info)
        check("info: 2 tabs", "tabs=2" in info, info.splitlines()[0] if info else "")
        check("info: url is file:// article", "article.html" in info)

        tabs = run(api.browser, "tabs", None, hwnd)
        print("--- browser tabs ---\n" + tabs)
        check("tabs: t1+t2 lines", "[t1]" in tabs and "[t2]" in tabs)
        check("tabs: selected marker", "selected" in tabs)

        out = run(api.browser, "tab", "2", hwnd)
        check("tab 2 selects", "tab" in out.lower(), out)
        time.sleep(0.5)
        tabs = run(api.browser, "tabs", None, hwnd)
        check("tab 2 now selected", tabs.count("selected") == 1 and "t2" in
              [l for l in tabs.splitlines() if "selected" in l][0])

        out = run(api.browser, "tab", t1, hwnd)  # title substring -> tab 1
        check("tab by title substring", "tab" in out.lower(), out)

        # observe() must NOT flag the article (it says 'robot' in prose)
        obs = run(api.observe, hwnd, "auto", 200)
        check("article: no challenge false-positive",
              "!! challenge detected" not in obs)

        text = run(api.browser, "text", None, hwnd, 4000)
        print("--- browser text (head) ---\n" + "\n".join(text.splitlines()[:12]))
        check("text: heading '# '", "# Deskctl Test Article" in text)
        check("text: link '[...]'", "[example domain]" in text)
        check("text: offscreen content", "Below the Fold" in text)
        check("text: ~tokens reported", "~tokens=" in text)

        # navigate the focused tab to a DIFFERENT page (visible change)
        t3 = tok()
        out = run(api.browser, "navigate", page_url("notes.html", t3), hwnd)
        print("--- navigate ---\n" + out)
        check("navigate changed", "changed" in out.lower().splitlines()[0],
              out.splitlines()[0] if out else "")
        time.sleep(0.5)
        info = run(api.browser, "info", None, hwnd)
        check("navigate new title", f"deskctl-test-notes-{t3}" in info, info.splitlines()[0])
    finally:
        ok = s.close()
        check("article chrome closed+cleaned", ok)


# --------------------------------------------------------------- challenges --

CHALLENGES = [
    ("grid", "fake_challenge_grid.html", "grid", {"rows": 16, "cols": 16}),
    ("text", "fake_challenge_text.html", "text", {}),
    ("checkbox", "fake_challenge_checkbox.html", "checkbox", {}),
]


def challenge_capture() -> None:
    print("\n=== fake challenges: detect -> gate -> challenge() ===")
    manifest = manifest_load()
    manifest.setdefault("sessions", {})
    for key, page, want_kind, dims in CHALLENGES:
        print(f"\n--- {key} ---")
        token = tok()
        s = ChromeSession(page, token).launch()
        try:
            s.focus()
            hwnd = s.window_spec()
            obs = run(api.observe, hwnd, "auto", 300)
            flagged = "!! challenge detected" in obs
            kind_ok = f"kind={want_kind}" in obs
            check(f"{key}: observe flags challenge", flagged, obs.splitlines()[-1] if obs else "")
            check(f"{key}: kind={want_kind}", kind_ok)

            # act() must refuse while the challenge window is foreground
            err = expect_error(lambda: run(api.act, "click", "100,100"), "")
            check(f"{key}: act refused", "challenge on screen" in err, err[:90])
            err = expect_error(
                lambda: run(api.window, "minimize", hwnd), ""
            )
            check(f"{key}: window refused", "challenge on screen" in err, err[:90])
            err = expect_error(
                lambda: run(api.browser, "tab", "t1", hwnd), ""
            )
            check(f"{key}: browser tab refused", "challenge on screen" in err, err[:90])

            kw = dict(dims)
            png, info = run(api.challenge, hwnd, kw.get("rows"), kw.get("cols"))
            print("--- challenge info ---\n" + info)
            cid = int(info.split("id=")[1].split()[0])
            path = Path(info.split("saved=")[1].split()[0])
            check(f"{key}: challenge id+png", path.exists() and path.stat().st_size > 1000,
                  str(path))
            manifest["sessions"][key] = {
                "hwnd": int(hwnd),
                "token": token,
                "page": page,
                "profile": str(s.profile),
                "pid": s.proc.pid if s.proc else 0,
                "challenge_id": cid,
                "png": str(path),
                "want_kind": want_kind,
                "dims": dims,
            }
            # LEAVE THE WINDOW OPEN - challenge_answer needs the same hwnd.
        except Exception as ex:
            print(f"{key} capture FAILED:", ex)
            check(f"{key}: capture", False, str(ex))
            try:
                s.close()
            except Exception:
                pass
    manifest_save(manifest)


def challenge_answer_all(answers_path: str) -> None:
    print("\n=== challenge_answer phase ===")
    manifest = manifest_load()
    answers = json.loads(Path(answers_path).read_text(encoding="utf-8"))
    sessions = manifest.get("sessions", {})
    for key, sess in sessions.items():
        print(f"\n--- {key} ---")
        ans = answers.get(key) or {}
        hwnd = int(sess["hwnd"])
        if not any(w.hwnd == hwnd for w in win.list_windows()):
            check(f"{key}: window still open", False, "hwnd gone - relaunch needed")
            continue
        try:
            run(api.focus, str(hwnd))
            time.sleep(0.4)
            assert_safe_target(sess["token"])
            # state holds ONE active challenge - re-capture for a fresh id
            # (same window/rect => same grid geometry => cells stay valid)
            dims = sess.get("dims") or {}
            _png, info = run(
                api.challenge, str(hwnd), dims.get("rows"), dims.get("cols")
            )
            cid = int(info.split("id=")[1].split()[0])
            print(f"fresh challenge id={cid}")
            out = run(
                api.challenge_answer,
                cid,
                ans.get("cells"),
                ans.get("text"),
                ans.get("submit", True),
                ans.get("enter", False),
            )
            print("answer:", out)
            check(f"{key}: cleared", "cleared" in out.lower(), out[:120])
            # state should be cleared now -> window close allowed again
            err = expect_error(lambda: run(api.window, "close", str(hwnd)), "")
            check(f"{key}: close allowed after clear", "challenge on screen" not in err, err[:80])
        except Exception as ex:
            check(f"{key}: answer", False, f"{type(ex).__name__}: {ex}")
        # cleanup chrome (window may already be closed)
        profile = Path(sess.get("profile", ""))
        proc_pid = sess.get("pid", 0)
        try:
            run(api.window, "close", str(hwnd))
        except Exception:
            pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not any(w.hwnd == hwnd for w in win.list_windows()):
                break
            time.sleep(0.4)
        # wait for pid exit (poll up to 15s - chrome takes a moment)
        import ctypes

        alive = True
        for _ in range(30):
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(proc_pid))
            if not h:
                alive = False
                break
            ctypes.windll.kernel32.CloseHandle(h)
            time.sleep(0.5)
        if alive:
            print(f"WARN {key}: chrome pid={proc_pid} still alive; leaving profile")
        else:
            import shutil

            shutil.rmtree(profile, ignore_errors=True)
            print(f"{key}: chrome closed, profile removed")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("capture", "answer"):
        print(__doc__)
        return 2
    if not Path(CHROME).exists():
        print("chrome.exe not found:", CHROME)
        return 2
    if sys.argv[1] == "capture":
        article_checks()
        challenge_capture()
    else:
        if len(sys.argv) < 3:
            print("answer phase needs a path to answers.json")
            return 2
        challenge_answer_all(sys.argv[2])

    print("\n=== summary ===")
    bad = [r for r in RESULTS if not r[1]]
    for name, ok, extra in RESULTS:
        print(("PASS " if ok else "FAIL ") + name)
    print(f"{len(RESULTS) - len(bad)}/{len(RESULTS)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
