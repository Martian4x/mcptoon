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

"""Delivery modes: which one is running, what each costs, and which is cheapest.

The confusion this module answers, stated plainly: "I installed mcptoon and I
cannot tell whether it is doing anything, or saving anything." Both halves are
true at once, and the reason is that mcptoon has **three** ways to reach an
agent, with very different token bills:

* ``direct`` — mcptoon is installed but the agent talks to its MCP servers
  itself. Every tool schema rides in context every turn. This is the *most*
  expensive state and the one a fresh install silently sits in.
* ``serve``  — ``mcptoon serve`` is registered in the agent (Claude Desktop,
  Cursor, Cline, Windsurf, VS Code, DSH via ``dsh-mcp-client``...). The agent
  loads mcptoon's ~9 gateway tools instead of every upstream schema: a small
  **fixed** cost every turn.
* ``cli``    — the agent runs ``mcptoon manifest`` / ``mcptoon call`` through a
  shell. Zero resident cost, and only the one tool it actually needs is fetched
  (a few hundred tokens) on the turns that use a tool. This is the **cheapest**
  mode when the agent has a shell and is willing to run a command.

The honest headline: the big "99%" saving is *definition-side* — it is what you
get by **not** loading all the upstream schemas. ``cli`` and ``serve`` both
deliver it; ``direct`` does not. Result-side compression is a rounding error by
comparison (measured 0.1–0.5%), so it must never be quoted as the reason to
install.

Everything here is read-only and cache-only: no server is spawned, so it is safe
to call from `status`, the footer, or a statusline that runs every prompt.
"""

from __future__ import annotations

__all__ = ["detect", "statusline", "explain", "run", "run_statusline"]

MODES = ("cli", "serve", "direct")


def _gateway_registered() -> bool:
    """True if `mcptoon serve` is a server entry in any known agent config.

    Mirrors ``cli._gateway_wired_in`` but kept local so ``mode`` has no import
    cycle with ``cli``. Best-effort: a malformed config reads as "not wired".
    """
    import json

    try:
        from .sync import (SELF_SERVER_NAME, _claude_desktop_path, _cline_path,
                           _cursor_path, _vscode_copilot_path, _windsurf_path)
    except Exception:
        return False
    paths = [*_cursor_path(), _claude_desktop_path(), _cline_path(),
             _windsurf_path(), _vscode_copilot_path()]
    for path in paths:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        for section in (data.get("mcpServers"), (data.get("mcp") or {}).get("servers")):
            if isinstance(section, dict) and SELF_SERVER_NAME in section:
                return True
    return False


def detect() -> dict:
    """Classify the current delivery mode from read-only, cache-only signals.

    Returns a dict with ``mode`` in ``{"serve", "cli", "direct", "empty"}`` plus
    the figures the explanation needs. Never raises: a broken cache or ledger
    degrades to zeros rather than breaking `status`.
    """
    try:
        from . import config as cfg
        servers = len(cfg.list_servers())
    except Exception:
        servers = 0

    try:
        from . import usage as usage_mod
        stats = usage_mod.get_usage_stats()
        calls = int(stats.get("total_calls", 0))
        routed = int(stats.get("routed_calls", calls))
        native = int(stats.get("native_calls", 0))
        by_host = dict(stats.get("by_host") or {})
    except Exception:
        calls = routed = native = 0
        by_host = {}

    wired = _gateway_registered()

    if servers == 0:
        mode = "empty"
    elif wired:
        mode = "serve"
    elif routed > 0 or native > 0:
        mode = "cli"
    else:
        mode = "direct"

    return {
        "mode": mode,
        "gateway_registered": wired,
        "servers": servers,
        "calls": calls,
        "routed": routed,
        "native": native,
        "by_host": by_host,
    }


def _figures() -> dict:
    """Catalog token figures, cache-only, or an empty dict when unavailable."""
    try:
        from . import footer as footer_mod
        return footer_mod.facts()
    except Exception:
        return {}


