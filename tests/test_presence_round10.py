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

"""Presence, round 8: the query gaps that were still open.

1. CSV export carries `ms` (it was in json but missing from csv).
2. `activity --tool <server:tool|tool>` filters to one tool.
3. `activity --search <keyword>` filters over host/server/tool/detail/err.
4. `activity --summary` reports p50/p90 latency, not just the mean.
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from mcptoon import cli, usage


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "activity.jsonl"
    monkeypatch.setenv("MCPTOON_ACTIVITY_FILE", str(path))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(usage, "_HOST", None, raising=False)
    monkeypatch.setattr(usage, "_SESSION_CALLS", 0, raising=False)
    monkeypatch.setattr(usage, "_USAGE_FILE", tmp_path / "usage.json", raising=False)
    return path


def _seed():
    usage.set_host("Cursor")
    usage.track_call("fetch", "fetch", ok=True, tokens_est=10, ms=20)
    usage.track_call("fetch", "fetch", ok=True, tokens_est=10, ms=30)
    usage.track_call("slow", "op", ok=True, tokens_est=5, ms=8000)
    usage.track_call("fetch", "boom", ok=False, tokens_est=0, ms=40,
                     err="METHOD_NOT_FOUND: x")


# ─── 1. CSV carries ms ──────────────────────────────────────────────────────

def test_csv_export_has_ms_column(ledger, capsys):
    usage.track_call("fetch", "fetch", ok=True, tokens_est=10, ms=123)
    cli._cmd_activity(["--export", "csv"], "auto")
    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    header = rows[0]
    assert "ms" in header
    ms_idx = header.index("ms")
    assert rows[1][ms_idx] == "123"


def test_csv_ms_empty_when_untimed(ledger, capsys):
    usage.track_call("fetch", "fetch", ok=True, tokens_est=10)  # no ms
    cli._cmd_activity(["--export", "csv"], "auto")
    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    ms_idx = rows[0].index("ms")
    assert rows[1][ms_idx] == ""


# ─── 2. --tool ──────────────────────────────────────────────────────────────

def test_tool_filter_server_and_tool(ledger, capsys):
    _seed()
    cli._cmd_activity(["--tool", "fetch:fetch"], "auto")
    out = capsys.readouterr().out
    assert "fetch:fetch" in out
    assert "slow:op" not in out and "fetch:boom" not in out


def test_tool_filter_bare_name(ledger, capsys):
    _seed()
    cli._cmd_activity(["--tool", "boom"], "auto")
    out = capsys.readouterr().out
    assert "fetch:boom" in out
    assert "fetch:fetch" not in out


def test_tool_filter_empty_window(ledger, capsys):
    _seed()
    cli._cmd_activity(["--tool", "nope"], "auto")
    assert "No routed calls recorded for tool 'nope'" in capsys.readouterr().out


def test_tool_filter_json_payload(ledger, capsys):
    _seed()
    cli._cmd_activity(["--tool", "fetch:fetch"], "json")
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "fetch:fetch"
    assert payload["count"] == 2


# ─── 3. --search ────────────────────────────────────────────────────────────

def test_search_matches_err_text(ledger, capsys):
    _seed()
    cli._cmd_activity(["--search", "not_found"], "auto")
    out = capsys.readouterr().out
    assert "fetch:boom" in out
    assert "fetch:fetch" not in out


def test_search_matches_tool_name(ledger, capsys):
    _seed()
    cli._cmd_activity(["--search", "slow"], "auto")
    out = capsys.readouterr().out
    assert "slow:op" in out


def test_search_is_case_insensitive(ledger, capsys):
    _seed()
    cli._cmd_activity(["--search", "METHOD_NOT_FOUND"], "auto")
    assert "fetch:boom" in capsys.readouterr().out


def test_search_no_match(ledger, capsys):
    _seed()
    cli._cmd_activity(["--search", "zzzznope"], "auto")
    assert "No routed calls matching" in capsys.readouterr().out


def test_tool_and_search_compose(ledger, capsys):
    _seed()
    # tool=fetch:fetch AND search=20 -> only the 20ms line (tool matches, err no)
    cli._cmd_activity(["--tool", "fetch:fetch", "--search", "fetch"], "auto")
    out = capsys.readouterr().out
    assert "fetch:fetch" in out
    assert "slow:op" not in out


# ─── 4. p50 / p90 ───────────────────────────────────────────────────────────

def test_percentile_nearest_rank():
    assert usage._percentile([], 50) is None
    assert usage._percentile([5], 50) == 5
    assert usage._percentile([10, 20, 30, 40], 50) == 20
    assert usage._percentile([10, 20, 30, 40], 90) == 40
    assert usage._percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == 9


def test_summary_p50_p90_from_window(ledger):
    _seed()  # ms values: 20, 30, 8000, 40
    s = usage.activity_summary()
    # sorted: 20,30,40,8000 -> p50 rank ceil(.5*4)=2 -> 30; p90 rank ceil(.9*4)=4 -> 8000
    assert s["p50_ms"] == 30
    assert s["p90_ms"] == 8000


def test_summary_p50_p90_none_when_untimed(ledger):
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)  # no ms
    s = usage.activity_summary()
    assert s["p50_ms"] is None and s["p90_ms"] is None


def test_summary_prints_p50_p90(ledger, capsys):
    _seed()
    cli._cmd_activity(["--summary"], "auto")
    out = capsys.readouterr().out
    assert "p50" in out and "p90" in out
    assert "30 ms" in out and "8,000 ms" in out
