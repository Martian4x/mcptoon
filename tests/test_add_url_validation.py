"""`mcptoon add --http` must reject a value that cannot be an MCP endpoint.

It used to accept any string — `javascript:alert(1)`, `ftp://x`, `not a url`, or a
host-less `http://` — and write it to config, so the mistake surfaced later as a
confusing connection error instead of at the keyboard. The check is deliberately
narrow (wrong scheme / no host only): a typo in the path, or an unusual-but-real
host, still passes, because rejecting those would block a command that would work.
"""
from __future__ import annotations

import sys

import pytest

from mcptoon import cli, config as cfg


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPTOON_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))


@pytest.mark.parametrize("url", [
    "not a url", "javascript:alert(1)", "ftp://x", "file:///etc/passwd",
    "http://", "https://", "", "  ",
])
def test_add_http_rejects_an_unusable_url(url, capsys):
    with pytest.raises(SystemExit) as exc:
        cli._cmd_add(["s1", "--http", url])
    assert exc.value.code == 1
    assert "--http" in capsys.readouterr().err
    assert "s1" not in cfg.list_servers(), "a rejected URL must not be written"


@pytest.mark.parametrize("url", [
    "https://ok.example/mcp", "http://localhost:3000/mcp",
    "https://api.example.com/mcp/v1", "https://127.0.0.1:8080",
])
def test_add_http_accepts_a_usable_url(url, capsys):
    cli._cmd_add(["s1", "--http", url])  # must not raise
    assert cfg.list_servers() == ["s1"]
    assert cfg.load_config()["s1"]["url"] == url


def test_the_reason_names_the_scheme():
    assert "javascript" in cli._http_url_problem("javascript:alert(1)")
    assert "host" in cli._http_url_problem("http://")
    assert cli._http_url_problem("https://ok.example/mcp") == ""


def test_add_stdio_is_unaffected(capsys):
    cli._cmd_add(["s2", "--stdio", "npx", "-y", "some-server"])
    assert cfg.load_config()["s2"]["transport"] == "stdio"


# ── add --stdio must catch "can never launch" at the keyboard ────────────────
def test_add_stdio_missing_path_is_refused(capsys):
    """A path the user wrote that does not exist is an unambiguous typo — refuse it
    rather than write a server that fails later as an opaque WinError 2 / ENOENT."""
    missing = str(cfg.__file__) + ".does-not-exist"  # an absolute path, guaranteed absent
    with pytest.raises(SystemExit) as exc:
        cli._cmd_add(["broken", "--stdio", missing, "arg"])
    assert exc.value.code == 1
    assert "does not exist" in capsys.readouterr().err
    assert "broken" not in cfg.list_servers(), "a refused server must not be written"


def test_add_stdio_missing_path_can_be_forced(capsys):
    """--force is the escape hatch: the user may know the binary appears later."""
    missing = str(cfg.__file__) + ".does-not-exist"
    cli._cmd_add(["broken", "--stdio", missing, "--force"])
    assert "broken" in cfg.list_servers()


def test_add_stdio_missing_bare_name_warns_but_adds(capsys):
    """A bare name not on PATH is only a warning: it may be installed later or live
    outside PATH, so the server is still added (and the warning names it)."""
    cli._cmd_add(["maybe", "--stdio", "definitely_not_on_path_xyz", "arg"])
    err = capsys.readouterr().err
    assert "not on PATH" in err and "definitely_not_on_path_xyz" in err
    assert "maybe" in cfg.list_servers()


def test_command_check_classifies(tmp_path):
    from mcptoon.cli import _command_check
    assert _command_check(sys.executable) == "ok"                 # absolute existing file
    assert _command_check(str(tmp_path / "nope")) == "missing_path"  # absolute absent
    assert _command_check("definitely_not_on_path_xyz") == "missing_name"
    assert _command_check("") == "missing_path"
