# windesktopctl

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
- a **CLI** (`windesktopctl ...`, `--json` for structured output) so a remote agent
  that can only run shell commands can drive the desktop.

Optionally also exposes **shell + filesystem tools** (`exec`, background
jobs, root-confined file read/write/edit/glob/grep/chunked upload+download)
so cloud agents can run commands and edit repos on this PC without GUI
automation — see **Shell & file tools** below.

All coordinates are physical virtual-screen pixels. Element ids, the last
screenshot mapping, and the diff baseline persist in
`%LOCALAPPDATA%\windesktopctl\state.json`, so ids are shared across CLI/MCP calls
and processes (stale after 10 minutes, or immediately if the window closes,
moves, or resizes).

## Install

```powershell
cd C:\path\to\windesktopctl
.\scripts\install.ps1        # finds Python >=3.11, makes .venv, pip install -e .
```

## Run modes

### 1. Local Devin (stdio, recommended)

```powershell
devin mcp add -s user windesktopctl -- C:\path\to\windesktopctl\.venv\Scripts\windesktopctl-mcp.exe
```

The server starts the `Ctrl+Alt+Shift+K` pause hotkey and the input-takeover
guard automatically. No network exposure at all.

### 2. Another PC over your tailnet (private)

On the PC running windesktopctl:

```powershell
.\start.cmd -Expose tailnet        # = windesktopctl serve --expose tailnet
```

It binds `127.0.0.1:8765`, runs `tailscale serve --bg 8765` (HTTPS on :443,
tailnet-only), prints the URL `https://<your-pc>.<tailnet>.ts.net/mcp` and a
ready-to-paste MCP config JSON. Put that JSON (with `Authorization: Bearer
<token>`) into the **other machine's** Devin MCP config. Ctrl+C stops the
server AND turns the tailscale exposure back off.

### 3. Devin Cloud via Funnel (public internet)

```powershell
.\start.cmd -Expose funnel -ShowToken -Copy
```

Same as tailnet but `tailscale funnel` exposes it publicly. First-time use
needs Funnel enabled once in the Tailscale admin console (the CLI prints the
`login.tailscale.com/f/funnel?node=…` link). **This is PUBLIC on the
internet, protected only by the bearer token** — the banner says so too.
Funnel hostnames show up in public certificate-transparency logs, so
internet scanners find the URL within minutes; that's expected — the token
blocks them and per-IP throttling bans repeat offenders.
Ctrl+C stops exposure.

Note: cloud sessions use MCP connections configured in the Devin web app;
we haven't verified the exact UI for adding a remote MCP server there —
check Devin's docs/settings for "MCP servers" and paste the same JSON block.

All serve modes share: token auth (below), Host-header validation,
`windesktopctl pause`/takeover/failsafe still apply, and `windesktopctl-mcp
--http` remains available for manual setups.

## Token management

The bearer token lives in `%LOCALAPPDATA%\windesktopctl\token` (created on
first use, ACL-restricted to your user). `WINDESKTOPCTL_TOKEN` env var
overrides the file when set.

```powershell
windesktopctl token              # masked token + file path
windesktopctl token --show       # full token
windesktopctl token --rotate     # new token; live-reloads, no restart needed
```

`serve`/`--http` re-check the token file on every request, so `--rotate`
locks out old clients immediately. Auth throttling is **per client IP**:
more than 10 failed auths from one IP in 60s bans only that IP with 429
(10 min, doubling per repeat offense up to 24h); other IPs — including one
presenting the right token — are unaffected, so scanners can't lock you
out. Failures are logged with client IP, path, method, User-Agent (truncated)
and Host — never the token; the first successful auth per IP per 10 min is
logged too, so you can see which client is yours.

## Safety / kill switch

## CLI examples

```powershell
windesktopctl windows                        # list top-level windows
windesktopctl observe --window focused       # text map of UI elements (ids [1]..[N])
windesktopctl observe --window notepad --mode ocr
windesktopctl observe --window chrome --dom  # browser page content only
windesktopctl --json observe                 # structured JSON instead of text
windesktopctl screenshot --target focused --max-width 1280   # saves last.png + info line
windesktopctl click 12                       # click element id from last observe
windesktopctl click 840,612                  # or raw screen coords
windesktopctl click i:640,360                # or coords on the last screenshot image
windesktopctl type "hello from windesktopctl"      # aborts mid-way if you take over
windesktopctl key ctrl+s
windesktopctl drag 100,100 400,300
windesktopctl scroll -3 840,612
windesktopctl scroll 2 --horizontal         # side-scroll
windesktopctl focus notepad
windesktopctl window minimize notepad
windesktopctl window move notepad --x 100 --y 100
windesktopctl launch notepad.exe
windesktopctl launch "https://example.com"
windesktopctl wait --for text --value "Save" --timeout 10
windesktopctl wait --for element --value "Button:OK"
windesktopctl wait --for changed --timeout 5
windesktopctl browser info chrome               # title, URL, tabs for a browser window
windesktopctl browser tabs chrome
windesktopctl browser tab 2 chrome              # or a title substring
windesktopctl browser text chrome --max-chars 4000   # whole page incl. offscreen
windesktopctl browser navigate "https://example.com" chrome
windesktopctl challenge chrome                  # grid-overlay PNG for a human check
windesktopctl challenge --region "C4:I10" --grid 4x4 chrome   # zoom into tiles, numbered
windesktopctl challenge-answer 3 --cells "3 8 14"      # relay the user's exact reply
windesktopctl challenge-answer 3 --cells "B3 C4"       # lettered grids
windesktopctl challenge-answer 3 --text "K7Q2-X9" --enter
windesktopctl token [--show|--rotate]           # HTTP bearer token
windesktopctl config [--init|--show]            # shell/file toggles + roots (config.toml)
windesktopctl serve --expose tailnet --port 8765 --copy
windesktopctl pause / windesktopctl resume / windesktopctl status
windesktopctl guard                         # foreground hotkey + takeover guard (CLI-only)
```

