# Copyright 2025-2026 activeing123
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Errors go to stderr; `config --help` shows config's own usage; add won't clobber.

Reported 2026-10-17:

- Error / usage messages were printed to stdout, so `mcptoon x >out 2>err` mixed
  failures into `out` and left `err` empty. They now go through `cli._err` (stderr),
  while legitimate empty results ("no matches") stay on stdout.
- `mcptoon config --help` fell back to the whole global page because `config` was
  never added to the per-command handler map, so its own get/set/unset/reset syntax
  was undiscoverable.
- `mcptoon add foo --stdio ...` silently overwrote an existing `foo`; a re-run or a
  typo'd name replaced a working server. It now refuses unless `--force`.
"""
import io
import sys
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import pytest

from mcptoon import cli
from mcptoon import config as cfg


def _run(argv):
    """Return (stdout, stderr, exit_code)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with patch.object(sys, "argv", ["mcptoon", *argv]), \
            redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return out.getvalue(), err.getvalue(), code


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    # One real server so a `policy set` reaches the policy validation (server
    # existence is now checked first, 2026-10-17).
    cfg_file.write_text(
        '{"servers": {"s": {"transport": "stdio", "command": ["x"], "args": []}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("MCPTOON_CONFIG_FILE", str(cfg_file))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.delenv("MCPTOON_SERVERS", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── errors on stderr, nothing on stdout ──────────────────────────────────────

@pytest.mark.parametrize("argv,needle", [
    (["remove", "nosuch"], "Server not found"),
    (["config", "get", "bogus"], "Unknown setting"),
    (["add"], "Usage: mcptoon add"),
    (["policy", "set", "s", "t", "nonsense"], "Unknown policy"),
    (["config", "wat"], "Unknown config action"),
    (["completion", "tcsh"], "Unknown shell"),
])
def test_errors_go_to_stderr_not_stdout(argv, needle, isolated):
    out, err, code = _run(argv)
    assert code != 0
    assert needle in err, f"{argv}: expected {needle!r} on stderr, got err={err!r}"
    # The error text itself must not leak to stdout (an unrelated first-run banner
    # may, but the diagnostic must be on stderr only).
    assert needle not in out, f"{argv}: error text leaked to stdout: {out!r}"


def test_empty_result_stays_on_stdout(tmp_path, monkeypatch):
    # "No servers configured" is the command's answer, not an error — stdout.
    monkeypatch.setenv("MCPTOON_CONFIG_FILE", str(tmp_path / "empty.json"))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.delenv("MCPTOON_SERVERS", raising=False)
    out, err, code = _run(["list"])
    assert "No servers configured" in out
    assert "No servers configured" not in err


# ── config --help shows config's own usage ───────────────────────────────────

def test_config_help_shows_its_own_usage():
    out, err, code = _run(["config", "--help"])
    text = out + err
    assert "mcptoon config" in text
    assert "config unset" in text and "config reset" in text, \
        "config --help must name its own unset/reset actions"
    # Not the whole global front door re-dumped.
    assert "one gateway for all your MCP tools" not in text


# ── add refuses to clobber an existing server ────────────────────────────────

def test_add_refuses_duplicate_without_force(isolated):
    cfg.add_server("foo", {"transport": "stdio", "command": ["python", "-m", "a"], "args": []})
    out, err, code = _run(["add", "foo", "--stdio", "python", "-m", "b"])
    assert code != 0
    assert "already exists" in err
    # The original must be untouched.
    cfg_foo = cfg.get_server_config("foo")
    assert cfg_foo["command"] + cfg_foo["args"] == ["python", "-m", "a"]


def test_add_force_overwrites(isolated):
    cfg.add_server("foo", {"transport": "stdio", "command": ["python", "-m", "a"], "args": []})
    out, err, code = _run(["add", "foo", "--stdio", "python", "-m", "b", "--force"])
    assert code == 0
    cfg_foo = cfg.get_server_config("foo")
    assert cfg_foo["command"] + cfg_foo["args"] == ["python", "-m", "b"]


def test_add_new_server_is_unaffected(isolated):
    out, err, code = _run(["add", "fresh", "--stdio", "python", "-m", "x"])
    assert code == 0
    assert cfg.get_server_config("fresh") is not None
