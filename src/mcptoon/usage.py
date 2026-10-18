# Copyright 2025 cxh
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

"""
mcptoon usage — Lightweight usage tracking

Tracks tool calls per server for cost analysis.
Data stored in ~/.cache/mcptoon/usage.json

Also keeps a plain-text *activity ledger* (activity.jsonl), which answers a
different question than usage.json does. usage.json is a counter ("how many
calls, which servers") and is deliberately lossy — its window is capped at 1000.
The ledger is the readable history a person scrolls when they ask "is mcptoon
even doing anything, and for which agent?" — one line per routed call, newest
last, `mcptoon activity` reads it back. Two files, two jobs: a counter cannot
answer "when was the last time Cursor used me", and a log is the wrong shape to
sum.
"""
import json
import os
import threading
import time
from pathlib import Path

from .config import CACHE_DIR

_USAGE_FILE = CACHE_DIR / "usage.json"
# The activity ledger. Kept beside usage.json (same cache root) so one HOME move
# relocates both; MCPTOON_ACTIVITY_FILE lets a caller (a test, or an agent that
# wants its own trail) point it elsewhere without touching the counter file.
_ACTIVITY_FILE = CACHE_DIR / "activity.jsonl"

# How many ledger lines to keep. A tool that has routed for months must not grow
# an unbounded file on someone's disk; the tail is what `activity` shows anyway.
_ACTIVITY_KEEP = 2000

# Concurrency guard: multiple agent processes/servers may record calls
# simultaneously (backported from the private layer, v0.5.6).
_usage_lock = threading.Lock()

# Which host is driving this process, when it can be known. `serve` learns it
# from the MCP `initialize` handshake (params.clientInfo.name) and calls
# `set_host`; a bare CLI run has no host. Process-global because one process is
# one conversation with one client — the same assumption `serve` already makes
# for its footer stamp. `None` means "not known", never a guessed name.
_HOST: "str | None" = None


def set_host(name: "str | None") -> None:
    """Record which agent/host this process is serving (called by `serve`).

    Best-effort and never raises: an unknown or malformed clientInfo must not
    break the bridge that is trying to report it. The value is only ever echoed
    into the ledger and `status`; nothing decides behaviour from it.
    """
    global _HOST
    try:
        cleaned = (name or "").strip()
        _HOST = cleaned[:60] or None
    except Exception:
        _HOST = None


def get_host() -> "str | None":
    """The host recorded for this process, or None if unknown."""
    return _HOST


# Routed-call count for *this* process only. `_emit_footer` reads it once at the
# end of a command to decide whether to print the presence line — "this command
# used mcptoon" is a per-process fact, so it must not be read off the on-disk
# counter (that is lifetime). `take_session_calls` returns and resets, so a
# long-lived process (serve) never accumulates across conversations.
_SESSION_CALLS = 0


def note_session_call() -> None:
    """Bump the per-process routed-call count. Called from `track_call`."""
    global _SESSION_CALLS
    try:
        _SESSION_CALLS += 1
    except Exception:
        pass


def take_session_calls() -> int:
    """Return and reset the per-process routed-call count."""
    global _SESSION_CALLS
    n = _SESSION_CALLS
    _SESSION_CALLS = 0
    return n


def _activity_file():
    override = os.environ.get("MCPTOON_ACTIVITY_FILE", "").strip()
    return Path(override) if override else _ACTIVITY_FILE


def _load_usage() -> dict:
    if not _USAGE_FILE.exists():
        return {"calls": [], "total": 0}
    try:
        return json.loads(_USAGE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"calls": [], "total": 0}