## MCP tools (27)

Desktop (14): `observe` (+`dom`), `screenshot` (returns image + info), `act`
(+`horizontal`), `windows`, `focus`, `wait_for`, `window`, `launch`,
`browser`, `challenge`, `challenge_answer`, `pause`, `resume`, `status`.
Tool docstrings carry the usage guidance (observe first, prefer element ids,
`i:x,y` for image coords).

Shell/file (13, see below): `exec`, `job_output`, `job_kill`, `job_list`,
`read_file`, `write_file`, `edit_file`, `list_dir`, `glob`, `grep`,
`file_info`, `download_chunk`, `upload_chunk`.

Element flags shown after name/value: `focused`, `disabled`, `checked`,
`expanded`/`collapsed`, `selected`, `password`. Scrollable containers appear
as role `Scroll` with `v=NN%` / `h=NN%`.

## Shell & file tools (config.toml)

For cloud agents and bots that need to **run commands and edit files** on
this PC without GUI automation. All 13 tools refuse while paused and honor
the `shell_enabled` / `file_tools_enabled` kill switches (re-read on every
call — edit `config.toml` and it takes effect immediately, no restart).

Config lives at `%LOCALAPPDATA%\windesktopctl\config.toml`
(`windesktopctl config --init` writes a commented template):

```toml
shell_enabled = true        # exec + job_* tools
file_tools_enabled = true   # read/write/edit/list/glob/grep/upload/download
file_roots = [              # file tools may ONLY touch paths inside these
  'C:\Users\funkm\repo\POE2Stuff',
  'C:\Users\funkm\repo\windesktopctl',
  'C:\Program Files (x86)\Grinding Gear Games\Path of Exile 2',
  'C:\Users\funkm\AppData\Roaming\Hex-Rays\IDA Pro',
]
```

- **`exec` is a FULL shell — it is NOT confined to `file_roots`.** The roots
  only limit the file tools; exec can read/write anywhere the user account
  can (cwd included). `shell="powershell"` (default) or `"cmd"`, optional
  `env`, `cwd`, `timeout_s` (1–1800, kills the whole process tree on
  timeout). No stdin — pass non-interactive flags.
- **Background jobs**: `exec(background=true)` returns `job_id=jN`; output
  streams to files, `job_output` returns only *new* bytes since your last
  call, `job_kill` kills the tree, `job_list` lists jobs. Jobs are
  in-memory only — a server restart forgets them.
- **File tools** require absolute paths inside `file_roots` (junctions and
  symlinks are resolved first — escapes are refused; UNC/device paths and
  NTFS alternate streams are rejected outright). `read_file` gives numbered
  lines or a base64 byte range; `edit_file` does exact-string replace
  (CRLF-aware); `grep` is regex with binary/oversize skipping; big files go
  through `download_chunk`/`upload_chunk` in ≤4 MB base64 chunks
  (upload is resumable via `.part` offsets, sha256-verified on `done`).
- Writes under `C:\Program Files (x86)\...` (the game folder) may fail
  without admin rights — that's expected; windesktopctl never elevates.
- These tools run on a **separate thread pool**, so a long exec can't
  block the GUI tools. In **HTTP mode they are only registered when the
  bearer token is strong** (≥32 chars, ≥16 distinct — the auto-generated
  token passes); a weak token prints a warning and the tools stay absent.
  In stdio mode they're always registered. Every call is appended to
  `%LOCALAPPDATA%\windesktopctl\audit.log` (JSONL: timestamp, client IP,
  tool, command/path, result metadata — never file contents, env values,
  or the token).

## Human challenges (CAPTCHA relay)

When `observe` on a browser window prints `!! challenge detected`, a
"verify you are human"-style page is on screen. windesktopctl **never solves or
guesses challenges** — no OCR-to-answer, no auto-clicking checkboxes, no
evasion. It only relays:

