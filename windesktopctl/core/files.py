"""File tools confined to config.toml ``file_roots``.

Every entry point: ``safety.check_paused()`` first, then
``config.file_tools_enabled()`` ("disabled in config.toml"), then path
validation via :func:`resolve_in_roots` (absolute path required; UNC/device
paths and NTFS alternate streams rejected; ``os.path.realpath`` resolves
junctions/symlinks and the result must sit inside a configured root).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path, PurePath

from . import WindesktopctlError
from . import config as cfg
from . import safety
from . import state as st

TEXT_READ_MAX_BYTES = 256 * 1024       # max text returned per read_file call
B64_READ_MAX_BYTES = 1024 * 1024       # max raw bytes per base64 read
CHUNK_MAX_BYTES = 4 * 1024 * 1024      # download/upload chunk cap
GREP_MAX_FILE_BYTES = 20 * 1024 * 1024
GREP_BUDGET_S = 30.0
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv"}


def _require_file_tools() -> None:
    safety.check_paused()
    if not cfg.file_tools_enabled():
        raise WindesktopctlError("file tools disabled in config.toml (file_tools_enabled=false)")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _root_pairs() -> list[tuple[str, str]]:
    """(normcased realpath, display) for each configured root."""
    out = []
    for r in cfg.file_roots():
        try:
            out.append((_norm(r), str(r)))
        except Exception:
            continue
    return out


def resolve_in_roots(path) -> Path:
    """Validate ``path`` and return its realpath'd Path inside a root.

    Rejects UNC/device paths (``\\\\``, ``\\\\?\\``, ``\\\\.\\``), NTFS
    alternate streams (a ``:`` after the drive spec), and non-absolute
    paths. The realpath must be inside one of config.toml's file_roots.
    """
    s = str(path).strip().strip('"')
    if not s:
        raise WindesktopctlError("empty path")
    if s.startswith(("\\\\", "//")):
        raise WindesktopctlError(f"UNC/device paths are not allowed: {s!r}")
    # NTFS ADS: any ':' after an optional drive spec (X:)
    rest = re.sub(r"^[A-Za-z]:", "", s, count=1)
    if ":" in rest:
        raise WindesktopctlError(f"NTFS alternate streams are not allowed: {s!r}")
    p = Path(s)
    if not p.is_absolute():
        raise WindesktopctlError(f"path must be absolute: {s!r}")
    roots = _root_pairs()
    if not roots:
        raise WindesktopctlError(
            "no file_roots configured in config.toml - file tools are unavailable"
        )
    real = _norm(s)
    for root_real, root_disp in roots:
        try:
            if os.path.commonpath([real, root_real]) == root_real:
                return Path(real)
        except ValueError:
            continue  # different drives
    listing = ", ".join(d for _r, d in roots)
    raise WindesktopctlError(
        f"path is outside the configured file_roots: {s!r} (roots: {listing})"
    )


def _require_dir(path) -> Path:
    p = resolve_in_roots(path)
    if not p.is_dir():
        raise WindesktopctlError(f"not a directory: {p}")
    return p


def _require_file(path) -> Path:
    p = resolve_in_roots(path)
    if not p.is_file():
        raise WindesktopctlError(f"not a file: {p}")
    return p


# ------------------------------------------------------------- read_file --

def read_file(
    path,
    offset: int = 1,
    limit: int = 2000,
    numbered: bool = True,
    mode: str = "text",
    byte_offset: int = 0,
    byte_length: int | None = None,
):
    """Read a file inside file_roots. Text mode returns numbered lines
    (offset is 1-based); base64 mode returns a raw byte range."""
    _require_file_tools()
    p = _require_file(path)
    if mode == "base64":
        return _read_b64(p, byte_offset, byte_length)
    if mode != "text":
        raise WindesktopctlError(f"mode must be text|base64 (got {mode!r})")
    return _read_text(p, offset, limit, numbered)


def _read_text(p: Path, offset: int, limit: int, numbered: bool) -> str:
    offset = max(1, int(offset))
    limit = max(1, min(int(limit), 20000))
    size = p.stat().st_size
    if size > 256 * 1024 * 1024:
        raise WindesktopctlError(
            f"file too large for text read ({size} bytes); use download_chunk"
        )
    data = p.read_bytes()
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    lo = min(offset, total + 1)
    hi = min(offset + limit - 1, total)
    if lo > total:
        body = ""
        rng = f"lines {offset}-{offset + limit - 1} of {total}"
    else:
        slice_ = lines[lo - 1 : hi]
        if numbered:
            body = "".join(f"{i}\t{line}\n" for i, line in enumerate(slice_, lo))
        else:
            body = "\n".join(slice_) + ("\n" if slice_ else "")
        rng = f"lines {lo}-{hi} of {total}"
    header = f"{p} {rng} ({size} bytes)"
    out = header + "\n" + body
    if len(out.encode("utf-8")) > TEXT_READ_MAX_BYTES:
        enc = out.encode("utf-8")[:TEXT_READ_MAX_BYTES]
        out = enc.decode("utf-8", errors="replace")
        out += (
            f"\n[... truncated at {TEXT_READ_MAX_BYTES} bytes; use offset/limit "
            "or download_chunk for more ...]"
        )
    return out


def _read_b64(p: Path, byte_offset: int, byte_length: int | None) -> str:
    size = p.stat().st_size
    off = max(0, int(byte_offset))
    length = B64_READ_MAX_BYTES if byte_length is None else int(byte_length)
    length = max(0, min(length, B64_READ_MAX_BYTES))
    with p.open("rb") as f:
        f.seek(off)
        data = f.read(length)
    return json.dumps(
        {
            "path": str(p),
            "offset": off,
            "length": len(data),
            "eof": off + len(data) >= size,
            "size": size,
            "data_b64": base64.b64encode(data).decode("ascii"),
        }
    )


# ------------------------------------------------------------- write_file --

def write_file(path, text: str | None = None, content_b64: str | None = None,
               append: bool = False) -> str:
    """Write a file inside file_roots (exactly one of text/content_b64).
    Non-append writes are atomic (temp file + os.replace)."""
    _require_file_tools()
    if (text is None) == (content_b64 is None):
        raise WindesktopctlError("write_file needs exactly one of text= or content_b64=")
    p = resolve_in_roots(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if content_b64 is not None:
        data = base64.b64decode(content_b64)
    else:
        data = str(text).encode("utf-8")
    if append:
        with p.open("ab") as f:
            f.write(data)
    else:
        tmp = p.with_name(p.name + ".windesktopctl.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
    return f"wrote {len(data)} bytes to {p}{' (appended)' if append else ''}"


# -------------------------------------------------------------- edit_file --

def _occurrences(text: str, old: str) -> list[int]:
    """1-based line numbers where ``old`` occurs (non-overlapping)."""
    out = []
    start = 0
    while True:
        i = text.find(old, start)
        if i < 0:
            return out
        out.append(text.count("\n", 0, i) + 1)
        start = i + len(old)


def edit_file(path, old: str, new: str, replace_all: bool = False) -> str:
    """Exact-string replace inside file_roots. ``old`` must be unique unless
    replace_all. CRLF files: retries with \\n normalized to \\r\\n."""
    _require_file_tools()
    if not old:
        raise WindesktopctlError("old must be non-empty")
    p = _require_file(path)
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise WindesktopctlError("not utf-8; use write_file/base64")

    count = text.count(old)
    o, n = old, new
    if count == 0 and "\r\n" in text and "\n" in old and "\r\n" not in old:
        o, n = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        count = text.count(o)
    if count == 0:
        raise WindesktopctlError("old string not found in file")
    if count > 1 and not replace_all:
        lines = _occurrences(text, o)[:3]
        raise WindesktopctlError(
            f"old string occurs {count} times; pass replace_all or a more "
            f"specific string (first at lines {lines})"
        )
    lines = _occurrences(text, o)
    replaced = lines if replace_all else lines[:1]
    if replace_all:
        text = text.replace(o, n)
    else:
        i = text.find(o)
        text = text[:i] + n + text[i + len(o) :]
    tmp = p.with_name(p.name + ".windesktopctl.tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, p)
    shown = replaced if len(replaced) <= 10 else replaced[:10] + ["..."]
    return f"replaced {len(replaced)} occurrence(s) in {p} at lines {shown}"


# ------------------------------------------------------------- list_dir --

def list_dir(path, max_entries: int = 1000) -> str:
    """List a directory inside file_roots: 'd|f  size  mtime  name',
    directories first."""
    _require_file_tools()
    p = _require_dir(path)
    max_entries = max(1, min(int(max_entries), 10000))
    entries = []
    with os.scandir(p) as it:
        for e in it:
            try:
                stt = e.stat(follow_symlinks=False)
            except OSError:
                continue
            kind = "d" if e.is_dir(follow_symlinks=False) else "f"
            mt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stt.st_mtime))
            entries.append((kind, stt.st_size, mt, e.name))
    entries.sort(key=lambda t: (t[0] != "d", t[3].lower()))
    truncated = len(entries) > max_entries
    entries = entries[:max_entries]
    lines = [f"{k}  {size:>12}  {mt}  {name}" for k, size, mt, name in entries]
    out = f"{p} ({len(lines)} entries)\n" + "\n".join(lines)
    if truncated:
        out += f"\n[... truncated at {max_entries} entries ...]"
    return out


# ----------------------------------------------------------------- glob --

def _allowed_skip_names(pattern: str) -> set[str]:
    """Skip-dir names the pattern explicitly mentions (e.g. '.venv/**')."""
    try:
        parts = PurePath(str(pattern).replace("\\", "/")).parts
    except Exception:
        return set()
    return {part for part in parts if part in SKIP_DIRS}


def _has_skip_component(rel_parts: tuple[str, ...], allowed: set[str]) -> bool:
    return any(part in SKIP_DIRS and part not in allowed for part in rel_parts)


def glob_files(pattern: str, path, max_results: int = 1000) -> str:
    """pathlib-style glob (supports **) under a root-checked directory.
    Skips .git/node_modules/__pycache__/.venv unless the pattern names them;
    every match is re-checked against the roots (junction escapes dropped)."""
    _require_file_tools()
    base = _require_dir(path)
    max_results = max(1, min(int(max_results), 10000))
    allowed = _allowed_skip_names(pattern)
    matches = []
    truncated = False
    try:
        for m in base.glob(str(pattern)):
            try:
                rel = m.relative_to(base)
            except ValueError:
                continue
            if _has_skip_component(rel.parts, allowed):
                continue
            try:
                resolve_in_roots(m)
            except WindesktopctlError:
                continue  # junction/symlink escape
            matches.append(m)
            if len(matches) > max_results:
                truncated = True
                matches = matches[:max_results]
                break
    except (OSError, ValueError, NotImplementedError) as e:
        raise WindesktopctlError(f"glob failed: {e}")
    matches.sort(key=lambda p: str(p).lower())
    out = "\n".join(str(m) for m in matches)
    if truncated:
        out += f"\n[... truncated at {max_results} results ...]"
    return out or "(no matches)"


# ----------------------------------------------------------------- grep --

def grep_files(
    pattern: str,
    path,
    glob: str | None = None,
    max_results: int = 200,
    case_insensitive: bool = False,
    context: int = 0,
) -> str:
    """Regex search over files under a root-checked directory. Skips binary
    files (NUL in first 8KB), files >20MB, and .git/node_modules/__pycache__/
    .venv dirs (unless `glob` names them). 30s budget -> partial results."""
    _require_file_tools()
    base = _require_dir(path)
    try:
        rx = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    except re.error as e:
        raise WindesktopctlError(f"bad regex: {e}")
    max_results = max(1, min(int(max_results), 5000))
    context = max(0, min(int(context), 10))
    allowed = _allowed_skip_names(glob) if glob else set()
    deadline = time.monotonic() + GREP_BUDGET_S
    hits: list[str] = []
    timed_out = False
    hit_cap = False

    for root, dirnames, filenames in os.walk(base):
        # prune skip dirs in place
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS or d in allowed
        ]
        for name in sorted(filenames):
            if time.monotonic() > deadline:
                timed_out = True
                break
            fp = Path(root) / name
            try:
                if glob and not PurePath(fp.relative_to(base).as_posix()).match(glob):
                    continue
                if _has_skip_component(fp.relative_to(base).parts, allowed):
                    continue
                stt = fp.stat()
                if not fp.is_file() or stt.st_size > GREP_MAX_FILE_BYTES or stt.st_size == 0:
                    continue
                with fp.open("rb") as f:
                    head = f.read(8192)
                    if b"\x00" in head:
                        continue
                    f.seek(0)
                    raw = f.read()
            except (OSError, ValueError):
                continue
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            rel = fp.relative_to(base).as_posix()
            lines = text.splitlines()
            matched_idx = [i for i, ln in enumerate(lines) if rx.search(ln)]
            if not matched_idx:
                continue
            shown: set[int] = set()
            group: list[str] = []
            for i in matched_idx:
                for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                    if j in shown:
                        continue
                    shown.add(j)
                    ln = lines[j][:300]
                    mark = ":" if j == i else "-"
                    group.append(f"{rel}{mark}{j + 1}{mark} {ln}")
                if len(hits) + len(group) >= max_results:
                    hit_cap = True
                    break
                if time.monotonic() > deadline:
                    timed_out = True
                    break
            hits.extend(group)
            if hit_cap or timed_out or len(hits) >= max_results:
                break
        if hit_cap or timed_out or len(hits) >= max_results:
            break

    out = "\n".join(hits[:max_results])
    notes = []
    if timed_out:
        notes.append(f"[... timed out after {GREP_BUDGET_S:.0f}s - partial results ...]")
    elif hit_cap or len(hits) >= max_results:
        notes.append(f"[... truncated at {max_results} results ...]")
    if notes:
        out = (out + "\n" if out else "") + "\n".join(notes)
    return out or "(no matches)"


# ------------------------------------------------------------- file_info --

def file_info(path) -> str:
    """size, mtime, sha256 (streamed) and 4MB-chunk count for a file."""
    _require_file_tools()
    p = _require_file(path)
    size = p.stat().st_size
    h = hashlib.sha256()
    with p.open("rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    mt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
    chunks = (size + CHUNK_MAX_BYTES - 1) // CHUNK_MAX_BYTES if size else 0
    return (
        f"{p}\nsize={size} mtime={mt} sha256={h.hexdigest()} "
        f"chunks={chunks} chunk_size={CHUNK_MAX_BYTES}"
    )


# ------------------------------------------------------- download_chunk --

def download_chunk(path, offset: int = 0, length: int = CHUNK_MAX_BYTES) -> str:
    """Return one base64 chunk (<=4MB) of a file inside file_roots."""
    _require_file_tools()
    p = _require_file(path)
    size = p.stat().st_size
    off = max(0, int(offset))
    length = max(0, min(int(length), CHUNK_MAX_BYTES))
    with p.open("rb") as f:
        f.seek(off)
        data = f.read(length)
    return json.dumps(
        {
            "path": str(p),
            "offset": off,
            "length": len(data),
            "eof": off + len(data) >= size,
            "size": size,
            "data_b64": base64.b64encode(data).decode("ascii"),
        }
    )


# --------------------------------------------------------- upload_chunk --

def upload_chunk(
    path,
    offset: int,
    data_b64: str,
    done: bool = False,
    sha256: str | None = None,
    overwrite: bool = False,
) -> str:
    """Resume-able upload to ``<path>.part``. ``offset`` must equal the
    current .part size (0 restarts). On done=True the .part is verified
    (sha256 when given), refused if the target exists and overwrite is
    false, then atomically moved into place."""
    _require_file_tools()
    p = resolve_in_roots(path)
    part = Path(str(p) + ".part")
    p.parent.mkdir(parents=True, exist_ok=True)
    data = base64.b64decode(data_b64)
    off = int(offset)
    cur = part.stat().st_size if part.exists() else 0
    if off == 0:
        part.write_bytes(data)  # start / restart (truncates .part)
    elif off == cur:
        with part.open("ab") as f:
            f.write(data)
    else:
        raise WindesktopctlError(
            f"offset mismatch: expected {cur} (current .part size), got {off}"
        )
    new_size = part.stat().st_size
    if not done:
        return f"chunk written: .part now {new_size} bytes; next offset={new_size}"
    if sha256:
        h = hashlib.sha256()
        with part.open("rb") as f:
            for blk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(blk)
        if h.hexdigest().lower() != sha256.lower():
            raise WindesktopctlError(
                f"sha256 mismatch: got {h.hexdigest()}, expected {sha256} "
                "(.part kept for resume/retry)"
            )
    if p.exists() and not overwrite:
        raise WindesktopctlError(
            f"target exists: {p} (pass overwrite=true to replace)"
        )
    os.replace(part, p)
    return f"uploaded {new_size} bytes to {p}"
