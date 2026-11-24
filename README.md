# deskctl

Local Windows desktop helper that lets an LLM agent (e.g. Devin) **observe and
control the user's desktop cheaply**. It reads the UI as **text first**
(UI Automation accessibility tree -> numbered elements), falls back to **OCR**
(Windows.Media.Ocr), and only uses **screenshots** when needed (downscaled,
with numbered set-of-marks boxes). The helper itself never calls an LLM.

Exposed two ways with the same operations:

- an **MCP server** (stdio by default, optional Streamable HTTP) for agents
  that speak MCP;
- a **CLI** (`deskctl ...`) so a remote agent that can only run shell commands
  can drive the desktop.

All coordinates are physical virtual-screen pixels. Element ids, the last
screenshot mapping, and the diff baseline persist in
`%LOCALAPPDATA%\deskctl\state.json`, so ids are shared across CLI/MCP calls
and processes (stale after 10 minutes).

## Install

```powershell
cd C:\Users\funkm\repo\deskctl
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
```

## Register with Devin (MCP, stdio)

```powershell
devin mcp add deskctl -- C:\Users\funkm\repo\deskctl\.venv\Scripts\deskctl-mcp.exe
```

The server starts the `Ctrl+Alt+Shift+K` pause hotkey automatically.

## CLI examples

```powershell
deskctl windows                        # list top-level windows
deskctl observe --window focused       # text map of UI elements (ids [1]..[N])
deskctl observe --window notepad --mode ocr
deskctl screenshot --target focused --max-width 1280   # saves last.png + info line
deskctl click 12                       # click element id from last observe
deskctl click 840,612                  # or raw screen coords
deskctl click i:640,360                # or coords on the last screenshot image
deskctl type "hello from deskctl"
deskctl key ctrl+s
deskctl drag 100,100 400,300
deskctl scroll -3 840,612
deskctl focus notepad
deskctl wait --timeout 5
deskctl pause / deskctl resume / deskctl status
deskctl guard                         # foreground Ctrl+Alt+Shift+K hotkey for CLI-only use
```

## MCP tools

`observe`, `screenshot` (returns image + info), `act`, `windows`, `focus`,
`wait_for_change`, `pause`, `resume`, `status`. Tool docstrings carry the
usage guidance (observe first, prefer element ids, `i:x,y` for image coords).

## HTTP mode + token

```powershell
$env:DESKCTL_TOKEN = "some-long-random-token"
.\.venv\Scripts\deskctl-mcp.exe --http --host 127.0.0.1 --port 8765
```

`DESKCTL_TOKEN` is **required** (the server refuses to start without it) and
every request must send `Authorization: Bearer <token>`; everything else gets
`401`. The MCP endpoint is `http://127.0.0.1:8765/mcp`. Default bind is
loopback only — do not expose it.

## Safety / kill switch

- `deskctl pause` / `deskctl resume` (or MCP `pause`/`resume`), or the global
  hotkey **Ctrl+Alt+Shift+K** (auto-started by `deskctl-mcp` and `deskctl guard`).
- **Failsafe:** moving the cursor into the top-left 2px corner of the virtual
  screen refuses the next action and pauses automation.
- Every action is appended as JSONL to `%LOCALAPPDATA%\deskctl\actions.log`
  (timestamps, action, resolved coords; typed text is logged as a length only).

## Tests

```powershell
.\.venv\Scripts\python -m pytest -q        # unit tests (no desktop interaction)
.\.venv\Scripts\python tests\smoke_live.py # live desktop smoke (touches real UI)
.\.venv\Scripts\python tests\mcp_stdio_check.py
.\.venv\Scripts\python tests\mcp_http_check.py
```
