"""GraphQL -> mcptoon adapter (zero-dependency).

Turns any GraphQL endpoint into callable mcptoon commands, the same way
`mcptoon openapi` turns a REST spec into commands:

    mcptoon graphql list    <endpoint> [--search KW] [--json] [--compact]
    mcptoon graphql inspect <endpoint> <command> [--json]
    mcptoon graphql call    <endpoint> <command> [arg=value ...] [--fields "..."]
    mcptoon graphql schema  <endpoint>          # every type, or --json
    mcptoon graphql help

Design constraints that shaped this module
------------------------------------------
* **Zero third-party dependencies.** Only stdlib at module level (`urllib`,
  `json`, `re`, `ssl`, `os`). No `graphql-core`, no code generation.
* **Introspection is a cache, not a hard requirement.** Most endpoints expose
  `__schema`; some disable it in production. When introspection fails,
  `--schema <file>` reads a previously saved introspection JSON so the tool
  still works — the same fallback a hand-written SDL would give.
* **A schema we did not write must not crash the CLI.** Every traversal skips
  malformed nodes (missing `fields`, dangling type refs) instead of raising.
* **Values are serialised to GraphQL literals, never string-interpolated.**
  `_render_literal` quotes strings with JSON escaping, renders enums bare,
  and converts input objects to `{key: value}` — so a value containing `"}`
  cannot break out of the query.

Public surface
--------------
    introspect(endpoint, headers=None, timeout=30, cache_ttl=None, refresh=False) -> dict
    extract_commands(schema) -> list[Operation]
    build_query(op, values, fields, types_by_name) -> str
    call(endpoint, command, values, fields=None, headers=None, timeout=30) -> CallResult
    run(rest, fmt="auto", head_n=0, max_chars=0, full=False) -> None
"""
from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .openapi import to_kebab, _parse_bool

__all__ = [
    "Arg",
    "Operation",
    "CallResult",
    "introspect",
    "extract_commands",
    "build_query",
    "call",
    "run",
]

# The standard introspection query. Nested TypeRef fragments are expanded by
# hand because GraphQL has no recursive fragment; 8 levels covers every real
# schema (list-of-non-null-of-list-of-object is ~5).
_INTROSPECTION_QUERY = """
query IntrospectionQuery {
  __schema {
    queryType { name }
    mutationType { name }
    subscriptionType { name }
    types {
      kind
      name
      description
      fields(includeDeprecated: true) {
        name
        description
        args {
          name
          description
          defaultValue
          type { ...TypeRef }
        }
        type { ...TypeRef }
        isDeprecated
        deprecationReason
      }
      inputFields { name description defaultValue type { ...TypeRef } }
      enumValues(includeDeprecated: true) { name description isDeprecated }
      interfaces { ...TypeRef }
      possibleTypes { ...TypeRef }
    }
  }
}
fragment TypeRef on __Type {
  kind name
  ofType { kind name ofType { kind name ofType { kind name
    ofType { kind name ofType { kind name ofType { kind name
      ofType { kind name ofType { kind name } } } } } } } }
}
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Arg:
    """One argument of a GraphQL field."""

    name: str            # CLI flag name, kebab-case
    wire_name: str       # the name the schema expects
    py_type: type        # int / float / bool / str / dict / list
    required: bool
    description: str
    choices: Optional[list] = None   # enum members, when the arg is an ENUM
    type_name: str = ""              # rendered GraphQL type, e.g. "[String!]!"


@dataclass
class Operation:
    """One callable field (a query or a mutation)."""

    name: str
    kind: str            # "query" | "mutation"
    wire_name: str
    description: str
    args: list = field(default_factory=list)
    return_type: str = ""
    return_ref: dict = field(default_factory=dict)

    def flags_help(self) -> list[str]:
        lines = []
        for a in self.args:
            req = " (required)" if a.required else ""
            extra = ""
            if a.choices:
                extra = f"  one of: {', '.join(a.choices[:8])}"
            lines.append(f"  --{a.name}  {a.type_name}  {a.description}{req}{extra}")
        return lines


@dataclass
class CallResult:
    status: int
    body: Any
    ok: bool
    content_type: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "ok": self.ok,
            "content_type": self.content_type,
            "body": self.body,
        }


# ---------------------------------------------------------------------------
# Type-ref helpers
# ---------------------------------------------------------------------------


def _type_ref_name(ref: Any) -> str:
    """Innermost named type of a TypeRef: `[Country!]!` -> `Country`."""
    seen = 0
    node = ref
    while isinstance(node, dict) and seen < 16:
        if node.get("name"):
            return node["name"]
        node = node.get("ofType")
        seen += 1
    return "Unknown"


def _is_list_ref(ref: Any) -> bool:
    node = ref
    seen = 0
    while isinstance(node, dict) and seen < 16:
        if node.get("kind") == "LIST":
            return True
        node = node.get("ofType")
        seen += 1
    return False


def _render_type_ref(ref: Any) -> str:
    """Human-readable GraphQL type for `inspect`: `[Country!]!`."""
    if not isinstance(ref, dict):
        return "Unknown"
    kind = ref.get("kind")
    if kind == "NON_NULL":
        return _render_type_ref(ref.get("ofType")) + "!"
    if kind == "LIST":
        return "[" + _render_type_ref(ref.get("ofType")) + "]"
    return ref.get("name") or "Unknown"


_SCALAR_PY = {"Int": int, "Float": float, "Boolean": bool, "ID": str, "String": str}


def _arg_shape(arg: dict, types_by_name: dict) -> tuple[type, Optional[list]]:
    """Map an introspection arg to (python type, enum choices)."""
    ref = arg.get("type")
    name = _type_ref_name(ref)
    tdef = types_by_name.get(name, {})
    kind = tdef.get("kind")
    is_list = _is_list_ref(ref)

    if kind == "ENUM":
        choices = [v.get("name") for v in (tdef.get("enumValues") or []) if v.get("name")]
        return (list if is_list else str), choices
    if kind == "INPUT_OBJECT":
        return list if is_list else dict, None
    if kind == "SCALAR":
        base = _SCALAR_PY.get(name, str)
        return (list if is_list else base), None
    # Unknown / OBJECT used as input (shouldn't happen, but be forgiving)
    return str, None


# ---------------------------------------------------------------------------
# Introspection + cache
# ---------------------------------------------------------------------------


def _cache_dir():
    """Reuse mcptoon's cache root, resolved live so `--dir` is honoured."""
    from . import cache as _cache

    return _cache._cache_dir() / "graphql"


