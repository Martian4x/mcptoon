"""Tests for MCP resources and prompts support.

Two layers:
  * client-level: MCPClient.list_resources / read_resource / list_prompts /
    get_prompt send the right JSON-RPC method names and return the right shape.
  * CLI-level: `mcptoon resources ...` / `mcptoon prompts ...` route correctly,
    infer a single server, and turn a missing capability into one clear line.

No real server is required: the pool is replaced with a stub.
"""
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcptoon import mcp_extras


def _capture(fn, *a, **kw):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            fn(*a, **kw)
        except SystemExit:
            pass
    return out.getvalue(), err.getvalue()


class _StubPool:
    """Records calls and returns canned MCP shapes."""

    def __init__(self):
        self.calls = []

    def list_resources(self, server):
        self.calls.append(("list_resources", server))
        return [
            {"uri": "memory://kg", "name": "knowledge-graph",
             "description": "The graph", "mimeType": "application/json"},
            {"uri": "memory://notes", "name": "notes",
             "description": "Scratch notes", "mimeType": "text/plain"},
        ]

    def list_resource_templates(self, server):
        self.calls.append(("list_resource_templates", server))
        return [{"uriTemplate": "file:///{path}", "name": "file"}]

    def read_resource(self, server, uri):
        self.calls.append(("read_resource", server, uri))
        return {"contents": [{"uri": uri, "mimeType": "application/json",
                              "text": '{"entities": []}'}]}

    def list_prompts(self, server):
        self.calls.append(("list_prompts", server))
        return [
            {"name": "fetch", "description": "Fetch a URL"},
            {"name": "summarize", "description": "Summarize text"},
        ]

    def get_prompt(self, server, prompt, arguments=None):
        self.calls.append(("get_prompt", server, prompt, arguments))
        return {"messages": [{"role": "user",
                              "content": {"type": "text",
                                          "text": "Summarize: hello"}}]}


class _NoResourcesPool(_StubPool):
    def list_resources(self, server):
        raise Exception("[-32601] Method not found: resources/list")


class _CliBase(unittest.TestCase):
    def setUp(self):
        self.pool = _StubPool()
        self._patches = [
            patch.object(mcp_extras, "_load_servers",
                         return_value={"only": {"transport": "stdio"}}),
            patch("mcptoon.router._get_pool", return_value=self.pool),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()


class TestResourcesCli(_CliBase):
    def test_list_infers_single_server(self):
        out, _ = _capture(mcp_extras.run, ["resources", "list"])
        self.assertIn("memory://kg", out)
        self.assertIn("memory://notes", out)
        self.assertIn(("list_resources", "only"), self.pool.calls)

    def test_list_json(self):
        out, _ = _capture(mcp_extras.run, ["resources", "list", "--json"])
        data = json.loads(out)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["uri"], "memory://kg")

    def test_list_search_by_uri(self):
        out, _ = _capture(mcp_extras.run,
                          ["resources", "list", "--search", "notes"])
        self.assertIn("memory://notes", out)
        self.assertNotIn("memory://kg", out)

    def test_list_search_no_match(self):
        out, _ = _capture(mcp_extras.run,
                          ["resources", "list", "--search", "zzz"])
        self.assertIn("no resources matching", out)

    def test_templates(self):
        out, _ = _capture(mcp_extras.run, ["resources", "templates"])
        self.assertIn("file:///{path}", out)

    def test_read_json(self):
        out, _ = _capture(mcp_extras.run,
                          ["resources", "read", "only", "memory://kg", "--json"])
        data = json.loads(out)
        self.assertEqual(data["contents"][0]["text"], '{"entities": []}')

    def test_read_raw_prints_only_text(self):
        # Regression: --raw is consumed by the global parser and arrives as
        # fmt="raw"; the module must honour that, not just a literal in args.
        out, _ = _capture(mcp_extras.run,
                          ["resources", "read", "only", "memory://kg"],
                          fmt="raw")
        self.assertEqual(out.strip(), '{"entities": []}')
        self.assertNotIn("contents", out)

    def test_read_raw_via_literal_flag(self):
        out, _ = _capture(mcp_extras.run,
                          ["resources", "read", "only", "memory://kg", "--raw"])
        self.assertEqual(out.strip(), '{"entities": []}')

    def test_binary_blob_under_text_stdout_does_not_crash(self):
        # Regression: --raw on a binary resource wrote to sys.stdout.buffer,
        # which does not exist when stdout is a StringIO (captured output,
        # a pipe wrapper). That raised AttributeError and exited 1. It must
        # fall back to base64 instead.
        import base64
        blob = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
        pool = _StubPool()
        pool.read_resource = lambda s, u: {"contents": [
            {"uri": "img://x", "mimeType": "image/png", "blob": blob}]}
        with patch("mcptoon.router._get_pool", return_value=pool):
            out, err = _capture(mcp_extras.run,
                                ["resources", "read", "only", "img://x"],
                                fmt="raw")
        self.assertNotIn("AttributeError", err)
        self.assertNotIn("buffer", err)
        self.assertEqual(out.strip(), blob)  # base64 fallback


