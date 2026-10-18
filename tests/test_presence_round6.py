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

"""Presence, round 4: export the ledger, name why a call failed, split the count.

Three things a person auditing or debugging wants that the tail view did not give:

1. `activity --export json|csv` — the whole ledger, not the last 20, in a form a
   spreadsheet or `jq` can take.
2. The ledger records *why* a call failed (`err`), and `activity` prints it, so a
   red line is not just red.
3. `status` splits "calls recorded" into routed-to-tools vs mcptoon's own tools
   (skill distribution), because they are two different things.
"""

from __future__ import annotations

import csv
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
    # usage.json is a module-level constant computed at import; isolate it too so
    # the routed/native split is deterministic regardless of other tests' writes.
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


# ─── 1. export ──────────────────────────────────────────────────────────────

def _seed(n=3):
    usage.set_host("Cursor")
    for i in range(n):
        usage.track_call("fetch", "fetch", ok=True, tokens_est=10 + i)


def test_export_json_has_every_entry(ledger, capsys):
    _seed(5)
    cli._cmd_activity(["--export", "json"], "auto")
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 5
    assert len(payload["entries"]) == 5


def test_export_json_is_chronological(ledger, capsys):
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    usage.track_call("fetch", "fetch", ok=True, tokens_est=2)
    cli._cmd_activity(["--export=json"], "auto")
    entries = json.loads(capsys.readouterr().out)["entries"]
    assert entries[0]["tokens"] <= entries[-1]["tokens"]  # oldest first


def test_export_csv_has_a_header_and_rows(ledger, capsys):
    _seed(2)
    cli._cmd_activity(["--export", "csv"], "auto")
    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    assert rows[0][:6] == ["ts", "iso", "host", "server", "tool", "ok"]
    assert len(rows) == 3  # header + 2


def test_export_bare_defaults_to_json(ledger, capsys):
    _seed(1)
    cli._cmd_activity(["--export"], "auto")
    out = capsys.readouterr().out
    assert out.lstrip().startswith("{")
    json.loads(out)  # parses


def test_export_respects_host_filter(ledger, capsys):
    usage.set_host("Cursor")
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    usage.set_host("Claude Desktop")
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    cli._cmd_activity(["--export", "json", "--host", "Cursor"], "auto")
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["entries"][0]["host"] == "Cursor"


# ─── 2. err is recorded and shown ───────────────────────────────────────────

def test_track_call_records_err_only_on_failure(ledger):
    usage.track_call("fetch", "fetch", ok=False, tokens_est=0,
                     err="METHOD_NOT_FOUND: nope")
    assert usage.read_activity(5)[0]["err"] == "METHOD_NOT_FOUND: nope"
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1, err="ignored")
    assert "err" not in usage.read_activity(5)[0]


def test_err_is_capped(ledger):
    usage.track_call("fetch", "fetch", ok=False, tokens_est=0, err="x" * 900)
    assert len(usage.read_activity(5)[0]["err"]) == 200


def test_activity_prints_the_error_reason(ledger, capsys):
    usage.track_call("fetch", "fetch", ok=False, tokens_est=0, err="CALL_ERROR: boom")
    cli._cmd_activity([], "auto")
    out = capsys.readouterr().out
    assert "✗ CALL_ERROR: boom" in out


def test_native_error_records_the_message(ledger):
    # A native tool that errors should carry its message into err.
    b = _bridge()
    payload = {"content": [{"type": "text", "text": "Error: bad argument"}],
               "isError": True}
    b._track_native("mcptoon_inspect", payload)
    entry = usage.read_activity(5)[0]
    assert entry["ok"] is False
    assert "bad argument" in entry["err"]


# ─── 3. status splits routed vs native ──────────────────────────────────────

def test_usage_stats_split_routed_and_native(ledger):
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    usage.track_call("mcptoon", "mcptoon_resolve_skills", ok=True, tokens_est=1)
    stats = usage.get_usage_stats()
    assert stats["native_calls"] == 1
    assert stats["routed_calls"] == 1
    assert stats["total_calls"] == 2


def test_status_prints_the_split(monkeypatch, capsys):
    monkeypatch.setattr(cli.cfg, "has_config_file", lambda: True)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: True)
    monkeypatch.setattr(usage, "activity_summary",
                        lambda: {"entries": 3, "last_host": "Cursor", "last_ts": None,
                                 "today_calls": 3, "hosts": {"Cursor": 3}})
    monkeypatch.setattr(usage, "get_usage_stats",
                        lambda: {"total_calls": 3, "success_rate": "3/3",
                                 "total_tokens_est": 30, "by_server": {"fetch": 2},
                                 "top_tools": {"fetch:fetch": 2},
                                 "by_host": {"Cursor": 3},
                                 "routed_calls": 2, "native_calls": 1})
    out = io.StringIO()
    with redirect_stdout(out):
        cli._cmd_status([], "auto")
    text = out.getvalue()
    assert "2 routed to tools" in text
    assert "1 via mcptoon's own tools" in text


def test_status_json_has_the_split(monkeypatch, capsys):
    monkeypatch.setattr(cli.cfg, "has_config_file", lambda: True)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: True)
    monkeypatch.setattr(usage, "activity_summary",
                        lambda: {"entries": 0, "last_host": None, "last_ts": None,
                                 "today_calls": 0, "hosts": {}})
    monkeypatch.setattr(usage, "get_usage_stats",
                        lambda: {"total_calls": 0, "success_rate": "0/0",
                                 "total_tokens_est": 0, "by_server": {},
                                 "top_tools": {}, "by_host": {},
                                 "routed_calls": 0, "native_calls": 0})
    out = io.StringIO()
    with redirect_stdout(out):
        cli._cmd_status([], "json")
    payload = json.loads(out.getvalue())
    assert payload["calls_routed"] == 0
    assert payload["calls_native"] == 0
