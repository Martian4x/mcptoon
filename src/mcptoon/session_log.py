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

"""session_log — what happened in this session, so a compacted agent can catch up.

What this is, and what it is NOT
--------------------------------
The project this mirrors (context-mode) rebuilds working state automatically by
installing **host hooks** — PreToolUse, PostToolUse, PreCompact, SessionStart. On
compaction it injects a session guide back into the model's context without being
asked. **mcptoon cannot do that**, and this module does not pretend to:

* MCP has no "the host is about to compact" event, and no way for a server to
  write into the host's context after the fact. The host owns compaction.
* So there is no automatic injection here. There is no `PreCompact` snapshot
  replayed at `SessionStart`, because neither hook exists on a pure MCP server.

What mcptoon *can* do, and does here: it sits between the agent and every
upstream tool, so it **sees every call that routes through it**. That is enough
to keep an honest event log — what tools ran, what failed, what was written,
what git commands ran — and to hand that log back as a compact "here is where you
were" summary **when the agent asks for it** (``mcptoon session`` or the
``mcptoon_session`` MCP tool).

The difference matters and is stated everywhere it could be misread: this is
**capture + on-demand recall**, not **automatic restore**. An agent that never
calls it gets no benefit; an agent that does gets the same information a
compaction snapshot would carry, minus the automatic part.

Honest limits
-------------
* **Only what routes through the gateway.** A tool the agent calls directly
  (bypassing mcptoon) is invisible here. That is the same set mcptoon can
  compress, so it is a coherent boundary, not an accident.
* **One session per bridge process.** A stdio bridge is one conversation; the
  log is scoped to it and to the project directory it runs in.
* **Classification is heuristic.** "Was this a file write?" is answered by the
  tool's name and arguments, not by understanding the tool. A write tool with an
  unusual name lands in "other". The log says ``other`` rather than guessing.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Events kept per project. A long session is thousands of calls; the point of
#: the log is the *shape* of the session, so old rows are trimmed.
MAX_EVENTS = 5000

#: Bytes of a result summary kept per event. Enough to recognise the call,
#: nowhere near enough to be a second copy of the data.
SUMMARY_CHARS = 240


def _log_dir() -> Path:
    override = os.environ.get("MCPTOON_SESSION_DIR")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("MCPTOON_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".mcptoon"
    return base / "sessions"


def _project_key() -> str:
    """A stable per-project name, so two projects never share a log."""
    cwd = os.environ.get("MCPTOON_SESSION_PROJECT") or os.getcwd()
    try:
        cwd = str(Path(cwd).resolve())
    except Exception:
        pass
    # Readable prefix + short hash: a log you can identify by eye, without
    # colliding on two projects that share a leaf directory name.
    import hashlib
    h = hashlib.sha1(cwd.encode("utf-8", "replace")).hexdigest()[:8]
    leaf = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(cwd).name or "root")[:40]
    return f"{leaf}-{h}"


def _db_path() -> Path:
    return _log_dir() / f"{_project_key()}.sqlite"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=5)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id      INTEGER PRIMARY KEY,
            ts      REAL NOT NULL,
            kind    TEXT NOT NULL,
            server  TEXT NOT NULL DEFAULT '',
            tool    TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            path    TEXT NOT NULL DEFAULT '',
            ok      INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    con.commit()
    return con


# ─── Classification (heuristic, and it says so) ───

_WRITE_HINTS = re.compile(
    r"(write|edit|create|patch|apply|insert|save|update|delete|remove|"
    r"rename|move|mkdir|touch)", re.I)
_READ_HINTS = re.compile(r"(read|get|fetch|list|search|find|grep|glob|query|show)", re.I)
# `git` as a tool-name segment (git_commit, git_status) or a git subcommand in the
# arguments. A plain `\bgit\b` misses "git_commit" because "_" is a word char.
_GIT_TOOL = re.compile(r"(^|[_\-.])git([_\-.]|$)", re.I)
_GIT_ARG = re.compile(r"\bgit\s+(checkout|commit|merge|rebase|stash|push|pull|diff|status|add|reset|branch|log)\b", re.I)

_PATH_KEYS = ("path", "file", "file_path", "filename", "target", "uri", "dir", "directory")


def _path_of(arguments: Any) -> str:
    """The single most likely path in a tool's arguments, or ''.

    Stored in its own column rather than re-parsed from the (truncated) summary
    JSON later — a truncated JSON string does not parse, which is exactly how an
    earlier version reported "files written: []" while writes were happening.
    """
    if not isinstance(arguments, dict):
        return ""
    for key in _PATH_KEYS:
        v = arguments.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:400]
    return ""


def classify(tool: str, arguments: Any, ok: bool) -> str:
    """Best-effort event kind from a tool name + its arguments.

    Returns one of a closed set so a reader (and the tests) can rely on it:
    ``error``, ``git``, ``write``, ``read``, ``other``. ``ok=False`` always wins
    — a failed write is an error first.
    """
    if not ok:
        return "error"
    blob = ""
    try:
        blob = json.dumps(arguments, ensure_ascii=False)[:400] if arguments else ""
    except (TypeError, ValueError):
        blob = str(arguments)[:400]
    if _GIT_TOOL.search(tool) or _GIT_ARG.search(blob):
        return "git"
    if _WRITE_HINTS.search(tool):
        return "write"
    if _READ_HINTS.search(tool):
        return "read"
    return "other"


def _summarise(arguments: Any) -> str:
    if not arguments:
        return ""
    try:
        text = json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(arguments)
    return text[:SUMMARY_CHARS]


# ─── Recording ───

def record(server: str, tool: str, arguments: Any, ok: bool = True) -> None:
    """Append one event. Never raises — logging must not break the call it logs."""
    try:
        con = _connect()
        try:
            con.execute(
                "INSERT INTO events(ts, kind, server, tool, summary, path, ok) "
                "VALUES(?,?,?,?,?,?,?)",
                (time.time(), classify(tool, arguments, ok), server, tool,
                 _summarise(arguments), _path_of(arguments), 1 if ok else 0),
            )
            # Trim in the same transaction: keep the newest MAX_EVENTS.
            con.execute(
                "DELETE FROM events WHERE id NOT IN "
                "(SELECT id FROM events ORDER BY id DESC LIMIT ?)", (MAX_EVENTS,)
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


# ─── Reading back ───

@dataclass
class SessionSummary:
    project: str
    events: int
    files_written: list[str]
    tools_used: list[dict]
    errors: list[dict]
    git_ops: list[str]
    first_ts: float | None
    last_ts: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "events": self.events,
            "filesWritten": self.files_written,
            "toolsUsed": self.tools_used,
            "errors": self.errors,
            "gitOps": self.git_ops,
            "firstTs": self.first_ts,
            "lastTs": self.last_ts,
            "note": ("capture + on-demand recall; mcptoon cannot inject this "
                     "into your context automatically (no host hook) — call it "
                     "after a compaction to catch up"),
        }


def _extract_paths(con: sqlite3.Connection) -> list[str]:
    """File paths from write events, newest first, de-duplicated."""
    paths: list[str] = []
    seen: set[str] = set()
    for (p,) in con.execute(
        "SELECT path FROM events WHERE kind='write' AND path != '' "
        "ORDER BY id DESC LIMIT 200"
    ):
        if p and p not in seen:
            seen.add(p)
            paths.append(p)
    return paths


def summarise(limit: int = 500) -> SessionSummary:
    con = _connect()
    try:
        row = con.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM events").fetchone()
        n, first_ts, last_ts = row[0], row[1], row[2]

        tools = [
            {"tool": t, "server": s, "calls": c}
            for t, s, c in con.execute(
                "SELECT tool, server, COUNT(*) AS c FROM events "
                "GROUP BY tool, server ORDER BY c DESC LIMIT 25")
        ]
        errors = [
            {"tool": t, "summary": sm[:120], "ts": ts}
            for t, sm, ts in con.execute(
                "SELECT tool, summary, ts FROM events WHERE ok=0 "
                "ORDER BY id DESC LIMIT 20")
        ]
        git_ops = [t for (t,) in con.execute(
            "SELECT DISTINCT tool FROM events WHERE kind='git' LIMIT 30")]
        files = _extract_paths(con)
        return SessionSummary(_project_key(), n, files, tools, errors, git_ops,
                              first_ts, last_ts)
    finally:
        con.close()


def purge() -> int:
    con = _connect()
    try:
        n = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        con.execute("DELETE FROM events")
        con.commit()
        return n
    finally:
        con.close()


# ─── CLI ───

_SESSION_USAGE = """mcptoon session — what happened this session, so you can catch up

