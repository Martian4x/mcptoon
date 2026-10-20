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

"""Delivery-mode honesty across surfaces (direct/cli/serve) + mode-recommend entry.

Pins five things the 2026-10-20 pass fixed:
  1. direct mode must NOT claim the CLI "0 resident / saves 99%" story (it saves
     nothing); statusline and status say so, and statusline drops the fake ≈saved.
  2. `mode recommend` is discoverable: in `help` and in bash/zsh/fish completion.
  3. `doctor` reports the delivery mode and warns on direct with one action.
  4. `bench` shows the CLI "0 resident" caliber row on a CLI machine only.
  5. `footer-facts --json` and `status --json` both carry the unified
     cli_credit={mode,resident_avoided,per_call,caliber}.

Rule throughout: a CLI/direct figure is never a relabel of the gateway figure,
and off the CLI path the CLI credit is absent (None / zero).
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import pytest

from mcptoon import bench, cli, config as cfg, footer as footer_mod
from mcptoon import manifest as manifest_mod, mode as mode_mod, usage as usage_mod


_FACTS = {
    "servers": 7, "tools": 209, "tokens_full": 640606, "tokens_slim": 56504,
    "name_index_tokens": 906, "tokens_saved": 584102, "savings_pct": 91.2,
    "gateway_tokens": 2499, "gateway_pct": 99.6, "gateway_saved": 638107,
    "token_caliber": "tiktoken cl100k_base", "calls_recorded": 2928,
    "cache_age_seconds": 10.0, "servers_uncached": 0, "cache_ttl_seconds": 300,
    "skills": 472, "skills_tokens_full": 1528934, "skills_pointer_tokens": 39,
    "note": None,
}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPTOON_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MCPTOON_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MCPTOON_FOOTER_STATE_FILE", str(tmp_path / "footer-state.json"))
    monkeypatch.setenv("MCPTOON_CLI_AHA_FILE", str(tmp_path / "cli-aha"))
    monkeypatch.setenv("MCPTOON_ACTIVITY_FILE", str(tmp_path / "activity.jsonl"))
    monkeypatch.setenv("MCPTOON_LANG", "en")
    return tmp_path


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


# ── 1. direct mode: statusline + status ─────────────────────────────────────

def _statusline(mode_name, lng):
    with patch.object(mode_mod, "detect", return_value={"mode": mode_name, "calls": 2928}), \
            patch.object(mode_mod, "_figures", return_value=_FACTS), \
            patch.object(mode_mod, "_exposed", return_value=9):
        return mode_mod.statusline(lng=lng)


def test_statusline_direct_never_claims_gateway_saving():
    en = _statusline("direct", "en")
    zh = _statusline("direct", "zh")
    # No fake "99% less" / "省 99%" and no fake ≈saved on a machine that saves 0.
    assert "99%" not in en and "省 99%" not in zh
    assert "saved" not in en and "≈省" not in zh
    assert "not wired" in en and "未接线" in zh


def test_statusline_cli_still_shows_zero_resident():
    en = _statusline("cli", "en")
    assert "0 resident" in en and "640,606" in en.replace(",", ",")


def test_status_direct_says_nothing_saved(isolated):
    tool = {"name": "t1", "description": "d", "inputSchema": {"type": "object"}}
    with patch.object(manifest_mod, "get_manifest", return_value={"srv": [tool]}), \
            patch.object(footer_mod, "current_mode", return_value="direct"), \
            patch.object(footer_mod, "facts", return_value=_FACTS), \
            patch.object(cli, "_gateway_wired_in", return_value=False), \
            patch.object(usage_mod, "get_usage_stats", return_value={
                "total_calls": 0, "success_rate": "", "by_server": {},
                "top_tools": {}, "routed_calls": 0, "native_calls": 0}):
        out, _err, _code = _run(["status"])
    assert "nothing is saved" in out
    assert "0 resident" not in out          # must not borrow the CLI line


def test_status_json_direct_resident_avoided_is_zero(isolated):
    tool = {"name": "t1", "description": "d", "inputSchema": {"type": "object"}}
    with patch.object(manifest_mod, "get_manifest", return_value={"srv": [tool]}), \
            patch.object(footer_mod, "current_mode", return_value="direct"), \
            patch.object(footer_mod, "facts", return_value=_FACTS), \
            patch.object(cli, "_gateway_wired_in", return_value=False), \
            patch.object(usage_mod, "get_usage_stats", return_value={
                "total_calls": 0, "success_rate": "", "by_server": {},
                "top_tools": {}, "routed_calls": 0, "native_calls": 0}):
        out, _err, _code = _run(["status", "--json"])
    data = json.loads(out)
    assert data["resident_avoided"] == 0
    assert data["delivery_mode"] == "direct"
    assert data["cli_credit"] is None


# ── 2. mode recommend is discoverable ───────────────────────────────────────

def test_help_lists_mode_recommend(isolated):
    out, _err, _code = _run(["help"])
    assert "mode recommend" in out


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_offers_recommend(isolated, shell):
    out, _err, _code = _run(["completion", shell])
    assert "recommend" in out


# ── 3. doctor reports the delivery mode ─────────────────────────────────────

def _run_doctor(tmp_path, mode_name):
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")
    buf = io.StringIO()
    code = 0
    with patch.object(cfg, "CONFIG_FILE", config_file), \
            patch.object(cfg, "CACHE_DIR", tmp_path / "cache"), \
            patch.object(cfg, "load_config", return_value={"good": {"transport": "stdio"}}), \
            patch.object(cfg, "has_config_file", return_value=True), \
            patch.object(manifest_mod, "get_server_tools",
                         side_effect=lambda name, use_cache=True: [{"name": "a"}]), \
            patch.object(mode_mod, "statusline_status",
                         return_value={"wired": True, "path": "p", "file_exists": True}), \
            patch.object(footer_mod, "current_mode", return_value=mode_name), \
            patch.object(footer_mod, "facts", return_value=_FACTS), \
            patch.dict(os.environ, {"MCPTOON_NO_STAR_HINT": "1", "CI": ""}), \
            redirect_stdout(buf):
        try:
            cli._cmd_doctor([], fmt="auto")
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return buf.getvalue(), code


def test_doctor_warns_on_direct_with_action(tmp_path):
    out, code = _run_doctor(tmp_path, "direct")
    assert "Mode: direct" in out
    assert "mcptoon sync --self" in out
    assert code == 0        # a not-wired install is a choice, not a fault


def test_doctor_reports_cli_zero_resident(tmp_path):
    out, code = _run_doctor(tmp_path, "cli")
    assert "Mode: cli" in out
    assert code == 0


# ── 4. bench shows the CLI caliber row (CLI only) ───────────────────────────

_TOOL_ROWS = [
    ("native: every full schema", 640606),
    ("manifest (name index)", 906),
    ("manifest --slim", 56504),
]


def test_bench_shows_cli_row_on_cli():
    with patch.object(footer_mod, "current_mode", return_value="cli"):
        out = bench._render("tiktoken", True, _TOOL_ROWS, 209,
                            [], 0, 0, "", "q", [])
    assert "CLI: 0 resident" in out
    assert "640,606" in out


def test_bench_off_cli_has_no_cli_row():
    with patch.object(footer_mod, "current_mode", return_value="serve"):
        out = bench._render("tiktoken", True, _TOOL_ROWS, 209,
                            [], 0, 0, "", "q", [])
    assert "CLI: 0 resident" not in out


# ── 5. footer-facts --json + status --json carry unified cli_credit ─────────

def test_footer_facts_json_has_cli_credit_on_cli(isolated):
    with patch.object(footer_mod, "facts", return_value=_FACTS), \
            patch.object(footer_mod, "current_mode", return_value="cli"):
        out, _err, _code = _run(["footer-facts", "--json"])
    data = json.loads(out)
    assert data["delivery_mode"] == "cli"
    assert data["cli_credit"] == {
        "mode": "cli", "resident_avoided": 640606,
        "per_call": 906, "caliber": "tiktoken cl100k_base"}


def test_footer_facts_json_cli_credit_none_off_cli(isolated):
    with patch.object(footer_mod, "facts", return_value=_FACTS), \
            patch.object(footer_mod, "current_mode", return_value="serve"):
        out, _err, _code = _run(["footer-facts", "--json"])
    data = json.loads(out)
    assert data["cli_credit"] is None
    assert data["delivery_mode"] == "serve"


def test_status_json_cli_credit_shape_on_cli(isolated):
    tool = {"name": "t1", "description": "d", "inputSchema": {"type": "object"}}
    with patch.object(manifest_mod, "get_manifest", return_value={"srv": [tool]}), \
            patch.object(footer_mod, "current_mode", return_value="cli"), \
            patch.object(footer_mod, "facts", return_value=_FACTS), \
            patch.object(cli, "_gateway_wired_in", return_value=False), \
            patch.object(usage_mod, "get_usage_stats", return_value={
                "total_calls": 0, "success_rate": "", "by_server": {},
                "top_tools": {}, "routed_calls": 0, "native_calls": 0}):
        out, _err, _code = _run(["status", "--json"])
    data = json.loads(out)
    # resident_avoided is status's own catalog sum (here one fake tool = 23);
    # cli_credit carries the same shape from footer.facts. Both present, both CLI.
    assert data["resident_avoided"] > 0
    assert set(data["cli_credit"]) == {"mode", "resident_avoided", "per_call", "caliber"}
    assert data["cli_credit"]["resident_avoided"] == 640606


# ── Phase 2 ─────────────────────────────────────────────────────────────────

# 6. footer.line: direct must not broadcast the gateway rate / cumulative saving.

def _footer_line(mode_name, lng):
    with patch.object(mode_mod, "detect", return_value={"mode": mode_name, "calls": 2965}):
        return footer_mod.line(dict(_FACTS), lng=lng)


def test_footer_line_direct_says_no_saving():
    en = _footer_line("direct", "en")
    zh = _footer_line("direct", "zh")
    assert "no saving" in en and "未接线·未省" in zh
    assert "saves" not in en and "≈省" not in zh   # no gateway rate/cumulative
    assert "99%" not in en and "省 99%" not in zh


def test_footer_line_cli_keeps_gateway_headline():
    # Off the direct path the line is unchanged (serve/cli keep the gateway story).
    en = _footer_line("cli", "en")
    assert "saves" in en


# 7. mode explain names the host recommendation at the top.

def test_mode_explain_shows_host_recommendation():
    with patch.object(mode_mod, "detect", return_value={"mode": "direct", "calls": 0}), \
            patch.object(mode_mod, "_figures", return_value=_FACTS), \
            patch.object(mode_mod, "_exposed", return_value=9), \
            patch.object(mode_mod, "recommend", return_value={
                "current": "direct", "recommended": "serve",
                "reason": "GUI host", "one_action": "run `mcptoon sync --self`"}):
        out = mode_mod.explain(lng="en")
    assert "For this host: serve" in out
    assert "mcptoon sync --self" in out


# 8. inspect <server> (list form) reports the set's token total on CLI.

def test_inspect_server_list_reports_total_on_cli(isolated):
    tools = [{"name": "a", "inputSchema": {"type": "object"}},
             {"name": "b", "inputSchema": {"type": "object"}}]
    with patch.object(manifest_mod, "get_server_tools", return_value=tools), \
            patch.object(footer_mod, "current_mode", return_value="cli"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        out, err, _code = _run(["inspect", "srv"])
    assert "schemas" in err and "CLI call" in err
    assert "schemas" not in out       # stdout stays pure data


def test_inspect_server_list_silent_off_cli(isolated):
    tools = [{"name": "a", "inputSchema": {"type": "object"}}]
    with patch.object(manifest_mod, "get_server_tools", return_value=tools), \
            patch.object(footer_mod, "current_mode", return_value="serve"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        _out, err, _code = _run(["inspect", "srv"])
    assert "schemas" not in err


# 9. manifest names its own name-index token count on CLI.

def test_manifest_reports_name_index_on_cli(isolated):
    manifest = {"srv": [{"name": "a"}, {"name": "b"}]}
    with patch.object(manifest_mod, "get_manifest", return_value=manifest), \
            patch.object(footer_mod, "current_mode", return_value="cli"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        out, err, _code = _run(["manifest"])
    assert "name index" in err and "906" in err
    assert "name index" not in out


def test_manifest_no_index_note_off_cli(isolated):
    manifest = {"srv": [{"name": "a"}]}
    with patch.object(manifest_mod, "get_manifest", return_value=manifest), \
            patch.object(footer_mod, "current_mode", return_value="serve"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        _out, err, _code = _run(["manifest"])
    assert "name index" not in err


# 10. bench on direct says "you save 0", not a bare 99%.

def test_bench_direct_warns_no_saving():
    with patch.object(footer_mod, "current_mode", return_value="direct"):
        out = bench._render("tiktoken", True, _TOOL_ROWS, 209, [], 0, 0, "", "q", [])
    assert "direct:" in out and "right now you save 0" in out
    assert "sync --self" in out
