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

"""Activity ledger + presence line: a user who installed mcptoon and forgot
can tell it is being used, by which agent, and when.

mcptoon installs silently and saves tokens invisibly, so "is it even doing
anything, and for which agent?" had no answer a person could find. This module
pins the surfaces that now answer it, and the switch that turns the noise down
without losing the signal:

1. **The activity ledger** (`usage.track_call` → `activity.jsonl`,
   `usage.read_activity` / `activity_summary`): one readable line per routed
   call — time, host, server:tool, ok, tokens. The counter in usage.json can say
   how many; it can never say which agent or when.
2. **Host identity** (`usage.set_host`): `serve` learns the client's name from
   the MCP `initialize` handshake and records it, so the ledger and `status` say
   "Cursor", not "unknown".
3. **`mcptoon activity`** and the `status` "Last used" row: the two commands a
   person actually runs to check.
4. **`presence` setting** and the `_emit_presence` stderr line: the
   routed-a-call notice, on its own switch separate from the savings `footer`.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest

from mcptoon import cli, config, usage


# ─── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Point the activity ledger at a temp file and clear process-global state.

    `usage._HOST` and `usage._SESSION_CALLS` are module globals (one process is
    one conversation); resetting them keeps tests independent, and the env var
    keeps the ledger off the real cache dir.
    """
    path = tmp_path / "activity.jsonl"
    monkeypatch.setenv("MCPTOON_ACTIVITY_FILE", str(path))
    monkeypatch.setattr(usage, "_HOST", None, raising=False)
    monkeypatch.setattr(usage, "_SESSION_CALLS", 0, raising=False)
    return path


# ─── 1. the ledger records one readable line per call ────────────────────────

def test_track_call_writes_one_ledger_line(ledger):
    usage.track_call("fetch", "fetch", ok=True, payload={"a": "x" * 200})
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "one call must write exactly one ledger line"
    entry = json.loads(lines[0])
    assert entry["server"] == "fetch" and entry["tool"] == "fetch"
    assert entry["ok"] is True
    assert entry["tokens"] > 0, "a payload must record its token cost"
    assert isinstance(entry["ts"], (int, float))


def test_track_call_records_errors_as_errors(ledger):
    usage.track_call("github", "search", ok=False, payload=None)
    entry = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert entry["ok"] is False
    assert entry["tokens"] == 0


def test_the_ledger_is_append_only_newest_last(ledger):
    usage.track_call("a", "t1", payload=None)
    usage.track_call("b", "t2", payload=None)
    entries = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()]
    assert [e["server"] for e in entries] == ["a", "b"], "append order must be kept"


def test_the_ledger_keeps_the_host_that_was_set(ledger):
    usage.set_host("Cursor")
    usage.track_call("fetch", "fetch", payload=None)
    entry = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert entry["host"] == "Cursor"


def test_host_defaults_to_none_not_a_guess(ledger):
    usage.track_call("fetch", "fetch", payload=None)
    entry = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert entry["host"] is None, "an unknown host must be null, never 'unknown'"


def test_set_host_ignores_blank_and_strips(ledger):
    usage.set_host("  ")
    assert usage.get_host() is None
    usage.set_host("  Claude Desktop  ")
    assert usage.get_host() == "Claude Desktop"