def _cache_path(endpoint: str, headers: Optional[dict]) -> Path:
    import hashlib

    key = endpoint
    if headers:
        joined = "&".join(f"{k}={v}" for k, v in sorted(headers.items()))
        key += "\n" + hashlib.sha256(joined.encode()).hexdigest()[:12]
    return _cache_dir() / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")


def _cache_get(endpoint: str, headers: Optional[dict], ttl: int) -> Optional[dict]:
    if ttl <= 0:
        return None
    path = _cache_path(endpoint, headers)
    try:
        if not path.is_file():
            return None
        import time

        if time.time() - path.stat().st_mtime > ttl:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        schema = payload.get("schema") if isinstance(payload, dict) else None
        return schema if isinstance(schema, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _cache_put(endpoint: str, headers: Optional[dict], schema: dict) -> None:
    try:
        path = _cache_path(endpoint, headers)
        path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile
        import time

        payload = {"endpoint": endpoint, "fetched_at": time.time(), "schema": schema}
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, str(path))
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:
        pass


def _post_json(url: str, payload: dict, headers: Optional[dict], timeout: int) -> tuple[int, str, bytes]:
    """POST a JSON body. Returns (status, content_type, raw). Never raises on
    HTTP error status — the caller inspects the body for GraphQL errors."""
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        ctype = exc.headers.get("Content-Type", "") if exc.headers else ""
        return exc.code, ctype, raw


