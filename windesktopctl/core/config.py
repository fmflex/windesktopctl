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


def init_config() -> Path:
    """Write the commented template if no config exists; return the path."""
    p = config_path()
    if not p.exists():
        p.write_text(CONFIG_TEMPLATE.format(path=p), encoding="utf-8")
    return p