def test_track_call_never_raises_even_if_the_ledger_dir_is_broken(monkeypatch, tmp_path):
    """Usage tracking must never break the tool call it is recording."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("MCPTOON_ACTIVITY_FILE", str(blocker / "activity.jsonl"))
    usage.track_call("fetch", "fetch", payload=None)  # must not raise


# ─── 2. read_activity / activity_summary ─────────────────────────────────────

def test_read_activity_returns_newest_first(ledger):
    for s in ("a", "b", "c"):
        usage.track_call(s, "t", payload=None)
    got = usage.read_activity(limit=10)
    assert [e["server"] for e in got] == ["c", "b", "a"]


def test_read_activity_respects_the_limit(ledger):
    for i in range(5):
        usage.track_call("s", f"t{i}", payload=None)
    assert len(usage.read_activity(limit=2)) == 2


def test_read_activity_skips_a_torn_line(ledger):
    usage.track_call("a", "t", payload=None)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write("{ this is not json\n")
    usage.track_call("b", "t", payload=None)
    got = usage.read_activity(limit=10)
    assert [e["server"] for e in got] == ["b", "a"], "a torn line is skipped, not fatal"


def test_read_activity_can_filter_by_host(ledger):
    usage.set_host("Cursor")
    usage.track_call("a", "t", payload=None)
    usage.set_host("Claude")
    usage.track_call("b", "t", payload=None)
    got = usage.read_activity(limit=10, host="Cursor")
    assert [e["server"] for e in got] == ["a"]


def test_activity_summary_counts_today_and_last_host(ledger):
    usage.set_host("Cursor")
    usage.track_call("a", "t", payload=None)
    usage.track_call("b", "t", payload=None)
    s = usage.activity_summary()
    assert s["entries"] == 2
    assert s["today_calls"] == 2
    assert s["last_host"] == "Cursor"
    assert s["hosts"] == {"Cursor": 2}


def test_activity_summary_on_empty_ledger_is_zeroed(ledger):
    s = usage.activity_summary()
    assert s["entries"] == 0
    assert s["last_host"] is None
    assert s["last_ts"] is None


# ─── 3. per-process routed-call counter ──────────────────────────────────────

def test_take_session_calls_returns_and_resets(ledger):
    usage.track_call("a", "t", payload=None)
    usage.track_call("b", "t", payload=None)
    assert usage.take_session_calls() == 2
    assert usage.take_session_calls() == 0, "take must reset the per-process count"


# ─── 4. `mcptoon activity` CLI ───────────────────────────────────────────────

def _run_activity(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        cli._cmd_activity(argv, "auto", 0, 0, False)
    return out.getvalue(), err.getvalue()


def test_activity_empty_says_so_and_points_at_the_ledger(ledger):
    out, _ = _run_activity([])
    assert "No routed calls recorded yet" in out
    assert str(ledger) in out


def test_activity_lists_recent_calls_with_host(ledger):
    usage.set_host("Cursor")
    usage.track_call("fetch", "fetch", ok=True, payload={"a": "y" * 100})
    out, _ = _run_activity([])
    assert "Cursor" in out
    assert "fetch:fetch" in out


def test_activity_head_limits_the_rows(ledger):
    for i in range(6):
        usage.track_call("s", f"t{i}", payload=None)
    # `--head` is a global flag the main parser turns into head_n; call the
    # handler the way the dispatcher does.
    out = io.StringIO()
    with redirect_stdout(out):
        cli._cmd_activity([], "auto", 2, 0, False)
    assert "last 2 routed call" in out.getvalue()


def test_activity_summary_flag(ledger):
    usage.set_host("Cursor")
    usage.track_call("a", "t", payload=None)
    out, _ = _run_activity(["--summary"])
    assert "Calls recorded  : 1" in out
    assert "Last used       : Cursor" in out


def test_activity_json_is_machine_readable(ledger):
    usage.track_call("fetch", "fetch", payload={"a": 1})
    out = io.StringIO()
    with redirect_stdout(out):
        cli._cmd_activity([], "json", 0, 0, False)
    payload = json.loads(out.getvalue())
    assert payload["count"] == 1
    assert payload["entries"][0]["server"] == "fetch"


def test_activity_clear_wipes_the_ledger_only(ledger):
    usage.track_call("a", "t", payload=None)
    assert ledger.exists()
    _run_activity(["--clear"])
    assert not ledger.exists()


# ─── 5. `status` shows who used it and when ──────────────────────────────────

def _run_status(monkeypatch):
    from mcptoon import manifest as manifest_mod
    monkeypatch.setattr(manifest_mod, "get_manifest", lambda use_cache=True: {})
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: False)
    out = io.StringIO()
    with redirect_stdout(out):
        cli._cmd_status([], "auto")
    return out.getvalue()


def test_status_shows_last_used_when_the_ledger_has_entries(ledger, monkeypatch):
    usage.set_host("Cursor")
    usage.track_call("a", "t", payload=None)
    out = _run_status(monkeypatch)
    assert "Last used" in out
    assert "Cursor" in out
    assert "mcptoon activity" in out


def test_status_says_no_calls_yet_when_ledger_is_empty(ledger, monkeypatch):
    out = _run_status(monkeypatch)
    assert "no routed calls yet" in out


def test_status_json_carries_the_presence_fields(ledger, monkeypatch):
    usage.set_host("Cursor")
    usage.track_call("a", "t", payload=None)
    from mcptoon import manifest as manifest_mod
    monkeypatch.setattr(manifest_mod, "get_manifest", lambda use_cache=True: {})
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: False)
    out = io.StringIO()
    with redirect_stdout(out):
        cli._cmd_status([], "json")
    payload = json.loads(out.getvalue())
    assert payload["activity_entries"] == 1
    assert payload["last_host"] == "Cursor"
    assert "presence_enabled" in payload


# ─── 6. the presence stderr line ─────────────────────────────────────────────

def test_presence_line_prints_after_a_routed_call(ledger, monkeypatch):
    monkeypatch.setattr(config, "presence_enabled", lambda: True)
    monkeypatch.setattr(config, "presence_mode", lambda: "on")
    usage.set_host("Cursor")
    usage.track_call("a", "t", payload=None)
    err = io.StringIO()
    with redirect_stderr(err):
        cli._emit_presence("call")
    text = err.getvalue()
    assert "mcptoon routed 1 call" in text
    assert "Cursor" in text
    assert "mcptoon activity" in text


def test_presence_line_is_silent_when_no_call_happened(ledger, monkeypatch):
    monkeypatch.setattr(config, "presence_enabled", lambda: True)
    monkeypatch.setattr(config, "presence_mode", lambda: "on")
    err = io.StringIO()
    with redirect_stderr(err):
        cli._emit_presence("status")
    assert err.getvalue() == "", "a command that routed nothing must stay silent"


def test_presence_line_respects_the_off_switch(ledger, monkeypatch):
    monkeypatch.setattr(config, "presence_enabled", lambda: False)
    monkeypatch.setattr(config, "presence_mode", lambda: "off")
    usage.track_call("a", "t", payload=None)
    err = io.StringIO()
    with redirect_stderr(err):
        cli._emit_presence("call")
    assert err.getvalue() == ""


def test_presence_line_is_silent_for_protocol_commands(ledger, monkeypatch):
    monkeypatch.setattr(config, "presence_enabled", lambda: True)
    monkeypatch.setattr(config, "presence_mode", lambda: "on")
    usage.track_call("a", "t", payload=None)
    err = io.StringIO()
    with redirect_stderr(err):
        cli._emit_presence("serve")
    assert err.getvalue() == "", "serve's stderr is a protocol stream"


def test_presence_brief_mode_drops_the_host_clause(ledger, monkeypatch):
    monkeypatch.setattr(config, "presence_enabled", lambda: True)
    monkeypatch.setattr(config, "presence_mode", lambda: "brief")
    usage.set_host("Cursor")
    usage.track_call("a", "t", payload=None)
    err = io.StringIO()
    with redirect_stderr(err):
        cli._emit_presence("call")
    text = err.getvalue()
    assert "routed 1 call" in text
    assert "Cursor" not in text, "brief mode drops the host clause on purpose"


def test_presence_pluralizes_multiple_calls(ledger, monkeypatch):
    monkeypatch.setattr(config, "presence_enabled", lambda: True)
    monkeypatch.setattr(config, "presence_mode", lambda: "on")
    usage.track_call("a", "t", payload=None)
    usage.track_call("b", "t", payload=None)
    err = io.StringIO()
    with redirect_stderr(err):
        cli._emit_presence("call")
    assert "routed 2 calls" in err.getvalue()


# ─── 7. the `presence` setting ───────────────────────────────────────────────

def test_presence_defaults_on():
    assert config.SETTING_DEFAULTS["presence"] == "on"
    assert config.presence_enabled() is True


def test_presence_setting_is_validated(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    config.set_setting("presence", "brief")
    assert config.presence_mode() == "brief"
    with pytest.raises(ValueError):
        config.set_setting("presence", "maybe")


def test_presence_mode_falls_back_to_on_for_a_bad_value(tmp_path, monkeypatch):
    """A hand-edited typo must keep the notice showing, not silently vanish."""
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "get_setting",
                        lambda key: "garbage" if key == "presence" else "on")
    assert config.presence_mode() == "on"


# ─── 8. reset_usage clears the ledger too ────────────────────────────────────

def test_reset_usage_clears_the_ledger(ledger):
    usage.track_call("a", "t", payload=None)
    assert ledger.exists()
    usage.reset_usage()
    assert not ledger.exists(), "reset must clear the ledger, not only the counter"


# ─── 9. serve captures the client name ───────────────────────────────────────

def test_serve_records_the_client_name_from_initialize(ledger):
    """The MCP handshake is where a host names itself; serve must keep it."""
    from mcptoon.serve import MCPServerBridge

    bridge = MCPServerBridge.__new__(MCPServerBridge)  # avoid full init
    bridge._handle_initialize({"clientInfo": {"name": "Cursor", "version": "1.2"},
                               "protocolVersion": "2025-06-18"})
    assert usage.get_host() == "Cursor"


def test_serve_initialize_without_client_info_leaves_host_unknown(ledger):
    from mcptoon.serve import MCPServerBridge

    bridge = MCPServerBridge.__new__(MCPServerBridge)
    bridge._handle_initialize({"protocolVersion": "2025-06-18"})
    assert usage.get_host() is None
