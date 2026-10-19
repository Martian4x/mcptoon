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

"""Presence, round 7: rank, prune, cap, colour, and today's skill deliveries.

Five additions that turn the ledger from "a list" into "a thing you can query
and tune":

1. `activity --top [N]` ranks by host and by tool.
2. `config set activity_keep N` tunes the ledger cap (default 2000).
3. `activity --clear --before <when>` prunes only old entries.
4. `activity` colours ERR/SLOW on a real terminal, plain when piped.
5. `status` / `summary` report today's skill distributions.
"""

from __future__ import annotations

import io
import json
import time
from contextlib import redirect_stdout

import pytest

from mcptoon import cli, config as cfg, usage


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "activity.jsonl"
    monkeypatch.setenv("MCPTOON_ACTIVITY_FILE", str(path))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(usage, "_HOST", None, raising=False)
    monkeypatch.setattr(usage, "_SESSION_CALLS", 0, raising=False)
    monkeypatch.setattr(usage, "_USAGE_FILE", tmp_path / "usage.json", raising=False)
    return path


def _age(ledger, index, seconds):
    """Rewrite the ts of the index-th (0-based, oldest) ledger line."""
    lines = ledger.read_text(encoding="utf-8").splitlines()
    e = json.loads(lines[index])
    e["ts"] = time.time() - seconds
    lines[index] = json.dumps(e)
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─── 1. activity --top ──────────────────────────────────────────────────────

def test_top_ranks_by_host_and_tool(ledger, capsys):
    usage.set_host("Cursor")
    for _ in range(3):
        usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    usage.set_host("Claude Desktop")
    usage.track_call("web", "search", ok=True, tokens_est=1)
    cli._cmd_activity(["--top"], "auto")
    out = capsys.readouterr().out
    assert "By host" in out and "By tool" in out
    assert "Cursor" in out and "fetch:fetch" in out
    assert "web:search" in out


def test_top_default_is_ten_and_caps(ledger, capsys):
    # Distinct counts so ranking is unambiguous: t0 has 1 call, t14 has 15.
    for i in range(15):
        for _ in range(i + 1):
            usage.track_call("s", f"t{i}", ok=True, tokens_est=1)
    cli._cmd_activity(["--top"], "auto")
    out = capsys.readouterr().out
    # default top 10 -> the ten busiest (t14..t5) shown, the quietest (t0) not.
    assert "s:t14" in out and "s:t5" in out
    assert "s:t0 " not in out


def test_top_honours_host_filter(ledger, capsys):
    usage.set_host("Cursor")
    usage.track_call("fetch", "a", ok=True, tokens_est=1)
    usage.set_host("Other")
    usage.track_call("fetch", "b", ok=True, tokens_est=1)
    cli._cmd_activity(["--top", "5", "--host", "Cursor"], "auto")
    out = capsys.readouterr().out
    assert "Cursor" in out
    assert "Other" not in out


def test_top_json_shape(ledger, capsys):
    usage.set_host("Cursor")
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)
    cli._cmd_activity(["--top"], "json")
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["by_host"]["Cursor"] == 1
    assert payload["by_tool"]["fetch:fetch"] == 1


def test_top_empty_says_so(ledger, capsys):
    cli._cmd_activity(["--top"], "auto")
    assert "No routed calls" in capsys.readouterr().out


# ─── 2. activity_keep setting ───────────────────────────────────────────────

def test_activity_keep_default():
    assert cfg.activity_keep() == 2000


def test_activity_keep_set_and_read(ledger):
    cfg.set_setting("activity_keep", "500")
    assert cfg.activity_keep() == 500


def test_activity_keep_rejects_non_number(ledger):
    with pytest.raises(ValueError):
        cfg.set_setting("activity_keep", "abc")


def test_activity_keep_rejects_out_of_range(ledger):
    with pytest.raises(ValueError):
        cfg.set_setting("activity_keep", "10")  # below MIN
    with pytest.raises(ValueError):
        cfg.set_setting("activity_keep", "999999999")  # above MAX