def introspect(
    endpoint: str,
    headers: Optional[dict] = None,
    timeout: int = 30,
    cache_ttl: Optional[int] = None,
    refresh: bool = False,
) -> dict:
    """Fetch and return the endpoint's `__schema` object (cached)."""
    if cache_ttl is None:
        try:
            cache_ttl = int(os.environ.get("MCPTOON_GRAPHQL_TTL", "3600"))
        except ValueError:
            cache_ttl = 3600

    if not refresh:
        cached = _cache_get(endpoint, headers, cache_ttl)
        if cached is not None:
            return cached

    status, ctype, raw = _post_json(
        endpoint, {"query": _INTROSPECTION_QUERY}, headers, timeout
    )
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise ValueError(
            f"endpoint did not return JSON (status {status}, {ctype or 'no type'})"
        ) from None

    if not isinstance(parsed, dict):
        raise ValueError(f"endpoint returned non-object JSON (status {status})")

    errors = parsed.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        msg = first.get("message") if isinstance(first, dict) else str(first)
        raise ValueError(
            f"introspection failed: {msg}. The server may disable __schema; "
            f"save an introspection JSON and pass --schema <file>."
        )

    schema = (parsed.get("data") or {}).get("__schema")
    if not isinstance(schema, dict):
        raise ValueError("introspection returned no __schema")
    if cache_ttl > 0:
        _cache_put(endpoint, headers, schema)
    return schema


def load_schema_file(path: str) -> dict:
    """Load a saved introspection JSON. Accepts either the bare `__schema`
    object or a full introspection response `{"data": {"__schema": ...}}`."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"schema file not found: {path}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("__schema"), dict):
        return data["__schema"]
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        inner = data["data"].get("__schema")
        if isinstance(inner, dict):
            return inner
    if isinstance(data, dict) and "types" in data:
        return data  # already a bare __schema
    raise ValueError("file is not a GraphQL introspection result")


# ---------------------------------------------------------------------------
# Command extraction
# ---------------------------------------------------------------------------


def _types_by_name(schema: dict) -> dict:
    out = {}
    for t in schema.get("types") or []:
        if isinstance(t, dict) and t.get("name"):
            out[t["name"]] = t
    return out


def _ops_from_type(type_name: Optional[str], kind: str, types_by_name: dict) -> list:
    if not type_name:
        return []
    tdef = types_by_name.get(type_name)
    if not isinstance(tdef, dict):
        return []
    ops = []
    used: dict[str, int] = {}
    for f in tdef.get("fields") or []:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        wire = f["name"]
        if wire.startswith("__"):
            continue
        base = to_kebab(wire)
        # Two fields can kebab to the same verb (`getUser` / `get_user`).
        if base in used:
            used[base] += 1
            name = f"{base}-{kind[0]}{used[base]}"
        else:
            used[base] = 0
            name = base
        args = []
        for a in f.get("args") or []:
            if not isinstance(a, dict) or not a.get("name"):
                continue
            py_type, choices = _arg_shape(a, types_by_name)
            args.append(Arg(
                name=to_kebab(a["name"]),
                wire_name=a["name"],
                py_type=py_type,
                required=a.get("defaultValue") is None
                and (a.get("type") or {}).get("kind") == "NON_NULL",
                description=(a.get("description") or "").strip().split("\n")[0],
                choices=choices,
                type_name=_render_type_ref(a.get("type")),
            ))
        ops.append(Operation(
            name=name,
            kind=kind,
            wire_name=wire,
            description=(f.get("description") or "").strip().split("\n")[0],
            args=args,
            return_type=_render_type_ref(f.get("type")),
            return_ref=f.get("type") or {},
        ))
    return ops


def extract_commands(schema: dict) -> list:
    """Every query and mutation, as callable operations."""
    types_by_name = _types_by_name(schema)
    ops = []
    q = (schema.get("queryType") or {}).get("name")
    m = (schema.get("mutationType") or {}).get("name")
    ops.extend(_ops_from_type(q, "query", types_by_name))
    ops.extend(_ops_from_type(m, "mutation", types_by_name))
    return ops


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------


def _render_literal(value: Any, enum: bool = False) -> str:
    """Python value -> GraphQL literal. Strings are JSON-escaped, enums bare."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        if enum:
            # An enum member is a bare name. Guard against injection by only
            # allowing identifier-shaped values here.
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                raise ValueError(f"not a valid enum value: {value!r}")
            return value
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_render_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{k}: {_render_literal(v)}" for k, v in value.items()
        ) + "}"
    return json.dumps(str(value), ensure_ascii=False)