1. `challenge()` re-detects and returns a PNG of the challenge area with a
   labeled grid overlay, saved to
   `%LOCALAPPDATA%\windesktopctl\challenge_<id>.png`. Tile grids with a
   regular lattice auto-switch to **numbered** cells (1..N — the user just
   says "3 8 14"); other challenges get lettered cells (rows A.., cols 1..,
   ~70px cells).
2. Show that image to the user, wait for their reply, and pass it **verbatim**
   to `challenge_answer(id, cells=..., text=...)` — numbers on numbered
   grids, lettered cells like `"B3 C4"` otherwise, `text` for character
   challenges (with `enter=True` to press Enter). It clicks/types exactly
   what you give it, then a verify/submit button when present, and reports
   whether the challenge cleared.
3. To zoom into tiles: `challenge(region="C4:I10", grid="4x4")` re-captures
   a sub-area of the previous capture with an exact numbered grid — choosing
   the region/grid size is layout work and is allowed; answering is still
   only the user's.
4. While a challenge is active (<10 min), `act`, `window`, and
   `browser tab|navigate` **refuse** on that window — `challenge_answer` is
   the only input path.

Honesty note: windesktopctl cannot verify that an answer really came from the
user — this is an agent-instruction contract enforced by the gating above.
If you want per-answer human approval, set a Devin permission "ask" rule on
the `challenge_answer` tool. `WINDESKTOPCTL_NOTIFY_WEBHOOK` (a Discord webhook
URL) optionally POSTs the challenge PNG so a human gets pinged; the URL is
never logged.

## HTTP mode (manual)

`windesktopctl serve` (above) is the easy path. The underlying server is also
available directly:

```powershell
.\.venv\Scripts\windesktopctl-mcp.exe --http --host 127.0.0.1 --port 8765
# tunnel/custom hostname? add: --allowed-host myhost.example.com:*
```

The token comes from `WINDESKTOPCTL_TOKEN` env or
`%LOCALAPPDATA%\windesktopctl\token` (auto-created, user-only ACL); see
**Token management**. Every request must send `Authorization: Bearer
<token>`; everything else gets `401`, and >10 failures in 60s from one IP
gets that IP a `429` (other IPs unaffected — see **Token management**).
Host headers are validated against DNS-rebinding rules: `127.0.0.1`,
`localhost`, and the bind host are allowed with or without port; repeat
`--allowed-host` for tunnel hostnames. The MCP endpoint is
`http://127.0.0.1:8765/mcp`. Default bind is loopback only.

## Safety / kill switch

- `windesktopctl pause` / `windesktopctl resume` (or MCP `pause`/`resume`), or the global
  hotkey **Ctrl+Alt+Shift+K** (auto-started by `windesktopctl-mcp` and
  `windesktopctl guard`). Pause also refuses the shell/file tools — it does
  NOT gate them on physical-input takeover, since running commands doesn't
  fight the user's mouse/keyboard.
- **Failsafe:** moving the cursor into the top-left 2px corner of the virtual
  screen refuses the next action and pauses automation.
- **User takeover:** physical keyboard/mouse input (detected via
  WH_MOUSE_LL/WH_KEYBOARD_LL, injected events ignored) makes actions refuse
  with "user is using the PC ...; retry when idle" for
  `WINDESKTOPCTL_IDLE_SECONDS` (default 3). `type` also aborts mid-string.
  Note: input injected by remote tools (e.g. Sunshine) does **not** count as
  the user. The MCP server or `windesktopctl guard` must be running for detection;
  `windesktopctl status` shows `takeover=live` vs `off`.
- Every action is appended as JSONL to `%LOCALAPPDATA%\windesktopctl\actions.log`
  (timestamps, action, resolved coords; typed text is logged as a length only).

## Privacy exclusions

Password-manager processes are never observed and are blacked out of
screen/region captures before OCR/encoding. Built-in list: `1password.exe`,
`keepass.exe`, `keepassxc.exe`, `bitwarden.exe`, `enpass.exe`.
`WINDESKTOPCTL_EXCLUDE` (comma-separated process names) **adds** to that list.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `WINDESKTOPCTL_TOKEN` | token file | env override for HTTP Bearer auth |
| `WINDESKTOPCTL_UIA` | `cached` | `cached` (FindAllBuildCache) or `walk` (BFS fallback) |
| `WINDESKTOPCTL_IDLE_SECONDS` | `3` | takeover idle window |
| `WINDESKTOPCTL_EXCLUDE` | built-ins | extra excluded process names (comma-sep) |
| `WINDESKTOPCTL_TOOLS` | all | comma-separated MCP tool allowlist |
| `WINDESKTOPCTL_NOTIFY_WEBHOOK` | — | optional Discord webhook for challenge PNGs |
| `WINDESKTOPCTL_HOME` | `%LOCALAPPDATA%\windesktopctl` | state dir override |

## Tests

```powershell
.\.venv\Scripts\python -m pytest -q        # unit tests (no desktop interaction)
.\.venv\Scripts\python tests\smoke_live.py # live desktop smoke (touches real UI)
.\.venv\Scripts\python tests\mcp_stdio_check.py
.\.venv\Scripts\python tests\mcp_http_check.py
```