def test_trim_uses_activity_keep(ledger, monkeypatch):
    # Force a tiny cap and confirm the trimmer keeps exactly that many lines.
    monkeypatch.setattr(usage, "_activity_keep", lambda: 3)
    for i in range(6):
        usage.track_call("s", f"t{i}", ok=True, tokens_est=1)
    lines = [ln for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 3
    tools = [json.loads(ln)["tool"] for ln in lines]
    assert tools == ["t3", "t4", "t5"]


# ─── 3. activity --clear --before ───────────────────────────────────────────

def test_clear_before_prunes_only_old(ledger, capsys):
    usage.track_call("s", "old1", ok=True, tokens_est=1)
    _age(ledger, 0, 30 * 86400)
    usage.track_call("s", "old2", ok=True, tokens_est=1)
    _age(ledger, 1, 30 * 86400)
    usage.track_call("s", "keep", ok=True, tokens_est=1)
    cli._cmd_activity(["--clear", "--before", "7d"], "auto")
    out = capsys.readouterr().out
    assert "Pruned 2" in out
    remaining = [e["tool"] for e in usage.read_activity(50)]
    assert remaining == ["keep"]


def test_clear_before_bad_value_exits_2(ledger, capsys):
    with pytest.raises(SystemExit) as exc:
        cli._cmd_activity(["--clear", "--before", "bogus"], "auto")
    assert exc.value.code == 2


def test_clear_before_keeps_unparseable_lines(ledger, capsys):
    usage.track_call("s", "old", ok=True, tokens_est=1)
    _age(ledger, 0, 30 * 86400)
    # append a torn line — a targeted prune must not eat it
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    cli._cmd_activity(["--clear", "--before", "7d"], "auto")
    body = ledger.read_text(encoding="utf-8")
    assert "{not json" in body


def test_clear_all_still_works(ledger, capsys):
    usage.track_call("s", "x", ok=True, tokens_est=1)
    cli._cmd_activity(["--clear"], "auto")
    assert "Cleared" in capsys.readouterr().out
    assert not ledger.exists()


# ─── 4. colour on a terminal only ───────────────────────────────────────────

def _entry(ok=True, ms=None):
    return {"ts": time.time(), "host": "Cursor", "server": "s", "tool": "t",
            "ok": ok, "tokens": 1, **({"ms": ms} if ms is not None else {})}


def test_activity_line_plain_when_piped(monkeypatch):
    monkeypatch.delenv("MCPTOON_FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in cli._activity_line(_entry(ok=False, ms=9999))


def test_activity_line_colours_err_on_terminal(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("MCPTOON_NO_COLOR", raising=False)
    monkeypatch.setenv("MCPTOON_FORCE_COLOR", "1")
    line = cli._activity_line(_entry(ok=False, ms=10))
    assert "\x1b[1;31mERR" in line


def test_activity_line_colours_slow_on_terminal(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("MCPTOON_NO_COLOR", raising=False)
    monkeypatch.setenv("MCPTOON_FORCE_COLOR", "1")
    line = cli._activity_line(_entry(ok=True, ms=cli._SLOW_CALL_MS + 1))
    assert "\x1b[1;31m" in line and "SLOW" in line


# ─── 5. today's skill distributions ─────────────────────────────────────────

def test_summary_counts_today_skill_dist(ledger):
    usage.track_call("mcptoon", "mcptoon_resolve_skills", ok=True, tokens_est=0,
                     detail="distributed: a, b")
    usage.track_call("mcptoon", "mcptoon_resolve_skills", ok=True, tokens_est=0,
                     detail="distributed: c")
    usage.track_call("fetch", "fetch", ok=True, tokens_est=1)  # not a distribution
    assert usage.activity_summary()["today_skill_dist"] == 2


def test_status_prints_skills_today(monkeypatch, capsys):
    monkeypatch.setattr(cli.cfg, "has_config_file", lambda: True)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: True)
    monkeypatch.setattr(usage, "activity_summary",
                        lambda: {"entries": 3, "last_host": "Cursor", "last_ts": 1.0,
                                 "first_ts": 1.0, "today_calls": 3,
                                 "today_skill_dist": 2, "hosts": {"Cursor": 3},
                                 "last_fail": None})
    monkeypatch.setattr(usage, "get_usage_stats",
                        lambda: {"total_calls": 3, "success_rate": "3/3",
                                 "total_tokens_est": 5, "by_server": {},
                                 "top_tools": {}, "by_host": {"Cursor": 3},
                                 "routed_calls": 3, "native_calls": 0})
    out = io.StringIO()
    with redirect_stdout(out):
        cli._cmd_status([], "auto")
    text = out.getvalue()
    assert "Skills today" in text
    assert "2 distributions" in text


def test_status_json_has_today_skills(monkeypatch, capsys):
    monkeypatch.setattr(cli.cfg, "has_config_file", lambda: True)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: True)
    monkeypatch.setattr(usage, "activity_summary",
                        lambda: {"entries": 0, "last_host": None, "last_ts": None,
                                 "first_ts": None, "today_calls": 0,
                                 "today_skill_dist": 0, "hosts": {},
                                 "last_fail": None})
    monkeypatch.setattr(usage, "get_usage_stats",
                        lambda: {"total_calls": 0, "success_rate": "0/0",
                                 "total_tokens_est": 0, "by_server": {},
                                 "top_tools": {}, "by_host": {},
                                 "routed_calls": 0, "native_calls": 0})
    out = io.StringIO()
    with redirect_stdout(out):
        cli._cmd_status([], "json")
    payload = json.loads(out.getvalue())
    assert "activity_today_skills" in payload
