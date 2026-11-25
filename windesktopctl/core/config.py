"""config.toml - feature toggles + file-tool roots, re-read on mtime change.

Location: ``<state_dir>/config.toml`` (WINDESKTOPCTL_HOME overrides the state
dir). Missing file -> safe defaults: everything disabled, no roots. The file
is stat'ed on every access so edits take effect without a restart.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from . import state as st

DEFAULTS = {
    "shell_enabled": False,
    "file_tools_enabled": False,
    "file_roots": [],
    "mcp_upstreams": {},
}

CONFIG_TEMPLATE = """\
# windesktopctl config - {path}
#
# shell_enabled: allow exec/job_* tools. exec is a FULL shell and is NOT
#   confined to file_roots - it can read/write anywhere the user account can.
# file_tools_enabled: allow read_file/write_file/edit_file/list_dir/glob/
#   grep/file_info/download_chunk/upload_chunk.
# file_roots: the file tools may only touch paths inside these directories
#   (checked after resolving junctions/symlinks; absolute paths required).
shell_enabled = false
file_tools_enabled = false
file_roots = []
#
# [mcp_upstreams.<name>] - forward MCP calls to a local MCP server.
#   url = "http://127.0.0.1:13337/mcp"        # streamable-HTTP upstream
#   # or: command = ["python", "-m", "server"]  # stdio upstream (+ env/cwd)
#   enabled = true
#   allow_unsafe = false    # false denies unsafe tools (py_eval etc.)
#   unsafe_tools = []       # extra unsafe names (built-in: py_eval,
#                         # py_exec_file for upstreams named ida*)
#   deny_tools = []         # always denied, even with allow_unsafe
#   headers = {{}}          # optional extra HTTP headers
#   # transport = "streamable-http"   # or "jsonrpc-http" for plain
#                                  # JSON-RPC /mcp endpoints (zeromcp)
#   # headless autostart (command upstreams / on-demand servers):
#   start_command = ["python", "-m", "idalib_server", "--port", "8745"]
#   start_if_unreachable = "ida"   # refuse to start when this upstream IS up
#   start_timeout_s = 300
#   # optional graceful stop via an upstream tool call before signals:
#   shutdown_tool = "idb_open"
#   shutdown_args = {{ input_path = 'C:\\db.i64', idle_ttl_sec = 10 }}
#
# [mcp_upstreams.ida]
# url = "http://127.0.0.1:13337/mcp"
# enabled = true
# allow_unsafe = false
"""

_cache: dict = {"key": None, "mtime_ns": None, "data": None}


def config_path() -> Path:
    return st.state_dir() / "config.toml"


def _read_raw() -> dict:
    p = config_path()
    key = str(p)
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        return dict(DEFAULTS)
    if _cache["key"] == key and _cache["mtime_ns"] == mtime and _cache["data"] is not None:
        return _cache["data"]
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    merged = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in data:
            merged[k] = data[k]
    _cache["key"] = key
    _cache["mtime_ns"] = mtime
    _cache["data"] = merged
    return merged


def load() -> dict:
    """Current config (defaults overlaid with file values)."""
    return dict(_read_raw())


def shell_enabled() -> bool:
    return bool(_read_raw().get("shell_enabled"))


def file_tools_enabled() -> bool:
    return bool(_read_raw().get("file_tools_enabled"))


def file_roots() -> list[str]:
    roots = _read_raw().get("file_roots") or []
    return [str(r) for r in roots]


_BUILTIN_UNSAFE = {"py_eval", "py_exec_file"}


def _str_list(v) -> list[str]:
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x) for x in v]


def mcp_upstreams() -> dict[str, dict]:
    """Normalized ``[mcp_upstreams.*]`` tables (fresh dicts each call).

    Each entry: name, kind ("http"|"stdio"), url/command/env/cwd/headers,
    enabled, allow_unsafe, unsafe (resolved set), deny (resolved set),
    start_command, start_if_unreachable, start_timeout_s.
    Entries with neither url nor command are skipped.
    """
    raw = _read_raw().get("mcp_upstreams") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for name, ent in raw.items():
        if not isinstance(ent, dict):
            continue
        name = str(name)
        url = ent.get("url")
        command = _str_list(ent.get("command"))
        kind = "http" if url else ("stdio" if command else None)
        if kind is None:
            continue
        allow_unsafe = bool(ent.get("allow_unsafe"))
        unsafe = set(_str_list(ent.get("unsafe_tools")))
        if name.startswith("ida"):
            unsafe |= _BUILTIN_UNSAFE
        deny = set(_str_list(ent.get("deny_tools")))
        if not allow_unsafe:
            deny |= unsafe
        headers = ent.get("headers") or {}
        out[name] = {
            "name": name,
            "kind": kind,
            "url": str(url) if url else None,
            "command": command or None,
            "env": {str(k): str(v) for k, v in (ent.get("env") or {}).items()}
            if isinstance(ent.get("env"), dict) else {},
            "cwd": str(ent["cwd"]) if ent.get("cwd") else None,
            "headers": {str(k): str(v) for k, v in headers.items()}
            if isinstance(headers, dict) else {},
            "transport": str(ent["transport"]) if ent.get("transport") else "streamable-http",
            "enabled": bool(ent.get("enabled", True)),
            "allow_unsafe": allow_unsafe,
            "unsafe": unsafe,
            "deny": deny,
            "start_command": _str_list(ent.get("start_command")) or None,
            "start_if_unreachable": str(ent["start_if_unreachable"])
            if ent.get("start_if_unreachable") else None,
            "start_timeout_s": int(ent.get("start_timeout_s") or 300),
            "shutdown_tool": str(ent["shutdown_tool"])
            if ent.get("shutdown_tool") else None,
            "shutdown_args": {str(k): v for k, v in ent["shutdown_args"].items()}
            if isinstance(ent.get("shutdown_args"), dict) else {},
        }
    return out


def init_config() -> Path:
    """Write the commented template if no config exists; return the path."""
    p = config_path()
    if not p.exists():
        p.write_text(CONFIG_TEMPLATE.format(path=p), encoding="utf-8")
    return p