def _exposed() -> int | None:
    """How many gateway tools the agent sees under compact exposure, or None."""
    try:
        from . import footer as footer_mod
        return footer_mod._exposed_tool_count()
    except Exception:
        return None


def _human(n) -> str:
    """1_900_000_000 -> '1.9B', 2_903 -> '2,903' — the footer's own helper."""
    try:
        from . import footer as footer_mod
        return footer_mod._human_tokens(int(n))
    except Exception:
        try:
            return f"{int(n):,}"
        except Exception:
            return str(n)


_MODE_TAG = {"serve": "serve", "cli": "cli", "direct": "direct!",
             "empty": "no servers"}


def statusline(lng: str | None = None) -> str:
    """One compact badge line, safe to run every prompt from any host.

    Deliberately <=80 display columns and one line: it is meant to sit in a
    status bar or be echoed at the top of a turn, so a wrapping badge defeats the
    purpose. Reads only cached figures — never spawns a server — so a host may
    call it on every prompt without paying a startup cost.
    """
    d = detect()
    f = _figures()
    exposed = _exposed() if f.get("gateway_tokens") else None
    full = f.get("tokens_full") or 0
    gw = f.get("gateway_tokens")

    pct = None
    if gw and full and exposed:
        try:
            from . import footer as footer_mod
            pct = footer_mod.display_pct(full - gw, full)
        except Exception:
            pct = None

    calls = int(f.get("calls_recorded") or d["calls"] or 0)
    est = None
    if gw:
        try:
            from . import footer as footer_mod
            est = footer_mod._cumulative_estimate(f)
        except Exception:
            est = None

    head = "🎉 mcptoon"
    tool_bit = f"{exposed} tools ({pct}% less)" if (exposed and pct) else None
    saved_bit = f"≈{_human(est[1])} saved · {_human(est[0])} calls" if est else (
        f"{_human(calls)} calls" if calls else None)
    tag = _MODE_TAG.get(d["mode"], d["mode"])

    parts = [head]
    if tool_bit:
        parts.append(tool_bit)
    if saved_bit:
        parts.append(saved_bit)
    parts.append(tag)
    line = " · ".join(parts)

    # Guard the <=80 budget: drop the cumulative clause if the line is too long.
    if len(line) > 80 and saved_bit and tool_bit:
        parts = [head, tool_bit, tag]
        line = " · ".join(parts)
    return line


def _mode_zh(mode: str) -> str:
    return {
        "cli": "CLI 模式（最省 · 零常驻）",
        "serve": "serve 模式（原生集成 · 每轮小固定开销）",
        "direct": "直连模式（最贵 · 装了没接）",
        "empty": "未配置任何 MCP 服务器",
    }.get(mode, mode)


