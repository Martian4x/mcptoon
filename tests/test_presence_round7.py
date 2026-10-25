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

"""Presence, round 5: see it live, see how slow, see when it broke, see the cap.

Four things an operator watching a running gateway needs:

1. Each call records how long it took (`ms`), and `activity` flags a slow one.
2. `activity --follow` streams new calls as they arrive (tail -f).
3. `activity --summary` reports the retention cap and the ledger's size, so the
   silent roll-off at 2000 lines is no longer invisible.
4. `status` names the most recent failed call — "did it break recently?".
"""

from __future__ import annotations

import io
import json
import threading
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
    monkeypatch.setattr(usage, "_USAGE_FILE", tmp_path / "usage.json", raising=False)
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


# ─── 1. ms + slow flag ──────────────────────────────────────────────────────

def test_track_call_records_ms(ledger):
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1, ms=42)
    assert usage.read_activity(5)[0]["ms"] == 42


def test_no_ms_key_when_not_timed(ledger):
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    assert "ms" not in usage.read_activity(5)[0]


def test_activity_flags_a_slow_call(ledger, capsys):
    usage.track_call("slow", "op", ok=True, tokens_est=1, ms=cli._SLOW_CALL_MS + 1)
    cli._cmd_activity([], "auto")
    assert "SLOW" in capsys.readouterr().out


def test_activity_does_not_flag_a_fast_call(ledger, capsys):
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1, ms=10)
    cli._cmd_activity([], "auto")
    assert "SLOW" not in capsys.readouterr().out


def test_activity_shows_ms_without_flag_for_midrange(ledger, capsys):
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1, ms=250)
    cli._cmd_activity([], "auto")
    out = capsys.readouterr().out
    assert "250 ms" in out and "SLOW" not in out


# ─── 2. --follow ────────────────────────────────────────────────────────────

def test_follow_shows_new_calls_and_hides_preseed(ledger, monkeypatch, capsys):
    usage.track_call("old", "seed", ok=True, tokens_est=1, ms=5)
    state = {"n": 0}

    def fake_sleep(_x):
        state["n"] += 1
        if state["n"] == 1:
            usage.track_call("newtool", "go", ok=True, tokens_est=9, ms=12)
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    cli._activity_follow(interval=0.01)
    out = capsys.readouterr().out
    assert "newtool:go" in out
    assert "old:seed" not in out


def test_follow_respects_host_filter(ledger, monkeypatch, capsys):
    usage.set_host("Cursor")
    usage.track_call("old", "seed", ok=True, tokens_est=1)
    state = {"n": 0}

    def fake_sleep(_x):
        state["n"] += 1
        if state["n"] == 1:
            usage.set_host("Claude Desktop")
            usage.track_call("a", "b", ok=True, tokens_est=1)
            usage.set_host("Cursor")
            usage.track_call("c", "d", ok=True, tokens_est=1)
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    cli._activity_follow(host_filter="Cursor", interval=0.01)
    out = capsys.readouterr().out
    assert "c:d" in out
    assert "a:b" not in out


# ─── 3. summary reports retention + size ────────────────────────────────────

def test_summary_reports_retention_and_size(ledger, capsys):
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1, ms=5)
    cli._cmd_activity(["--summary"], "auto")
    out = capsys.readouterr().out
    assert "Retention" in out
    assert "2,000 calls kept" in out
    assert "Ledger size" in out


def test_human_bytes_formats_sizes():
    assert cli._human_bytes(512) == "512 B"
    assert cli._human_bytes(2048) == "2.0 KB"
    assert cli._human_bytes(5 * 1024 * 1024) == "5.0 MB"
    assert cli._human_bytes("bad") == "—"


# ─── 4. status last failure ─────────────────────────────────────────────────

def test_summary_captures_last_failure(ledger):
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    usage.track_call("fetch", "boom", ok=False, tokens_est=0, err="CALL_ERROR: x")
    lf = usage.activity_summary()["last_fail"]
    assert lf["tool"] == "boom"
    assert lf["err"] == "CALL_ERROR: x"


def test_last_failure_is_none_when_all_ok(ledger):
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    assert usage.activity_summary()["last_fail"] is None


def test_status_prints_last_failure(monkeypatch, capsys):
    monkeypatch.setattr(cli.cfg, "has_config_file", lambda: True)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: True)
    monkeypatch.setattr(usage, "activity_summary",
                        lambda: {"entries": 2, "last_host": "Cursor", "last_ts": 1.0,
                                 "today_calls": 2, "hosts": {"Cursor": 2},
                                 "last_fail": {"host": "Cursor", "server": "fetch",
                                               "tool": "boom", "ts": 1.0,
                                               "err": "CALL_ERROR: x"}})
    monkeypatch.setattr(usage, "get_usage_stats",
                        lambda: {"total_calls": 2, "success_rate": "1/2",
                                 "total_tokens_est": 5, "by_server": {"fetch": 2},
                                 "top_tools": {"fetch:boom": 1}, "by_host": {"Cursor": 2},
                                 "routed_calls": 2, "native_calls": 0})
    out = io.StringIO()
    with redirect_stdout(out):
        cli._cmd_status([], "auto")
    text = out.getvalue()
    assert "Last failure" in text
    assert "fetch:boom" in text
    assert "CALL_ERROR: x" in text


def test_status_json_has_last_fail(monkeypatch, capsys):
    monkeypatch.setattr(cli.cfg, "has_config_file", lambda: True)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: True)
    monkeypatch.setattr(usage, "activity_summary",
                        lambda: {"entries": 0, "last_host": None, "last_ts": None,
                                 "today_calls": 0, "hosts": {}, "last_fail": None})
    monkeypatch.setattr(usage, "get_usage_stats",
                        lambda: {"total_calls": 0, "success_rate": "0/0",
                                 "total_tokens_est": 0, "by_server": {},
                                 "top_tools": {}, "by_host": {},
                                 "routed_calls": 0, "native_calls": 0})
    out = io.StringIO()
    with redirect_stdout(out):
        cli._cmd_status([], "json")
    payload = json.loads(out.getvalue())
    assert "last_fail" in payload