Usage:
    mcptoon session                 Compact "where was I" summary
    mcptoon session --json          Machine-readable
    mcptoon session --purge         Delete this project's event log

What it is: the gateway sits in front of every tool call that routes through it,
so it can log what ran, what failed, what was written and what git commands ran.
After a compaction, run this to get the same shape a snapshot would carry.

What it is NOT: automatic. mcptoon cannot inject anything into your context
(MCP has no pre-compact hook), so you have to ask. A tool called directly,
bypassing the gateway, is not in this log.
"""


def run(rest: list[str], fmt: str = "auto", head_n: int = 0, max_chars: int = 0,
        full: bool = False) -> None:
    if "--help" in rest or "-h" in rest:
        print(_SESSION_USAGE)
        return
    if "--purge" in rest:
        n = purge()
        print(f"purged {n} event(s) for {_project_key()}")
        return
    s = summarise()
    if fmt == "json":
        print(json.dumps(s.as_dict(), ensure_ascii=False, indent=1))
        return
    if s.events == 0:
        print(f"no events logged for this project ({s.project}).")
        print("The gateway logs calls as they route through it — nothing has yet.")
        return
    print(f"session: {s.project}")
    print(f"events:  {s.events}")
    if s.files_written:
        print("\nfiles written:")
        for p in s.files_written[:15]:
            print(f"  {p}")
    if s.tools_used:
        print("\ntools used:")
        for t in s.tools_used[:10]:
            print(f"  {t['calls']:>4}×  {t['server']}_{t['tool']}")
    if s.errors:
        print("\nunresolved-looking errors (last 20):")
        for e in s.errors[:10]:
            print(f"  {e['tool']}: {e['summary']}")
    if s.git_ops:
        print("\ngit operations: " + ", ".join(s.git_ops[:10]))
    print("\nnote: capture + on-demand recall. mcptoon cannot inject this into "
          "your context automatically — that needs a host hook MCP does not have.")
