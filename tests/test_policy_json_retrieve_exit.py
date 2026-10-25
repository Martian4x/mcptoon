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

"""`policy list --json` and bare `retrieve` exit code.

Reported 2026-10-18:
- `policy list --json` printed human text; scripts could not read the map.
- `mcptoon retrieve` (no handle) printed usage but exited 0, so
  `mcptoon retrieve && echo ok` reported success on a no-op.
"""
import io
import json
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from mcptoon import cli
from mcptoon import config as cfg


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with patch.object(cli.sys, "argv", ["mcptoon", *argv]), \
            redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return out.getvalue(), err.getvalue(), code


# ── policy list --json ───────────────────────────────────────────────────────

def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPTOON_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MCPTOON_COMPRESSION_FILE", str(tmp_path / "compression.json"))
    monkeypatch.delenv("MCPTOON_SERVERS", raising=False)


def test_policy_list_json_with_policy(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    cfg.add_server("s", {"transport": "stdio", "command": ["x"], "args": []})
    cfg.save_policies({"s:tool": "raw"})
    out, err, code = _run(["policy", "list", "--json"])
    assert json.loads(out) == {"s:tool": "raw"}


def test_policy_list_json_empty(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    out, err, code = _run(["policy", "list", "--json"])
    assert json.loads(out) == {}


def test_policy_list_human_unchanged(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    out, err, code = _run(["policy"])
    assert "No compression policies configured" in out


# ── retrieve no-arg exit code ────────────────────────────────────────────────

def test_retrieve_no_arg_exits_1():
    out, err, code = _run(["retrieve"])
    assert code == 1
    assert "Usage: mcptoon retrieve" in out


def test_retrieve_help_exits_0():
    # `retrieve --help` goes through the shared command-help page (a success).
    out, err, code = _run(["retrieve", "--help"])
    assert code == 0
    assert "retrieve" in out