def _coerce_arg(raw: str, arg: Arg) -> Any:
    """CLI string -> python value of the arg's declared type."""
    if arg.py_type is int:
        return int(raw)
    if arg.py_type is float:
        return float(raw)
    if arg.py_type is bool:
        return _parse_bool(raw)
    if arg.py_type in (dict, list):
        parsed = json.loads(raw)  # raises on bad JSON; caller reports it
        if arg.py_type is dict and not isinstance(parsed, dict):
            raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
        if arg.py_type is list and not isinstance(parsed, list):
            raise ValueError(f"expected a JSON array, got {type(parsed).__name__}")
        return parsed
    return raw


def _auto_select(op: Operation, types_by_name: dict) -> str:
    """Pick a sensible default selection set: scalar/enum fields of the return
    type. Falls back to one level of object fields, then `__typename`."""
    ret_name = _type_ref_name(op.return_ref)
    tdef = types_by_name.get(ret_name, {})
    fields = [f for f in (tdef.get("fields") or []) if isinstance(f, dict) and f.get("name")]

    picks = []
    for f in fields:
        leaf = _type_ref_name(f.get("type"))
        if types_by_name.get(leaf, {}).get("kind") in ("SCALAR", "ENUM"):
            picks.append(f["name"])
        if len(picks) >= 12:
            break

    if not picks:
        for f in fields:
            leaf = _type_ref_name(f.get("type"))
            leafdef = types_by_name.get(leaf, {})
            if leafdef.get("kind") == "OBJECT":
                sub = [
                    sf["name"] for sf in (leafdef.get("fields") or [])
                    if types_by_name.get(_type_ref_name(sf.get("type")), {}).get("kind")
                    in ("SCALAR", "ENUM")
                ][:8]
                if sub:
                    picks.append(f"{f['name']} {{ {' '.join(sub)} }}")
                    break
    return " ".join(picks) if picks else "__typename"


def build_query(
    op: Operation,
    values: dict,
    fields: Optional[str],
    types_by_name: dict,
) -> str:
    """Assemble the GraphQL document for one operation."""
    parts = []
    for a in op.args:
        if a.name not in values:
            continue
        lit = _render_literal(values[a.name], enum=bool(a.choices))
        parts.append(f"{a.wire_name}: {lit}")
    arg_str = f"({', '.join(parts)})" if parts else ""

    selection = (fields or "").strip() or _auto_select(op, types_by_name)
    body = f"{op.wire_name}{arg_str} {{ {selection} }}"
    if op.kind == "mutation":
        return f"mutation {{ {body} }}"
    return f"{{ {body} }}"


# ---------------------------------------------------------------------------
# Calling
# ---------------------------------------------------------------------------


def call(
    endpoint: str,
    command: str,
    values: Optional[dict] = None,
    fields: Optional[str] = None,
    headers: Optional[dict] = None,
    timeout: int = 30,
    schema: Optional[dict] = None,
) -> CallResult:
    """Run one query/mutation. Raises KeyError for an unknown command."""
    if schema is None:
        schema = introspect(endpoint, headers=headers, timeout=timeout)
    types_by_name = _types_by_name(schema)
    ops = {op.name: op for op in extract_commands(schema)}
    if command not in ops:
        raise KeyError(
            f"unknown command {command!r}; known: "
            + ", ".join(sorted(ops)[:10]) + (" ..." if len(ops) > 10 else "")
        )
    op = ops[command]

    values = dict(values or {})
    missing = [a.name for a in op.args if a.required and a.name not in values]
    if missing:
        raise ValueError(f"missing required argument(s): {', '.join(missing)}")

    query = build_query(op, values, fields, types_by_name)
    status, ctype, raw = _post_json(endpoint, {"query": query}, headers, timeout)

    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return CallResult(
            status=status, body=raw.decode("utf-8", errors="replace"),
            ok=False, content_type=ctype,
        )

    # GraphQL signals failure *inside* a 200 body via `errors`.
    ok = status < 400 and not (isinstance(parsed, dict) and parsed.get("errors"))
    return CallResult(status=status, body=parsed, ok=ok, content_type=ctype)


# ---------------------------------------------------------------------------
# CLI entry:  mcptoon graphql <action> ...
# ---------------------------------------------------------------------------

