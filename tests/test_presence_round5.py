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

"""Presence, round 3: name the capability, filter by agent, and warn when silent.

The earlier rounds made calls visible ("was I used, by whom, when"). These close
the three questions that visibility left open:

1. **What capability moved** — `resolve_skills` recorded only that it ran, not
   which skills it handed out. `track_call(detail=...)` now carries the skill
   names into the ledger, and `activity` prints them under the call.
2. **Which agent used me** — `usage` had a by-server breakdown but no by-host one,
   and `activity` could not filter to a single agent. Both are now answered.
3. **Quietly working vs quietly broken** — a wired-in gateway that never records a
   call looks identical to one that is idle. `status` and `doctor` now say so,
   softly, instead of staying mute.
"""

from __future__ import annotations

import io
import threading
import time
from contextlib import redirect_stdout

import pytest

from mcptoon import cli, usage
from mcptoon.serve import MCPServerBridge


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "activity.jsonl"
    monkeypatch.setenv("MCPTOON_ACTIVITY_FILE", str(path))
    monkeypatch.setattr(usage, "_HOST", None, raising=False)
    monkeypatch.setattr(usage, "_SESSION_CALLS", 0, raising=False)
    return path


def _bridge():
    b = MCPServerBridge.__new__(MCPServerBridge)
    b._tool_index = {}
    b._tool_index_lock = threading.RLock()
    b._lock = threading.RLock()
    b._initialized = True
    b._servers = {}
    b._output_format = "auto"
    b._footer_stamped = True
    return b


# ─── 1. detail names the distributed capability ─────────────────────────────

def test_track_call_writes_detail_into_the_ledger(ledger):
    usage.track_call("mcptoon", "mcptoon_resolve_skills", ok=True,
                     tokens_est=10, detail="distributed: xiewen-shipin")
    entry = usage.read_activity(5)[0]
    assert entry["detail"] == "distributed: xiewen-shipin"


def test_track_call_without_detail_has_no_detail_key(ledger):
    usage.track_call("fetch", "fetch", ok=True, tokens_est=3)
    assert "detail" not in usage.read_activity(5)[0]


def test_detail_is_capped_to_keep_a_line_sane(ledger):
    usage.track_call("mcptoon", "mcptoon_resolve_skills", ok=True,
                     tokens_est=1, detail="x" * 5000)
    assert len(usage.read_activity(5)[0]["detail"]) == 400


def test_resolve_call_records_the_skill_slugs(ledger):
    # Drive _track_native with a payload shaped like a real resolve result, so the
    # assertion does not depend on this machine having a built skill catalog.
    b = _bridge()
    usage.set_host("Cursor")
    payload = {
        "content": [{"type": "text", "text": "{}"}],
        "structuredContent": {"query": "t", "k": 3,
                              "shortlist": [{"slug": "xiewen-shipin"},
                                            {"slug": "duanju-juben"}]},
        "isError": False,
    }
    b._track_native("mcptoon_resolve_skills", payload)
    entry = usage.read_activity(5)[0]
    assert entry["tool"] == "mcptoon_resolve_skills"
    assert entry["detail"] == "distributed: xiewen-shipin, duanju-juben"


def test_activity_prints_the_detail_line(ledger, capsys):
    usage.track_call("mcptoon", "mcptoon_resolve_skills", ok=True, tokens_est=5,
                     detail="distributed: manju, book")
    cli._cmd_activity([], "auto")
    out = capsys.readouterr().out
    assert "↳ distributed: manju, book" in out


# ─── 2. by-host in usage + --host filter in activity ────────────────────────

def test_get_usage_stats_exposes_by_host(ledger):
    usage.set_host("Cursor")
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    usage.set_host("Claude Desktop")
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    by_host = usage.get_usage_stats()["by_host"]
    assert by_host == {"Cursor": 1, "Claude Desktop": 1}


def test_usage_prints_a_by_host_block(ledger, capsys):
    usage.set_host("Cursor")
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    cli._cmd_usage([], "auto")
    out = capsys.readouterr().out
    assert "By host:" in out and "Cursor" in out


def test_activity_host_filter_keeps_one_agent(ledger, capsys):
    usage.set_host("Cursor")
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    usage.set_host("Claude Desktop")
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    cli._cmd_activity(["--host", "Cursor"], "auto")
    out = capsys.readouterr().out
    assert "from Cursor" in out
    assert "Claude Desktop" not in out


def test_activity_host_equals_form(ledger, capsys):
    usage.set_host("Cursor")
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    usage.set_host("Claude Desktop")
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    cli._cmd_activity(["--host=Claude Desktop"], "auto")
    out = capsys.readouterr().out
    assert "from Claude Desktop" in out


def test_activity_host_filter_with_no_match_says_so(ledger, capsys):
    usage.set_host("Cursor")
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    cli._cmd_activity(["--host", "Nope"], "auto")
    out = capsys.readouterr().out
    assert "No routed calls recorded from host 'Nope'" in out


# ─── 3. quietly working vs quietly broken ───────────────────────────────────

def test_installed_but_silent_true_when_wired_and_empty(monkeypatch):
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: True)
    assert cli._installed_but_silent({"entries": 0}) is True


def test_installed_but_silent_false_when_not_wired(monkeypatch):
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: False)
    assert cli._installed_but_silent({"entries": 0}) is False


def test_installed_but_silent_false_right_after_a_call(monkeypatch):
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: True)
    assert cli._installed_but_silent({"entries": 1, "last_ts": time.time()}) is False


def test_installed_but_silent_true_when_stale_over_a_day(monkeypatch):
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: True)
    old = time.time() - 25 * 3600
    assert cli._installed_but_silent({"entries": 3, "last_ts": old}) is True


def test_status_warns_when_wired_but_silent(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: True)
    monkeypatch.setattr(cli.cfg, "has_config_file", lambda: True)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: {})
    monkeypatch.setattr(usage, "activity_summary",
                        lambda: {"entries": 0, "last_host": None, "last_ts": None,
                                 "today_calls": 0, "hosts": {}})
    monkeypatch.setattr(usage, "get_usage_stats",
                        lambda: {"total_calls": 0, "success_rate": "0/0",
                                 "total_tokens_est": 0, "by_server": {},
                                 "top_tools": {}, "by_host": {}})
    out = io.StringIO()
    with redirect_stdout(out):
        cli._cmd_status([], "auto")
    text = out.getvalue()
    assert "no routed calls yet" in text
    assert "Wired into an agent, but no call has come through" in text


def test_status_is_quiet_when_a_call_exists(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: True)
    monkeypatch.setattr(cli.cfg, "has_config_file", lambda: True)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: {})
    monkeypatch.setattr(usage, "activity_summary",
                        lambda: {"entries": 2, "last_host": "Cursor",
                                 "last_ts": time.time(), "today_calls": 2,
                                 "hosts": {"Cursor": 2}})
    monkeypatch.setattr(usage, "get_usage_stats",
                        lambda: {"total_calls": 2, "success_rate": "2/2",
                                 "total_tokens_est": 10, "by_server": {"fetch": 2},
                                 "top_tools": {"fetch:fetch": 2},
                                 "by_host": {"Cursor": 2}})
    out = io.StringIO()
    with redirect_stdout(out):
        cli._cmd_status([], "auto")
    text = out.getvalue()
    assert "Wired into an agent, but no call has come through" not in text
    assert "Nothing routed in over a day" not in text
