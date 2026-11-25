"""`windesktopctl serve` - run the MCP HTTP server with optional Tailscale
exposure (tailnet = private to your tailnet, funnel = public internet).

Always binds 127.0.0.1; tailscale proxies to it. On exit only the exposure
WE enabled is turned off (`tailscale serve|funnel --https=443 off`) - never
`tailscale serve reset`, which would clear unrelated config.
"""

from __future__ import annotations

import atexit
import json
import shutil
import subprocess
import sys
import urllib.request

from .core import token as tok
from .mcp_server import _http_allowed_hosts, _silence_uiautomation, build_http_app

SERVE_HTTPS_PORT = 443
_TS_TIMEOUT = 20


class ServeError(Exception):
    pass


def _ts(*args: str, timeout: int = _TS_TIMEOUT) -> subprocess.CompletedProcess:
    exe = shutil.which("tailscale")
    if not exe:
        raise ServeError("tailscale not found on PATH")
    return subprocess.run(
        [exe, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _ts_json(*args: str) -> dict:
    r = _ts(*args, "--json")
    if r.returncode != 0:
        raise ServeError(
            f"tailscale {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[:300]}"
        )
    out = (r.stdout or "").strip()
    if not out or out in ("{}", "null"):
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"_raw": out}


def dns_name() -> str | None:
    """Self DNS name (no trailing dot) from `tailscale status --json`."""
    try:
        st = _ts_json("status")
    except ServeError:
        return None
    name = ((st.get("Self") or {}).get("DNSName") or "").strip()
    return name.rstrip(".") or None


def _port_443_configured(status: dict) -> bool:
    """True if tailscale serve/funnel status --json shows anything on :443."""
    if not isinstance(status, dict):
        return False
    raw = status.get("_raw")
    if raw is not None:
        # unparseable output - be conservative: any mention of :443 counts
        return ":443" in raw
    for section in ("TCP", "Web", "AllowFunnel"):
        blk = status.get(section)
        if not isinstance(blk, dict):
            continue
        for key in blk:
            tail = str(key).rsplit(":", 1)[-1]
            if tail == str(SERVE_HTTPS_PORT):
                return True
            # Web keys look like "host.tail.ts.net:443"
            if str(key).endswith(f":{SERVE_HTTPS_PORT}"):
                return True
    return False


def serve_status() -> dict:
    try:
        return _ts_json("serve", "status")
    except ServeError:
        return {}


def check_exposable(mode: str) -> str | None:
    """Pre-flight for tailnet/funnel. Returns an error string or None."""
    if shutil.which("tailscale") is None:
        return "tailscale not found on PATH"
    try:
        _ts("status")
    except Exception as ex:
        return f"tailscale status failed: {ex}"
    st = serve_status()
    if _port_443_configured(st):
        return (
            "tailscale already has a serve/funnel config on :443 - refusing to "
            "overwrite it. Inspect with `tailscale serve status` and remove it "
            "first."
        )
    if mode == "funnel":
        try:
            _ts("funnel", "status")
        except Exception:
            pass
    return None


def _partial_output(ex: subprocess.TimeoutExpired) -> str:
    """Combine whatever a timed-out subprocess printed before being killed."""
    parts = []
    for chunk in (ex.stdout, ex.stderr):
        if chunk is None:
            continue
        parts.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace"))
    return "\n".join(parts)


def _not_enabled_message(out: str, what: str) -> str | None:
    """If tailscale printed '<what> is not enabled on your tailnet' plus an
    enable URL, return a user-actionable error; else None."""
    low = out.lower()
    if "not enabled on your tailnet" not in low:
        return None
    url = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("https://"):
            url = line
            break
    if url is None:
        for tok in out.replace("\n", " ").split():
            if tok.startswith("https://"):
                url = tok.rstrip(".,)")
                break
    msg = f"tailscale {what} is not enabled on this tailnet"
    if url:
        msg += f" - enable it once at {url}, then retry"
    return msg


def enable_expose(mode: str, port: int) -> None:
    """`tailscale serve|funnel --bg <port>`; raises ServeError on failure.

    When serve/funnel is not enabled on the tailnet, the CLI prints the
    admin-console enable URL and then hangs - so on timeout we inspect the
    partial output and surface that URL instead of a bare 'timed out'.
    """
    if mode == "tailnet":
        args = ["serve", "--bg", str(port)]
        what = "serve"
    elif mode == "funnel":
        args = ["funnel", "--bg", "--yes", str(port)]
        what = "funnel"
    else:
        raise ServeError(f"unknown expose mode {mode!r}")
    try:
        r = _ts(*args, timeout=30)
    except subprocess.TimeoutExpired as ex:
        out = _partial_output(ex)
        msg = _not_enabled_message(out, what)
        if msg:
            raise ServeError(msg)
        raise ServeError(f"tailscale {' '.join(args)} timed out")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        if mode == "funnel" and "funnel" in msg.lower() and "enable" in msg.lower():
            msg += " (first-time Funnel needs one-time enable in the admin console)"
        ne = _not_enabled_message(msg, what)
        if ne:
            raise ServeError(ne)
        raise ServeError(f"tailscale {' '.join(args)} failed: {msg[:400]}")


def disable_expose(mode: str) -> str | None:
    """Turn off ONLY the exposure we enabled. Returns a warning or None."""
    if mode == "tailnet":
        args = ["serve", f"--https={SERVE_HTTPS_PORT}", "off"]
    elif mode == "funnel":
        args = ["funnel", f"--https={SERVE_HTTPS_PORT}", "off"]
    else:
        return None
    try:
        r = _ts(*args, timeout=30)
    except Exception as ex:
        return f"could not disable tailscale {mode} exposure: {ex}"
    # verify it's actually gone
    try:
        if _port_443_configured(serve_status()):
            return (
                f"warning: tailscale {mode} exposure may still be active - "
                "check `tailscale serve status`"
            )
    except Exception:
        pass
    if r.returncode != 0 and "handler does not exist" not in (r.stderr or ""):
        return f"tailscale disable returned {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}"
    return None


def _mcp_json(url: str, token_value: str) -> str:
    return json.dumps(
        {
            "mcpServers": {
                "windesktopctl": {
                    "url": url,
                    "headers": {"Authorization": f"Bearer {token_value}"},
                }
            }
        },
        indent=2,
        ensure_ascii=False,
    )


def _copy_clipboard(text: str) -> bool:
    """Put text on the clipboard via clip.exe (content is pure ASCII)."""
    try:
        r = subprocess.run(
            ["clip"], input=text.encode("utf-8"), capture_output=True, timeout=10
        )
        return r.returncode == 0
    except Exception:
        return False


def _banner(mode: str, port: int, token_value: str, show_token: bool, copy: bool) -> None:
    name = dns_name() if mode in ("tailnet", "funnel") else None
    if mode == "local":
        url = f"http://127.0.0.1:{port}/mcp"
    else:
        url = f"https://{name}/mcp" if name else f"https://<tailnet-dns-name>/mcp"
    shown = token_value if show_token else tok.masked(token_value)
    print("=" * 60)
    print(f"windesktopctl serve  mode={mode}  port={port}")
    print(f"URL: {url}")
    print(f"token: {shown}  (file: {tok.token_path()})")
    print("MCP client config:")
    print(_mcp_json(url, shown))
    if copy:
        if _copy_clipboard(_mcp_json(url, token_value)):
            print("(full config with REAL token copied to clipboard)")
        else:
            print("(clipboard copy failed - rerun with --show-token)", file=sys.stderr)
    if mode == "funnel":
        print("!! PUBLIC on the internet; protected only by the token; "
              "Ctrl+C to stop exposure")
    print("Ctrl+C to stop.")
    print("=" * 60, flush=True)


def run(expose: str = "local", port: int = 8765,
        show_token: bool = False, copy: bool = False) -> int:
    _silence_uiautomation()
    expose = str(expose).lower()
    if expose not in ("local", "tailnet", "funnel"):
        print(f"serve: --expose must be local|tailnet|funnel (got {expose!r})",
              file=sys.stderr)
        return 2

    from .core import safety

    safety.start_hotkey_guard()

    provider = tok.TokenProvider()
    token_value, _path, created = tok.ensure_token()
    provider.token()  # prime cache

    enabled = False
    if expose != "local":
        err = check_exposable(expose)
        if err:
            print(f"serve: {err}", file=sys.stderr)
            return 2
        try:
            enable_expose(expose, port)
            enabled = True
        except ServeError as ex:
            print(f"serve: {ex}", file=sys.stderr)
            return 2

    if enabled:
        mode = expose

        def _cleanup():
            w = disable_expose(mode)
            if w:
                print(f"serve: {w}", file=sys.stderr)

        atexit.register(_cleanup)
    else:
        _cleanup = None

    # allowed hosts: localhost defaults + tailscale DNS name when exposed
    extra: list[str] = []
    if expose != "local":
        name = dns_name()
        if name:
            extra.extend([name, f"{name}:*"])
    allowed = _http_allowed_hosts("127.0.0.1", extra)

    _banner(expose, port, token_value, show_token, copy)

    app = build_http_app(allowed, provider)
    import uvicorn

    try:
        # proxy_headers on + forwarded_allow_ips=127.0.0.1 (the default):
        # trust X-Forwarded-For only from the local tailscale proxy, so the
        # per-IP auth throttle sees real client IPs and clients can't spoof.
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
        )
    except KeyboardInterrupt:
        pass
    finally:
        if _cleanup:
            _cleanup()
    return 0