_GRAPHQL_USAGE = """\
mcptoon graphql — drive any GraphQL endpoint like an MCP server

Usage:
    mcptoon graphql list    <endpoint> [--json] [--compact] [--search KW]
    mcptoon graphql inspect <endpoint> <command> [--json]
    mcptoon graphql call    <endpoint> <command> [arg=value ...]
                            [--fields "a b { c }"] [--json] [--raw]
                            [--destructive] [--no-check]
    mcptoon graphql schema  <endpoint> [--json]
    mcptoon graphql help

  <endpoint> is the GraphQL HTTP URL, e.g. https://example.com/graphql.
  Arguments are the field's own — run `inspect` to see them. Values are typed
  from the schema: Int/Float/Boolean are bare, String/ID quoted, objects and
  arrays take JSON text.
  `--fields` is a GraphQL selection set; omit it and mcptoon picks the return
  type's scalar fields for you.
  A mutation whose name sounds destructive (delete/drop/…) is refused unless
  --destructive is passed; responses are scanned for prompt injection and
  leaked credentials unless --no-check is passed.

Options:
    --schema <file>   Read a saved introspection JSON instead of __schema
                      (for endpoints that disable introspection).
    --refresh         Re-introspect, ignoring the cache (default TTL 3600s,
                      override with MCPTOON_GRAPHQL_TTL).
    --auth-header K:V  Add a request header. Value may be env:VAR or file:PATH.
"""


def _parse_auth_headers(args: list) -> tuple[list, dict]:
    """Pull `--auth-header K:V` out of args, resolving env:/file: prefixes."""
    rest, headers = [], {}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--auth-header" and i + 1 < len(args):
            raw = args[i + 1]
            i += 2
            key, _, val = raw.partition(":")
            key, val = key.strip(), val.strip()
            if val.startswith("env:"):
                val = os.environ.get(val[4:], "")
            elif val.startswith("file:"):
                val = Path(val[5:]).read_text(encoding="utf-8").strip()
            if key:
                headers[key] = val
            continue
        rest.append(a)
        i += 1
    return rest, headers


def _extract_auth_flag(args: list) -> tuple:
    """Pull `--auth oauth:<name>` (or `--auth=...`) out of args."""
    rest, spec = [], None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--auth" and i + 1 < len(args):
            spec = args[i + 1]
            i += 2
            continue
        if a.startswith("--auth="):
            spec = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    return spec, rest


def _oauth_headers(spec: str) -> dict:
    """Turn `oauth:<name>` (or a bare `<name>`) into a Bearer header."""
    if ":" in spec:
        scheme, _, name = spec.partition(":")
    else:
        scheme, name = "oauth", spec
    if scheme != "oauth":
        raise ValueError(
            f"unsupported --auth scheme {scheme!r}; only 'oauth:<name>' is supported"
        )
    if not name:
        raise ValueError("--auth oauth:<name> is missing the name")
    from . import oauth as _oauth

    return _oauth.headers_for(name)


def _emit_list(schema: dict, args: list, fmt: str) -> None:
    compact = "--compact" in args or fmt == "compact"
    as_json = "--json" in args or fmt == "json"

    search = None
    for i, a in enumerate(args):
        if a == "--search" and i + 1 < len(args):
            search = args[i + 1]
            break
        if a.startswith("--search="):
            search = a.split("=", 1)[1]
            break

    ops = extract_commands(schema)
    if search:
        needle = search.lower()
        ops = [
            op for op in ops
            if needle in op.name.lower() or needle in (op.description or "").lower()
        ]
        if not ops:
            print(f"mcptoon graphql list: nothing matches {search!r}", file=sys.stderr)
            return

    if as_json:
        payload = [
            {
                "name": op.name,
                "kind": op.kind,
                "description": op.description,
                "returns": op.return_type,
                "arguments": [
                    {
                        "name": a.name,
                        "type": a.type_name,
                        "required": a.required,
                        "description": a.description,
                        "choices": a.choices,
                    }
                    for a in op.args
                ],
            }
            for op in ops
        ]
        print(json.dumps(payload, ensure_ascii=False))
        return
    if compact:
        print(" ".join(op.name for op in ops))
        return
    for op in ops:
        tag = "M" if op.kind == "mutation" else "Q"
        print(f"  {op.name:<32} {tag}  {op.description}")


