# windesktopctl

Local Windows desktop control for agents: reads the UI **as text first**
(UI Automation tree → numbered elements), falls back to **OCR**, then
**screenshots**. Plus **shell + filesystem tools** and **MCP passthrough**
to other local MCP servers — as an MCP server (stdio or HTTP) and a CLI.

## Install & run

**Install** — `cd C:\path\to\windesktopctl; .\scripts\install.ps1`
(finds Python ≥3.11, creates `.venv`, `pip install -e .`).

**MCP client over stdio** — register in your MCP client config (auto-
starts the pause hotkey and takeover guard; no network exposure):

```json
{ "mcpServers": { "windesktopctl": { "command": "C:\\path\\to\\windesktopctl\\.venv\\Scripts\\windesktopctl-mcp.exe" } } }
```

**MCP client over HTTP** — `windesktopctl serve` (via `start.cmd`):

```powershell
.\start.cmd -Expose local      # http://127.0.0.1:8765/mcp + bearer token
.\start.cmd -Expose tailnet    # tailnet-only HTTPS via `tailscale serve`
.\start.cmd -Expose funnel     # PUBLIC internet via `tailscale funnel`
```

Binds `127.0.0.1:8765`, prints URL + masked token (`-ShowToken` full,
`-Copy` copies the client JSON), requires `Authorization: Bearer
<token>`; Ctrl+C stops the server **and** the Tailscale exposure. First
tailnet/funnel use needs one-time enablement in the Tailscale admin
console — the CLI prints the `login.tailscale.com` link. Client config
(`https://<host>.<tailnet>.ts.net/mcp` for exposed modes; funnel is
public — token-only, hostname in public CT logs):

```json
{ "mcpServers": { "windesktopctl": { "url": "http://127.0.0.1:8765/mcp", "headers": { "Authorization": "Bearer <token>" } } } }
```

## Tools (30)

- **Desktop (11):** `observe` (numbered UI-element text map; `dom=True` for browser pages), `screenshot`, `act` (click/double/right/move/drag/scroll/type/key), `windows`, `focus`, `wait_for`, `window` (min/max/restore/close/move/resize), `launch`, `pause`, `resume`, `status`.
- **Browser & challenges (3):** `browser` (info/tabs/tab/text/navigate), `challenge` (grid PNG of a human check — **never solved automatically**, only relays the user's answer), `challenge_answer` (only input allowed on an active challenge page).
- **Shell (4):** `exec` (powershell default or cmd; `background=true` → `job_id`), `job_output` (incremental), `job_kill`, `job_list`.
- **Files (9):** `read_file`, `write_file`, `edit_file`, `list_dir`, `glob`, `grep`, `file_info`, `download_chunk`, `upload_chunk` — confined to `file_roots`; big files move in ≤4 MB base64 chunks.
- **MCP passthrough (3):** `mcp_servers`, `mcp_tools`, `mcp_call` — forward calls to upstream MCP servers from `config.toml`. Workflow: `mcp_servers` → `mcp_tools` (narrow `filter`) → `mcp_call`.

## Configuration

`%LOCALAPPDATA%\windesktopctl\config.toml` — re-read on every call (edits
apply live). `windesktopctl config --init` writes a commented template.

```toml
shell_enabled = true          # exec + job_* tools
file_tools_enabled = true     # read/write/edit/list/glob/grep/chunk tools
file_roots = [                # file tools may ONLY touch paths inside these
  'C:\path\to\project-a',
  'C:\path\to\project-b',
]

# upstream MCP servers -> mcp_servers / mcp_tools / mcp_call
[mcp_upstreams.example]                    # streamable-HTTP upstream
url = "http://127.0.0.1:8080/mcp"
enabled = true
allow_unsafe = false                       # false denies unsafe tools
# headers = { "X-Key" = "..." }             # optional

[mcp_upstreams.localtool]                  # stdio upstream
command = ['C:\path\to\python.exe', '-m', 'my_mcp_server']
# env = { "KEY" = "value" }   cwd = 'C:\path\to\workdir'

[mcp_upstreams.headless_srv]               # autostart-on-demand pattern
url = "http://127.0.0.1:8745/mcp"
enabled = true
allow_unsafe = false
transport = "jsonrpc-http"                  # plain JSON-RPC endpoints (zeromcp)
start_command = ['C:\path\to\python.exe', '-m', 'srv', '--port', '8745', 'C:\path\to\data.bin']
start_if_unreachable = "example"           # don't start while this is up
start_timeout_s = 300
shutdown_tool = "idb_open"                  # optional graceful stop via a
shutdown_args = { idle_ttl_sec = 10 }       # tool call before signals/kill
```

Upstream names starting `ida` additionally deny `py_eval`/`py_exec_file`
unless `allow_unsafe = true`; `unsafe_tools`/`deny_tools` add denials.
`transport` defaults to `streamable-http`; `command` instead of `url`
makes it stdio (optional `env`/`cwd`). `start_command` upstreams
autostart when unreachable **and** their `start_if_unreachable` target is
down; the child logs to `<state>/upstreams/` and stops via `shutdown_tool`
when configured, else CTRL_BREAK, else `taskkill` after 60 s.
`exec` is a full shell — **NOT confined to `file_roots`**.

## Security

- Bearer token in `%LOCALAPPDATA%\windesktopctl\token` (user-only ACL,
  auto-created); `WINDESKTOPCTL_TOKEN` env overrides.
  `windesktopctl token [--show|--rotate]` — rotation live-reloads.
- Auth throttling is **per client IP**: >10 failures/60s bans that IP for
  10 min, doubling per repeat offense up to 24h; other IPs unaffected.
  Logs carry IP/path/method/User-Agent/Host — never tokens. Host headers
  validated against DNS-rebinding rules (loopback + bind host +
  `--allowed-host` repeats).
- Shell/file/MCP-passthrough tools register over HTTP **only with a
  strong token** (≥32 chars, ≥16 distinct); stdio always registers them.
- Pause: `windesktopctl pause`/`resume`, MCP `pause`/`resume`, hotkey
  **Ctrl+Alt+Shift+K**, or the failsafe (cursor into the top-left 2px
  corner → pause). Pause refuses every tool, shell/file included.
- User takeover: physical input (LL hooks; injected events ignored)
  refuses GUI actions while you're active (`WINDESKTOPCTL_IDLE_SECONDS`,
  default 3s); `type` aborts mid-string. Doesn't gate shell/file tools.
