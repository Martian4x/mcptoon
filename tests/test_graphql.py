"""Tests for the GraphQL adapter.

Most tests run against a hand-built introspection schema (no network). One
live test hits a public endpoint and is skipped unless MCPTOON_LIVE_GRAPHQL=1.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcptoon import graphql


def _capture(fn, *a, **kw):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        fn(*a, **kw)
    return out.getvalue(), err.getvalue()


def _scalar(name):
    return {"kind": "SCALAR", "name": name}


def _ref(name, kind="SCALAR"):
    return {"kind": kind, "name": name}


def _non_null(inner):
    return {"kind": "NON_NULL", "name": None, "ofType": inner}


def _list(inner):
    return {"kind": "LIST", "name": None, "ofType": inner}


def _sample_schema():
    """A small schema covering scalars, enums, lists, objects, mutations."""
    return {
        "queryType": {"name": "Query"},
        "mutationType": {"name": "Mutation"},
        "types": [
            {
                "kind": "OBJECT",
                "name": "Query",
                "fields": [
                    {
                        "name": "hero",
                        "description": "A hero",
                        "args": [
                            {"name": "episode", "description": "which",
                             "defaultValue": None,
                             "type": _non_null(_ref("Episode", "ENUM"))},
                            {"name": "limit", "description": "how many",
                             "defaultValue": None,
                             "type": _ref("Int")},
                        ],
                        "type": _ref("Character", "OBJECT"),
                    },
                    {
                        "name": "heroes",
                        "description": "All heroes",
                        "args": [],
                        "type": _list(_non_null(_ref("Character", "OBJECT"))),
                    },
                ],
            },
            {
                "kind": "OBJECT",
                "name": "Mutation",
                "fields": [
                    {
                        "name": "deleteHero",
                        "description": "Remove a hero",
                        "args": [{"name": "id", "description": "id",
                                  "defaultValue": None,
                                  "type": _non_null(_ref("ID"))}],
                        "type": _ref("Boolean"),
                    },
                    {
                        "name": "createHero",
                        "description": "Add a hero",
                        "args": [{"name": "name", "description": "name",
                                  "defaultValue": None,
                                  "type": _non_null(_ref("String"))}],
                        "type": _ref("Boolean"),
                    },
                ],
            },
            {
                "kind": "OBJECT",
                "name": "Character",
                "fields": [
                    {"name": "id", "args": [], "type": _ref("ID")},
                    {"name": "name", "args": [], "type": _ref("String")},
                    {"name": "age", "args": [], "type": _ref("Int")},
                    {"name": "friend", "args": [],
                     "type": _ref("Character", "OBJECT")},
                ],
            },
            {
                "kind": "ENUM",
                "name": "Episode",
                "enumValues": [
                    {"name": "NEWHOPE"}, {"name": "EMPIRE"}, {"name": "JEDI"},
                ],
            },
            _scalar("String"), _scalar("Int"), _scalar("Float"),
            _scalar("Boolean"), _scalar("ID"),
        ],
    }


class TestTypeRefs(unittest.TestCase):
    def test_render_non_null_list(self):
        ref = _non_null(_list(_non_null(_ref("Country", "OBJECT"))))
        self.assertEqual(graphql._render_type_ref(ref), "[Country!]!")

    def test_type_ref_name_digs_to_innermost(self):
        ref = _non_null(_list(_non_null(_ref("Country", "OBJECT"))))
        self.assertEqual(graphql._type_ref_name(ref), "Country")

    def test_is_list_detects_nested_list(self):
        self.assertTrue(graphql._is_list_ref(_non_null(_list(_ref("X")))))
        self.assertFalse(graphql._is_list_ref(_non_null(_ref("X"))))


class TestExtractCommands(unittest.TestCase):
    def setUp(self):
        self.ops = {op.name: op for op in graphql.extract_commands(_sample_schema())}

    def test_queries_and_mutations_are_both_listed(self):
        self.assertIn("hero", self.ops)
        self.assertIn("delete-hero", self.ops)
        self.assertEqual(self.ops["hero"].kind, "query")
        self.assertEqual(self.ops["delete-hero"].kind, "mutation")

    def test_argument_types_are_inferred(self):
        hero = self.ops["hero"]
        by = {a.name: a for a in hero.args}
        self.assertIs(by["episode"].py_type, str)       # enum -> str
        self.assertEqual(by["episode"].choices, ["NEWHOPE", "EMPIRE", "JEDI"])
        self.assertIs(by["limit"].py_type, int)
        self.assertTrue(by["episode"].required)          # NON_NULL, no default
        self.assertFalse(by["limit"].required)           # nullable

    def test_dunder_fields_are_skipped(self):
        schema = _sample_schema()
        schema["types"][0]["fields"].append(
            {"name": "__typename", "args": [], "type": _ref("String")}
        )
        names = [op.name for op in graphql.extract_commands(schema)]
        self.assertNotIn("__typename", names)

    def test_malformed_nodes_do_not_crash(self):
        broken = {"queryType": {"name": "Query"}, "types": [
            {"kind": "OBJECT", "name": "Query", "fields": [
                "not-a-dict", {"name": ""}, {"name": "ok", "args": ["bad"],
                                            "type": None},
            ]},
        ]}
        ops = graphql.extract_commands(broken)   # must not raise
        self.assertEqual([o.name for o in ops], ["ok"])


class TestRenderLiteral(unittest.TestCase):
    def test_strings_are_json_escaped(self):
        # A quote in the value must not break out of the literal.
        self.assertEqual(graphql._render_literal('a"b'), '"a\\"b"')

    def test_bool_and_null(self):
        self.assertEqual(graphql._render_literal(True), "true")
        self.assertEqual(graphql._render_literal(False), "false")
        self.assertEqual(graphql._render_literal(None), "null")

    def test_enum_is_bare(self):
        self.assertEqual(graphql._render_literal("NEWHOPE", enum=True), "NEWHOPE")

    def test_enum_rejects_injection(self):
        with self.assertRaises(ValueError):
            graphql._render_literal('x) { evil', enum=True)

    def test_list_and_object(self):
        self.assertEqual(graphql._render_literal([1, 2]), "[1, 2]")
        self.assertEqual(graphql._render_literal({"a": 1}), '{a: 1}')


class TestBuildQuery(unittest.TestCase):
    def setUp(self):
        self.schema = _sample_schema()
        self.types = graphql._types_by_name(self.schema)
        self.ops = {op.name: op for op in graphql.extract_commands(self.schema)}

    def test_query_with_args_and_explicit_fields(self):
        q = graphql.build_query(self.ops["hero"],
                                {"episode": "EMPIRE", "limit": 3}, "name id",
                                self.types)
        self.assertEqual(q, '{ hero(episode: EMPIRE, limit: 3) { name id } }')

    def test_mutation_gets_mutation_wrapper(self):
        q = graphql.build_query(self.ops["create-hero"], {"name": "Bob"}, "id",
                                self.types)
        self.assertEqual(q, 'mutation { createHero(name: "Bob") { id } }')

    def test_auto_selection_picks_scalar_fields(self):
        q = graphql.build_query(self.ops["hero"], {"episode": "JEDI"}, None,
                                self.types)
        # Character's scalars are id, name, age — friend is an object, skipped.
        self.assertIn("id", q)
        self.assertIn("name", q)
        self.assertIn("age", q)
        self.assertNotIn("friend", q)

    def test_auto_selection_falls_back_to_typename(self):
        # A return type with no scalar fields must still produce a valid query.
        schema = {"queryType": {"name": "Q"}, "types": [
            {"kind": "OBJECT", "name": "Q", "fields": [
                {"name": "x", "args": [], "type": _ref("O", "OBJECT")}]},
            {"kind": "OBJECT", "name": "O", "fields": [
                {"name": "inner", "args": [], "type": _ref("O", "OBJECT")}]},
        ]}
        ops = {o.name: o for o in graphql.extract_commands(schema)}
        types = graphql._types_by_name(schema)
        q = graphql.build_query(ops["x"], {}, None, types)
        self.assertIn("__typename", q)


class TestIntrospectionCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["MCPTOON_CACHE_DIR"] = self._tmp
        import mcptoon.config as cfg
        self._old = cfg.CACHE_DIR
        cfg.CACHE_DIR = self._tmp
        self._old_ttl = os.environ.pop("MCPTOON_GRAPHQL_TTL", None)

    def tearDown(self):
        os.environ.pop("MCPTOON_CACHE_DIR", None)
        import mcptoon.config as cfg
        cfg.CACHE_DIR = self._old
        if self._old_ttl is not None:
            os.environ["MCPTOON_GRAPHQL_TTL"] = self._old_ttl
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _response(self):
        return json.dumps({"data": {"__schema": _sample_schema()}}).encode()

    def test_second_introspect_hits_cache(self):
        calls = []

        def fake_post(url, payload, headers, timeout):
            calls.append(url)
            return 200, "application/json", self._response()

        with patch.object(graphql, "_post_json", side_effect=fake_post):
            graphql.introspect("https://x.test/graphql")
            graphql.introspect("https://x.test/graphql")
        self.assertEqual(len(calls), 1)

    def test_refresh_bypasses_cache(self):
        calls = []

        def fake_post(url, payload, headers, timeout):
            calls.append(url)
            return 200, "application/json", self._response()

        with patch.object(graphql, "_post_json", side_effect=fake_post):
            graphql.introspect("https://x.test/graphql")
            graphql.introspect("https://x.test/graphql", refresh=True)
        self.assertEqual(len(calls), 2)

    def test_introspection_errors_surface_as_valueerror(self):
        def fake_post(url, payload, headers, timeout):
            return 200, "application/json", json.dumps(
                {"errors": [{"message": "introspection disabled"}]}
            ).encode()

        with patch.object(graphql, "_post_json", side_effect=fake_post):
            with self.assertRaises(ValueError) as ctx:
                graphql.introspect("https://x.test/graphql", refresh=True)
        self.assertIn("introspection", str(ctx.exception).lower())


class TestSchemaFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bare_schema_file(self):
        p = Path(self.tmp) / "s.json"
        p.write_text(json.dumps({"__schema": _sample_schema()}), encoding="utf-8")
        schema = graphql.load_schema_file(str(p))
        self.assertIn("types", schema)

    def test_full_response_file(self):
        p = Path(self.tmp) / "s.json"
        p.write_text(json.dumps({"data": {"__schema": _sample_schema()}}),
                     encoding="utf-8")
        schema = graphql.load_schema_file(str(p))
        self.assertIn("types", schema)

    def test_non_introspection_file_is_rejected(self):
        p = Path(self.tmp) / "s.json"
        p.write_text('{"hello": "world"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            graphql.load_schema_file(str(p))


class TestCall(unittest.TestCase):
    def setUp(self):
        self.schema = _sample_schema()

    def test_call_builds_query_and_parses_response(self):
        captured = {}

        def fake_post(url, payload, headers, timeout):
            captured["query"] = payload["query"]
            return 200, "application/json", json.dumps(
                {"data": {"hero": {"name": "Luke"}}}
            ).encode()

        with patch.object(graphql, "_post_json", side_effect=fake_post):
            res = graphql.call("https://x.test/graphql", "hero",
                               {"episode": "JEDI"}, fields="name",
                               schema=self.schema)
        self.assertTrue(res.ok)
        self.assertIn("hero(episode: JEDI)", captured["query"])
        self.assertEqual(res.body["data"]["hero"]["name"], "Luke")

    def test_graphql_errors_in_200_body_mark_not_ok(self):
        def fake_post(url, payload, headers, timeout):
            return 200, "application/json", json.dumps(
                {"data": None, "errors": [{"message": "boom"}]}
            ).encode()

        with patch.object(graphql, "_post_json", side_effect=fake_post):
            res = graphql.call("https://x.test/graphql", "hero",
                               {"episode": "JEDI"}, fields="name",
                               schema=self.schema)
        self.assertFalse(res.ok)

    def test_missing_required_arg_raises(self):
        with self.assertRaises(ValueError):
            graphql.call("https://x.test/graphql", "hero", {},
                         schema=self.schema)

    def test_unknown_command_raises_keyerror(self):
        with self.assertRaises(KeyError):
            graphql.call("https://x.test/graphql", "nope", {},
                         schema=self.schema)


class TestCliSurface(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.schema_path = Path(self.tmp) / "s.json"
        self.schema_path.write_text(json.dumps({"__schema": _sample_schema()}),
                                    encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_compact(self):
        out, _ = _capture(graphql.run,
                          ["list", "https://x.test/graphql", "--compact",
                           "--schema", str(self.schema_path)])
        self.assertIn("hero", out)
        self.assertIn("delete-hero", out)

    def test_list_search_filters(self):
        out, _ = _capture(graphql.run,
                          ["list", "https://x.test/graphql", "--compact",
                           "--search", "delete", "--schema", str(self.schema_path)])
        self.assertIn("delete-hero", out)
        self.assertNotIn("create-hero", out)
        self.assertNotIn("heroes", out)

    def test_inspect_shows_args(self):
        out, _ = _capture(graphql.run,
                          ["inspect", "https://x.test/graphql", "hero",
                           "--schema", str(self.schema_path)])
        self.assertIn("--episode", out)
        self.assertIn("--limit", out)

    def test_call_destructive_mutation_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            _capture(graphql.run,
                     ["call", "https://x.test/graphql", "delete-hero", "id=1",
                      "--json", "--schema", str(self.schema_path)])
        self.assertEqual(ctx.exception.code, 1)

    def test_help_mentions_all_actions(self):
        out, _ = _capture(graphql.run, ["help"])
        for action in ("list", "inspect", "call", "schema"):
            self.assertIn(action, out)

    def test_unknown_action_exits(self):
        with self.assertRaises(SystemExit):
            _capture(graphql.run, ["bogus"])

    def test_call_network_error_is_clean_not_a_traceback(self):
        with self.assertRaises(SystemExit) as ctx:
            _capture(graphql.run,
                     ["call", "https://x.test/graphql", "create-hero",
                      "name=Bob", "--json", "--schema", str(self.schema_path)])
        # Unreachable endpoint: exit 1, never an uncaught URLError traceback.
        self.assertEqual(ctx.exception.code, 1)


class TestZeroDeps(unittest.TestCase):
    def test_graphql_module_imports_only_stdlib_at_module_level(self):
        import ast
        src = Path(graphql.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        bad = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    top = a.name.split(".")[0]
                    if top not in (
                        "json", "os", "re", "ssl", "sys", "urllib",
                        "dataclasses", "pathlib", "typing",
                    ):
                        bad.append(a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # relative import inside the package
                top = (node.module or "").split(".")[0]
                if top not in (
                    "json", "os", "re", "ssl", "sys", "urllib",
                    "dataclasses", "pathlib", "typing", "__future__",
                ):
                    bad.append(node.module)
        self.assertEqual(bad, [], f"non-stdlib module-level imports: {bad}")


@unittest.skipUnless(os.environ.get("MCPTOON_LIVE_GRAPHQL") == "1",
                     "set MCPTOON_LIVE_GRAPHQL=1 to run the live test")
class TestLiveGraphQL(unittest.TestCase):
    ENDPOINT = "https://graphql-pokemon2.vercel.app/"

    def test_list_inspect_call(self):
        schema = graphql.introspect(self.ENDPOINT, refresh=True)
        ops = {op.name: op for op in graphql.extract_commands(schema)}
        self.assertIn("pokemon", ops)
        res = graphql.call(self.ENDPOINT, "pokemon", {"name": "Pikachu"},
                           fields="name number", schema=schema)
        self.assertTrue(res.ok)
        self.assertEqual(res.body["data"]["pokemon"]["name"], "Pikachu")


if __name__ == "__main__":
    unittest.main()
