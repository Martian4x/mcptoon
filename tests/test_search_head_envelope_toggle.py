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

"""`search --head`, `call --envelope` error exit, `toggle` tool validation.

Reported 2026-10-17:
- `search <q> --head N` ignored the cap in slim/compact/human modes (only json/toon
  honoured it), so `--head 5` printed all 20 hits.
- `call --envelope` returned the raw MCP result with `isError: true` and exited 0,
  while the non-envelope path already exited 1 on a tool error.
- `toggle <server> <unknown-tool>` wrote a dead "disabled" entry and exited 0.
"""
import io
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from mcptoon import cli
from mcptoon import config as cfg
from mcptoon import cache as cache_mod


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


def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPTOON_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.delenv("MCPTOON_SERVERS", raising=False)


# ── search --head caps every output mode ─────────────────────────────────────

_HITS = [{"server": "s", "name": f"tool{i}", "description": "d", "params": "", "score": 0.9}
         for i in range(20)]


def test_search_head_caps_human_output():
    with patch("mcptoon.manifest.search_tools", return_value=_HITS):
        out, err, code = _run(["search", "x", "--head", "3"])
    assert "Found 3 of 20" in out
    assert out.count("tool") >= 3


def test_search_head_caps_slim_output():
    with patch("mcptoon.manifest.search_tools", return_value=_HITS):
        out, err, code = _run(["search", "x", "--head", "4", "--slim"])
    assert len([ln for ln in out.splitlines() if ln.strip()]) == 4


def test_search_head_caps_json_output():
    import json
    with patch("mcptoon.manifest.search_tools", return_value=_HITS):
        out, err, code = _run(["search", "x", "--head", "4", "--json"])
    assert len(json.loads(out)) == 4


def test_search_without_head_shows_all():
    with patch("mcptoon.manifest.search_tools", return_value=_HITS):
        out, err, code = _run(["search", "x"])
    assert "Found 20 tool(s)" in out


# ── call --envelope error must exit non-zero ─────────────────────────────────

def test_call_envelope_iserror_exits_nonzero():
    fake = {"content": [{"type": "text", "text": "boom"}], "isError": True}
    with patch.object(cli, "call_tool", return_value=fake):
        out, err, code = _run(["call", "srv", "tool", "{}", "--envelope"])
    assert code == 1


def test_call_envelope_success_exits_zero():
    fake = {"content": [{"type": "text", "text": "ok"}], "isError": False}
    with patch.object(cli, "call_tool", return_value=fake):
        out, err, code = _run(["call", "srv", "tool", "{}", "--envelope"])
    assert code == 0


# ── toggle: an unknown tool is refused when the cache knows the server ────────

def test_toggle_rejects_unknown_tool_when_cached(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    cfg.add_server("srv", {"transport": "stdio", "command": ["x"], "args": []})
    with patch.object(cache_mod, "get_cached_tools_any_age",
                      return_value=([{"name": "real"}], 1.0)):
        out, err, code = _run(["toggle", "srv", "ghost"])
    assert code != 0
    assert "not found" in err


def test_toggle_accepts_real_tool(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    cfg.add_server("srv", {"transport": "stdio", "command": ["x"], "args": []})
    with patch.object(cache_mod, "get_cached_tools_any_age",
                      return_value=([{"name": "real"}], 1.0)):
        out, err, code = _run(["toggle", "srv", "real"])
    assert code == 0
