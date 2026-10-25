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

"""Presence, round 6: query the ledger by time, by failure, and see reliability.

Four convenience/visibility additions on top of the earlier presence work:

1. `activity --since <today|1h|24h|7d|YYYY-MM-DD>` narrows to a time window.
2. `activity --failures` shows only failed calls.
3. `mcptoon demo` writes a ledger line, so a brand-new user sees it recording.
4. `activity --summary` reports success rate and latency (uses the `ms` field).
"""

from __future__ import annotations

import json
import time

import pytest

from mcptoon import cli, usage


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "activity.jsonl"
    monkeypatch.setenv("MCPTOON_ACTIVITY_FILE", str(path))
    monkeypatch.setattr(usage, "_HOST", None, raising=False)
    monkeypatch.setattr(usage, "_SESSION_CALLS", 0, raising=False)
    monkeypatch.setattr(usage, "_USAGE_FILE", tmp_path / "usage.json", raising=False)
    return path


def _age_last_line(ledger, seconds):
    """Rewrite the newest ledger line's ts to `seconds` ago (to test --since)."""
    lines = ledger.read_text(encoding="utf-8").splitlines()
    e = json.loads(lines[-1])
    e["ts"] = time.time() - seconds
    lines[-1] = json.dumps(e)
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─── 1. --since ─────────────────────────────────────────────────────────────

def test_parse_since_understands_relative_and_dates():
    assert cli._parse_since("all") == 0
    assert cli._parse_since("1h") is not None
    assert cli._parse_since("24h") < cli._parse_since("1h")
    assert cli._parse_since("7d") < cli._parse_since("24h")
    assert cli._parse_since("today") is not None
    assert cli._parse_since("2026-10-18") is not None
    assert cli._parse_since("nonsense") is None
    assert cli._parse_since("") is None


def test_since_hides_old_calls(ledger, capsys):
    usage.track_call("fetch", "old", ok=True, tokens_est=1)
    _age_last_line(ledger, 3 * 86400)  # 3 days old
    usage.track_call("fetch", "recent", ok=True, tokens_est=1)
    cli._cmd_activity(["--since", "1h"], "auto")
    out = capsys.readouterr().out
    assert "recent" in out
    assert "fetch:old" not in out


def test_since_bad_value_exits_2(ledger, capsys):
    with pytest.raises(SystemExit) as exc:
        cli._cmd_activity(["--since", "bogus"], "auto")
    assert exc.value.code == 2


def test_failures_applied_before_the_head_cap(ledger, capsys):
    # The head cap must apply AFTER the --failures filter, so `--failures --head 2`
    # means "the last 2 failures", not "failures among the last 2 calls". Interleave
    # successes so the two readings actually differ: newest-first is
    # f3, ok, ok, f2, ok, ok, f1, ok. Cap-first would keep f3, ok -> 1 failure;
    # filter-first keeps f3, f2, f1 -> head 2 -> f3, f2.
    for name in ("f1", "ok", "ok", "f2", "ok", "ok", "f3", "ok"):
        ok = name == "ok"
        usage.track_call("fetch", name, ok=ok, tokens_est=1,
                         err=None if ok else "e")
    cli._cmd_activity(["--failures"], "auto", head_n=2)
    out = capsys.readouterr().out
    assert "fetch:f3" in out and "fetch:f2" in out
    assert "fetch:f1" not in out


# ─── 2. --failures ──────────────────────────────────────────────────────────

def test_failures_only_shows_red_lines(ledger, capsys):
    usage.track_call("fetch", "good", ok=True, tokens_est=1)
    usage.track_call("fetch", "bad", ok=False, tokens_est=0, err="CALL_ERROR: x")
    cli._cmd_activity(["--failures"], "auto")
    out = capsys.readouterr().out
    assert "fetch:bad" in out
    assert "fetch:good" not in out


def test_failures_empty_says_good_news(ledger, capsys):
    usage.track_call("fetch", "good", ok=True, tokens_est=1)
    cli._cmd_activity(["--failures"], "auto")
    assert "No failed calls recorded" in capsys.readouterr().out


def test_failures_with_since_combines(ledger, capsys):
    usage.track_call("fetch", "badold", ok=False, tokens_est=0, err="e")
    _age_last_line(ledger, 3 * 86400)
    usage.track_call("fetch", "badnew", ok=False, tokens_est=0, err="e")
    cli._cmd_activity(["--failures", "--since", "1h"], "auto")
    out = capsys.readouterr().out
    assert "badnew" in out
    assert "badold" not in out


def test_failures_export_honours_the_filter(ledger, capsys):
    usage.track_call("fetch", "good", ok=True, tokens_est=1)
    usage.track_call("fetch", "bad", ok=False, tokens_est=0, err="e")
    cli._cmd_activity(["--export", "json", "--failures"], "auto")
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["entries"][0]["tool"] == "bad"


# ─── 3. demo writes a ledger line ───────────────────────────────────────────

def test_demo_records_to_the_ledger(ledger, monkeypatch):
    from mcptoon import demo

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def initialize(self):
            pass

        def list_tools(self):
            return [{"name": "echo"}]

        def call_tool(self, name, args):
            return {"content": [{"type": "text", "text": "hi"}]}

        def close(self):
            pass

    monkeypatch.setattr(demo, "MCPClient", FakeClient)
    demo._demo_call("demo", ["python", "-m", "x"], "hi", quick=True)
    entries = usage.read_activity(5)
    assert entries and entries[0]["server"] == "demo"
    assert entries[0]["tool"] == "echo"


# ─── 4. summary success rate + latency ──────────────────────────────────────

def test_summary_counts_ok_fail_slow_avg(ledger):
    usage.track_call("fetch", "a", ok=True, tokens_est=1, ms=40)
    usage.track_call("fetch", "b", ok=True, tokens_est=1, ms=5000)
    usage.track_call("fetch", "c", ok=False, tokens_est=0, ms=60, err="e")
    s = usage.activity_summary()
    assert s["ok_count"] == 2
    assert s["fail_count"] == 1
    assert s["slow_count"] == 1  # only the 5000ms one
    assert s["avg_ms"] == int((40 + 5000 + 60) / 3)


def test_summary_prints_success_and_latency(ledger, capsys):
    usage.track_call("fetch", "a", ok=True, tokens_est=1, ms=100)
    usage.track_call("fetch", "b", ok=False, tokens_est=0, ms=100, err="e")
    cli._cmd_activity(["--summary"], "auto")
    out = capsys.readouterr().out
    assert "Success" in out and "1/2" in out and "50.0%" in out
    assert "Latency" in out and "avg 100 ms" in out


def test_summary_avg_ms_none_when_untimed(ledger):
    usage.track_call("fetch", "a", ok=True, tokens_est=1)  # no ms
    assert usage.activity_summary()["avg_ms"] is None
