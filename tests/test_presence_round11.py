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

"""Delivery modes, round 11: which mode is running, and the visibility badge.

The question these pin: "I installed mcptoon and I cannot tell whether it is
doing anything, or saving anything." mcptoon has three delivery modes with very
different token bills (cli = cheapest, serve = small fixed, direct = most
expensive). These tests fix the classification, the statusline budget, the
report/status wiring, and the footer default.
"""

from __future__ import annotations

import json

import pytest

from mcptoon import cli, mode, usage


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Isolate every on-disk surface these tests touch (no real state)."""
    path = tmp_path / "activity.jsonl"
    monkeypatch.setenv("MCPTOON_ACTIVITY_FILE", str(path))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(usage, "_HOST", None, raising=False)
    monkeypatch.setattr(usage, "_SESSION_CALLS", 0, raising=False)
    monkeypatch.setattr(usage, "_USAGE_FILE", tmp_path / "usage.json", raising=False)
    return path


def _patch_mode(monkeypatch, *, servers=7, wired=False, routed=0, native=0):
    """Force mode.detect() inputs without touching real configs/ledger."""
    monkeypatch.setattr(mode, "_gateway_registered", lambda: wired)
    monkeypatch.setattr(mode, "detect", lambda: {
        "mode": ("empty" if servers == 0 else
                 "serve" if wired else
                 "cli" if (routed or native) else "direct"),
        "gateway_registered": wired,
        "servers": servers,
        "calls": routed + native,
        "routed": routed,
        "native": native,
        "by_host": {},
    })


# ─── 1. classification ──────────────────────────────────────────────────────

def test_mode_empty_when_no_servers(monkeypatch):
    monkeypatch.setattr(mode, "_gateway_registered", lambda: False)
    monkeypatch.setattr("mcptoon.config.list_servers", lambda: [])
    assert mode.detect()["mode"] == "empty"


def test_mode_serve_when_gateway_wired(monkeypatch):
    monkeypatch.setattr("mcptoon.config.list_servers", lambda: ["a", "b"])
    monkeypatch.setattr(mode, "_gateway_registered", lambda: True)
    # A wired gateway with nothing left to take over is plain `serve`; force an
    # empty takeover plan so the test does not read this machine's real configs.
    monkeypatch.setattr("mcptoon.sync.takeover_plan", lambda *a, **k: [])
    assert mode.detect()["mode"] == "serve"


def test_mode_serve_alongside_when_gateway_wired_but_entries_remain(monkeypatch):
    # Gateway registered, but host configs still list upstream entries it manages:
    # those hosts keep loading every schema natively, so this is NOT `serve`.
    monkeypatch.setattr("mcptoon.config.list_servers", lambda: ["a", "b"])
    monkeypatch.setattr(mode, "_gateway_registered", lambda: True)
    monkeypatch.setattr("mcptoon.sync.takeover_plan", lambda *a, **k: [
        {"agent": "cursor", "agent_name": "Cursor", "path": "p", "servers": ["a", "b"]},
        {"agent": "cline", "agent_name": "Cline", "path": "q", "servers": ["a"]},
    ])
    d = mode.detect()
    assert d["mode"] == "serve-alongside"
    assert d["alongside"] is True
    assert d["direct_entries"] == 3


def test_mode_cli_when_routed_calls_exist(monkeypatch):
    monkeypatch.setattr("mcptoon.config.list_servers", lambda: ["a"])
    monkeypatch.setattr(mode, "_gateway_registered", lambda: False)
    monkeypatch.setattr(usage, "get_usage_stats",
                        lambda: {"total_calls": 5, "routed_calls": 5,
                                 "native_calls": 0, "by_host": {"CLI": 5}})
    assert mode.detect()["mode"] == "cli"


def test_mode_direct_when_installed_but_unused(monkeypatch):
    monkeypatch.setattr("mcptoon.config.list_servers", lambda: ["a"])
    monkeypatch.setattr(mode, "_gateway_registered", lambda: False)
    monkeypatch.setattr(usage, "get_usage_stats",
                        lambda: {"total_calls": 0, "routed_calls": 0,
                                 "native_calls": 0, "by_host": {}})
    assert mode.detect()["mode"] == "direct"


# ─── 2. statusline budget + content ─────────────────────────────────────────

def test_statusline_is_one_short_line(monkeypatch):
    _patch_mode(monkeypatch, servers=7, routed=2903)
    monkeypatch.setattr(mode, "_figures", lambda: {
        "gateway_tokens": 2499, "tokens_full": 640834, "tools": 209,
        "calls_recorded": 2903})
    monkeypatch.setattr(mode, "_exposed", lambda: 9)
    line = mode.statusline(lng="en")  # pin language: the badge now follows resolve_lang
    assert "\n" not in line
    assert len(line) <= 80, f"statusline is {len(line)} cols: {line!r}"
    assert line.startswith("🎉 mcptoon")


def test_statusline_names_the_mode(monkeypatch):
    _patch_mode(monkeypatch, servers=7, wired=True)
    monkeypatch.setattr(mode, "_figures", lambda: {})
    monkeypatch.setattr(mode, "_exposed", lambda: 9)
    assert "serve" in mode.statusline(lng="en")


def test_statusline_marks_direct_loudly(monkeypatch):
    # The expensive state must be visibly flagged, not silently equal to the rest.
    _patch_mode(monkeypatch, servers=7, wired=False, routed=0, native=0)
    monkeypatch.setattr(mode, "_figures", lambda: {})
    monkeypatch.setattr(mode, "_exposed", lambda: 9)
    assert "direct→sync" in mode.statusline(lng="en")


def test_statusline_alongside_says_no_saving_not_99(monkeypatch):
    # Gateway wired but direct entries remain: the badge must NOT credit a gateway
    # saving the machine is not living. It must say "no saving" and name takeover.
    monkeypatch.setattr(mode, "detect", lambda: {
        "mode": "serve-alongside", "gateway_registered": True, "alongside": True,
        "direct_entries": 48, "servers": 7, "calls": 3119, "routed": 3111,
        "native": 8, "by_host": {}})
    monkeypatch.setattr(mode, "_figures", lambda: {
        "gateway_tokens": 2499, "tokens_full": 640606, "tools": 209,
        "calls_recorded": 3119})
    monkeypatch.setattr(mode, "_exposed", lambda: 9)
    en = mode.statusline(lng="en")
    zh = mode.statusline(lng="zh")
    assert "99%" not in en and "省 99%" not in zh
    assert "saved" not in en and "≈省" not in zh
    assert "no saving" in en and "未省" in zh
    assert "serve→takeover" in en and "serve→接管" in zh
    assert len(en) <= 80, f"statusline is {len(en)} cols: {en!r}"


# ─── 3. explain names CLI as cheapest and states the honest headline ─────────

def test_explain_says_cli_is_cheapest(monkeypatch):
    _patch_mode(monkeypatch, servers=7, routed=10)
    monkeypatch.setattr(mode, "_figures", lambda: {
        "gateway_tokens": 2499, "tokens_full": 640834, "tools": 209})
    monkeypatch.setattr(mode, "_exposed", lambda: 9)
    out = mode.explain(lng="zh")
    assert "CLI" in out and "最省" in out
    # The honest boundary: result compression is a rounding error, not the reason.
    assert "0.1" in out and "0.5" in out


def test_explain_flags_direct_mode(monkeypatch):
    _patch_mode(monkeypatch, servers=7, wired=False, routed=0, native=0)
    monkeypatch.setattr(mode, "_figures", lambda: {})
    monkeypatch.setattr(mode, "_exposed", lambda: 9)
    out = mode.explain(lng="zh")
    assert "直连" in out and "没在帮你省" in out


# ─── 4. CLI wiring: mode / statusline commands ──────────────────────────────

def test_cmd_mode_prints_explanation(monkeypatch, capsys):
    _patch_mode(monkeypatch, servers=7, routed=5)
    monkeypatch.setattr(mode, "_figures", lambda: {})
    monkeypatch.setattr(mode, "_exposed", lambda: 9)
    monkeypatch.setattr("mcptoon.config.resolve_lang", lambda: "zh")
    mode.run([], "auto")
    out = capsys.readouterr().out
    assert "三种交付模式" in out and "CLI" in out


def test_cmd_mode_json_has_mode_field(monkeypatch, capsys):
    _patch_mode(monkeypatch, servers=7, routed=5)
    monkeypatch.setattr(mode, "_figures", lambda: {"tools": 209})
    monkeypatch.setattr(mode, "_exposed", lambda: 9)
    mode.run([], "json")
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "cli"
    assert payload["statusline"].startswith("🎉 mcptoon")


def test_cmd_statusline_plain(monkeypatch, capsys):
    _patch_mode(monkeypatch, servers=7, routed=5)
    monkeypatch.setattr(mode, "_figures", lambda: {})
    monkeypatch.setattr(mode, "_exposed", lambda: 9)
    mode.run_statusline([], "auto")
    out = capsys.readouterr().out.strip()
    assert out.startswith("🎉 mcptoon") and "\n" not in out


def test_cmd_statusline_json(monkeypatch, capsys):
    _patch_mode(monkeypatch, servers=7, wired=True)
    monkeypatch.setattr(mode, "_figures", lambda: {})
    monkeypatch.setattr(mode, "_exposed", lambda: 9)
    mode.run_statusline([], "json")
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "serve"
    assert payload["statusline"].startswith("🎉 mcptoon")


# ─── 5. status shows the mode row ───────────────────────────────────────────

def test_status_shows_mode_row(monkeypatch, capsys, ledger):
    _patch_mode(monkeypatch, servers=7, routed=5)
    monkeypatch.setattr("mcptoon.config.list_servers", lambda: ["a"] * 7)
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: False)
    cli._cmd_status([], "auto")
    out = capsys.readouterr().out
    assert "Mode" in out and "CLI" in out


# ─── 6. report carries the mode section ─────────────────────────────────────

def test_report_has_mode_section(monkeypatch):
    from mcptoon import report
    _patch_mode(monkeypatch, servers=7, routed=5)
    data = report.build(query="x", k=5)
    assert data["mode"]["current"] == "cli"
    assert data["mode"]["cheapest"] == "cli"
    text = report.render(data, lng="zh")
    assert "交付模式" in text