def _emit_inspect(schema: dict, args: list, fmt: str) -> None:
    if not args:
        print("mcptoon graphql inspect: missing <command>", file=sys.stderr)
        sys.exit(1)
    target = to_kebab(args[0])
    ops = {op.name: op for op in extract_commands(schema)}
    op = ops.get(target)
    if op is None:
        print(f"mcptoon graphql inspect: unknown command {target!r}", file=sys.stderr)
        sys.exit(1)
    if "--json" in args or fmt == "json":
        print(json.dumps({
            "name": op.name,
            "kind": op.kind,
            "wire_name": op.wire_name,
            "description": op.description,
            "returns": op.return_type,
            "arguments": [
                {"name": a.name, "type": a.type_name, "required": a.required,
                 "description": a.description, "choices": a.choices}
                for a in op.args
            ],
        }, ensure_ascii=False))
        return
    print(f"{op.name}  {op.kind}  -> {op.return_type}")
    if op.description:
        print(f"  {op.description}")
    if op.args:
        print("  arguments:")
        for line in op.flags_help():
            print(line)
    else:
        print("  (no arguments)")


def _emit_call(endpoint: str, schema: dict, raw_flags: list, fmt: str,
               headers: dict) -> None:
    if not raw_flags:
        print("mcptoon graphql call: missing <command>", file=sys.stderr)
        sys.exit(1)
    command = to_kebab(raw_flags[0])

    as_json = "--json" in raw_flags or fmt == "json"
    as_raw = "--raw" in raw_flags
    is_destructive = "--destructive" in raw_flags
    skip_checks = "--no-check" in raw_flags
    head_limit = 0
    fields = None

    values: dict = {}
    i = 1
    while i < len(raw_flags):
        a = raw_flags[i]
        if a in ("--json", "--raw", "--smart", "--toon", "--mcptoon", "--slim",
                 "--full", "--pretty", "--destructive", "--no-check"):
            i += 1
            continue
        if a == "--head" and i + 1 < len(raw_flags):
            try:
                head_limit = int(raw_flags[i + 1])
            except ValueError:
                print(f"mcptoon graphql call: --head needs an integer, got "
                      f"{raw_flags[i + 1]!r}", file=sys.stderr)
                sys.exit(1)
            i += 2
            continue
        if a.startswith("--head="):
            try:
                head_limit = int(a.split("=", 1)[1])
            except ValueError:
                print("mcptoon graphql call: --head needs an integer", file=sys.stderr)
                sys.exit(1)
            i += 1
            continue
        if a == "--fields" and i + 1 < len(raw_flags):
            fields = raw_flags[i + 1]
            i += 2
            continue
        if a.startswith("--fields="):
            fields = a.split("=", 1)[1]
            i += 1
            continue
        if a.startswith("--"):
            key = a[2:]
            if "=" in key:
                key, _, val = key.partition("=")
                values[to_kebab(key)] = val
                i += 1
            elif i + 1 < len(raw_flags) and not raw_flags[i + 1].startswith("--"):
                values[to_kebab(key)] = raw_flags[i + 1]
                i += 2
            else:
                values[to_kebab(key)] = "true"
                i += 1
        elif "=" in a:
            key, _, val = a.partition("=")
            values[to_kebab(key)] = val
            i += 1
        else:
            i += 1

    # Same safety gate every MCP call goes through.
    from . import router as _router

    if not is_destructive:
        danger = _router._check_dangerous("graphql", command, values)
        if danger:
            print(f"mcptoon graphql call: refused — {danger}. "
                  f"Pass --destructive to run it anyway.", file=sys.stderr)
            sys.exit(1)

    types_by_name = _types_by_name(schema)
    ops = {op.name: op for op in extract_commands(schema)}
    op = ops.get(command)
    if op is None:
        print(f"mcptoon graphql call: unknown command {command!r}", file=sys.stderr)
        sys.exit(1)

    # Coerce each provided value to the declared type before building the query.
    typed: dict = {}
    arg_by_name = {a.name: a for a in op.args}
    for key, raw in values.items():
        arg = arg_by_name.get(key)
        if arg is None:
            # Unknown arg: pass through as a string rather than silently drop.
            typed[key] = raw
            continue
        try:
            typed[key] = _coerce_arg(raw, arg)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"mcptoon graphql call: bad value for --{key}: {exc}",
                  file=sys.stderr)
            sys.exit(1)

    missing = [a.name for a in op.args if a.required and a.name not in typed]
    if missing:
        print(f"mcptoon graphql call: missing required argument(s): "
              f"{', '.join('--' + m for m in missing)}", file=sys.stderr)
        sys.exit(1)

    query = build_query(op, typed, fields, types_by_name)
    try:
        status, ctype, raw = _post_json(endpoint, {"query": query}, headers, timeout=30)
    except (urllib.error.URLError, OSError) as exc:
        print(f"mcptoon graphql call: request failed: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        parsed = raw.decode("utf-8", errors="replace")
    ok = status < 400 and not (isinstance(parsed, dict) and parsed.get("errors"))

    if not skip_checks:
        poison = _router._check_poisoning(parsed)
        if poison:
            print(f"mcptoon graphql call: response may contain prompt injection: "
                  f"{poison}. Pass --no-check to see it anyway.", file=sys.stderr)
            sys.exit(1)
        leak = _router._check_credential_leak(parsed)
        if leak:
            print(f"mcptoon graphql call: response may contain exposed "
                  f"credentials: {leak}. Pass --no-check to see it anyway.",
                  file=sys.stderr)
            sys.exit(1)

    try:
        from . import usage as _usage

        _usage.track_call("graphql", command, ok=ok, payload=parsed)
    except Exception:
        pass

    body = parsed
    if head_limit > 0 and isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, dict):
            for k, v in list(data.items()):
                if isinstance(v, list):
                    data[k] = v[:head_limit]

    if as_raw and isinstance(body, str):
        print(body)
        if not ok:
            sys.exit(1)
        return
    if as_json or isinstance(body, (dict, list)):
        print(json.dumps(
            {"status": status, "ok": ok, "content_type": ctype, "body": body},
            ensure_ascii=False, indent=None,
        ))
        if not ok:
            sys.exit(1)
        return
    print(body)
    if not ok:
        sys.exit(1)