def explain(lng: str | None = None) -> str:
    """The three modes side by side, the current one, and the cheapest one.

    Chinese by default on this machine; English when ``lng == "en"``. Every
    number is read from the live catalog (cache-only) or the ledger, never
    invented; the per-mode costs are the *shape* (fixed vs per-use vs idle), with
    the live catalog size substituted where it is known.
    """
    zh = (lng or "zh") != "en"
    d = detect()
    f = _figures()
    gw = f.get("gateway_tokens")
    full = f.get("tokens_full") or 0
    exposed = _exposed() if gw else None
    tools = f.get("tools")

    L = []
    a = L.append
    if zh:
        a("mcptoon 三种交付模式 — 当前、最省、怎么选")
        a("─" * 54)
        a(f"  当前模式：{_mode_zh(d['mode'])}")
        a("")
        a("  ① CLI 模式（最省 · 零常驻）")
        a("     agent 用 shell 跑 `mcptoon manifest` / `mcptoon call`。")
        a("     闲置 0 token；用时只取它要的那一个工具（约几百 token）。")
        a("     前提：agent 有终端，且愿意敲命令。")
        a("")
        a("  ② serve 模式（零配置 · 原生集成 · 每轮小固定开销）")
        a(f"     `mcptoon serve` 注册进 agent，只暴露 {exposed or '~9'} 个网关工具。")
        if gw and full:
            a(f"     每轮固定约 {gw:,} token（对比直连全量 {full:,}）。")
        a("     前提：宿主支持 MCP（Claude Desktop / Cursor / Cline / DSH…）。")
        a("")
        a("  ③ 直连模式（最贵 · 装了没接）")
        a(f"     agent 自己加载全部 {tools or '所有'} 个工具 schema，每轮都烧。")
        a("     这是新装后最容易停留的状态，也是“感觉不到它”的根源。")
        a("")
        a("  最省的是 ①CLI：闲置 0、按需才花。")
        a("  最稳的是 ②serve：零配置、每轮只多一点点。")
        a("  两者都省“定义侧”的 99%；结果压缩只是零头（实测 0.1–0.5%）。")
        if d["mode"] == "direct":
            a("")
            a("  ⚠ 你现在是直连模式：mcptoon 没在帮你省。接 serve 或改用 CLI。")
        elif d["mode"] == "cli":
            a("")
            a("  ✓ 你现在是 CLI 模式：已经是最省，前提是 agent 真在敲命令。")
        elif d["mode"] == "serve":
            a("")
            a("  ✓ 你现在是 serve 模式：每轮小固定开销，已省定义侧大头。")
    else:
        a("mcptoon delivery modes — current, cheapest, how to choose")
        a("─" * 54)
        a(f"  Current mode: {d['mode']}")
        a("")
        a("  1) CLI (cheapest, zero resident cost)")
        a("     agent runs `mcptoon manifest` / `mcptoon call` via a shell.")
        a("     Idle 0 tokens; a use fetches only the one tool it needs.")
        a("     Requires: the agent has a terminal and will run a command.")
        a("")
        a("  2) serve (zero-config, native, small fixed per-turn cost)")
        a(f"     `mcptoon serve` registered; only {exposed or '~9'} gateway tools load.")
        if gw and full:
            a(f"     Fixed ~{gw:,} tokens/turn (vs {full:,} for full schemas).")
        a("")
        a("  3) direct (most expensive — installed but not wired)")
        a(f"     The agent loads all {tools or 'every'} schemas every turn.")
        a("     The default state after install; why it feels invisible.")
        a("")
        a("  Cheapest is 1) CLI: idle 0, pay only on use.")
        a("  Steadiest is 2) serve: zero-config, small fixed cost.")
        a("  Both save the definition-side 99%; result compression is 0.1–0.5%.")
    return "\n".join(L)


def run(rest: list[str], fmt: str = "auto", head_n: int = 0, max_chars: int = 0,
        full: bool = False) -> None:
    """CLI entry: `mcptoon mode [--json]`.

    Usage:
        mcptoon mode            Which mode you are in, what each costs, which is cheapest
        mcptoon mode --json     The same facts as JSON
    """
    from . import config as cfg
    from . import output

    if fmt == "json":
        d = detect()
        f = _figures()
        print(output.render(output.stamp_schema({
            **d,
            "tools": f.get("tools"),
            "tokens_full": f.get("tokens_full"),
            "gateway_tokens": f.get("gateway_tokens"),
            "gateway_pct": f.get("gateway_pct"),
            "statusline": statusline(),
        }), fmt="json", max_chars=max_chars))
        return
    print(explain(lng=cfg.resolve_lang()))


def run_statusline(rest: list[str], fmt: str = "auto", head_n: int = 0,
                   max_chars: int = 0, full: bool = False) -> None:
    """CLI entry: `mcptoon statusline [--json]` — one badge line.

    Usage:
        mcptoon statusline          One-line badge for a status bar / turn header
        mcptoon statusline --json   The badge plus its inputs as JSON
    """
    from . import output

    line = statusline()
    if fmt == "json":
        print(output.render(output.stamp_schema({
            "statusline": line, **detect(),
        }), fmt="json", max_chars=max_chars))
        return
    print(line)
