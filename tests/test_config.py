"""Unit tests for core/config.py - defaults, mtime reload, --init template."""

import pytest

from windesktopctl.core import config as cfg
from windesktopctl.core import state as st


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "wdhome"
    d.mkdir()
    monkeypatch.setenv("WINDESKTOPCTL_HOME", str(d))
    return d


def _write(home, text):
    p = home / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_when_missing(home):
    assert cfg.shell_enabled() is False
    assert cfg.file_tools_enabled() is False
    assert cfg.file_roots() == []


def test_load_values(home):
    _write(home, "shell_enabled = true\nfile_tools_enabled = true\nfile_roots = ['C:/x', 'D:/y']\n")
    d = cfg.load()
    assert d["shell_enabled"] is True
    assert d["file_tools_enabled"] is True
    assert d["file_roots"] == ["C:/x", "D:/y"]
    assert cfg.shell_enabled() is True
    assert cfg.file_roots() == ["C:/x", "D:/y"]


def test_partial_file_keeps_defaults(home):
    _write(home, "shell_enabled = true\n")
    d = cfg.load()
    assert d["shell_enabled"] is True
    assert d["file_tools_enabled"] is False
    assert d["file_roots"] == []


def test_mtime_reload(home):
    p = _write(home, "shell_enabled = false\n")
    assert cfg.shell_enabled() is False
    p.write_text("shell_enabled = true\n", encoding="utf-8")
    # force a distinct mtime (some filesystems have coarse granularity)
    import os, time
    stt = p.stat()
    os.utime(p, ns=(stt.st_atime_ns, stt.st_mtime_ns + 1_000_000))
    assert cfg.shell_enabled() is True


def test_invalid_toml_falls_back(home):
    _write(home, "this is not = = toml {{{")
    d = cfg.load()
    assert d["shell_enabled"] is False


def test_init_config_writes_template(home):
    p = cfg.init_config()
    assert p == home / "config.toml"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "shell_enabled" in text and "file_roots" in text
    # second call does not overwrite
    p.write_text("shell_enabled = true\n", encoding="utf-8")
    cfg.init_config()
    assert "shell_enabled = true" in p.read_text(encoding="utf-8")


def test_state_dir_respects_env(home):
    assert st.state_dir() == home
