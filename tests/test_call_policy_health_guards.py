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

"""Input-conflict and exit-code fixes reported 2026-10-17.

- `mcptoon call` accepted a positional JSON arg AND `--stdin`; stdin silently won
  and the positional args were dropped with no warning.
- `mcptoon policy set <server> <tool> ...` wrote an entry for a non-existent
  server or tool and exited 0, leaving a dead rule in the policy list.
- `mcptoon health` printed "1 dead" but exited 0 (the JSON path already exited 1),
  so a CI health check read a broken machine as healthy.
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
    monkeypatch.setenv("MCPTOON_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("MCPTOON_SERVERS", raising=False)


# ── call: positional JSON + --stdin is a conflict ────────────────────────────

def test_call_json_and_stdin_together_is_refused(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"a": 1}'))
    out, err, code = _run(["call", "srv", "tool", '{"b": 2}', "--stdin"])
    assert code != 0
    assert "not both" in err


def test_call_stdin_alone_still_works(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"a": 1}'))
    with patch.object(cli, "call_tool", return_value={"ok": True}) as m:
        out, err, code = _run(["call", "srv", "tool", "--stdin"])
    assert code == 0
    assert m.call_args.args[2] == {"a": 1}


# ── policy: server / tool must exist ─────────────────────────────────────────

def test_policy_set_rejects_unknown_server(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    out, err, code = _run(["policy", "set", "ghost", "tool", "raw"])
    assert code != 0
    assert "not found" in err
    assert cfg.list_policies() == {}, "a rejected policy must not be persisted"


def test_policy_set_rejects_unknown_tool_when_cached(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    cfg.add_server("srv", {"transport": "stdio", "command": ["x"], "args": []})
    with patch.object(cache_mod, "get_cached_tools_any_age",
                      return_value=([{"name": "real_tool"}], 5.0)):
        out, err, code = _run(["policy", "set", "srv", "ghost_tool", "raw"])
    assert code != 0
    assert "not found" in err
    assert cfg.list_policies() == {}


def test_policy_set_accepts_a_real_tool(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    cfg.add_server("srv", {"transport": "stdio", "command": ["x"], "args": []})
    with patch.object(cache_mod, "get_cached_tools_any_age",
                      return_value=([{"name": "real_tool"}], 5.0)):
        out, err, code = _run(["policy", "set", "srv", "real_tool", "raw"])
    assert code == 0
    assert cfg.list_policies()  # something was written


def test_policy_set_wildcard_skips_tool_check(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    cfg.add_server("srv", {"transport": "stdio", "command": ["x"], "args": []})
    out, err, code = _run(["policy", "set", "srv", "*", "raw"])
    assert code == 0


# ── health: a dead server must exit non-zero in both modes ────────────────────

_DEAD = [{"server": "broken", "transport": "stdio", "status": "timeout",
          "tools": 0, "latency_ms": 20000, "error": "RESPONSE_TIMEOUT"}]
_ALIVE = [{"server": "ok", "transport": "stdio", "status": "ok",
           "tools": 5, "latency_ms": 100, "error": ""}]


def test_health_text_mode_exits_nonzero_on_dead():
    with patch("mcptoon.health.check_all", return_value=_DEAD):
        out, err, code = _run(["health"])
    assert code == 1


def test_health_json_mode_exits_nonzero_on_dead():
    with patch("mcptoon.health.check_all", return_value=_DEAD):
        out, err, code = _run(["health", "--json"])
    assert code == 1


def test_health_exits_zero_when_all_alive():
    with patch("mcptoon.health.check_all", return_value=_ALIVE):
        out, err, code = _run(["health"])
    assert code == 0
