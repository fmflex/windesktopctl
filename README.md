# deskctl

Local Windows desktop helper that lets an LLM agent (e.g. Devin) **observe and
control the user's desktop cheaply**. It reads the UI as **text first**
(UI Automation accessibility tree -> numbered elements, via a single cached
`FindAllBuildCache` COM call with a `uiautomation` BFS fallback), falls back
to **OCR** (Windows.Media.Ocr), and only uses **screenshots** when needed
(downscaled, with numbered set-of-marks boxes). The helper itself never calls
an LLM. Design ideas borrowed from Windows-MCP (see THIRD_PARTY_NOTICES.md).

Exposed two ways with the same operations:

- an **MCP server** (stdio by default, optional Streamable HTTP) for agents
  that speak MCP;
- a **CLI** (`deskctl ...`, `--json` for structured output) so a remote agent
  that can only run shell commands can drive the desktop.

All coordinates are physical virtual-screen pixels. Element ids, the last
screenshot mapping, and the diff baseline persist in
`%LOCALAPPDATA%\deskctl\state.json`, so ids are shared across CLI/MCP calls
and processes (stale after 10 minutes, or immediately if the window closes,
moves, or resizes).

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

The server starts the `Ctrl+Alt+Shift+K` pause hotkey and the input-takeover
guard automatically.

## CLI examples

```powershell
deskctl windows                        # list top-level windows
deskctl observe --window focused       # text map of UI elements (ids [1]..[N])
deskctl observe --window notepad --mode ocr
deskctl observe --window chrome --dom  # browser page content only
deskctl --json observe                 # structured JSON instead of text
deskctl screenshot --target focused --max-width 1280   # saves last.png + info line
deskctl click 12                       # click element id from last observe
deskctl click 840,612                  # or raw screen coords
deskctl click i:640,360                # or coords on the last screenshot image
deskctl type "hello from deskctl"      # aborts mid-way if you take over
deskctl key ctrl+s
deskctl drag 100,100 400,300
deskctl scroll -3 840,612
deskctl scroll 2 --horizontal         # side-scroll
deskctl focus notepad
deskctl window minimize notepad
deskctl window move notepad --x 100 --y 100
deskctl launch notepad.exe
deskctl launch "https://example.com"
deskctl wait --for text --value "Save" --timeout 10
deskctl wait --for element --value "Button:OK"
deskctl wait --for changed --timeout 5
deskctl pause / deskctl resume / deskctl status
deskctl guard                         # foreground hotkey + takeover guard (CLI-only)
```

## MCP tools (11)

`observe` (+`dom`), `screenshot` (returns image + info), `act` (+`horizontal`),
`windows`, `focus`, `wait_for`, `window`, `launch`, `pause`, `resume`,
`status`. Tool docstrings carry the usage guidance (observe first, prefer
element ids, `i:x,y` for image coords).

Element flags shown after name/value: `focused`, `disabled`, `checked`,
`expanded`/`collapsed`, `selected`, `password`. Scrollable containers appear
as role `Scroll` with `v=NN%` / `h=NN%`.

## HTTP mode + token

```powershell
$env:DESKCTL_TOKEN = "some-long-random-token"
.\.venv\Scripts\deskctl-mcp.exe --http --host 127.0.0.1 --port 8765
# tunnel/custom hostname? add: --allowed-host myhost.example.com:*
```

`DESKCTL_TOKEN` is **required** (the server refuses to start without it) and
every request must send `Authorization: Bearer <token>`; everything else gets
`401`. Host headers are validated against DNS-rebinding rules: `127.0.0.1`,
`localhost`, and the bind host are allowed with or without port; repeat
`--allowed-host` for tunnel hostnames. The MCP endpoint is
`http://127.0.0.1:8765/mcp`. Default bind is loopback only — do not expose it.

## Safety / kill switch

- `deskctl pause` / `deskctl resume` (or MCP `pause`/`resume`), or the global
  hotkey **Ctrl+Alt+Shift+K** (auto-started by `deskctl-mcp` and
  `deskctl guard`).
- **Failsafe:** moving the cursor into the top-left 2px corner of the virtual
  screen refuses the next action and pauses automation.
- **User takeover:** physical keyboard/mouse input (detected via
  WH_MOUSE_LL/WH_KEYBOARD_LL, injected events ignored) makes actions refuse
  with "user is using the PC ...; retry when idle" for
  `DESKCTL_IDLE_SECONDS` (default 3). `type` also aborts mid-string.
  Note: input injected by remote tools (e.g. Sunshine) does **not** count as
  the user. The MCP server or `deskctl guard` must be running for detection;
  `deskctl status` shows `takeover=live` vs `off`.
- Every action is appended as JSONL to `%LOCALAPPDATA%\deskctl\actions.log`
  (timestamps, action, resolved coords; typed text is logged as a length only).

## Privacy exclusions

Password-manager processes are never observed and are blacked out of
screen/region captures before OCR/encoding. Built-in list: `1password.exe`,
`keepass.exe`, `keepassxc.exe`, `bitwarden.exe`, `enpass.exe`.
`DESKCTL_EXCLUDE` (comma-separated process names) **adds** to that list.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `DESKCTL_TOKEN` | — | required for `--http` Bearer auth |
| `DESKCTL_UIA` | `cached` | `cached` (FindAllBuildCache) or `walk` (BFS fallback) |
| `DESKCTL_IDLE_SECONDS` | `3` | takeover idle window |
| `DESKCTL_EXCLUDE` | built-ins | extra excluded process names (comma-sep) |
| `DESKCTL_TOOLS` | all | comma-separated MCP tool allowlist |
| `DESKCTL_HOME` | `%LOCALAPPDATA%\deskctl` | state dir override |

## Tests

```powershell
.\.venv\Scripts\python -m pytest -q        # unit tests (no desktop interaction)
.\.venv\Scripts\python tests\smoke_live.py # live desktop smoke (touches real UI)
.\.venv\Scripts\python tests\mcp_stdio_check.py
.\.venv\Scripts\python tests\mcp_http_check.py
```
