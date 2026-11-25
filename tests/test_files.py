"""Unit tests for core/files.py - path guard + file tools (no network)."""

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from windesktopctl.core import WindesktopctlError
from windesktopctl.core import config as cfg
from windesktopctl.core import files


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp WINDESKTOPCTL_HOME + a roots dir inside file_roots."""
    home = tmp_path / "wdhome"
    home.mkdir()
    roots = tmp_path / "roots"
    roots.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("WINDESKTOPCTL_HOME", str(home))

    def set_config(shell=True, files_enabled=True, root=None):
        root = root if root is not None else roots
        (home / "config.toml").write_text(
            f"shell_enabled = {'true' if shell else 'false'}\n"
            f"file_tools_enabled = {'true' if files_enabled else 'false'}\n"
            f"file_roots = ['{str(root).replace(chr(92), '/')}']\n",
            encoding="utf-8",
        )

    set_config()
    return SimpleNamespace(home=home, roots=roots, outside=outside, set_config=set_config)


# ----------------------------------------------------------- path guard --

def test_resolve_accepts_inside(env):
    p = env.roots / "sub" / "f.txt"
    p.parent.mkdir(parents=True)
    p.write_text("x")
    assert files.resolve_in_roots(str(p)) == Path(os.path.normcase(os.path.realpath(str(p))))


def test_resolve_rejects_dotdot_escape(env):
    with pytest.raises(WindesktopctlError, match="outside"):
        files.resolve_in_roots(str(env.roots / ".." / "outside" / "f.txt"))


def test_resolve_rejects_absolute_outside(env):
    with pytest.raises(WindesktopctlError, match="outside"):
        files.resolve_in_roots(str(env.outside / "f.txt"))


def test_resolve_rejects_relative(env):
    with pytest.raises(WindesktopctlError, match="absolute"):
        files.resolve_in_roots("relative/path.txt")


def test_resolve_rejects_unc(env):
    for bad in (r"\\server\share\f.txt", "//server/share/f.txt"):
        with pytest.raises(WindesktopctlError):
            files.resolve_in_roots(bad)


def test_resolve_rejects_ads(env):
    p = env.roots / "f.txt"
    p.write_text("x")
    for bad in (str(p) + ":stream", str(p) + ":$DATA"):
        with pytest.raises(WindesktopctlError, match="alternate"):
            files.resolve_in_roots(bad)


def test_resolve_case_insensitive(env):
    p = env.roots / "Sub" / "F.TXT"
    p.parent.mkdir(parents=True)
    p.write_text("x")
    upper = str(p).upper()
    assert files.resolve_in_roots(upper).name.lower() == "f.txt"


def test_resolve_prefix_trick(env):
    evil = env.roots.parent / (env.roots.name + "Evil")
    evil.mkdir(exist_ok=True)
    (evil / "f.txt").write_text("x")
    with pytest.raises(WindesktopctlError, match="outside"):
        files.resolve_in_roots(str(evil / "f.txt"))


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_resolve_junction_escape(env):
    target = env.outside / "jtarget"
    target.mkdir()
    (target / "secret.txt").write_text("secret")
    link = env.roots / "jlink"
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"mklink /J failed: {r.stderr or r.stdout}")
    # path goes through the junction but realpath lands outside the root
    with pytest.raises(WindesktopctlError, match="outside"):
        files.resolve_in_roots(str(link / "secret.txt"))


def test_resolve_no_roots_configured(env):
    env.set_config(root=env.outside)  # roots now point elsewhere
    # our path is under old roots -> outside new root
    p = env.roots / "f.txt"
    p.write_text("x")
    with pytest.raises(WindesktopctlError, match="outside"):
        files.resolve_in_roots(str(p))


# ------------------------------------------------------------- gates --

def test_file_tools_disabled(env):
    env.set_config(files_enabled=False)
    p = env.roots / "f.txt"
    p.write_text("x")
    with pytest.raises(WindesktopctlError, match="disabled in config.toml"):
        files.read_file(str(p))


def test_paused_blocks(env):
    (env.home / "paused").write_text("x")
    p = env.roots / "f.txt"
    p.write_text("x")
    with pytest.raises(WindesktopctlError, match="paused"):
        files.read_file(str(p))


# ----------------------------------------------------------- read_file --

def test_read_file_numbered(env):
    p = env.roots / "a.txt"
    p.write_text("one\ntwo\nthree\n", encoding="utf-8")
    out = files.read_file(str(p))
    assert "lines 1-3 of 3" in out
    assert "1\tone" in out and "3\tthree" in out


def test_read_file_offset_limit(env):
    p = env.roots / "a.txt"
    p.write_text("\n".join(f"l{i}" for i in range(1, 11)) + "\n")
    out = files.read_file(str(p), offset=3, limit=2)
    assert "lines 3-4 of 10" in out
    assert "3\tl3" in out and "4\tl4" in out and "5\tl5" not in out


def test_read_file_unnumbered_and_past_eof(env):
    p = env.roots / "a.txt"
    p.write_text("a\nb\n")
    out = files.read_file(str(p), numbered=False)
    assert "\n1\t" not in out
    out2 = files.read_file(str(p), offset=99)
    assert "of 2" in out2


def test_read_file_base64(env):
    p = env.roots / "bin.dat"
    data = bytes(range(256)) * 4
    p.write_bytes(data)
    out = json.loads(files.read_file(str(p), mode="base64", byte_offset=100, byte_length=50))
    assert out["offset"] == 100 and out["length"] == 50
    assert out["eof"] is False and out["size"] == len(data)
    assert base64.b64decode(out["data_b64"]) == data[100:150]


def test_read_file_missing(env):
    with pytest.raises(WindesktopctlError, match="not a file"):
        files.read_file(str(env.roots / "nope.txt"))


# ----------------------------------------------------------- write_file --

def test_write_file_text_and_parents(env):
    p = env.roots / "deep" / "dir" / "f.txt"
    out = files.write_file(str(p), text="hello")
    assert "5 bytes" in out
    assert p.read_text() == "hello"


def test_write_file_append(env):
    p = env.roots / "f.txt"
    files.write_file(str(p), text="a")
    files.write_file(str(p), text="b", append=True)
    assert p.read_text() == "ab"


def test_write_file_b64(env):
    p = env.roots / "bin.dat"
    data = b"\x00\x01\x02\xff" * 100
    files.write_file(str(p), content_b64=base64.b64encode(data).decode())
    assert p.read_bytes() == data


def test_write_file_exactly_one_content(env):
    p = env.roots / "f.txt"
    with pytest.raises(WindesktopctlError, match="exactly one"):
        files.write_file(str(p))
    with pytest.raises(WindesktopctlError, match="exactly one"):
        files.write_file(str(p), text="a", content_b64="eA==")


def test_write_file_outside_root(env):
    with pytest.raises(WindesktopctlError, match="outside"):
        files.write_file(str(env.outside / "evil.txt"), text="x")


# ------------------------------------------------------------ edit_file --

def test_edit_file_unique(env):
    p = env.roots / "f.txt"
    p.write_text("alpha\nbeta\ngamma\n")
    out = files.edit_file(str(p), "beta", "BETA")
    assert "replaced 1" in out and "2" in out
    assert p.read_text() == "alpha\nBETA\ngamma\n"


def test_edit_file_multiple_requires_replace_all(env):
    p = env.roots / "f.txt"
    p.write_text("x\nfoo\ny\nfoo\nz\n")
    with pytest.raises(WindesktopctlError, match="3 times|occurs 2"):
        files.edit_file(str(p), "foo", "bar")
    out = files.edit_file(str(p), "foo", "bar", replace_all=True)
    assert "replaced 2" in out
    assert p.read_text() == "x\nbar\ny\nbar\nz\n"


def test_edit_file_not_found(env):
    p = env.roots / "f.txt"
    p.write_text("abc")
    with pytest.raises(WindesktopctlError, match="not found"):
        files.edit_file(str(p), "zzz", "q")


def test_edit_file_crlf_fallback(env):
    p = env.roots / "f.txt"
    p.write_bytes(b"line1\r\nline2\r\nline3\r\n")
    out = files.edit_file(str(p), "line2\n", "LINE2\n")
    assert "replaced 1" in out
    assert p.read_bytes() == b"line1\r\nLINE2\r\nline3\r\n"


def test_edit_file_non_utf8(env):
    p = env.roots / "f.bin"
    p.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(WindesktopctlError, match="not utf-8"):
        files.edit_file(str(p), "a", "b")


# ------------------------------------------------------------ list_dir --

def test_list_dir_dirs_first(env):
    (env.roots / "zdir").mkdir()
    (env.roots / "adir").mkdir()
    (env.roots / "b.txt").write_text("x")
    (env.roots / "a.txt").write_text("x")
    out = files.list_dir(str(env.roots))
    lines = out.splitlines()[1:]
    assert lines[0].strip().endswith("adir")
    assert lines[1].strip().endswith("zdir")
    assert " b.txt" in out or "b.txt" in lines[2]


# ----------------------------------------------------------------- glob --

def test_glob_basic_and_recursive(env):
    (env.roots / "src" / "pkg").mkdir(parents=True)
    (env.roots / "top.py").write_text("x")
    (env.roots / "src" / "a.py").write_text("x")
    (env.roots / "src" / "pkg" / "b.py").write_text("x")
    (env.roots / "src" / "c.txt").write_text("x")
    out = files.glob_files("*.py", str(env.roots))
    assert "top.py" in out and "a.py" not in out and "c.txt" not in out
    out = files.glob_files("**/*.py", str(env.roots))
    assert "top.py" in out and "a.py" in out and "b.py" in out
    assert "c.txt" not in out


def test_glob_skips_vendor_dirs(env):
    (env.roots / ".venv" / "lib").mkdir(parents=True)
    (env.roots / ".venv" / "lib" / "x.py").write_text("x")
    (env.roots / "node_modules" / "pkg").mkdir(parents=True)
    (env.roots / "node_modules" / "pkg" / "y.js").write_text("x")
    (env.roots / "ok.py").write_text("x")
    out = files.glob_files("**/*", str(env.roots))
    assert "ok.py" in out
    assert ".venv" not in out and "node_modules" not in out
    # naming the dir in the pattern allows it
    out2 = files.glob_files(".venv/**/*.py", str(env.roots))
    assert "x.py" in out2


def test_glob_truncation(env):
    for i in range(5):
        (env.roots / f"f{i}.txt").write_text("x")
    out = files.glob_files("*.txt", str(env.roots), max_results=3)
    assert "truncated" in out


# ----------------------------------------------------------------- grep --

def test_grep_basic(env):
    (env.roots / "a.py").write_text("hello world\nfoo bar\nHELLO again\n")
    (env.roots / "b.txt").write_text("nothing here\n")
    out = files.grep_files("hello", str(env.roots), case_insensitive=True)
    assert "a.py:1: hello world" in out
    assert "a.py:3: HELLO again" in out
    assert "b.txt" not in out


def test_grep_context_and_glob(env):
    (env.roots / "a.txt").write_text("one\ntwo TARGET three\nfour\n")
    (env.roots / "sub").mkdir()
    (env.roots / "sub" / "b.py").write_text("TARGET py\n")
    out = files.grep_files("TARGET", str(env.roots), glob="*.txt", context=1)
    assert "a.txt:2: two TARGET three" in out
    assert "a.txt-1- one" in out and "a.txt-3- four" in out
    assert "b.py" not in out


def test_grep_skips_binary_and_big(env):
    (env.roots / "bin.dat").write_bytes(b"\x00\x01TARGET\x00")
    (env.roots / "big.txt").write_text("TARGET " + "x" * (21 * 1024 * 1024))
    (env.roots / "ok.txt").write_text("TARGET ok\n")
    out = files.grep_files("TARGET", str(env.roots))
    assert "ok.txt" in out
    assert "bin.dat" not in out and "big.txt" not in out


def test_grep_skips_vendor_dirs(env):
    (env.roots / ".git").mkdir()
    (env.roots / ".git" / "config").write_text("TARGET git\n")
    (env.roots / "ok.txt").write_text("TARGET ok\n")
    out = files.grep_files("TARGET", str(env.roots))
    assert "ok.txt" in out and ".git" not in out


def test_grep_bad_regex(env):
    with pytest.raises(WindesktopctlError, match="bad regex"):
        files.grep_files("([unclosed", str(env.roots))


# ------------------------------------------------------------ file_info --

def test_file_info_sha(env):
    p = env.roots / "f.bin"
    data = os.urandom(100)
    p.write_bytes(data)
    out = files.file_info(str(p))
    assert hashlib.sha256(data).hexdigest() in out
    assert "size=100" in out and "chunks=1" in out


# -------------------------------------------------------- download_chunk --

def test_download_chunk_ranges(env):
    p = env.roots / "big.bin"
    data = os.urandom(5 * 1024 * 1024 + 123)
    p.write_bytes(data)
    chunks = []
    off = 0
    while True:
        r = json.loads(files.download_chunk(str(p), offset=off, length=4 * 1024 * 1024))
        chunks.append(base64.b64decode(r["data_b64"]))
        off = r["offset"] + r["length"]
        if r["eof"]:
            break
    assert b"".join(chunks) == data


def test_download_chunk_length_cap(env):
    p = env.roots / "f.bin"
    p.write_bytes(b"x" * 100)
    r = json.loads(files.download_chunk(str(p), length=999 * 1024 * 1024))
    assert r["length"] <= files.CHUNK_MAX_BYTES


# --------------------------------------------------------- upload_chunk --

def test_upload_chunk_roundtrip(env):
    p = env.roots / "up.bin"
    data = os.urandom(3 * 1024 * 1024)
    sha = hashlib.sha256(data).hexdigest()
    # two chunks
    c1, c2 = data[:2 * 1024 * 1024], data[2 * 1024 * 1024:]
    out = files.upload_chunk(str(p), 0, base64.b64encode(c1).decode())
    assert "next offset=" in out
    files.upload_chunk(str(p), len(c1), base64.b64encode(c2).decode(),
                       done=True, sha256=sha)
    assert p.read_bytes() == data
    assert not Path(str(p) + ".part").exists()


def test_upload_chunk_offset_mismatch(env):
    p = env.roots / "up.bin"
    files.upload_chunk(str(p), 0, base64.b64encode(b"aaaa").decode())
    with pytest.raises(WindesktopctlError, match="offset mismatch") as ei:
        files.upload_chunk(str(p), 2, base64.b64encode(b"bbbb").decode())
    assert "expected 4" in str(ei.value)
    # offset=0 restarts (truncates) - allowed by design
    files.upload_chunk(str(p), 0, base64.b64encode(b"zz").decode())
    assert Path(str(p) + ".part").read_bytes() == b"zz"


def test_upload_chunk_sha_mismatch_keeps_part(env):
    p = env.roots / "up.bin"
    with pytest.raises(WindesktopctlError, match="sha256 mismatch"):
        files.upload_chunk(str(p), 0, base64.b64encode(b"data").decode(),
                           done=True, sha256="0" * 64)
    # .part kept for resume; target not created
    assert Path(str(p) + ".part").exists()
    assert not p.exists()


def test_upload_chunk_refuses_overwrite(env):
    p = env.roots / "up.bin"
    p.write_bytes(b"original")
    with pytest.raises(WindesktopctlError, match="target exists"):
        files.upload_chunk(str(p), 0, base64.b64encode(b"new").decode(),
                           done=True)
    # overwrite=true succeeds
    files.upload_chunk(str(p), 0, base64.b64encode(b"new").decode(),
                       done=True, overwrite=True)
    assert p.read_bytes() == b"new"


def test_upload_chunk_resume_after_offset_error(env):
    p = env.roots / "up.bin"
    files.upload_chunk(str(p), 0, base64.b64encode(b"aaaa").decode())
    # client retries with correct offset -> appends
    files.upload_chunk(str(p), 4, base64.b64encode(b"bbbb").decode())
    assert Path(str(p) + ".part").read_bytes() == b"aaaabbbb"
