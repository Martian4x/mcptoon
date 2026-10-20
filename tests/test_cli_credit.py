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

"""CLI-mode credit: the ten surfaces that make a CLI user's saving visible.

The complaint this module pins: on a CLI machine mcptoon saves a lot but *shows
none of the credit*, because every surface quoted the serve "209→9 via the
gateway" story — a fact a CLI user is not living. These tests lock the CLI
caliber ("0 resident, the catalog never loads") onto each surface, and lock the
two invariants that keep the change honest:

1. the CLI figure is a *second* caliber, never a relabel of the gateway figure;
2. nothing corrupts a machine channel (stdout stays clean under `--json`).
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import pytest

from mcptoon import cli, config as cfg, footer as footer_mod, mode, report, savings


_FACTS = {
    "servers": 7,
    "tools": 209,
    "tokens_full": 640606,
    "tokens_slim": 56504,
    "name_index_tokens": 906,
    "tokens_saved": 584102,
    "savings_pct": 91.2,
    "gateway_tokens": 2499,
    "gateway_pct": 99.6,
    "gateway_saved": 638107,
    "token_caliber": "tiktoken cl100k_base",
    "calls_recorded": 2928,
    "cache_age_seconds": 10.0,
    "servers_uncached": 0,
    "cache_ttl_seconds": 300,
    "skills": 472,
    "skills_tokens_full": 1528934,
    "skills_pointer_tokens": 39,
    "note": None,
}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Redirect every user path so the suite never touches the real machine."""
    monkeypatch.setenv("MCPTOON_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MCPTOON_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MCPTOON_FOOTER_STATE_FILE", str(tmp_path / "footer-state.json"))
    monkeypatch.setenv("MCPTOON_CLI_AHA_FILE", str(tmp_path / "cli-aha"))
    monkeypatch.setenv("MCPTOON_ACTIVITY_FILE", str(tmp_path / "activity.jsonl"))
    monkeypatch.setenv("MCPTOON_LANG", "en")
    monkeypatch.delenv("MCPTOON_SERVERS", raising=False)
    return tmp_path


