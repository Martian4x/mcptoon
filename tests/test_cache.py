# Copyright 2025-2026 activeing123
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the License for the specific language
# governing permissions and limitations under the License.

"""Tests for schema cache content fingerprinting and staleness handling."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from mcptoon import cache as c


def _tools(names_reqs):
    return [{"name": n, "inputSchema": {"type": "object", "required": list(r)}}
            for n, r in names_reqs]


class CacheIsolated(unittest.TestCase):
    """Redirect the cache through the env var the product reads.

    `_CACHE_FILE` / `_LOCK_FILE` used to be module constants and these tests
    patched them directly. They are functions now, because the constants were
    frozen at import and silently ignored `MCPTOON_CACHE_DIR` — so the setting
    that is supposed to relocate the cache moved `config._cache_dir()` and
    nothing else. Isolating by env rather than by patching module attributes is
    also the convention this repo settled on after the destructive-command
    incident: a test that patches only what it happens to know about leaves the
    rest of the real machine reachable.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)
        self.cache_file = self.cache_dir / "schema_cache.json"
        self._env = mock.patch.dict(os.environ, {"MCPTOON_CACHE_DIR": str(self.cache_dir)})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)


class TestFingerprint(CacheIsolated):
    def test_fingerprint_stable_and_content_sensitive(self):
        a = c.tools_fingerprint(_tools([("add", ["a", "b"])]))
        b = c.tools_fingerprint(_tools([("add", ["a", "b"])]))
        self.assertEqual(a, b, "same surface -> same signature")
        # required-arg change flips the signature
        d = c.tools_fingerprint(_tools([("add", ["a"])]))
        self.assertNotEqual(a, d)
        # added tool flips it
        e = c.tools_fingerprint(_tools([("add", ["a", "b"]), ("mul", ["x"])]))
        self.assertNotEqual(a, e)

    def test_set_reports_changed_only_on_surface_change(self):
        tools = _tools([("add", ["a", "b"])])
        self.assertTrue(c.set_cached_tools("s", tools), "first write counts as change")
        self.assertFalse(c.set_cached_tools("s", tools), "identical rewrite -> unchanged")
        self.assertTrue(c.set_cached_tools("s", _tools([("add", ["a", "b"]), ("del", ["id"])])),
                        "adding a tool -> changed")

    def test_fingerprint_stored_on_entry(self):
        # serve's drift logging depends on the fp field persisting in the cache.
        tools = _tools([("add", ["a"])])
        c.set_cached_tools("s", tools)
        entry = c._load_cache()["s"]
        self.assertEqual(entry["fp"], c.tools_fingerprint(tools))
        self.assertIn("tools", entry)
        self.assertIn("ts", entry)

    def test_fingerprint_survives_malformed_tools(self):
        # Real servers sometimes emit null inputSchema / junk entries; the
        # fingerprint must not raise.
        self.assertIsInstance(c.tools_fingerprint([
            {"name": "ok", "inputSchema": None},
            {"name": "no_schema"},
            "not-a-dict",
            {"inputSchema": {"required": None}},
        ]), str)


class TestPerServerStorage(CacheIsolated):
    """The 2026-10-17 split: one file per server, so writers never clobber.

    A single shared `schema_cache.json` read-modify-written whole by every writer
    lost data the moment two processes cached at once (the `serve` gateway + any
    CLI). These pin the two properties that fix it: distinct servers land in
    distinct files, and concurrent writers all survive.
    """

    def test_each_server_gets_its_own_file(self):
        c.set_cached_tools("alpha", _tools([("a", [])]))
        c.set_cached_tools("beta", _tools([("b", [])]))
        subdir = self.cache_dir / "schema_cache"
        files = sorted(p.name for p in subdir.glob("*.json"))
        self.assertEqual(len(files), 2, "two servers must be two files")
        # and the reader still sees both, by name
        self.assertEqual(set(c.load_all_tools()), {"alpha", "beta"})

    def test_legacy_single_file_is_still_read(self):
        # An upgrade must not lose the cache the user already has.
        (self.cache_dir / "schema_cache.json").write_text(
            json.dumps({"old": {"tools": _tools([("x", [])]), "ts": 1, "fp": "z"}}),
            encoding="utf-8")
        self.assertIsNotNone(c.get_cached_tools_any_age("old")[0])
        self.assertEqual(list(c.load_all_tools()), ["old"])

    def test_per_server_file_overrides_legacy_entry(self):
        (self.cache_dir / "schema_cache.json").write_text(
            json.dumps({"s": {"tools": _tools([("old", [])]), "ts": 1, "fp": "a"}}),
            encoding="utf-8")
        c.set_cached_tools("s", _tools([("new", [])]))
        tools = c.get_cached_tools_any_age("s")[0]
        self.assertEqual([t["name"] for t in tools], ["new"],
                         "the fresh per-server file must win over the legacy copy")

    def test_concurrent_writers_do_not_lose_entries(self):
        """The regression: 4 processes × 400 servers must leave 1600, not ~14."""
        repo_src = str(Path(c.__file__).resolve().parents[1])
        worker = textwrap.dedent("""
            import sys
            from mcptoon.cache import set_cached_tools
            wid = sys.argv[1]
            for i in range(150):
                set_cached_tools(f"w{wid}_{i}", [{"name": "t", "inputSchema": {"required": []}}])
        """)
        env = dict(os.environ, PYTHONPATH=repo_src,
                   MCPTOON_CACHE_DIR=str(self.cache_dir), MCPTOON_HOME=str(self.cache_dir))
        procs = [subprocess.Popen([sys.executable, "-c", worker, str(w)], env=env)
                 for w in range(4)]
        for p in procs:
            self.assertEqual(p.wait(), 0)
        got = len(c.load_all_tools())
        self.assertEqual(got, 600, f"expected 600 entries, got {got} (data loss)")


if __name__ == "__main__":
    unittest.main()
