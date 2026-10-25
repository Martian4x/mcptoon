# Copyright 2025 activeing123
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
mcptoon cache — Schema cache for tool discovery

Caches tool lists per server to avoid repeated initialize/list_tools round-trips.
TTL: 5 minutes (configurable via MCPTOON_CACHE_TTL env var)

Multi-process safe by construction: **one file per server**. Writing a server's
entry only ever rewrites that server's own file, so two processes caching two
different servers can never overwrite each other. This replaced a single shared
``schema_cache.json`` that every writer read-modify-wrote whole under a
threading.Lock — which is not a *process* lock, so the long-lived `serve` gateway
and a short-lived CLI running at the same time clobbered each other's writes.
Measured 2026-10-17: four processes writing 400 servers each left **14 of 1600**
entries (1586 lost) on the old design. Each per-server write is a unique-temp-file
``os.replace`` (atomic on Windows and POSIX), so a reader never sees a half file.

A legacy single-file ``schema_cache.json`` (from before this split) is still read
for compatibility — merged under the per-server files — so an upgrade does not
lose the cache the user already has. It is never written again; the next refresh
of each server lands in its own file.
"""
import hashlib
import json
import os
import tempfile
import time
import threading
from pathlib import Path

from . import config as _config

_DEFAULT_TTL = 300  # 5 minutes

# In-process lock. Only guards the rare legacy-file rewrite in
# `clear_server_cache`; per-server writes need no lock because each targets its
# own file and lands via atomic replace.
_process_lock = threading.Lock()

_LEGACY_NAME = "schema_cache.json"
_SUBDIR = "schema_cache"


def _cache_dir() -> Path:
    """Cache root, resolved at call time so `--dir` and `MCPTOON_CACHE_DIR` work.

    Deliberately NOT a module constant, and it reads `config.CACHE_DIR` *live*
    rather than binding it at import. `mcptoon --dir X` (and tests) rebind
    `config.CACHE_DIR` after import via `config.apply_home`; a frozen import-time
    copy would keep writing to the real `~/.cache/mcptoon` under an isolated run —
    the bug reported 2026-10-17 where `mcptoon --dir X` leaked cache writes out of
    X. Order: explicit `MCPTOON_CACHE_DIR` env wins, then the (possibly rebound)
    `config.CACHE_DIR`.
    """
    env = os.environ.get("MCPTOON_CACHE_DIR")
    if env:
        return Path(env)
    return Path(_config.CACHE_DIR)


def _legacy_file() -> Path:
    """Pre-split single cache file; read-only compatibility path."""
    return _cache_dir() / _LEGACY_NAME


def _server_file(server: str) -> Path:
    """This server's own cache file. sha1 so any server name is a safe filename."""
    digest = hashlib.sha1(server.encode("utf-8")).hexdigest()
    return _cache_dir() / _SUBDIR / f"{digest}.json"


def _cache_file() -> Path:
    """Deprecated alias kept for callers/tests that referenced the old single file.

    Points at the legacy path; new code uses `_server_file`/`_cache_dir`.
    """
    return _legacy_file()


def _get_ttl() -> int:
    try:
        return int(os.environ.get("MCPTOON_CACHE_TTL", _DEFAULT_TTL))
    except ValueError:
        return _DEFAULT_TTL


