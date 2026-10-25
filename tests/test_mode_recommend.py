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

"""`mcptoon mode recommend`: which mode *this* host should use, and one action.

The whole point is a decision the config cannot make on its own: whether the
host can run a shell each turn. These tests pin every branch of the decision
table, and the honesty rule that a confident recommendation is never given when
the shell capability is unknown (it must carry `needs_confirm`).
"""

from __future__ import annotations

import json
import io
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from mcptoon import cli, mode


def _detect(mode_name: str) -> dict:
    return {"mode": mode_name, "gateway_registered": mode_name == "serve",
            "servers": 7, "calls": 0, "routed": 0, "native": 0, "by_host": {}}


def _rec(mode_name: str, host: dict, lng: str = "en") -> dict:
    with patch.object(mode, "detect", return_value=_detect(mode_name)):
        return mode.recommend(lng=lng, host=host)


SHELL = {"host": "DSH", "shell": True}
GUI = {"host": None, "shell": None}


# ── the decision table, one test per branch ─────────────────────────────────

def test_already_cli_stays_cli_no_confirm():
    r = _rec("cli", SHELL)
    assert r["recommended"] == "cli"
    assert r["needs_confirm"] is False


def test_serve_with_shell_keeps_serve_and_mentions_the_tradeoff():
    r = _rec("serve", SHELL)
    assert r["recommended"] == "serve"
    # The reason must name CLI as an option *and* its cost, not just say "cli".
    assert "CLI" in r["reason"]
    assert "command" in r["reason"].lower() or "per-turn" in r["reason"].lower()


def test_serve_without_shell_keeps_serve():
    r = _rec("serve", GUI)
    assert r["recommended"] == "serve"
    assert r["needs_confirm"] is False


def test_serve_alongside_recommends_takeover():
    # Gateway wired but the direct entries are not taken over: the fix is takeover,
    # not "keep serve" — nothing is being saved yet.
    r = _rec("serve-alongside", SHELL)
    assert r["one_action"] == "mcptoon sync --takeover"
    assert r["needs_confirm"] is False
    assert "takeover" in r["reason"].lower()


def test_direct_with_shell_recommends_cli():
    r = _rec("direct", SHELL)
    assert r["recommended"] == "cli"
    assert r["needs_confirm"] is False
    assert "call" in r["one_action"]


def test_direct_without_shell_recommends_serve_and_asks_to_confirm():
    r = _rec("direct", GUI)
    # serve is the zero-config safe default; but the shell fact is unknown, so
    # the caller must confirm before telling a shell-capable host to stay on serve.
    assert r["recommended"] == "serve"
    assert r["needs_confirm"] is True
    assert "sync --self" in r["one_action"]


def test_empty_recommends_quickstart():
    r = _rec("empty", GUI)
    assert r["recommended"] == "serve"
    assert "quickstart" in r["one_action"]


# ── the honesty rule: never push CLI at an unknown/GUI host ──────────────────

def test_unknown_host_is_never_told_cli_directly():
    """With no shell signal, no branch may *recommend cli* outright."""
    for m in ("direct", "serve", "cli", "empty"):
        r = _rec(m, GUI)
        if m != "cli":  # already-cli is a fact, not a push
            assert r["recommended"] != "cli", f"{m} pushed CLI at an unknown host"


def test_needs_confirm_is_true_only_when_shell_unknown_and_it_matters():
    assert _rec("direct", GUI)["needs_confirm"] is True
    assert _rec("direct", SHELL)["needs_confirm"] is False
    assert _rec("serve", GUI)["needs_confirm"] is False
    assert _rec("cli", GUI)["needs_confirm"] is False


# ── detect_host: positive signal -> shell True; absence -> None (not False) ──

def test_detect_host_reads_a_shell_marker(monkeypatch):
    for k in ("DSH_SHELL", "CLAUDECODE", "CODEX_HOME", "TERM_PROGRAM"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DSH_SHELL", "1")
    h = mode.detect_host()
    assert h["shell"] is True and h["host"] == "DSH"


def test_detect_host_absence_is_none_not_false(monkeypatch):
    for k in ("DSH_SHELL", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_HOME",
              "CURSOR_TRACE_ID", "TERM_PROGRAM"):
        monkeypatch.delenv(k, raising=False)
    h = mode.detect_host()
    # Absence is not proof of no shell — it must be "unknown", never False.
    assert h["shell"] is None
    assert h["host"] is None


# ── the CLI surface: human + --json, and it is wired to `mode recommend` ────

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


def test_mode_recommend_json_has_the_contract_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("MCPTOON_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MCPTOON_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MCPTOON_LANG", "en")
    with patch.object(mode, "detect", return_value=_detect("direct")), \
            patch.object(mode, "detect_host", return_value=SHELL):
        out, _err, _code = _run(["mode", "recommend", "--json"])
    data = json.loads(out)
    for key in ("current", "recommended", "reason", "one_action", "needs_confirm"):
        assert key in data, f"missing contract field {key}"
    assert data["current"] == "direct"
    assert data["recommended"] == "cli"


def test_mode_recommend_human_is_pasteable(monkeypatch, tmp_path):
    monkeypatch.setenv("MCPTOON_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MCPTOON_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MCPTOON_LANG", "en")
    with patch.object(mode, "detect", return_value=_detect("serve")), \
            patch.object(mode, "detect_host", return_value=GUI):
        out, _err, _code = _run(["mode", "recommend"])
    assert "recommendation" in out.lower()
    assert "serve" in out
    assert "Next:" in out


def test_mode_without_recommend_still_shows_the_full_explainer(monkeypatch, tmp_path):
    monkeypatch.setenv("MCPTOON_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MCPTOON_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MCPTOON_LANG", "en")
    with patch.object(mode, "detect", return_value=_detect("cli")), \
            patch.object(mode, "_figures", return_value={"tools": 209}):
        out, _err, _code = _run(["mode"])
    # The plain command is unchanged: the three-mode explainer, not a recommendation.
    assert "delivery modes" in out.lower()
    assert "recommendation" not in out.lower()
