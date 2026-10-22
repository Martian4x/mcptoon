"""Tests for the mcp2cli-parity features: manifest sort/top, transport,
base-url, cache-key, complete, bake, and OAuth DCR.

Each block tests behaviour, not implementation: the sort order is observed
through the rendered manifest, the cache-key through distinct files on disk,
bake's secret refusal through the exit code, and DCR through a real fake
registration endpoint (no network — a local http.server thread).
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcptoon import manifest as manifest_mod
from mcptoon import usage as usage_mod


def _capture(fn, *a, **kw):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            fn(*a, **kw)
        except SystemExit:
            pass
    return out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# manifest --sort / --top
# ---------------------------------------------------------------------------


class TestSortManifest(unittest.TestCase):
    def setUp(self):
        self.m = {
            "srv": [
                {"name": "zebra", "description": "z"},
                {"name": "apple", "description": "a"},
                {"name": "mango", "description": "m"},
            ]
        }

    def test_default_keeps_order(self):
        out = manifest_mod.sort_manifest(self.m, "default")
        self.assertEqual([t["name"] for t in out["srv"]],
                         ["zebra", "apple", "mango"])

    def test_alpha(self):
        out = manifest_mod.sort_manifest(self.m, "alpha")
        self.assertEqual([t["name"] for t in out["srv"]],
                         ["apple", "mango", "zebra"])

    def test_unknown_falls_back_to_default(self):
        out = manifest_mod.sort_manifest(self.m, "bogus")
        self.assertEqual([t["name"] for t in out["srv"]],
                         ["zebra", "apple", "mango"])

    def test_usage_orders_by_call_count(self):
        idx = {("srv", "mango"): {"calls": 10, "last_ts": 1.0},
               ("srv", "apple"): {"calls": 3, "last_ts": 5.0}}
        with patch.object(usage_mod, "tool_usage_index", return_value=idx):
            out = manifest_mod.sort_manifest(self.m, "usage")
        self.assertEqual([t["name"] for t in out["srv"]],
                         ["mango", "apple", "zebra"])  # zebra=0 calls, last

    def test_recent_orders_by_timestamp(self):
        idx = {("srv", "zebra"): {"calls": 1, "last_ts": 100.0},
               ("srv", "apple"): {"calls": 1, "last_ts": 50.0}}
        with patch.object(usage_mod, "tool_usage_index", return_value=idx):
            out = manifest_mod.sort_manifest(self.m, "recent")
        self.assertEqual([t["name"] for t in out["srv"]],
                         ["zebra", "apple", "mango"])

    def test_error_server_left_untouched(self):
        m = {"bad": [{"error": "cannot start"}]}
        out = manifest_mod.sort_manifest(m, "alpha")
        self.assertEqual(out["bad"], [{"error": "cannot start"}])

    def test_cap_tools_per_server(self):
        out = manifest_mod.cap_tools_per_server(self.m, 2)
        self.assertEqual(len(out["srv"]), 2)

    def test_cap_zero_is_noop(self):
        out = manifest_mod.cap_tools_per_server(self.m, 0)
        self.assertEqual(len(out["srv"]), 3)


class TestToolUsageIndex(unittest.TestCase):
    def test_folds_calls_and_tracks_last_ts(self):
        data = {"calls": [
            {"server": "s", "tool": "t", "ts": 1.0},
            {"server": "s", "tool": "t", "ts": 5.0},
            {"server": "s", "tool": "u", "ts": 3.0},
        ]}
        with patch.object(usage_mod, "_load_usage", return_value=data):
            idx = usage_mod.tool_usage_index()
        self.assertEqual(idx[("s", "t")]["calls"], 2)
        self.assertEqual(idx[("s", "t")]["last_ts"], 5.0)
        self.assertEqual(idx[("s", "u")]["calls"], 1)

    def test_corrupt_usage_returns_empty(self):
        with patch.object(usage_mod, "_load_usage", side_effect=Exception("boom")):
            self.assertEqual(usage_mod.tool_usage_index(), {})


# ---------------------------------------------------------------------------
# --transport
# ---------------------------------------------------------------------------


class TestTransport(unittest.TestCase):
    def test_valid_transports_accepted(self):
        from mcptoon import client
        for t in ("auto", "sse", "streamable"):
            c = client.MCPClient(http="http://x/mcp", http_transport=t)
            self.assertEqual(c._http_transport, t)

    def test_invalid_rejected(self):
        from mcptoon import client
        with self.assertRaises(ValueError):
            client.MCPClient(http="http://x/mcp", http_transport="bogus")

    def test_accept_header_order(self):
        from mcptoon import client
        self.assertEqual(client.TRANSPORT_PREF_ACCEPT["auto"],
                         "application/json, text/event-stream")
        self.assertTrue(client.TRANSPORT_PREF_ACCEPT["sse"]
                        .startswith("text/event-stream"))
        self.assertEqual(client.TRANSPORT_PREF_ACCEPT["streamable"],
                         "application/json")

    def test_process_default_applies_when_server_has_none(self):
        from mcptoon import client
        old = client.get_http_transport_default()
        try:
            client.set_http_transport("sse")
            with patch.object(client.MCPClient, "initialize", return_value={}):
                pool = client.MCPClientPool({"s": {"transport": "http",
                                                   "url": "http://x/mcp"}})
                c = pool._get_client("s")  # noqa: SLF001 - test reaches internals
            self.assertEqual(c._http_transport, "sse")
        finally:
            client.set_http_transport(old)

    def test_per_server_beats_process_default(self):
        from mcptoon import client
        old = client.get_http_transport_default()
        try:
            client.set_http_transport("sse")
            with patch.object(client.MCPClient, "initialize", return_value={}):
                pool = client.MCPClientPool({"s": {"transport": "http",
                                                   "url": "http://x/mcp",
                                                   "http_transport": "streamable"}})
                c = pool._get_client("s")  # noqa: SLF001
            self.assertEqual(c._http_transport, "streamable")
        finally:
            client.set_http_transport(old)

    def test_set_http_transport_rejects_bad(self):
        from mcptoon import client
        with self.assertRaises(ValueError):
            client.set_http_transport("nope")


# ---------------------------------------------------------------------------
# --cache-key  (openapi)
# ---------------------------------------------------------------------------


class TestCacheKey(unittest.TestCase):
    def test_distinct_keys_give_distinct_files(self):
        from mcptoon import openapi
        p0 = openapi._spec_cache_path("https://x/s.json", None, None)
        p1 = openapi._spec_cache_path("https://x/s.json", None, "v1")
        p2 = openapi._spec_cache_path("https://x/s.json", None, "v2")
        self.assertEqual(len({p0.name, p1.name, p2.name}), 3)

    def test_traversal_key_stays_in_cache_dir(self):
        from mcptoon import openapi
        base = openapi._spec_cache_path("https://x/s.json", None, None)
        evil = openapi._spec_cache_path("https://x/s.json", None,
                                        "../../etc/passwd")
        self.assertEqual(evil.parent, base.parent)

    def test_no_key_matches_old_behaviour(self):
        from mcptoon import openapi
        a = openapi._spec_cache_path("https://x/s.json", None)
        b = openapi._spec_cache_path("https://x/s.json", None, None)
        self.assertEqual(a.name, b.name)


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


class _CompletePool:
    def __init__(self, result=None, raises=None):
        self._r = result
        self._raises = raises
        self.calls = []

    def complete(self, server, ref, argument, value, context=None):
        self.calls.append((server, ref, argument, value))
        if self._raises:
            raise self._raises
        return self._r


class TestCompleteCli(unittest.TestCase):
    def _run(self, pool, argv, fmt="auto"):
        from mcptoon import mcp_extras
        with patch.object(mcp_extras, "_load_servers", return_value={"only": {}}), \
                patch("mcptoon.router._get_pool", return_value=pool):
            return _capture(mcp_extras.run, argv, fmt=fmt)

    def test_prompt_ref(self):
        pool = _CompletePool(result={"completion": {"values": ["San Francisco"],
                                                    "hasMore": False}})
        out, err = self._run(pool, ["complete", "only", "my-prompt:city=San"])
        self.assertIn("San Francisco", out)
        self.assertEqual(pool.calls[0][1:], ("my-prompt", "city", "San"))

    def test_resource_ref_with_scheme(self):
        pool = _CompletePool(result={"completion": {"values": ["x"]}})
        self._run(pool, ["complete", "only", "file:///{path}:path=/a"])
        # ref keeps the :// and the arg separator is the last ':' not in ://
        self.assertEqual(pool.calls[0][1], "file:///{path}")

    def test_prefix_with_equals(self):
        pool = _CompletePool(result={"completion": {"values": []}})
        self._run(pool, ["complete", "only", "p:q=a=b=c"])
        self.assertEqual(pool.calls[0][3], "a=b=c")

    def test_missing_spec(self):
        pool = _CompletePool()
        out, err = self._run(pool, ["complete", "only"])
        self.assertIn("missing", err.lower())

    def test_no_values_message(self):
        pool = _CompletePool(result={"completion": {"values": []}})
        out, err = self._run(pool, ["complete", "only", "p:q=z"])
        self.assertIn("no completions", out)

    def test_capability_error_is_one_line(self):
        pool = _CompletePool(raises=Exception("[-32601] Method not found"))
        out, err = self._run(pool, ["complete", "only", "p:q=z"])
        self.assertIn("does not support", err)
        self.assertNotIn("Traceback", err)


# ---------------------------------------------------------------------------
# bake
# ---------------------------------------------------------------------------


class TestBake(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bake-test-")
        self.state = Path(self.tmp) / "state"
        self.state.mkdir(parents=True)
        self._patch = patch("mcptoon.config.STATE_DIR", self.state)
        self._patch.start()
        from mcptoon import bake
        self.bake = bake

    def tearDown(self):
        self._patch.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_and_list(self):
        out, err = _capture(self.bake.run,
                            ["bake", "create", "pet",
                             "--openapi", "https://x/spec.json"])
        self.assertIn("created", out)
        out, _ = _capture(self.bake.run, ["bake", "list"])
        self.assertIn("pet", out)
        self.assertIn("openapi", out)

    def test_literal_secret_refused(self):
        out, err = _capture(self.bake.run,
                            ["bake", "create", "bad", "--openapi", "https://x/s.json",
                             "--auth-header", "Authorization:Bearer sk-secret"])
        self.assertIn("must reference a secret", err)
        # nothing was written
        self.assertEqual(self.bake.load_bakes(), {})

    def test_env_reference_accepted(self):
        out, err = _capture(self.bake.run,
                            ["bake", "create", "ok", "--openapi", "https://x/s.json",
                             "--auth-header", "Authorization:env:TOKEN"])
        self.assertIn("created", out)
        b = self.bake.load_bakes()["ok"]
        self.assertEqual(b["auth_headers"], ["Authorization:env:TOKEN"])

    def test_file_reference_accepted(self):
        _capture(self.bake.run,
                 ["bake", "create", "f", "--graphql", "https://x/graphql",
                  "--auth-header", "Authorization:file:/run/secrets/tok"])
        b = self.bake.load_bakes()["f"]
        self.assertEqual(b["auth_headers"], ["Authorization:file:/run/secrets/tok"])

    def test_show_masks_a_literal_if_hand_edited(self):
        # Simulate someone hand-editing the file to insert a literal.
        self.bake._save_bakes({"h": {"kind": "openapi", "target": "u",
                                     "auth_headers": ["Authorization:Bearer sk-abcdef123456"]}})
        out, _ = _capture(self.bake.run, ["bake", "show", "h"])
        self.assertNotIn("sk-abcdef123456", out)
        self.assertIn("***", out)

    def test_show_keeps_reference_visible(self):
        _capture(self.bake.run, ["bake", "create", "r", "--openapi", "u",
                                 "--auth-header", "Authorization:env:TOKEN"])
        out, _ = _capture(self.bake.run, ["bake", "show", "r"])
        self.assertIn("env:TOKEN", out)

    def test_duplicate_create_refused(self):
        _capture(self.bake.run, ["bake", "create", "d", "--openapi", "u"])
        out, err = _capture(self.bake.run, ["bake", "create", "d", "--openapi", "u"])
        self.assertIn("already exists", err)

    def test_update_changes_target(self):
        _capture(self.bake.run, ["bake", "create", "u", "--openapi", "old"])
        _capture(self.bake.run, ["bake", "update", "u", "--openapi", "new"])
        self.assertEqual(self.bake.load_bakes()["u"]["target"], "new")

    def test_remove(self):
        _capture(self.bake.run, ["bake", "create", "x", "--openapi", "u"])
        _capture(self.bake.run, ["bake", "remove", "x"])
        self.assertEqual(self.bake.load_bakes(), {})

    def test_install_wrapper_has_no_url_or_secret(self):
        _capture(self.bake.run, ["bake", "create", "w", "--openapi",
                                 "https://secret.example/spec.json",
                                 "--auth-header", "Authorization:env:TOKEN"])
        bindir = Path(self.tmp) / "bin"
        _capture(self.bake.run, ["bake", "install", "w", "--bin", str(bindir)])
        files = list(bindir.iterdir())
        self.assertEqual(len(files), 1)
        body = files[0].read_text(encoding="utf-8")
        self.assertNotIn("secret.example", body)  # no URL
        self.assertNotIn("TOKEN", body)           # no header
        self.assertIn("bake run w", body)         # only the name

    def test_create_needs_a_kind(self):
        out, err = _capture(self.bake.run, ["bake", "create", "nokind"])
        self.assertIn("--openapi", err)

    def test_state_file_is_0600_on_posix(self):
        if os.name == "nt":
            self.skipTest("POSIX permission bits")
        _capture(self.bake.run, ["bake", "create", "p", "--openapi", "u"])
        mode = (self.state / "bakes.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


# ---------------------------------------------------------------------------
# OAuth DCR — real fake registration endpoint
# ---------------------------------------------------------------------------


class _RegHandler(BaseHTTPRequestHandler):
    state = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        _RegHandler.state["body"] = body
        if self.path == "/register":
            payload = json.dumps({"client_id": "cid-123",
                                  "client_secret": "sec-456"}).encode()
        else:
            # token endpoint
            payload = json.dumps({"access_token": "AT", "token_type": "Bearer",
                                  "expires_in": 3600}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class TestDCR(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _RegHandler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_register_client_body(self):
        from mcptoon import oauth
        resp = oauth.register_client(
            f"http://127.0.0.1:{self.port}/register",
            "http://127.0.0.1:8765/callback", client_name="mcptoon")
        self.assertEqual(resp["client_id"], "cid-123")
        body = _RegHandler.state["body"]
        self.assertEqual(body["token_endpoint_auth_method"], "none")
        self.assertEqual(body["redirect_uris"], ["http://127.0.0.1:8765/callback"])
        self.assertEqual(body["grant_types"],
                         ["authorization_code", "refresh_token"])
        self.assertEqual(body["client_name"], "mcptoon")

    def test_register_client_no_client_id_raises(self):
        from mcptoon import oauth
        with patch.object(oauth, "_post_json", return_value={"nope": 1}):
            with self.assertRaises(ValueError):
                oauth.register_client("http://x/register", "http://r/cb")

    def test_register_client_scope_included(self):
        from mcptoon import oauth
        oauth.register_client(f"http://127.0.0.1:{self.port}/register",
                              "http://r/cb", scope="read write")
        self.assertEqual(_RegHandler.state["body"]["scope"], "read write")


class TestBakeZeroDeps(unittest.TestCase):
    def test_bake_module_imports_stdlib_only(self):
        import ast
        from mcptoon import bake
        src = Path(bake.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        allowed = {"json", "os", "re", "sys", "pathlib", "typing", "__future__"}
        bad = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] not in allowed:
                        bad.append(a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue
                if (node.module or "").split(".")[0] not in allowed:
                    bad.append(node.module)
        self.assertEqual(bad, [], f"non-stdlib module-level imports: {bad}")


if __name__ == "__main__":
    unittest.main()