def tools_fingerprint(tools: list[dict]) -> str:
    """Content signature for a server's tool set: sorted names + each tool's
    required-param list. Cheap and stable; changes exactly when the callable
    surface changes (tool added/removed, or a tool's required args change),
    which is precisely when a cached manifest becomes unsafe to trust.
    Defensive against malformed servers (null inputSchema, non-dict entries).
    """
    surface = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        req = (t.get("inputSchema") or {}).get("required") or []
        surface.append((t.get("name", "?"), sorted(req)))
    surface.sort()
    blob = json.dumps(surface, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_legacy() -> dict:
    data = _read_json(_legacy_file())
    return data if isinstance(data, dict) else {}


def _load_cache() -> dict:
    """Load the whole cache as ``{server: {"tools", "ts", "fp"}}``.

    Merges the legacy single file (older entries) under the per-server files
    (newer ones). Per-server files win on collision, since they are what current
    writes produce. Never raises — a corrupt file is skipped, not fatal, because
    the cache is an optimisation and a broken cache must not break a command.
    """
    cache: dict = {}
    for name, entry in _read_legacy().items():
        if isinstance(entry, dict):
            cache[name] = entry
    subdir = _cache_dir() / _SUBDIR
    if subdir.is_dir():
        for path in subdir.glob("*.json"):
            entry = _read_json(path)
            if not entry:
                continue
            name = entry.get("server")
            if not name:
                continue
            cache[name] = {k: entry[k] for k in ("tools", "ts", "fp") if k in entry}
    return cache


def load_all_tools() -> dict:
    """``{server: [tool, ...]}`` for every cached server that has tools.

    The one reader for "give me every cached schema" (bench, bench_tokens). It
    goes through the same backend as the live commands, so a layout change can
    never leave a measurement reading a file that no longer exists.
    """
    return {name: entry["tools"] for name, entry in _load_cache().items()
            if isinstance(entry, dict) and entry.get("tools")}


def _entry_for(server: str) -> dict | None:
    """One server's entry: its own file first, else the legacy file."""
    entry = _read_json(_server_file(server))
    if isinstance(entry, dict):
        return entry
    legacy = _read_legacy()
    entry = legacy.get(server)
    return entry if isinstance(entry, dict) else None


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON to `path` atomically via a *unique* temp file + os.replace.

    The temp name is unique per call (mkstemp), so concurrent writers — even in
    the same process — never share a scratch file. os.replace is atomic on both
    Windows and POSIX, so a reader sees either the old file or the new one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, str(path))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def get_cached_tools(server: str) -> list[dict] | None:
    """Get cached tools for a server, or None if cache miss/expired."""
    entry = _entry_for(server)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > _get_ttl():
        return None
    return entry.get("tools", [])


def get_cached_tools_any_age(server: str) -> tuple[list[dict] | None, float | None]:
    """Cached tools for a server even when the TTL has expired.

    Returns (tools, age_seconds), or (None, None) when the server was never
    cached. This exists for the one caller that must never wait on a server:
    the per-turn footer. `get_cached_tools` deliberately returns None for stale
    entries so that callers refresh — correct for `status`/`manifest`, fatal for
    a footer, because a stale cache means spawning every configured server
    (~23s here). A footer that sometimes takes 23s is worse than no footer, so
    it reads whatever is on disk and says how old it is instead of refreshing.
    """
    entry = _entry_for(server)
    if not entry:
        return None, None
    return entry.get("tools", []), time.time() - entry.get("ts", 0)


def set_cached_tools(server: str, tools: list[dict]) -> bool:
    """Cache tools for a server (one file per server, atomic write).

    Stores a content fingerprint alongside the tools and returns True when the
    server's *callable surface* (tool names + required args) actually changed
    versus what was cached, False when it is identical. `serve` uses that
    signal to log MCP tool drift across manifest refreshes without diffing
    payloads itself.

    Writes only this server's own file, so it is safe against a concurrent
    writer caching a *different* server (the `serve` gateway + a CLI at once).
    """
    fp = tools_fingerprint(tools)
    old = _entry_for(server)
    old_fp = old.get("fp") if old else None
    _atomic_write(_server_file(server), {
        "server": server, "tools": tools, "ts": time.time(), "fp": fp,
    })
    return old_fp != fp


def clear_cache():
    """Clear all cached tools (per-server files + the legacy file)."""
    with _process_lock:
        try:
            _legacy_file().unlink()
        except FileNotFoundError:
            pass
        subdir = _cache_dir() / _SUBDIR
        if subdir.is_dir():
            for path in subdir.glob("*.json"):
                try:
                    path.unlink()
                except OSError:
                    pass


def clear_server_cache(server: str):
    """Clear cached tools for a specific server."""
    with _process_lock:
        try:
            _server_file(server).unlink()
        except FileNotFoundError:
            pass
        # Also drop it from the legacy file, if it lives there (pre-split entry).
        legacy = _read_legacy()
        if server in legacy:
            del legacy[server]
            _atomic_write(_legacy_file(), legacy)