class TestResourcesCapabilityError(unittest.TestCase):
    def setUp(self):
        self.pool = _NoResourcesPool()
        self.p = patch("mcptoon.router._get_pool", return_value=self.pool)
        self.p.start()
        self.p2 = patch.object(mcp_extras, "_load_servers",
                               return_value={"only": {}})
        self.p2.start()

    def tearDown(self):
        self.p.stop()
        self.p2.stop()

    def test_missing_capability_is_one_clear_line(self):
        out, err = _capture(mcp_extras.run, ["resources", "list"])
        self.assertIn("does not support resources", err)
        self.assertNotIn("Traceback", err)


class TestPromptsCli(_CliBase):
    def test_list(self):
        out, _ = _capture(mcp_extras.run, ["prompts", "list"])
        self.assertIn("fetch", out)
        self.assertIn("summarize", out)

    def test_list_json(self):
        out, _ = _capture(mcp_extras.run, ["prompts", "list", "--json"])
        data = json.loads(out)
        self.assertEqual([p["name"] for p in data], ["fetch", "summarize"])

    def test_list_search(self):
        out, _ = _capture(mcp_extras.run, ["prompts", "list", "--search", "summ"])
        self.assertIn("summarize", out)
        self.assertNotIn("fetch ", out)

    def test_get_passes_arguments(self):
        out, _ = _capture(mcp_extras.run,
                          ["prompts", "get", "only", "summarize",
                           "text=hello", "--json"])
        data = json.loads(out)
        self.assertEqual(data["messages"][0]["content"]["text"], "Summarize: hello")
        # the argument map reached the pool
        call = [c for c in self.pool.calls if c[0] == "get_prompt"][0]
        self.assertEqual(call[3], {"text": "hello"})

    def test_get_raw_prints_message_text(self):
        out, _ = _capture(mcp_extras.run,
                          ["prompts", "get", "only", "summarize", "text=hi"],
                          fmt="raw")
        self.assertEqual(out.strip(), "Summarize: hello")


class TestServerSelection(unittest.TestCase):
    def test_multiple_servers_without_name_fails(self):
        with patch.object(mcp_extras, "_load_servers",
                          return_value={"a": {}, "b": {}}):
            out, err = _capture(mcp_extras.run, ["resources", "list"])
        self.assertIn("multiple servers", err)

    def test_no_servers_fails(self):
        with patch.object(mcp_extras, "_load_servers", return_value={}):
            out, err = _capture(mcp_extras.run, ["resources", "list"])
        self.assertIn("no servers configured", err)


class TestClientMethods(unittest.TestCase):
    """The client must send the exact JSON-RPC method names from the spec."""

    def _client(self):
        from mcptoon.client import MCPClient
        c = MCPClient(http="http://x.test/mcp")
        c._initialized = True
        return c

    def test_list_resources_method(self):
        c = self._client()
        seen = {}

        def fake_request(method, params):
            seen["method"] = method
            return {"resources": [{"uri": "a://b"}]}

        with patch.object(c, "_request", side_effect=fake_request):
            out = c.list_resources()
        self.assertEqual(seen["method"], "resources/list")
        self.assertEqual(out, [{"uri": "a://b"}])

    def test_list_resource_templates_method(self):
        c = self._client()
        seen = {}

        def fake_request(method, params):
            seen["method"] = method
            return {"resourceTemplates": [{"uriTemplate": "x://{y}"}]}

        with patch.object(c, "_request", side_effect=fake_request):
            out = c.list_resource_templates()
        self.assertEqual(seen["method"], "resources/templates/list")
        self.assertEqual(out, [{"uriTemplate": "x://{y}"}])

    def test_read_resource_method_and_params(self):
        c = self._client()
        seen = {}

        def fake_request(method, params):
            seen["method"] = method
            seen["params"] = params
            return {"contents": [{"uri": "a://b", "text": "hi"}]}

        with patch.object(c, "_request", side_effect=fake_request):
            out = c.read_resource("a://b")
        self.assertEqual(seen["method"], "resources/read")
        self.assertEqual(seen["params"], {"uri": "a://b"})
        self.assertEqual(out["contents"][0]["text"], "hi")

    def test_list_prompts_method(self):
        c = self._client()
        seen = {}

        def fake_request(method, params):
            seen["method"] = method
            return {"prompts": [{"name": "p"}]}

        with patch.object(c, "_request", side_effect=fake_request):
            out = c.list_prompts()
        self.assertEqual(seen["method"], "prompts/list")
        self.assertEqual(out, [{"name": "p"}])

    def test_get_prompt_method_and_params(self):
        c = self._client()
        seen = {}

        def fake_request(method, params):
            seen["method"] = method
            seen["params"] = params
            return {"messages": []}

        with patch.object(c, "_request", side_effect=fake_request):
            c.get_prompt("p", {"a": "1"})
        self.assertEqual(seen["method"], "prompts/get")
        self.assertEqual(seen["params"], {"name": "p", "arguments": {"a": "1"}})

    def test_get_prompt_without_arguments_omits_key(self):
        c = self._client()
        seen = {}

        def fake_request(method, params):
            seen["params"] = params
            return {"messages": []}

        with patch.object(c, "_request", side_effect=fake_request):
            c.get_prompt("p")
        self.assertEqual(seen["params"], {"name": "p"})


class TestZeroDeps(unittest.TestCase):
    def test_mcp_extras_imports_only_stdlib_at_module_level(self):
        import ast
        src = Path(mcp_extras.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        allowed = {"base64", "json", "sys", "typing", "__future__"}
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