def count_tokens(payload) -> int:
    """Token cost of a payload, on the *same caliber* `mcptoon bench` uses.

    tiktoken cl100k_base when installed, otherwise ``len//4``. Delegates to
    ``bench._tokenizer`` so there is exactly one caliber in the codebase — the
    footer, `usage`, `stats` and `bench` can never quote two different numbers
    for the same payload. Never raises: a measurement failure returns 0.
    """
    if payload is None:
        return 0
    try:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return 0
    if not text:
        return 0
    try:
        from .bench import _tokenizer
        encode, _name, _exact = _tokenizer()
        return max(1, int(encode(text)))
    except Exception:
        return max(1, len(text) // 4)


def _save_usage(data: dict):
    """Atomic write: unique tmp file + os.replace — readers never see a
    torn/partial usage.json even under concurrent writers."""
    tmp = None
    try:
        _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _USAGE_FILE.with_name(f"usage.json.tmp.{os.getpid()}.{id(data)}")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, _USAGE_FILE)
    except OSError:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def track_call(server: str, tool: str, ok: bool = True, tokens_est: int | None = None,
               payload=None, detail: "str | None" = None):
    """Record a tool call.

    Crash-safe by contract: usage tracking must never break the actual
    tool call it is recording.

    ``payload`` is the tool result; when given and ``tokens_est`` is not, the
    token cost is counted on the ``bench`` caliber (tiktoken cl100k_base when
    available, else ``len//4``). Before v0.7.21 every caller passed no tokens, so
    ``mcptoon usage`` reported ``Tokens (est): 0`` on every machine — a tool that
    claims to save tokens and can never show one. Errors and unknown results
    still record 0 rather than guessing.

    ``detail`` is an optional short, human-readable note carried into the activity
    ledger only (never the usage counter): for a skill distribution it is the
    skill names handed out, so `mcptoon activity` can show *what* capability moved,
    not just that `mcptoon_resolve_skills` was called. It is not counted or
    tokenised; it exists so the "which of my capabilities did it distribute"
    question has an answer.
    """
    if tokens_est is None:
        tokens_est = count_tokens(payload)
    try:
        with _usage_lock:
            data = _load_usage()
            data["calls"].append({
                "server": server,
                "tool": tool,
                "ok": ok,
                "tokens": int(tokens_est or 0),
                "ts": time.time(),
            })
            # Keep last 1000 calls
            if len(data["calls"]) > 1000:
                data["calls"] = data["calls"][-1000:]
            data["total"] = data.get("total", 0) + 1
            # Lifetime result-token total, so a cumulative figure exists at all.
            # The window above is capped, so summing it understates history; this
            # counter is the honest lifetime basis for "how much has flowed
            # through the gateway". Additive key — a legacy file without it falls
            # back to the window sum (see `_lifetime_result_tokens`).
            data["result_tokens"] = data.get("result_tokens", 0) + int(tokens_est or 0)
            _save_usage(data)
    except Exception:
        pass
    # The readable half. Separate try/except from the counter write on purpose:
    # one failing must not stop the other, and neither may ever reach the caller.
    note_session_call()
    _append_activity(server, tool, ok=ok, tokens=int(tokens_est or 0), detail=detail)


def _append_activity(server: str, tool: str, ok: bool = True, tokens: int = 0,
                     host: "str | None" = None, detail: "str | None" = None) -> None:
    """Append one line to the activity ledger. Never raises.

    One JSON object per line: ``{ts, host, server, tool, ok, tokens}`` plus
    ``detail`` when the caller supplied one (a skill distribution lists the skill
    names it handed out). ``host`` is the client that drove this process (see
    ``set_host``) unless the caller passes one explicitly. This is the record
    `mcptoon activity` and `status` read to answer "was I used, by whom, when,
    and what did it hand over" — the counter in usage.json can say how many, never
    which agent, when, or which capability.
    """
    try:
        entry = {
            "ts": round(time.time(), 3),
            "host": (host if host is not None else _HOST) or None,
            "server": server,
            "tool": tool,
            "ok": bool(ok),
            "tokens": int(tokens or 0),
        }
        if detail:
            entry["detail"] = str(detail)[:400]
        line = json.dumps(entry, ensure_ascii=False)
        path = _activity_file()
        with _usage_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Read-then-rewrite only when trimming: appending is the common path
            # and stays O(1). A cross-process lost line during a trim is the same
            # accepted trade the other counters make (see record_skill_use).
            trimmed = None
            if path.exists():
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as fh:
                        lines = fh.readlines()
                    if len(lines) >= _ACTIVITY_KEEP:
                        trimmed = lines[-(_ACTIVITY_KEEP - 1):]
                except OSError:
                    trimmed = None
            if trimmed is not None:
                with path.open("w", encoding="utf-8") as fh:
                    fh.writelines(trimmed)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def read_activity(limit: int = 20, host: "str | None" = None) -> list[dict]:
    """The most recent ledger entries, newest first. Never raises.

    ``limit`` caps how many come back (a big file must not flood a terminal);
    ``host`` filters to one client when given. A malformed line is skipped, not
    fatal — the ledger is append-only from many processes and one torn write
    from a crash must not hide the rest.
    """
    path = _activity_file()
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if host is not None and entry.get("host") != host:
            continue
        out.append(entry)
        if limit and len(out) >= limit:
            break
    return out


