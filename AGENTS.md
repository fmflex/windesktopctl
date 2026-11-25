# windesktopctl — agent notes

Local Windows desktop observe/control helper: UIA text-first, OCR fallback,
screenshots on demand. MCP server + CLI share `windesktopctl.core.api`; all core ops
run on a single COM-initialized worker thread (`windesktopctl.core.worker`).

## Layout

- `windesktopctl/core/` — dpi, worker, capture (mss), uia (uiautomation; cached
  `FindAllBuildCache` path + BFS `walk()` fallback, `WINDESKTOPCTL_UIA` selects),
  ocr (winrt via winocr deps), elements (pure logic + state flags), state
  (JSON in %LOCALAPPDATA%\windesktopctl, WINDESKTOPCTL_HOME overrides), input (ctypes
  SendInput, hwheel), windows (list/focus + min/max/restore/close/move/resize),
  diff, privacy (WINDESKTOPCTL_EXCLUDE + capture blackouts), safety
  (pause/failsafe/hotkey/LL-hook takeover), token (bearer token file/ACL/
  rotate/live-reload provider), api (high-level ops).
- `windesktopctl/mcp_server.py` — 14 FastMCP tools + `--http` Bearer-auth
  (token provider + 429 throttle) + Host-header allowlist (`--allowed-host`);
  `WINDESKTOPCTL_TOOLS` filters tools. `build_http_app()` is shared.
- `windesktopctl/serve.py` — `windesktopctl serve` CLI backend: binds
  127.0.0.1, optional `tailscale serve|funnel --bg <port>` exposure with
  :443 pre-check and turn-off-only-what-we-enabled cleanup (never `reset`).
- `windesktopctl/core/challenge.py` — pure logic for the human-challenge relay:
  `detect_challenge`, grid overlay (lettered + numbered modes), cell/region
  parsing, lattice inference, webhook multipart body.
  windesktopctl NEVER solves challenges; `challenge_answer` is the only input
  path while one is active (act/window/browser tab|navigate are gated).
- `windesktopctl/cli.py` — argparse CLI (`--json` global), exit 1 on error;
  `token`, `serve`, `guard` subcommands.
- `scripts/install.ps1`, `scripts/start.ps1`, `start.cmd` — one-command
  install + serve launchers.
- `tests/` — pytest unit tests (pure logic) + live/manual check scripts.
- `THIRD_PARTY_NOTICES.md` — Windows-MCP MIT notice (v2 design borrow).

## Commands

```powershell
.\.venv\Scripts\python -m pip install -e .[dev]   # install (console scripts)
.\.venv\Scripts\python -m pytest -q              # unit tests
.\.venv\Scripts\python tests\smoke_live.py       # live desktop smoke (manual)
.\.venv\Scripts\python tests\mcp_stdio_check.py  # MCP stdio check (manual)
.\.venv\Scripts\python tests\mcp_http_check.py   # MCP HTTP 401 check (manual)
```

## Conventions / gotchas

- **Never write to stdout in the MCP server process** except MCP protocol —
  uiautomation's logger is neutered in `mcp_server._silence_uiautomation`;
  diagnostics go to stderr or `%LOCALAPPDATA%\windesktopctl\actions.log`.
- All screen coords are physical virtual-screen px (process is per-monitor
  DPI aware via `windesktopctl.core.dpi`, applied at `windesktopctl.core` import).
- Element ids / screenshot mapping live in `state.json`; targets older than
  10 min are refused as stale (`state.OBSERVE_MAX_AGE`, `SHOT_MAX_AGE`). For
  window observes, `resolve_target` also refuses when the hwnd is gone,
  re-focuses it if not foreground, and refuses if the rect moved.
- `act()`/`window()`/`launch()` gate on `safety.check_before_action()`
  (pause flag + failsafe corner + user-takeover via LL hooks; injected input
  ignored, heartbeat file `user_active` shared with CLI processes) and log
  every action to `actions.log`.
- OCR needs winrt `Windows.Media.Ocr`; it degrades to `WindesktopctlError` if the
  language pack/engine is missing — observe(mode="auto") falls back gracefully.
- `WINDESKTOPCTL_EXCLUDE` adds excluded process names (password managers built-in);
  excluded windows can't be observed and are blacked out of captures.
- Do NOT commit secrets; `WINDESKTOPCTL_TOKEN` env overrides the token file
  for HTTP mode (`token --rotate` live-reloads; >10 auth fails/60s -> 429).