- `audit.log` (JSONL): ts, client IP, tool, command/path or upstream-call
  metadata — never contents, env values, or the token.
- Password-manager processes are never observed and are blacked out of
  captures; `WINDESKTOPCTL_EXCLUDE` adds names.
- Challenges are **never solved or guessed** — only the user's verbatim
  answer is relayed; other input tools refuse on that window meanwhile.

## CLI

```powershell
windesktopctl observe                         # focused window as numbered elements
windesktopctl --json observe --window notepad # structured output for a window
windesktopctl click 12                        # element id, "x,y", or "i:x,y" (screenshot coords)
windesktopctl type "hello"
windesktopctl key ctrl+s
windesktopctl wait --for text --value "Saved" --timeout 10
windesktopctl browser text --window chrome   # page text, incl. offscreen
windesktopctl token --rotate
```

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `WINDESKTOPCTL_TOKEN` | token file | env override for Bearer auth |
| `WINDESKTOPCTL_UIA` | `cached` | `cached` (FindAllBuildCache) or `walk` (BFS fallback) |
| `WINDESKTOPCTL_IDLE_SECONDS` | `3` | takeover idle window |
| `WINDESKTOPCTL_EXCLUDE` | built-ins | extra excluded process names (comma-sep) |
| `WINDESKTOPCTL_TOOLS` | all | comma-separated MCP tool allowlist |
| `WINDESKTOPCTL_NOTIFY_WEBHOOK` | — | optional webhook that receives challenge PNGs |
| `WINDESKTOPCTL_HOME` | `%LOCALAPPDATA%\windesktopctl` | state dir override |

## Development

`.\.venv\Scripts\python -m pytest -q` runs the unit tests (no desktop
needed); `tests\smoke_live.py`, `tests\live_shell_check.py` and
`tests\live_ida_check.py` are the manual/live checks. Design ideas
borrowed from Windows-MCP — see `THIRD_PARTY_NOTICES.md`.
