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

"""The remaining CLI-credit surfaces: stats, usage, activity --summary,
inspect, and the --toon machine receipt.

Same honesty rule as tests/test_cli_credit.py: the CLI figure appears ONLY on the
CLI path, is never a relabel of the gateway number, and never corrupts a machine
channel (stdout stays pure; receipts go to stderr).
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import pytest

from mcptoon import cli, config as cfg, footer as footer_mod


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
    monkeypatch.delenv("MCPTOON_SERVERS", raising=False)
    return tmp_path


def _run(argv, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with patch.object(cli.sys, "argv", ["mcptoon", *argv]), \
            redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return out.getvalue(), err.getvalue(), code


# ── 1. stats: CLI line leads, gateway line demoted ──────────────────────────

def test_stats_leads_with_cli_caliber_and_demotes_gateway(isolated):
    tool = {"name": "t1", "description": "d", "inputSchema": {"type": "object"}}
    with patch.object(cli.manifest_mod, "get_manifest", return_value={"srv": [tool]}), \
            patch.object(footer_mod, "current_mode", return_value="cli"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        out, _err, _code = _run(["stats"])
    assert "0 resident" in out
    assert "only if you wire serve" in out
    assert "640,606" in out


def test_stats_off_cli_has_no_cli_line(isolated):
    tool = {"name": "t1", "description": "d", "inputSchema": {"type": "object"}}
    with patch.object(cli.manifest_mod, "get_manifest", return_value={"srv": [tool]}), \
            patch.object(footer_mod, "current_mode", return_value="serve"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        out, _err, _code = _run(["stats"])
    assert "0 resident" not in out


# ── 2. usage: a CLI line, only on the CLI path ──────────────────────────────

def test_usage_shows_cli_credit(isolated):
    with patch.object(footer_mod, "current_mode", return_value="cli"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        out, _err, _code = _run(["usage"])
    assert "CLI mode" in out and "640,606" in out


def test_usage_off_cli_has_no_cli_line(isolated):
    with patch.object(footer_mod, "current_mode", return_value="serve"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        out, _err, _code = _run(["usage"])
    assert "640,606" not in out


# ── 3. activity --summary: CLI footer, matching the list view ───────────────

def test_activity_summary_shows_cli_credit(isolated):
    ledger = isolated / "activity.jsonl"
    ledger.write_text(json.dumps({
        "ts": 1_700_000_000.0, "host": "CLI", "server": "fetch", "tool": "fetch",
        "ok": True, "tokens": 100}) + "\n", encoding="utf-8")
    with patch.object(footer_mod, "current_mode", return_value="cli"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        out, _err, _code = _run(["activity", "--summary"])
    assert "CLI mode" in out and "640,606" in out


def test_activity_summary_off_cli_has_no_cli_line(isolated):
    ledger = isolated / "activity.jsonl"
    ledger.write_text(json.dumps({
        "ts": 1_700_000_000.0, "host": "CLI", "server": "fetch", "tool": "fetch",
        "ok": True, "tokens": 100}) + "\n", encoding="utf-8")
    with patch.object(footer_mod, "current_mode", return_value="serve"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        out, _err, _code = _run(["activity", "--summary"])
    assert "640,606" not in out


# ── 4. inspect: names the one schema's size, CLI context ────────────────────

def test_inspect_reports_one_schema_tokens_on_cli(isolated):
    info = {"name": "fetch", "description": "d",
            "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}}}
    with patch.object(cli.manifest_mod, "inspect_tool", return_value=info), \
            patch.object(footer_mod, "current_mode", return_value="cli"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        out, err, _code = _run(["inspect", "srv", "fetch"])
    # The token note is a human hint -> stderr, and it names both the one schema
    # and the full catalog kept out.
    assert "this one schema" in err and "640,606" in err
    # stdout stays the pure schema data.
    assert "this one schema" not in out


def test_inspect_off_cli_adds_no_token_line(isolated):
    """A serve user's inspect output must be byte-unchanged: no CLI token note."""
    info = {"name": "fetch", "description": "d",
            "inputSchema": {"type": "object", "properties": {}}}
    with patch.object(cli.manifest_mod, "inspect_tool", return_value=info), \
            patch.object(footer_mod, "current_mode", return_value="serve"), \
            patch.object(footer_mod, "facts", return_value=_FACTS):
        out, err, _code = _run(["inspect", "srv", "fetch"])
    assert "this one schema" not in err
    assert "640,606" not in err


# ── 5. call --toon: machine receipt on stderr, stdout pure ──────────────────

def test_call_toon_keeps_stdout_clean_and_receipt_on_stderr(isolated, monkeypatch):
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
        out, err, code = _run(["call", "srv", "tool", "{}", "--toon"])
    assert code == 0
    # stdout must not carry the receipt object (it is a toon payload).
    assert "_mcptoon_receipt" not in out
    first = err.strip().splitlines()[0]
    payload = json.loads(first)
    assert payload["_mcptoon_receipt"]["resident_avoided"] == 640606