def _run(argv, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    kw = {}
    if stdin is not None:
        kw["new"] = io.StringIO(stdin)
        import sys as _sys
        with patch.object(_sys, "stdin", kw["new"]):
            pass
    with patch.object(cli.sys, "argv", ["mcptoon", *argv]), \
            redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return out.getvalue(), err.getvalue(), code


# ── the shared helpers: one caliber, honestly labelled ───────────────────────

def test_current_mode_reads_the_classifier():
    with patch.object(mode, "detect", return_value={"mode": "cli"}):
        assert footer_mod.current_mode() == "cli"


def test_cli_credit_is_none_off_the_cli_path():
    """A serve machine must not be handed the CLI figure."""
    with patch.object(footer_mod, "current_mode", return_value="serve"):
        assert footer_mod.cli_credit(_FACTS) is None


def test_cli_credit_reports_resident_and_per_call():
    with patch.object(footer_mod, "current_mode", return_value="cli"):
        c = footer_mod.cli_credit(_FACTS)
    assert c["resident_avoided"] == 640606
    assert c["per_call"] == 906


def test_cli_credit_line_names_the_absolute_not_a_rate():
    with patch.object(footer_mod, "current_mode", return_value="cli"):
        zh = footer_mod.cli_credit_line(_FACTS, "zh")
        en = footer_mod.cli_credit_line(_FACTS, "en")
    assert "0 常驻" in zh and "640,606" in zh
    assert "0 resident" in en and "640,606" in en
    # No percentage: the CLI claim is an absolute, not the serve rate.
    assert "%" not in zh and "%" not in en


def test_tool_schema_tokens_reads_the_cache(monkeypatch):
    from mcptoon import cache as cache_mod

    monkeypatch.setattr(cache_mod, "get_cached_tools_any_age",
                        lambda s: ([{"name": "fetch", "description": "x",
                                     "inputSchema": {"type": "object"}}], 0))
    n = footer_mod.tool_schema_tokens("fetch", "fetch")
    assert n > 0
    assert footer_mod.tool_schema_tokens("fetch", "missing") == 0


# ── 1. statusline: CLI credits "0 resident", not the gateway count ───────────

def test_statusline_cli_shows_resident_not_gateway_tools():
    d = {"mode": "cli", "calls": 2928, "routed": 2928, "native": 0,
         "servers": 7, "gateway_registered": False, "by_host": {}}
    with patch.object(mode, "detect", return_value=d), \
            patch.object(mode, "_figures", return_value=_FACTS), \
            patch.object(mode, "_exposed", return_value=9), \
            patch("mcptoon.footer._cumulative_estimate",
                  return_value=(2928, 1_900_000_000)):
        zh = mode.statusline(lng="zh")
        en = mode.statusline(lng="en")
    assert "0 常驻" in zh and "640,606" in zh
    assert "0 resident" in en and "640,606" in en
    assert "9 工具" not in zh  # the serve tool-count must not lead a CLI badge


def test_statusline_serve_keeps_the_gateway_count():
    d = {"mode": "serve", "calls": 10, "routed": 10, "native": 0,
         "servers": 7, "gateway_registered": True, "by_host": {}}
    with patch.object(mode, "detect", return_value=d), \
            patch.object(mode, "_figures", return_value=_FACTS), \
            patch.object(mode, "_exposed", return_value=9), \
            patch("mcptoon.footer._cumulative_estimate", return_value=(10, 1000)):
        zh = mode.statusline(lng="zh")
    assert "9 工具" in zh


# ── 2 + 3. call receipt: human line on stderr, machine receipt under --json ──

def _receipt_patches():
    return [
        patch.object(footer_mod, "current_mode", return_value="cli"),
        patch.object(footer_mod, "facts", return_value=_FACTS),
        patch.object(footer_mod, "tool_schema_tokens", return_value=288),
        patch.object(footer_mod, "lang", return_value="en"),
    ]


def test_receipt_data_carries_the_numbers():
    ps = _receipt_patches()
    for p in ps:
        p.start()
    try:
        data = cli._cli_receipt_data("fetch", "fetch", {"content": []})
    finally:
        for p in ps:
            p.stop()
    assert data == {"mode": "cli", "resident_avoided": 640606,
                    "schema_tokens": 288, "caliber": "tiktoken cl100k_base"}


def test_receipt_data_is_none_on_failure():
    with patch.object(footer_mod, "current_mode", return_value="cli"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        assert cli._cli_receipt_data("fetch", "fetch",
                                     {"error": True, "message": "boom"}) is None
        assert cli._cli_receipt_data("fetch", "fetch", {"isError": True}) is None


def test_receipt_data_is_none_off_cli():
    with patch.object(footer_mod, "current_mode", return_value="serve"):
        assert cli._cli_receipt_data("fetch", "fetch", {"content": []}) is None


def test_emit_receipt_human_goes_to_stderr():
    ps = _receipt_patches()
    for p in ps:
        p.start()
    try:
        with patch.object(cfg, "receipt_enabled", return_value=True), \
                patch.object(cfg, "cli_aha_seen", return_value=True):
            err = io.StringIO()
            with redirect_stderr(err):
                cli._emit_cli_receipt("fetch", "fetch", {"content": []})
    finally:
        for p in ps:
            p.stop()
    text = err.getvalue()
    assert "pulled 1 schema" in text and "640,606" in text


def test_emit_receipt_machine_is_json_on_stderr():
    ps = _receipt_patches()
    for p in ps:
        p.start()
    try:
        with patch.object(cfg, "receipt_enabled", return_value=True):
            err = io.StringIO()
            with redirect_stderr(err):
                cli._emit_cli_receipt("fetch", "fetch", {"content": []},
                                      machine=True)
    finally:
        for p in ps:
            p.stop()
    payload = json.loads(err.getvalue().strip())
    assert payload["_mcptoon_receipt"]["resident_avoided"] == 640606
    assert payload["_mcptoon_receipt"]["schema_tokens"] == 288


def test_receipt_off_switch_silences_it():
    ps = _receipt_patches()
    for p in ps:
        p.start()
    try:
        with patch.object(cfg, "receipt_enabled", return_value=False):
            err = io.StringIO()
            with redirect_stderr(err):
                cli._emit_cli_receipt("fetch", "fetch", {"content": []})
    finally:
        for p in ps:
            p.stop()
    assert err.getvalue() == ""


# ── 8. first-call aha: exactly once, then a marker ──────────────────────────

def test_first_call_aha_fires_once_then_stops(isolated):
    ps = _receipt_patches()
    for p in ps:
        p.start()
    try:
        with patch.object(cfg, "receipt_enabled", return_value=True):
            err1 = io.StringIO()
            with redirect_stderr(err1):
                cli._emit_cli_receipt("fetch", "fetch", {"content": []})
            assert "First CLI call" in err1.getvalue()
            assert cfg.cli_aha_seen() is True
            err2 = io.StringIO()
            with redirect_stderr(err2):
                cli._emit_cli_receipt("fetch", "fetch", {"content": []})
            assert "First CLI call" not in err2.getvalue()
    finally:
        for p in ps:
            p.stop()


# ── end-to-end: `call` actually emits the receipt via main() ─────────────────
# These drive the real `_cmd_call` wiring (not just the helper), so a regression
# in where the receipt is hooked into `_cmd_call` is caught.

def test_call_emits_human_receipt_on_stderr(isolated, monkeypatch):
    monkeypatch.setenv("MCPTOON_SERVERS",
                       json.dumps({"servers": {"srv": {"command": "true", "args": []}}}))
    res = {"content": [{"type": "text", "text": "ok"}]}
    with patch.object(cli, "_maybe_welcome", lambda *a, **k: None), \
            patch.object(footer_mod, "current_mode", return_value="cli"), \
            patch.object(footer_mod, "facts", return_value=_FACTS), \
            patch.object(footer_mod, "tool_schema_tokens", return_value=288), \
            patch.object(cfg, "receipt_enabled", return_value=True), \
            patch.object(cfg, "cli_aha_seen", return_value=True), \
            patch.object(cli, "call_tool", return_value=res):
        out, err, code = _run(["call", "srv", "tool", "{}"])
    assert code == 0
    assert "pulled 1 schema" in err   # the CLI receipt line lands on stderr
    assert "pulled 1 schema" not in out  # and never on the tool-result stdout


def test_call_json_keeps_stdout_clean_and_receipt_on_stderr(isolated, monkeypatch):
    monkeypatch.setenv("MCPTOON_SERVERS",
                       json.dumps({"servers": {"srv": {"command": "true", "args": []}}}))
    res = {"content": [{"type": "text", "text": "ok"}]}
    with patch.object(cli, "_maybe_welcome", lambda *a, **k: None), \
            patch.object(footer_mod, "current_mode", return_value="cli"), \
            patch.object(footer_mod, "facts", return_value=_FACTS), \
            patch.object(footer_mod, "tool_schema_tokens", return_value=288), \
            patch.object(cfg, "receipt_enabled", return_value=True), \
            patch.object(cfg, "cli_aha_seen", return_value=True), \
            patch.object(cli, "call_tool", return_value=res):
        out, err, code = _run(["call", "srv", "tool", "{}", "--json"])
    assert code == 0
    json.loads(out)                    # stdout must stay parseable JSON
    # The machine receipt is a single JSON object line on stderr (before the
    # ambient footer block, which may follow it on the same stream).
    first = err.strip().splitlines()[0]
    payload = json.loads(first)
    assert payload["_mcptoon_receipt"]["resident_avoided"] == 640606


# ── 4. status: the Tool definitions row speaks the CLI caliber ──────────────

def test_status_tool_definitions_is_cli_caliber(isolated, monkeypatch):
    monkeypatch.setenv("MCPTOON_LANG", "en")
    tool = {"name": "t1", "description": "d",
            "inputSchema": {"type": "object", "properties": {}}}
    with patch.object(cli.manifest_mod, "get_manifest", return_value={"srv": [tool]}), \
            patch.object(cli, "_gateway_wired_in", return_value=False), \
            patch.object(footer_mod, "gateway_savings", return_value=(2499, 99.6)), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        out, _err, _code = _run(["status"])
    assert "0 resident" in out
    assert "via the gateway" not in out


# ── 6. footer-facts: appends the CLI credit line ────────────────────────────

def test_footer_facts_appends_cli_credit(isolated):
    # Hermetic: mock facts() so the shared token memo is neither read nor written.
    with patch.object(footer_mod, "current_mode", return_value="cli"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        out, _err, _code = _run(["footer-facts"])
    assert "0 resident" in out
    assert "mcptoon:" in out  # the shared block still leads


def test_footer_facts_off_cli_has_no_credit_line(isolated):
    with patch.object(footer_mod, "current_mode", return_value="serve"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        out, _err, _code = _run(["footer-facts"])
    assert "0 resident" not in out


# ── 7. mode explain: names the user's own saving ────────────────────────────

def test_mode_explain_names_the_users_own_saving():
    d = {"mode": "cli", "calls": 2928, "routed": 2928, "native": 0,
         "servers": 7, "gateway_registered": False, "by_host": {}}
    with patch.object(mode, "detect", return_value=d), \
            patch.object(mode, "_figures", return_value=_FACTS), \
            patch.object(mode, "_exposed", return_value=9):
        zh = mode.explain("zh")
    assert "2,928" in zh and "640,606" in zh
    assert "就你本人而言" in zh


# ── 9. activity: a CLI credit footer under the list ─────────────────────────

def test_activity_shows_cli_credit_footer(isolated):
    ledger = isolated / "activity.jsonl"
    ledger.write_text(json.dumps({
        "ts": 1_700_000_000.0, "host": "CLI", "server": "fetch", "tool": "fetch",
        "ok": True, "tokens": 100}) + "\n", encoding="utf-8")
    with patch.object(footer_mod, "current_mode", return_value="cli"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        out, _err, _code = _run(["activity"])
    assert "640,606" in out
    assert "CLI mode" in out or "CLI 模式" in out


# ── 5. report: ⑤ splits CLI-avoided from gateway-equivalent ─────────────────

def test_report_cumulative_splits_the_two_calibers(isolated):
    # Hermetic: mock facts() so this never touches (or pollutes) the shared token
    # memo the footer's own cache tests depend on.
    with patch.object(footer_mod, "current_mode", return_value="cli"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        data = report.build()
    assert data["cli_credit"]["resident_avoided"] == _FACTS["tokens_full"]
    assert "gateway_equiv" in data["cumulative"]
    text = report.render(data, lng="en")
    assert "gateway-equivalent" in text
    assert "CLI: full catalog never resident" in text


# ── 10. savings: labels the headline as gateway-equivalent, names CLI truth ──

def test_savings_render_labels_the_cli_caliber():
    data = {
        "caliber": "tiktoken cl100k_base",
        "total_tokens": 2_500_000_000,
        "calls": 4000,
        "milestones_hit": savings.milestones(2_500_000_000),
        "next": savings.next_milestone(2_500_000_000),
        "first_call_ts": None,
        "cli_credit": {"mode": "cli", "resident_avoided": 640606, "per_call": 906},
    }
    en = savings.render(data, lng="en")
    zh = savings.render(data, lng="zh")
    assert "640,606" in en and "serve-caliber" in en
    assert "640,606" in zh and "serve 口径" in zh


def test_savings_build_carries_cli_credit(isolated):
    # Hermetic: mock facts() so the build neither reads nor writes the shared memo.
    with patch.object(footer_mod, "current_mode", return_value="cli"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        data = savings.build()
    assert data.get("cli_credit") is not None
    assert data["cli_credit"]["resident_avoided"] == _FACTS["tokens_full"]
