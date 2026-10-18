# Copyright 2025-2026 cxh
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

"""Global-flag support for `list`, `manifest --head`, `toggle --list --json`.

Reported 2026-10-18:
- `list` had no access to the global output flags: `--json` printed human text,
  `--head` did nothing, `--max-chars` was ignored.
- `manifest --head N` was ignored in the human/compact/toon paths.
- `toggle --list --json` printed human text instead of JSON.
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


def _isolated(tmp_path, monkeypatch, servers):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    monkeypatch.setenv("MCPTOON_CONFIG_FILE", str(cfg_file))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.delenv("MCPTOON_SERVERS", raising=False)


_SERVERS = {
    "alpha": {"transport": "stdio", "command": ["python"], "args": ["-m", "a"]},
    "beta": {"transport": "http", "url": "https://b.example/mcp"},
    "gamma": {"transport": "stdio", "command": ["python"], "args": ["-m", "g"]},
}


# ── list ─────────────────────────────────────────────────────────────────────

def test_list_json_is_structured(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch, _SERVERS)
    out, err, code = _run(["list", "--json"])
    data = json.loads(out)
    assert {r["name"] for r in data} == {"alpha", "beta", "gamma"}
    beta = next(r for r in data if r["name"] == "beta")
    assert beta["transport"] == "http" and beta["url"] == "https://b.example/mcp"
    alpha = next(r for r in data if r["name"] == "alpha")
    assert alpha["command"] == ["python", "-m", "a"]


def test_list_head_caps_servers(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch, _SERVERS)
    out, err, code = _run(["list", "--head", "2"])
    assert "2 of 3" in out
    assert out.count("[stdio]") + out.count("[http]") == 2


def test_list_json_empty_is_empty_array(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch, {})
    out, err, code = _run(["list", "--json"])
    assert json.loads(out) == []


def test_list_human_unchanged_without_flags(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch, _SERVERS)
    out, err, code = _run(["list"])
    assert "Configured servers (3):" in out


# ── manifest --head ──────────────────────────────────────────────────────────

_MANIFEST = {"s1": [{"name": "a"}], "s2": [{"name": "b"}], "s3": [{"name": "c"}]}


def test_manifest_head_caps_servers():
    with patch("mcptoon.manifest.get_manifest", return_value=_MANIFEST):
        out, err, code = _run(["manifest", "--head", "2"])
    assert "s1" in out and "s2" in out
    assert "s3" not in out


def test_manifest_without_head_shows_all():
    with patch("mcptoon.manifest.get_manifest", return_value=_MANIFEST):
        out, err, code = _run(["manifest"])
    assert "s1" in out and "s2" in out and "s3" in out


# ── toggle --list --json ─────────────────────────────────────────────────────

def test_toggle_list_json(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch, _SERVERS)
    with patch.object(cfg, "TOGGLE_FILE", tmp_path / "toggles.json"):
        cfg.toggle_tool("alpha", "a")
        out, err, code = _run(["toggle", "--list", "--json"])
    assert json.loads(out) == ["alpha:a"]


def test_toggle_list_json_empty(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch, _SERVERS)
    with patch.object(cfg, "TOGGLE_FILE", tmp_path / "toggles.json"):
        out, err, code = _run(["toggle", "--list", "--json"])
    assert json.loads(out) == []


def test_toggle_list_human_unchanged(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch, _SERVERS)
    with patch.object(cfg, "TOGGLE_FILE", tmp_path / "toggles.json"):
        out, err, code = _run(["toggle", "--list"])
    assert "No tools are disabled" in out