def run(rest: list, fmt: str = "auto", head_n: int = 0, max_chars: int = 0,
        full: bool = False) -> None:
    """CLI entry point for `mcptoon graphql ...`."""
    action = rest[0] if rest else ""
    args = rest[1:]

    if action in ("", "help", "-h", "--help"):
        print(_GRAPHQL_USAGE)
        return

    if action not in ("list", "inspect", "call", "schema"):
        print(f"mcptoon: unknown graphql action {action!r}", file=sys.stderr)
        print(_GRAPHQL_USAGE, file=sys.stderr)
        sys.exit(1)

    args, headers = _parse_auth_headers(args)

    # `--auth oauth:<name>` resolves a stored OAuth token into a header.
    auth_spec, args = _extract_auth_flag(args)
    if auth_spec:
        try:
            headers.update(_oauth_headers(auth_spec))
        except (ValueError, OSError) as exc:
            print(f"mcptoon graphql: {exc}", file=sys.stderr)
            sys.exit(1)

    refresh = "--refresh" in args
    if refresh:
        args = [a for a in args if a != "--refresh"]

    # --schema <file> bypasses introspection entirely.
    schema_file = None
    for i, a in enumerate(args):
        if a == "--schema" and i + 1 < len(args):
            schema_file = args[i + 1]
            args = args[:i] + args[i + 2:]
            break
        if a.startswith("--schema="):
            schema_file = a.split("=", 1)[1]
            args = args[:i] + args[i + 1:]
            break

    if not args:
        print(f"mcptoon graphql {action}: missing <endpoint>", file=sys.stderr)
        sys.exit(1)
    endpoint = args[0]
    remaining = args[1:]

    try:
        if schema_file:
            schema = load_schema_file(schema_file)
        else:
            schema = introspect(endpoint, headers=headers or None, refresh=refresh)
    except (ValueError, FileNotFoundError, urllib.error.URLError, OSError) as exc:
        print(f"mcptoon graphql: {exc}", file=sys.stderr)
        sys.exit(1)

    if action == "list":
        _emit_list(schema, remaining, fmt)
    elif action == "inspect":
        _emit_inspect(schema, remaining, fmt)
    elif action == "schema":
        if "--json" in remaining or fmt == "json":
            print(json.dumps(schema, ensure_ascii=False))
            return
        types_by_name = _types_by_name(schema)
        for name in sorted(types_by_name):
            if name.startswith("__"):
                continue
            t = types_by_name[name]
            print(f"  {name:<32} {t.get('kind')}")
    else:
        _emit_call(endpoint, schema, remaining, fmt, headers)
