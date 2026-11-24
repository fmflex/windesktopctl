# deskctl — agent notes

Local Windows desktop observe/control helper: UIA text-first, OCR fallback,
screenshots on demand. MCP server + CLI share `deskctl.core.api`; all core ops
run on a single COM-initialized worker thread (`deskctl.core.worker`).

## Layout

- `deskctl/core/` — dpi, worker, capture (mss), uia (uiautomation), ocr
  (winrt via winocr deps), elements (pure logic), state (JSON in
  %LOCALAPPDATA%\deskctl, DESKCTL_HOME overrides), input (ctypes SendInput),
  windows, diff, safety (pause/failsafe/hotkey), api (high-level ops).
- `deskctl/mcp_server.py` — FastMCP tools + `--http` Bearer-auth mode.
- `deskctl/cli.py` — argparse CLI, exit 1 on error.
- `tests/` — pytest unit tests (pure logic) + live/manual check scripts.

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
  diagnostics go to stderr or `%LOCALAPPDATA%\deskctl\actions.log`.
- All screen coords are physical virtual-screen px (process is per-monitor
  DPI aware via `deskctl.core.dpi`, applied at `deskctl.core` import).
- Element ids / screenshot mapping live in `state.json`; targets older than
  10 min are refused as stale (`state.OBSERVE_MAX_AGE`, `SHOT_MAX_AGE`).
- `act()` gates on `safety.check_before_action()` (pause flag + failsafe
  corner) and logs every action to `actions.log`.
- OCR needs winrt `Windows.Media.Ocr`; it degrades to `DeskctlError` if the
  language pack/engine is missing — observe(mode="auto") falls back gracefully.
- Do NOT commit secrets; `DESKCTL_TOKEN` is env-only for HTTP mode.