def activity_summary() -> dict:
    """Totals read off the ledger, for `status` and `activity --summary`.

    Everything here comes from the ledger, so it can answer "who and when",
    which the counter cannot. ``last_host`` / ``last_ts`` are the newest line's;
    ``today_calls`` counts entries whose local calendar day is today.
    """
    path = _activity_file()
    if not path.exists():
        return {"entries": 0, "last_host": None, "last_ts": None,
                "today_calls": 0, "hosts": {}}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return {"entries": 0, "last_host": None, "last_ts": None,
                "today_calls": 0, "hosts": {}}
    hosts: dict[str, int] = {}
    entries = 0
    last_host = None
    last_ts = None
    today_calls = 0
    today = time.strftime("%Y-%m-%d")
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            e = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(e, dict):
            continue
        entries += 1
        h = e.get("host") or "CLI"
        hosts[h] = hosts.get(h, 0) + 1
        ts = e.get("ts")
        if isinstance(ts, (int, float)):
            last_ts = ts
            try:
                if time.strftime("%Y-%m-%d", time.localtime(ts)) == today:
                    today_calls += 1
            except (OSError, ValueError, OverflowError):
                pass
        last_host = h
    return {
        "entries": entries,
        "last_host": last_host,
        "last_ts": last_ts,
        "today_calls": today_calls,
        "hosts": dict(sorted(hosts.items(), key=lambda x: -x[1])),
    }


def _lifetime_calls(data: dict) -> int:
    """The lifetime call count, read from the counter that is actually lifetime.

    ``track_call`` keeps two things: a capped rolling window (``calls``, the last
    1000 entries, used for the by-server/by-tool breakdown) and a running counter
    (``total``). Until 2026-09-26 only the window was read, so every surface that
    quotes a call count — ``mcptoon usage``, ``mcptoon status``, ``mcptoon stats``
    and the footer's live figure — froze at exactly 1000 and could never climb
    again. The real count on the author's machine was 3,896 while all four said
    1,000; the footer's change gate then suppressed the whole line, because a
    number that never moves reads as "nothing new to say".

    ``max`` keeps a legacy file (written before ``total`` existed) honest: its
    window is the best count available, and it can never overstate one.
    """
    calls = data.get("calls") or []
    try:
        total = int(data.get("total", 0))
    except (TypeError, ValueError):
        total = 0
    return max(total, len(calls))


def get_usage_stats() -> dict:
    """Get usage statistics.

    ``total_calls`` is the lifetime count (see ``_lifetime_calls``);
    ``window_calls`` is the size of the capped window the breakdowns are computed
    over, and is what ``success_rate`` is measured against. They are equal until
    the 1000th call and diverge after it, on purpose: a success rate over an
    unbounded history would drift less and less, while a lifetime count that stops
    at 1000 is simply wrong.
    """
    data = _load_usage()
    calls = data.get("calls", [])

    by_server: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    total_tokens = 0
    ok_count = 0

    for c in calls:
        sk = f"{c['server']}:{c['tool']}"
        by_server[c["server"]] = by_server.get(c["server"], 0) + 1
        by_tool[sk] = by_tool.get(sk, 0) + 1
        total_tokens += c.get("tokens", 0)
        if c.get("ok"):
            ok_count += 1

    return {
        "total_calls": _lifetime_calls(data),
        "window_calls": len(calls),
        "success_rate": f"{ok_count}/{len(calls)}" if calls else "0/0",
        "total_tokens_est": total_tokens,
        "lifetime_result_tokens": _lifetime_result_tokens(data),
        "by_server": dict(sorted(by_server.items(), key=lambda x: -x[1])),
        "top_tools": dict(sorted(by_tool.items(), key=lambda x: -x[1])[:10]),
        # By-agent breakdown, derived from the activity ledger (the counter window
        # records no host). `usage` could say which *servers* were used but never
        # which *agent* used them; on a multi-agent machine "who is using me" was
        # unanswerable here. Empty when there is no ledger yet.
        "by_host": _by_host(),
    }


def _by_host() -> dict[str, int]:
    """Per-agent call counts from the ledger, newest-host ordering. Never raises."""
    try:
        return activity_summary().get("hosts") or {}
    except Exception:
        return {}


def _lifetime_result_tokens(data: dict) -> int:
    """Lifetime total of tool-result tokens, read from the additive counter.

    Same shape as ``_lifetime_calls``: prefer the running counter
    (``result_tokens``), fall back to the capped window's sum for a file written
    before the counter existed — and take the ``max``, because the two are honest
    in opposite directions during the transition. The counter only starts
    counting on the day it is added, so on that day it is *smaller* than the
    window it sits beside; the window is capped, so later it is the smaller one.
    The lifetime total is at least either of them, so ``max`` is the best
    available estimate and can never overstate the true lifetime figure.
    """
    try:
        total = int(data.get("result_tokens", 0))
    except (TypeError, ValueError):
        total = 0
    window = sum(int(c.get("tokens") or 0) for c in (data.get("calls") or []))
    return max(total, window)


def reset_usage():
    """Clear all usage data — the counter *and* the activity ledger.

    Both are "what has mcptoon done for me", so clearing one and leaving the
    other would make `usage` say 0 while `activity` still listed history.
    """
    _save_usage({"calls": [], "total": 0})
    try:
        _activity_file().unlink(missing_ok=True)
    except OSError:
        pass
