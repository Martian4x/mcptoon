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

There is a fourth state, ``serve-alongside``, that looks like ``serve`` but is
not: the gateway is registered in *some* host while *other* host configs still
list the upstream servers mcptoon manages. Those hosts keep loading every full
schema natively, so the gateway saves nothing there — the machine is loading
*catalog + gateway*, i.e. slightly MORE than before install. Reporting "saves
99%" here credits mcptoon for a saving it is not delivering. The honest reading
is "wired but not taken over — run `mcptoon sync --takeover`".

Everything here is read-only and cache-only: no server is spawned, so it is safe
to call from `status`, the footer, or a statusline that runs every prompt.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

__all__ = ["detect", "statusline", "explain", "run", "run_statusline",
           "statusline_command", "install_statusline", "uninstall_statusline",
           "statusline_status", "detect_host", "recommend", "render_recommendation",
           "run_recommend"]

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

    Returns a dict with ``mode`` in
    ``{"serve", "serve-alongside", "cli", "direct", "empty"}`` plus the figures
    the explanation needs. Never raises: a broken cache or ledger degrades to
    zeros rather than breaking `status`.
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

    # `wired` alone is not the whole story. A gateway can be registered in one
    # agent while OTHER agent configs still hold the upstream server entries
    # mcptoon manages — so those hosts keep loading every full schema directly and
    # the gateway saves nothing there. `serve` claims the big definition-side
    # saving; `serve-alongside` is the state where the gateway is present but has
    # not taken those entries over, so the honest reading is "installed, gateway
    # wired, but NOT saving yet — run `mcptoon sync --takeover`". Without this
    # state the footer/status/statusline broadcast "saves 99%" on a machine that
    # is actually loading MORE than before (catalog + gateway), which is the
    # "credit points the wrong way" defect. `takeover_plan()` is read-only and
    # ~1.7 ms; it never raises (falls back to plain serve).
    alongside = False
    direct_entries = 0
    if wired:
        try:
            from . import sync as sync_mod
            _plan = sync_mod.takeover_plan()
            direct_entries = sum(len(r.get("servers") or []) for r in _plan)
            alongside = direct_entries > 0
        except Exception:
            alongside, direct_entries = False, 0

    if servers == 0:
        mode = "empty"
    elif wired:
        mode = "serve-alongside" if alongside else "serve"
    elif routed > 0 or native > 0:
        mode = "cli"
    else:
        mode = "direct"

    return {
        "mode": mode,
        "gateway_registered": wired,
        # The alongside detail travels with the mode so every surface can say the
        # concrete number ("48 direct entries in 6 hosts") and the one fix without
        # re-walking the configs.
        "alongside": alongside,
        "direct_entries": direct_entries,
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


def _cli_credit_json(d: dict | None = None) -> dict | None:
    """The unified cli_credit dict for mode/statusline `--json`, or None.

    Same shape as `footer.cli_credit` (mode/resident_avoided/per_call/caliber) so
    every JSON surface — mode, statusline, status, footer-facts, credit — speaks
    one contract. Built here from the already-read figures/mode so these JSON
    views do not each re-derive it (and cannot disagree). None off the CLI path.
    """
    try:
        _d = detect() if d is None else d
        if _d.get("mode") != "cli":
            return None
        f = _figures()
        full = int(f.get("tokens_full") or 0)
        if not full:
            return None
        return {
            "mode": "cli",
            "resident_avoided": full,
            "per_call": int(f.get("name_index_tokens") or 0),
            "caliber": f.get("token_caliber") or "",
        }
    except Exception:
        return None


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


_MODE_TAG = {"serve": "serve", "serve-alongside": "serve→takeover",
             "cli": "cli", "direct": "direct→sync", "empty": "no servers"}
# The Chinese tags are short on purpose: a status bar counts a CJK glyph as two
# columns, so these must stay tiny to keep the whole line within 80 columns.
# `direct→sync` / `直连→sync` turns the bare "!" warning into the fix: a direct
# machine is not saving, and the arrow names the one command that starts it.
# `serve→takeover` / `serve→接管` is the same idea for the alongside state: the
# gateway is wired but has not taken the upstream entries over, so it is not
# saving yet and the fix is `mcptoon sync --takeover`.
_MODE_TAG_ZH = {"serve": "serve", "serve-alongside": "serve→接管",
                "cli": "CLI", "direct": "直连→sync", "empty": "无服务器"}


def statusline(lng: str | None = None) -> str:
    """One compact badge line, safe to run every prompt from any host.

    Deliberately <=80 display columns and one line: it is meant to sit in a
    status bar or be echoed at the top of a turn, so a wrapping badge defeats the
    purpose. Reads only cached figures — never spawns a server. The per-turn cost
    is the interpreter's own start-up (~0.5 s here) plus a stat walk of the cache;
    the catalog token totals are memoised so they are *not* recomputed per prompt
    (measured 2026-10-19: recomputing them was ~440 ms and dominated the line).
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

    # Language follows the same chain every other human-facing string uses
    # (explicit setting > MCPTOON_LANG > OS UI > LANG > en). The badge used to be
    # hard-coded English, which meant a Chinese-configured machine showed an
    # English status bar — the one surface a user stares at all day.
    if lng is None:
        from . import config as _cfg
        lng = _cfg.resolve_lang()
    zh = lng == "zh"

    head = "🎉 mcptoon"
    # CLI mode credits a *different* fact than serve. Under CLI the gateway is not
    # wired in, so "9 tools, 99% less" is a serve story the CLI user is not living;
    # what they actually bank is "zero resident" — the whole catalog never loads.
    # Put that on the badge so the status bar stops crediting CLI for a gateway
    # saving. Serve/direct keep the gateway tool-count figure unchanged.
    cli_credit = (d["mode"] == "cli" and full)
    # direct = installed but not wired: the agent loads the full catalog native and
    # saves NOTHING. The old badge fell through to the gateway "9 tools (99% less)"
    # story, which is exactly the serve figure a direct user is not living — and it
    # reads as "you're saving 99%" when the truth is the opposite. Say it plainly.
    is_direct = d["mode"] == "direct"
    # serve-alongside = gateway wired in one host, but other hosts still list the
    # upstream entries — so those keep loading the full catalog and the gateway
    # saves nothing yet. Same "not saving" truth as direct, different fix
    # (`sync --takeover`), so it must not fall through to the "省 N%" gateway line.
    is_alongside = d["mode"] == "serve-alongside"
    _no_saving = is_direct or is_alongside
    if zh:
        if cli_credit:
            tool_bit = f"0 常驻·全量{_human(full)}没进上下文"
        elif is_alongside:
            tool_bit = (f"未接管·全量{_human(full)}仍直连·未省" if full
                        else "未接管·未省")
        elif is_direct:
            tool_bit = (f"未接线·全量{_human(full)}常驻·未省" if full
                        else "未接线·未省")
        else:
            tool_bit = f"{exposed} 工具（省 {pct}%）" if (exposed and pct) else None
        if _no_saving:
            saved_bit = f"{_human(calls)} 次·未省" if calls else None
        else:
            saved_bit = f"≈省{_human(est[1])} · {_human(est[0])}次" if est else (
                f"{_human(calls)} 次" if calls else None)
        tag = _MODE_TAG_ZH.get(d["mode"], d["mode"])
    else:
        if cli_credit:
            tool_bit = f"0 resident·{_human(full)} kept out"
        elif is_alongside:
            tool_bit = (f"not taken over·full {_human(full)} still direct·no saving"
                        if full else "not taken over·no saving")
        elif is_direct:
            tool_bit = (f"not wired·full {_human(full)} resident·no saving" if full
                        else "not wired·no saving")
        else:
            tool_bit = f"{exposed} tools ({pct}% less)" if (exposed and pct) else None
        if _no_saving:
            saved_bit = f"{_human(calls)} calls·no saving" if calls else None
        else:
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

    # Guard the <=80 DISPLAY-column budget (a CJK glyph counts as two columns, so
    # len() under-counts the Chinese badge). Drop the cumulative clause first.
    from .welcome import display_width

    if display_width(line) > 80 and saved_bit and tool_bit:
        parts = [head, tool_bit, tag]
        line = " · ".join(parts)
    return line


def _mode_zh(mode: str) -> str:
    return {
        "cli": "CLI 模式（最省 · 零常驻）",
        "serve": "serve 模式（原生集成 · 每轮小固定开销）",
        "serve-alongside": "serve 模式（网关已接 · 但没接管直连条目 · 还没省）",
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
        # Answer the question the user actually has ("which should I use?") right
        # at the top, instead of making them run `mode recommend` separately.
        try:
            _rec = recommend(lng=lng)
            if _rec and _rec.get("recommended"):
                _why = _rec.get("one_action") or _rec.get("reason") or ""
                a(f"  本机建议：{_mode_zh(_rec['recommended'])} —— {_why}")
        except Exception:
            pass
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
        elif d["mode"] == "serve-alongside":
            a("")
            a(f"  ⚠ 你现在是 serve（网关已接），但宿主里还有 {d.get('direct_entries', 0):,} "
              "个直连条目没被接管——")
            a("    这些宿主导航还在每轮加载全部 schema，网关没替你省。跑 "
              "`mcptoon sync --takeover` 接管后，才真正省。")
        elif d["mode"] == "cli":
            a("")
            a("  ✓ 你现在是 CLI 模式：已经是最省，前提是 agent 真在敲命令。")
            # Make the saving concrete for *this* user, not just a principle: the
            # number of calls they have routed, each of which avoided loading the
            # whole catalog. Only when there is history and a measured catalog.
            _routed = int(d.get("routed") or d.get("calls") or 0)
            if full and _routed > 0:
                a(f"     就你本人而言：已 {_routed:,} 次调用，每次都没把全量 "
                  f"{full:,} token 的 schema 塞进上下文。")
        elif d["mode"] == "serve":
            a("")
            a("  ✓ 你现在是 serve 模式：每轮小固定开销，已省定义侧大头。")
    else:
        a("mcptoon delivery modes — current, cheapest, how to choose")
        a("─" * 54)
        a(f"  Current mode: {d['mode']}")
        try:
            _rec = recommend(lng=lng)
            if _rec and _rec.get("recommended"):
                _why = _rec.get("one_action") or _rec.get("reason") or ""
                a(f"  For this host: {_rec['recommended']} — {_why}")
        except Exception:
            pass
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
        if d["mode"] == "cli":
            a("")
            a("  ✓ You are in CLI mode: already the cheapest, provided the agent")
            a("    actually runs commands.")
            _routed = int(d.get("routed") or d.get("calls") or 0)
            if full and _routed > 0:
                a(f"    Your own saving so far: {_routed:,} calls, each of which did")
                a(f"    NOT load the full {full:,}-token schema set into context.")
        elif d["mode"] == "serve-alongside":
            a("")
            a("  ⚠ You are on serve, but the gateway has NOT taken over the direct")
            a(f"    entries: {d.get('direct_entries', 0):,} of them still load every")
            a("    schema natively, so the gateway saves nothing yet.")
            a("    → `mcptoon sync --takeover` routes them through the gateway.")
    return "\n".join(L)


# ─── Recommendation: which mode *should* this machine use? ───
#
# `detect()` answers "what mode is running now". This answers the different,
# harder question a user actually asks: "which one should *I* use?".
#
# The decision hinges on ONE fact mcptoon cannot measure from config: can this
# host reliably run a shell command every turn? A shell-capable agent (Claude
# Code, Codex, DSH, a Cursor terminal) can use CLI; a pure-MCP GUI (Claude
# Desktop, Windsurf GUI) cannot, and pushing CLI at it is wrong. We read the few
# host signals that *are* observable (env vars), and when they are absent we do
# not guess — we return ``needs_confirm=True`` so the skill asks the user. That
# boundary is the whole point: a confident wrong recommendation is worse than an
# honest "confirm this".

# Env markers that mean "the current process is an agent that can shell out".
# Kept as a plain, testable table so a new host is one line, not a code change.
_SHELL_HOST_ENV = (
    ("DSH_SHELL", "DSH"),
    ("CLAUDECODE", "Claude Code"),
    ("CLAUDE_CODE_ENTRYPOINT", "Claude Code"),
    ("CODEX_HOME", "Codex"),
    ("CURSOR_TRACE_ID", "Cursor"),
)


def detect_host() -> dict:
    """Best-effort read of the *calling* host and whether it can run a shell.

    Returns ``{"host": name|None, "shell": True|False|None}``. ``shell`` is
    ``True`` only on a positive signal, ``False`` never asserted (absence is not
    proof a host lacks a shell), and ``None`` means "unknown — confirm". The
    distinction matters: a GUI user who is told "you can use CLI" wastes effort.
    """
    for env, name in _SHELL_HOST_ENV:
        if os.environ.get(env):
            return {"host": name, "shell": True}
    # TERM_PROGRAM is set by terminal emulators; a present one is a weak-but-real
    # shell signal even outside a known agent.
    if os.environ.get("TERM_PROGRAM"):
        return {"host": os.environ["TERM_PROGRAM"], "shell": True}
    return {"host": None, "shell": None}


def recommend(lng: str | None = None, host: dict | None = None) -> dict:
    """The recommended mode for *this* machine, with a reason and one action.

    Decision table (each branch justified in the module comment above):

    * already ``cli``           -> keep CLI (it is the cheapest; nothing to do)
    * ``serve`` + shell host    -> CLI is a *possible* downgrade; say so, but the
                                   default advice is "you already save; keep it"
    * ``serve`` + GUI / unknown -> keep serve (do not push CLI at a GUI)
    * ``direct``                -> you save nothing; wire serve now, and if you
                                   have a shell you may prefer CLI
    * ``empty``                 -> configure servers first

    ``needs_confirm`` is True whenever the shell capability is unknown *and* it
    would change the answer — the skill must then ask the user, not assume.
    """
    if host is None:
        host = detect_host()
    d = detect()
    mode = d["mode"]
    shell = host.get("shell")
    from . import config as _cfg
    if lng is None:
        lng = _cfg.resolve_lang()
    zh = lng == "zh"

    if mode == "empty":
        recommended, needs_confirm = "serve", False
        reason = ("还没配置任何 MCP 服务器；先 `mcptoon quickstart` 把服务器接进来"
                  if zh else
                  "No MCP servers configured yet; run `mcptoon quickstart` first")
        one_action = "mcptoon quickstart" if zh else "mcptoon quickstart"
    elif mode == "cli":
        recommended, needs_confirm = "cli", False
        reason = ("你已经在最省的 CLI 模式：闲置 0、按需只取 1 个 schema"
                  if zh else
                  "You are already on the cheapest CLI path: idle 0, fetch one schema on demand")
        one_action = "mcptoon statusline" if zh else "mcptoon statusline"
    elif mode == "direct":
        # No saving yet. serve is the zero-config fix; CLI is the cheaper fix if
        # (and only if) the host can run a shell.
        needs_confirm = shell is None
        recommended = "cli" if shell else "serve"
        if shell:
            reason = ("现在是直连（没在省）；你有 shell，可直接改用 `mcptoon call`（最省）"
                      if zh else
                      "You are in direct mode (saving nothing); you have a shell, so switch to `mcptoon call` (cheapest)")
        else:
            reason = ("现在是直连（没在省）；先 `mcptoon sync --self` 接上 serve（零配置，立刻省）"
                      if zh else
                      "You are in direct mode (saving nothing); run `mcptoon sync --self` to wire serve (zero-config, saves now)")
        one_action = ("mcptoon call <server> <tool> '{...}'" if (shell and not needs_confirm)
                      else "mcptoon sync --self")
    elif mode == "serve-alongside":
        # Gateway is wired, but the upstream entries it manages are still listed
        # in some host configs, so those hosts keep loading every full schema and
        # the gateway saves nothing there. This is NOT "keep serve" — the user is
        # paying for a gateway that has not taken over yet. The one fix is
        # `sync --takeover`, which drops the direct entries so the gateway is the
        # only path. A shell host could still switch to CLI, but takeover is the
        # smaller, in-place fix, so it is the default action regardless of shell.
        needs_confirm = False
        recommended = "serve"
        reason = ("网关已接，但宿主里还有直连条目没被接管——那些宿主导航还在每轮加载全部 "
                  "schema，网关没替你省。跑 `mcptoon sync --takeover` 接管后才真省"
                  if zh else
                  "The gateway is wired, but the hosts still list the upstream entries "
                  "it manages, so they load every schema natively and the gateway saves "
                  "nothing yet. Run `mcptoon sync --takeover` to route them through it")
        one_action = "mcptoon sync --takeover"
    else:  # serve
        needs_confirm = False
        recommended = "serve"
        if shell:
            reason = ("你已接 serve（每轮小固定开销）；你有 shell，可降级到 CLI 更省（代价：每轮要敲命令）"
                      if zh else
                      "You are on serve (small fixed per-turn cost); you have a shell, so CLI would save more (cost: run a command each turn)")
        else:
            reason = ("你已接 serve：每轮小固定开销，已省定义侧大头；保持即可"
                      if zh else
                      "You are on serve: small fixed per-turn cost, the big definition-side saving; keep it")
        one_action = "mcptoon mode" if zh else "mcptoon mode"

    return {
        "current": mode,
        "recommended": recommended,
        "reason": reason,
        "one_action": one_action,
        "needs_confirm": needs_confirm,
        "host": host.get("host"),
        "shell": shell,
    }


def render_recommendation(rec: dict, lng: str = "en") -> str:
    """Human, pasteable text for `mcptoon mode recommend`."""
    zh = lng == "zh"
    L = []
    a = L.append
    a(("模式推荐" if zh else "Mode recommendation") + f"  —  {rec['current']} → {rec['recommended']}")
    a("─" * 46)
    a(("  " if not zh else "  ") + rec["reason"])
    a("")
    if rec.get("needs_confirm"):
        a("  ⚠ 我判断不了这台宿主能否每轮敲 shell —— 请确认：agent 有终端、愿意执行命令吗？"
          if zh else
          "  ⚠ I cannot tell whether this host can run a shell each turn — please confirm: does the agent have a terminal and will it run commands?")
        a("     能 → 用 CLI 最省；不能 → 用 serve。"
          if zh else
          "     yes -> CLI is cheapest; no -> use serve.")
        a("")
    a(("  下一步：" if zh else "  Next: ") + rec["one_action"])
    return "\n".join(L)


def run_recommend(rest: list[str], fmt: str = "auto", head_n: int = 0,
                  max_chars: int = 0, full: bool = False) -> None:
    """CLI entry: `mcptoon mode recommend [--json]`."""
    from . import config as cfg
    from . import output

    lng = cfg.resolve_lang()
    rec = recommend(lng=lng)
    if fmt == "json":
        print(output.render(output.stamp_schema(rec), fmt="json",
                            max_chars=max_chars))
        return
    print(render_recommendation(rec, lng=lng))


def run(rest: list[str], fmt: str = "auto", head_n: int = 0, max_chars: int = 0,
        full: bool = False) -> None:
    """CLI entry: `mcptoon mode [recommend] [--json]`.

    Usage:
        mcptoon mode              Which mode you are in, what each costs, which is cheapest
        mcptoon mode --json       The same facts as JSON
        mcptoon mode recommend    Which mode *this* host should use, and one action
        mcptoon mode recommend --json   The same recommendation as JSON
    """
    if "recommend" in rest:
        run_recommend(rest, fmt, head_n, max_chars, full)
        return
    from . import config as cfg
    from . import output

    if fmt == "json":
        d = detect()
        f = _figures()
        try:
            _rec = recommend()
            _rec_now = _rec.get("recommended")
            _rec_act = _rec.get("one_action")
        except Exception:
            _rec_now, _rec_act = None, None
        print(output.render(output.stamp_schema({
            **d,
            "tools": f.get("tools"),
            "tokens_full": f.get("tokens_full"),
            "gateway_tokens": f.get("gateway_tokens"),
            "gateway_pct": f.get("gateway_pct"),
            "statusline": statusline(),
            "delivery_mode": d.get("mode"),
            "cli_credit": _cli_credit_json(d),
            "recommended": _rec_now,
            "one_action": _rec_act,
        }), fmt="json", max_chars=max_chars))
        return
    print(explain(lng=cfg.resolve_lang()))


def run_statusline(rest: list[str], fmt: str = "auto", head_n: int = 0,
                   max_chars: int = 0, full: bool = False) -> None:
    """CLI entry: `mcptoon statusline [--json|--install|--uninstall|--check]`.

    Usage:
        mcptoon statusline              One-line badge for a status bar / turn header
        mcptoon statusline --json       The badge plus its inputs as JSON
        mcptoon statusline --install    Wire it into Claude Code's status bar
        mcptoon statusline --uninstall  Remove exactly that entry again
        mcptoon statusline --check      Report whether it is wired (exit 1 if not)
        mcptoon statusline --guide      How to wire it into OTHER hosts (tmux, shell, ...)
    """
    from . import output

    if "--guide" in rest:
        _emit_statusline_guide(fmt=fmt, max_chars=max_chars)
        return
    if "--install" in rest or "--uninstall" in rest:
        _emit_statusline_wiring(
            install="--install" in rest,
            dry="--dry" in rest,
            fmt=fmt, max_chars=max_chars)
        return
    if "--check" in rest:
        _emit_statusline_check(fmt=fmt, max_chars=max_chars)
        return

    line = statusline()
    if fmt == "json":
        _d = detect()
        print(output.render(output.stamp_schema({
            "statusline": line, **_d,
            "delivery_mode": _d.get("mode"),
            "cli_credit": _cli_credit_json(_d),
        }), fmt="json", max_chars=max_chars))
        return
    print(line)


# ─── Wiring the badge into a host's status bar ───
#
# The badge existed since 0.8.22 but nothing put it where a user would see it:
# `presence` writes to stderr and `statusline` had to be pasted into a config by
# hand. The measured result is the complaint this module opens with — "I installed
# it and cannot tell it is doing anything." This section closes that gap for the
# one host whose status-bar schema is documented: Claude Code reads
# `statusLine.command` from `~/.claude/settings.json`.
#
# Design limits, each one deliberate:
# * **Only the command, nothing else.** We set one key in one file. No other
#   setting is touched, so a user's model/theme/permissions survive untouched.
# * **Byte-reversible.** `--uninstall` removes exactly the key we wrote and deletes
#   the file only when it was ours alone, matching the promise `mcptoon off` already
#   makes elsewhere. Removal cuts our inserted text span back out of the original
#   bytes rather than re-serialising the whole document, so install→uninstall
#   restores the file character for character — not merely value for value. Verified
#   against 2-space, 4-space, tab-indented, minified, no-trailing-newline, CRLF,
#   nested-same-key and CJK files, plus a 2000-document fuzz (2026-10-19).
# * **`<file>.bak` once, never clobbered.** Same rule as the server config: the
#   first snapshot is the one closest to what the user had.
# * **Never silently overwrite a different statusLine.** If the user already points
#   `statusLine` at their own script, installing would destroy it; we refuse and
#   say so instead.

_CLAUDE_SETTINGS_REL = (".claude", "settings.json")


def _claude_settings_path() -> Path:
    """Claude Code's user settings file (`~/.claude/settings.json`).

    `USERPROFILE` is read at call time, not import time, so a test that redirects
    home (or a sandbox run) never writes to the real profile — the same trap
    `sync.py` documents for its agent table.
    """
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return Path(home).joinpath(*_CLAUDE_SETTINGS_REL)


def statusline_command() -> str:
    """The exact command string a host should run to print the badge.

    `--json` is deliberately absent: Claude Code renders the command's stdout as
    the status line, so it must be the one plain line, not a JSON envelope.
    """
    return "mcptoon statusline"


def _read_settings_raw(path: Path) -> tuple[dict, str | None, str]:
    """`(data, error, raw_text)`. A missing file is `({}, None, "")`."""
    if not path.exists():
        return {}, None, ""
    try:
        # newline="" keeps CRLF/CR as-is: a text-mode read would silently rewrite a
        # Windows file's line endings to LF, breaking the byte-reversibility promise.
        with open(path, encoding="utf-8", newline="") as fh:
            raw = fh.read()
    except OSError as e:
        return {}, str(e), ""
    if not raw.strip():
        return {}, None, raw
    try:
        data = json.loads(raw)
    except ValueError as e:
        return {}, f"not valid JSON: {e}", raw
    if not isinstance(data, dict):
        return {}, "top level is not a JSON object", raw
    return data, None, raw


def _read_settings(path: Path) -> tuple[dict, str | None]:
    """`(data, error)`. A missing file is `({}, None)`; unparseable is an error."""
    data, err, _raw = _read_settings_raw(path)
    return data, err


# Byte-reversibility by text splicing, not re-serialisation.
#
# Measured 2026-10-19: a `json.dumps` round trip reformats the whole file — a
# 4-space file comes back 2-space, `["Bash"]` gets exploded one-per-line, a
# minified file is re-indented. So "we only added one key" was quietly false: the
# user's model/theme/permissions block got reflowed. Re-serialising *cannot* be
# byte-exact because `json.dumps` normalises arrays and spacing.
#
# Instead we splice our one member into the original text and, on uninstall, cut
# exactly that span back out (the same anchor-and-cut discipline `sync.py` uses for
# its skill-pointer block). Everything outside the splice is the user's original
# bytes, untouched.

# Values of `statusLine` that mean "empty / not set" — safe for us to own.
def _statusline_is_ours(value) -> bool:
    return isinstance(value, dict) and value.get("command") == statusline_command()


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def _string_end(text: str, i: int) -> int:
    """Index just past the JSON string starting at `text[i] == '"'`."""
    assert text[i] == '"'
    i += 1
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    raise ValueError("unterminated string")


def _value_end(text: str, i: int) -> int:
    """Index just past the JSON value starting at `text[i]` (objects/arrays/strings
    are bracketed or quoted; a number/true/false/null runs to the next delimiter)."""
    i = _skip_ws(text, i)
    if i >= len(text):
        raise ValueError("unexpected end of input")
    c = text[i]
    if c == '"':
        return _string_end(text, i)
    if c in "{[":
        depth = 0
        while i < len(text):
            ch = text[i]
            if ch == '"':
                i = _string_end(text, i)
                continue
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise ValueError("unbalanced brackets")
    # scalar: number / true / false / null
    while i < len(text) and text[i] not in ",}] \t\r\n":
        i += 1
    return i


def _find_member(text: str, key: str) -> tuple[int, int, int] | None:
    """Locate a top-level `"key": value` member in an object's text.

    Returns `(value_start, value_end, member_start)` where `member_start` is the
    index of the opening quote of the key (for whole-member removal). `None` when
    the key is not present. Top-level only — a nested `"statusLine"` must not match.
    """
    i = _skip_ws(text, 0)
    if i >= len(text) or text[i] != "{":
        return None
    i += 1
    while True:
        i = _skip_ws(text, i)
        if i >= len(text) or text[i] == "}":
            return None
        member_start = i
        if text[i] != '"':
            return None
        key_end = _string_end(text, i)
        try:
            found_key = json.loads(text[i:key_end])
        except ValueError:
            return None
        i = _skip_ws(text, key_end)
        if i >= len(text) or text[i] != ":":
            return None
        i = _skip_ws(text, i + 1)
        v_start = i
        v_end = _value_end(text, i)
        if found_key == key:
            return v_start, v_end, member_start
        i = _skip_ws(text, v_end)
        if i < len(text) and text[i] == ",":
            i += 1
            continue
        if i < len(text) and text[i] == "}":
            return None
        return None


# Our inserted member, and the exact text `_splice_member` appends. Uninstall cuts
# precisely this span, so the two must stay in lockstep.
def _our_member_text() -> str:
    return '"statusLine": ' + json.dumps(
        {"type": "command", "command": statusline_command()}, ensure_ascii=False)


def _splice_member(raw: str, member: str) -> str:
    """Insert `member` as the first member of the top-level object in `raw`.

    If `raw` is an empty object (`{}` / blank / missing) we build a whole object.
    Otherwise we splice right after the opening `{` plus a newline + two-space
    indent, matching the common settings layout, and leave the rest byte-for-byte.
    """
    if not raw.strip():
        return "{\n  " + member + "\n}\n"
    i = _skip_ws(raw, 0)
    if i >= len(raw) or raw[i] != "{":
        raise ValueError("top level is not an object")
    j = _skip_ws(raw, i + 1)
    if j < len(raw) and raw[j] == "}":
        # `{ }` — keep it on the same simple shape.
        return "{\n  " + member + "\n}\n"
    return raw[:i + 1] + "\n  " + member + "," + raw[i + 1:]


def _cut_member(raw: str, member_start: int, value_end: int) -> str:
    """Remove the `"key": value` member that begins at `member_start` and whose
    value ends at `value_end`, plus its trailing comma and the whitespace that
    preceded it — restoring the pre-splice text byte for byte."""
    n = len(raw)
    k = _skip_ws(raw, value_end)
    if k < n and raw[k] == ",":
        # member had a following sibling: drop member + comma, keep the rest
        return raw[:member_start] + raw[k + 1:].lstrip(" \t")
    # member was last: drop the preceding comma + the whitespace/newline we added
    back = member_start
    while back > 0 and raw[back - 1] in " \t":
        back -= 1
    if back > 0 and raw[back - 1] == ",":
        back -= 1
    if back > 0 and raw[back - 1] == "\n":
        back -= 1
    return raw[:back] + raw[value_end:]


def _cut_our_inserted(raw: str) -> str | None:
    """Reverse `_splice_member` exactly by removing the span it inserted.

    `_splice_member` put `"\\n  " + member + ","` right after the opening `{`. If
    that exact span is still there (the user did not reorder the file), cutting it
    out restores the pre-install bytes character for character. Returns `None` when
    the span is not where we left it, so the caller falls back to `_cut_member`.
    """
    i = _skip_ws(raw, 0)
    if i >= len(raw) or raw[i] != "{":
        return None
    inserted = "\n  " + _our_member_text() + ","
    if raw[i + 1:i + 1 + len(inserted)] == inserted:
        return raw[:i + 1] + raw[i + 1 + len(inserted):]
    return None


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` atomically (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-statusline-",
                              suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, str(path))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _backup_once(path: Path) -> None:
    """Snapshot `path` to `<path>.bak` once. Never clobbers an existing `.bak`."""
    try:
        if not path.exists():
            return
        bak = path.with_suffix(path.suffix + ".bak")
        if bak.exists():
            return
        # newline="" on both ends so the snapshot is byte-identical (a CRLF file
        # must not be restored as LF).
        with open(path, encoding="utf-8", newline="") as src:
            content = src.read()
        with open(bak, "w", encoding="utf-8", newline="") as dst:
            dst.write(content)
    except OSError:
        pass


def statusline_status() -> dict:
    """Where the badge is wired, without changing anything (safe for `doctor`)."""
    path = _claude_settings_path()
    data, err = _read_settings(path)
    current = data.get("statusLine") if isinstance(data.get("statusLine"), dict) else None
    cmd = current.get("command") if current else None
    return {
        "host": "claude-code",
        "path": str(path),
        "file_exists": path.exists(),
        "wired": cmd == statusline_command(),
        "command": cmd,
        "error": err,
    }


def install_statusline(*, dry: bool = False) -> dict:
    """Point Claude Code's `statusLine` at `mcptoon statusline`.

    Returns `{"path", "changed", "action", "error"}` where `action` is one of
    `already`, `installed`, `would-install`, `refused`, `error`.
    """
    path = _claude_settings_path()
    if path.exists() and path.is_dir():
        return {"path": str(path), "changed": False, "action": "error",
                "error": f"{path} is a directory, not a settings file — "
                         "move it out of the way first"}
    data, err, raw = _read_settings_raw(path)
    if err:
        return {"path": str(path), "changed": False, "action": "error",
                "error": f"{path} {err} — fix or move it first, refusing to rewrite it"}
    existing = data.get("statusLine")
    if _statusline_is_ours(existing):
        return {"path": str(path), "changed": False, "action": "already", "error": None}
    if existing is not None and not (
            isinstance(existing, dict) and existing.get("command") in (None, "")):
        return {"path": str(path), "changed": False, "action": "refused",
                "error": ("statusLine is already set to something else; "
                          "remove it first if you want mcptoon to own the badge"),
                "current": existing}
    # A present-but-empty statusLine (`{}` or `{"type": ...}` with no command) is
    # "ours to fill": strip it out of the raw text first, then splice a clean one,
    # so we never leave two members or a dangling comma.
    base_raw = raw
    if isinstance(existing, dict):
        span = _find_member(raw, "statusLine")
        if span is not None:
            _v_start, v_end, member_start = span
            base_raw = _cut_member(raw, member_start, v_end)
    if dry:
        return {"path": str(path), "changed": False, "action": "would-install",
                "error": None}
    try:
        new_text = _splice_member(base_raw, _our_member_text())
    except ValueError as e:
        return {"path": str(path), "changed": False, "action": "error",
                "error": f"{path} could not be spliced: {e}"}
    try:
        _backup_once(path)
        _atomic_write_text(path, new_text)
    except OSError as e:
        return {"path": str(path), "changed": False, "action": "error", "error": str(e)}
    return {"path": str(path), "changed": True, "action": "installed", "error": None}


def uninstall_statusline(*, dry: bool = False) -> dict:
    """Remove exactly the `statusLine` entry this module wrote.

    Deletes the file only when the leftover object is empty — i.e. mcptoon created
    it solely to hold the badge. The removal cuts our member's exact text span back
    out of the original bytes, so an install→uninstall round trip restores the file
    character for character, not merely value for value.
    """
    path = _claude_settings_path()
    if not path.exists():
        return {"path": str(path), "changed": False, "action": "absent", "error": None}
    if path.is_dir():
        return {"path": str(path), "changed": False, "action": "error",
                "error": f"{path} is a directory, not a settings file"}
    data, err, raw = _read_settings_raw(path)
    if err:
        return {"path": str(path), "changed": False, "action": "error", "error": err}
    existing = data.get("statusLine")
    if not _statusline_is_ours(existing):
        return {"path": str(path), "changed": False, "action": "not-ours", "error": None}
    if dry:
        return {"path": str(path), "changed": False, "action": "would-remove", "error": None}
    # Remove every key so the leftover object is empty; if only ours was present the
    # file goes away entirely (as `off` promises). For the multi-key case we cut our
    # exact inserted span back out (byte-exact); only if the user reordered the file
    # do we fall back to a span cut, then to a canonical write.
    remaining = {k: v for k, v in data.items() if k != "statusLine"}
    try:
        if not remaining:
            # Did *we* create this file, or was it a pre-existing `{}` we filled in?
            # A `.bak` exists exactly when the file was here before our first install.
            bak = path.with_suffix(path.suffix + ".bak")
            if bak.exists():
                # restore the original bytes verbatim rather than deleting the file
                with open(bak, encoding="utf-8", newline="") as fh:
                    _atomic_write_text(path, fh.read())
                return {"path": str(path), "changed": True, "action": "removed",
                        "error": None}
            path.unlink()
            return {"path": str(path), "changed": True, "action": "removed-file",
                    "error": None}
        new_text = _cut_our_inserted(raw)
        if new_text is None:
            span = _find_member(raw, "statusLine")
            if span is None:
                # Should not happen (data had the key), but never half-rewrite.
                new_text = json.dumps(remaining, indent=2, ensure_ascii=False) + "\n"
            else:
                _v_start, v_end, member_start = span
                new_text = _cut_member(raw, member_start, v_end)
        _atomic_write_text(path, new_text)
    except OSError as e:
        return {"path": str(path), "changed": False, "action": "error", "error": str(e)}
    return {"path": str(path), "changed": True, "action": "removed", "error": None}


def _emit_statusline_wiring(*, install: bool, dry: bool, fmt: str,
                            max_chars: int) -> None:
    from . import config as cfg
    from . import output

    zh = cfg.resolve_lang() == "zh"
    result = install_statusline(dry=dry) if install else uninstall_statusline(dry=dry)
    if fmt == "json":
        print(output.render(output.stamp_schema(result), fmt="json",
                            max_chars=max_chars))
        if result.get("action") in ("error", "refused"):
            sys.exit(1)
        return
    action = result.get("action")
    path = result.get("path")
    if action in ("installed", "would-install"):
        if zh:
            head = ("将接入" if action == "would-install" else "已接入")
            print(f"状态栏{head}：Claude Code 会显示 `{statusline_command()}`")
            print(f"  文件：{path}")
            print("  下一个 Claude Code 会话会在终端底部渲染它。")
            print("  撤销：mcptoon statusline --uninstall")
        else:
            prefix = "would wire" if action == "would-install" else "wired"
            print(f"statusline {prefix}: Claude Code will show `{statusline_command()}`")
            print(f"  file: {path}")
            print("  next Claude Code session renders it at the bottom of the terminal.")
            print("  undo: mcptoon statusline --uninstall")
    elif action == "already":
        print(f"状态栏已接入 {path} —— 无需改动。" if zh
              else f"statusline already wired in {path} — nothing to do.")
    elif action in ("removed", "removed-file", "would-remove"):
        if zh:
            verb = "将移除" if action == "would-remove" else "已移除"
            print(f"状态栏{verb}：{path}")
            if action == "removed-file":
                print("  文件里没有别的内容，因此已删除（与 `off` 的承诺一致）。")
        else:
            verb = "would remove" if action == "would-remove" else "removed"
            print(f"statusline {verb} from {path}.")
            if action == "removed-file":
                print("  the file held nothing else, so it was deleted (as `off` promises).")
    elif action == "not-ours":
        print(f"{path} 里没有我们的状态栏 —— 无需移除。" if zh
              else f"statusline not present in {path} — nothing to remove.")
    elif action == "absent":
        print(f"{path} 不存在 —— 无需移除。" if zh
              else f"no settings file at {path} — nothing to remove.")
    elif action == "refused":
        if zh:
            print(f"拒绝：{result.get('error')}", file=sys.stderr)
            print(f"  当前 statusLine：{json.dumps(result.get('current'))}",
                  file=sys.stderr)
        else:
            print(f"refused: {result.get('error')}", file=sys.stderr)
            print(f"  current statusLine: {json.dumps(result.get('current'))}",
                  file=sys.stderr)
        sys.exit(1)
    else:
        print(f"状态栏出错：{result.get('error')}" if zh
              else f"statusline error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def _emit_statusline_check(*, fmt: str, max_chars: int) -> None:
    from . import config as cfg
    from . import output

    zh = cfg.resolve_lang() == "zh"
    st = statusline_status()
    if fmt == "json":
        print(output.render(output.stamp_schema(st), fmt="json", max_chars=max_chars))
        sys.exit(0 if st["wired"] else 1)
    if st["wired"]:
        print(f"状态栏已接入：{st['path']}" if zh else f"statusline wired: {st['path']}")
        print(f"  命令：{st['command']}" if zh else f"  command: {st['command']}")
        sys.exit(0)
    if st.get("error"):
        print(f"状态栏：读不了 {st['path']}：{st['error']}" if zh
              else f"statusline: cannot read {st['path']}: {st['error']}",
              file=sys.stderr)
        sys.exit(1)
    print(f"状态栏未接入：{st['path']}" if zh
          else f"statusline NOT wired: {st['path']}")
    if st["command"]:
        print(f"  它当前指向别的东西：{st['command']}" if zh
              else f"  it currently runs something else: {st['command']}")
    print("  修复：mcptoon statusline --install" if zh
          else "  fix: mcptoon statusline --install")
    sys.exit(1)


def _emit_statusline_guide(*, fmt: str, max_chars: int) -> None:
    """How to put the badge on a host that has no automatic wiring.

    Only Claude Code has a documented status-bar schema, so `--install` wires that
    one host. Every other host — tmux, a shell prompt, a terminal multiplexer, a
    custom IDE — can still show the badge, but only by running the command itself.
    This prints the exact one-liners so a user on Codex/Gemini/Cursor/DSH is not
    told "not supported" and left to guess. The command is the same everywhere
    (`mcptoon statusline`); only the place you paste it differs.
    """
    from . import config as cfg

    zh = cfg.resolve_lang() == "zh"
    cmd = statusline_command()
    if fmt == "json":
        from . import output
        print(output.render({
            "command": cmd,
            "hosts": {
                "claude-code": "automatic: mcptoon statusline --install",
                "tmux": f'set -g status-right "#({cmd})"',
                "bash-zsh": f'PS1="$( {cmd} ) $PS1"  # or PROMPT_COMMAND',
                "fish": f'function fish_prompt; {cmd}; echo " $ "; end',
                "no-status-bar": "run `mcptoon statusline` in a side pane, or paste it after a turn",
            },
        }, fmt="json", max_chars=max_chars))
        return

    L = []
    a = L.append
    if zh:
        a("把徽章接到各种宿主（只有 Claude Code 能自动接）")
        a("")
        a("  Claude Code —— 自动：")
        a("      mcptoon statusline --install")
        a("")
        a("  其他宿主没有状态栏接口，但都能自己跑这条命令。粘到对应位置即可：")
        a("")
        a("  tmux（右下角状态栏）：")
        a(f'      set -g status-right "#({cmd})"')
        a("")
        a("  bash / zsh（提示符前）：")
        a(f'      PS1="$( {cmd} ) $PS1"')
        a("      或放进 PROMPT_COMMAND，每次回车刷新")
        a("")
        a("  fish：")
        a(f"      function fish_prompt; {cmd}; echo ' $ '; end")
        a("")
        a("  没有状态栏的宿主（Codex / Gemini / Cursor / DSH 等）：")
        a("      开个侧边终端，常驻跑 `mcptoon statusline`；")
        a("      或在一轮结束时把 `mcptoon statusline` 的输出贴出来。")
        a("")
        a("  命令都一样，只是粘的地方不同。它只读缓存、不连服务器，随时跑都安全。")
    else:
        a("Wire the badge into any host (only Claude Code is automatic)")
        a("")
        a("  Claude Code — automatic:")
        a("      mcptoon statusline --install")
        a("")
        a("  Other hosts have no status-bar API, but all can run this one command.")
        a("  Paste it where your host shows status:")
        a("")
        a("  tmux (status-right):")
        a(f'      set -g status-right "#({cmd})"')
        a("")
        a("  bash / zsh (before your prompt):")
        a(f'      PS1="$( {cmd} ) $PS1"')
        a("      or put it in PROMPT_COMMAND to refresh each Enter")
        a("")
        a("  fish:")
        a(f"      function fish_prompt; {cmd}; echo ' $ '; end")
        a("")
        a("  Hosts with no status bar (Codex / Gemini / Cursor / DSH, ...):")
        a("      run `mcptoon statusline` in a side terminal, or")
        a("      paste its output after a turn.")
        a("")
        a("  Same command everywhere; only the paste location differs. It reads the")
        a("  cache and never contacts a server, so it is safe to run any time.")
    print("\n".join(L))
