"""OpenAPI → mcptoon adapter (zero-dependency).

Turns any OpenAPI 3.x / Swagger 2.0 spec into callable mcptoon commands, so a
plain REST API can be driven the same way an MCP server is: list what is
available, inspect one operation, call it with flags.

Design constraints that shaped this module
------------------------------------------
* **Zero third-party dependencies.** Only stdlib is imported at module level
  (`urllib`, `json`, `re`, `ssl`). YAML support is *optional*: `yaml` is
  imported inside a `try/except ImportError` block, which
  `scripts/check_zero_deps.py` explicitly allows as an "optional enhancement".
  Without pyyaml, a YAML spec fails with a clear message instead of crashing.
* **A spec we did not write must not crash the CLI.** Every traversal tolerates
  malformed nodes (non-dict path items, missing `name`, dangling `$ref`) by
  skipping them or leaving them unresolved, never by raising.
* **Remote specs are cached, local files are not.** A URL spec is stored under
  mcptoon's cache root for `MCPTOON_OPENAPI_TTL` seconds (default 3600) so
  repeated `list`/`inspect`/`call` do not re-download it. `--refresh` bypasses
  the cache. Nothing is fetched or written at import time.

Public surface
--------------
    load_spec(source, headers=None, timeout=30, cache_ttl=None, refresh=False) -> dict
    resolve_refs(spec) -> dict
    extract_commands(spec) -> list[Operation]
    call(spec, command_name, flags, headers=None, timeout=30) -> CallResult
    run(rest, fmt="auto") -> None        # the `mcptoon openapi ...` entry

Attribution
-----------
The behaviour (operation naming, path/operation parameter merge, nullable
union collapse, request-body negotiation) follows the conventions popularised
by knowsuchagency/mcp2cli (MIT). This is an independent stdlib implementation,
not a copy of that code.
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

__all__ = [
    "Param",
    "Operation",
    "CallResult",
    "load_spec",
    "resolve_refs",
    "extract_commands",
    "call",
    "run",
]

_OPENAPI_METHODS = ("get", "post", "put", "delete", "patch", "head", "options")
# Methods that a user may invoke. HEAD/OPTIONS are listed so they are not
# silently dropped, but they are rarely useful as CLI verbs.
_HTTP_METHOD_SET = frozenset(_OPENAPI_METHODS)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def to_kebab(name: str) -> str:
    """`createUser` / `create_user` / `CreateUser` -> `create-user`.

    Matches the CLI convention mcptoon already uses for its own subcommands,
    so an OpenAPI operation and an MCP tool named the same thing land on the
    same verb.
    """
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    s = s.replace("_", "-").replace(" ", "-")
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-").lower()


def _collapse_nullable(schema: Any) -> dict:
    """Reduce `{"anyOf": [T, {"type": "null"}]}` to `T`.

    Pydantic / FastAPI / FastMCP all emit Optional[T] this way with no
    top-level ``type``. Left alone, every optional field would be typed as a
    string. Outer keys such as ``description`` are preserved.
    """
    if not isinstance(schema, dict) or "type" in schema:
        return schema if isinstance(schema, dict) else {}
    unions = [k for k in ("anyOf", "oneOf") if isinstance(schema.get(k), list)]
    if len(unions) != 1:
        return schema
    branches = schema[unions[0]]
    concrete = [
        b for b in branches if not (isinstance(b, dict) and b.get("type") == "null")
    ]
    if len(branches) != 2 or len(concrete) != 1 or not isinstance(concrete[0], dict):
        return schema
    outer = {k: v for k, v in schema.items() if k != unions[0]}
    return {**concrete[0], **outer}


def _schema_type(schema: dict) -> tuple[type, str]:
    """Map a JSON-schema type to a Python callable type and a hint suffix.

    Returns ``(python_type, suffix)`` where ``suffix`` is appended to the help
    text so the user sees how arrays/objects are expected (as JSON text).
    """
    schema = _collapse_nullable(schema)
    t = schema.get("type")
    if isinstance(t, list):  # e.g. ["string", "null"]
        t = next((x for x in t if x != "null"), None)
    if t == "integer":
        return int, ""
    if t == "number":
        return float, ""
    if t == "boolean":
        return _parse_bool, ""
    if t == "array":
        return str, " (JSON array)"
    if t == "object":
        return str, " (JSON object)"
    # No explicit type: infer from a homogeneous enum so numeric enums stay
    # numeric (argparse would otherwise reject int choices as strings).
    enum = schema.get("enum")
    if isinstance(enum, list) and enum and not any(isinstance(v, bool) for v in enum):
        if all(isinstance(v, int) for v in enum):
            return int, ""
        if all(isinstance(v, (int, float)) for v in enum):
            return float, ""
    return str, ""


def _parse_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _coerce(value: Any, py_type: type) -> Any:
    """Coerce a raw string flag value to the operation's expected type."""
    if value is None:
        return None
    if py_type is int:
        return int(value)
    if py_type is float:
        return float(value)
    if py_type is _parse_bool:
        return _parse_bool(value)
    return value


# ---------------------------------------------------------------------------
# $ref resolution (internal refs only)
# ---------------------------------------------------------------------------


def _json_pointer_tokens(pointer: str) -> list[str]:
    """`components/schemas/Pet` -> ["components", "schemas", "Pet"]."""
    return [
        tok.replace("~1", "/").replace("~0", "~")
        for tok in pointer.split("/")
        if tok != ""
    ]


def _lookup_pointer(root: Any, tokens: list[str]) -> Any:
    node = root
    for tok in tokens:
        if isinstance(node, dict) and tok in node:
            node = node[tok]
        elif isinstance(node, list) and tok.isdigit() and int(tok) < len(node):
            node = node[int(tok)]
        else:
            raise LookupError(tok)
    return node


def resolve_refs(spec: dict) -> dict:
    """Inline every internal ``$ref`` (``#/...``).

    External refs and cycles are left unresolved on purpose: fetching remote
    refs would add a network dependency to a pure function, and a cycle cannot
    be inlined without a memo marker. A dangling ref is reported once and left
    alone — a broken spec should degrade, not explode.
    """
    import copy

    warned: set[str] = set()

    def _resolve(node: Any, root: Any, seen: frozenset) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if ref in seen or not ref.startswith("#/"):
                    return node
                try:
                    target = _lookup_pointer(root, _json_pointer_tokens(ref[2:]))
                except LookupError:
                    if ref not in warned:
                        warned.add(ref)
                        print(
                            f"Warning: unresolvable $ref {ref!r}; leaving unresolved.",
                            file=sys.stderr,
                        )
                    return node
                return _resolve(copy.deepcopy(target), root, seen | {ref})
            return {k: _resolve(v, root, seen) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(item, root, seen) for item in node]
        return node

    return _resolve(copy.deepcopy(spec), spec, frozenset())


# ---------------------------------------------------------------------------
# Spec loading
# ---------------------------------------------------------------------------


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def _http_get(url: str, headers: Optional[dict], timeout: int) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _absolutize_servers(spec: dict, source_url: str) -> None:
    """Resolve relative ``servers[].url`` against the spec's own URL.

    Real specs routinely declare ``servers: [{"url": "/api/v3"}]`` — relative
    to wherever the spec was served from. Left relative, the assembled request
    URL is ``/api/v3/pet/2``, which urllib rejects with "unknown url type".
    Normalising here means every downstream consumer sees an absolute base.
    """
    parsed = urllib.parse.urlsplit(source_url)
    if not parsed.scheme or not parsed.netloc:
        return
    origin = f"{parsed.scheme}://{parsed.netloc}"
    servers = spec.get("servers")
    if not isinstance(servers, list):
        return
    for server in servers:
        if not isinstance(server, dict):
            continue
        url = server.get("url")
        if not isinstance(url, str) or not url:
            continue
        if url.startswith("/"):
            server["url"] = origin + url
        elif "://" not in url:
            # Relative to the spec's directory, e.g. "v1" next to openapi.json.
            base_dir = source_url.rsplit("/", 1)[0]
            server["url"] = base_dir + "/" + url


def _spec_cache_dir():
    """Where resolved specs are cached. Reuses mcptoon's cache root, resolved
    live so `--dir` and `MCPTOON_CACHE_DIR` are honoured (same rule as cache.py)."""
    from . import cache as _cache

    return _cache._cache_dir() / "openapi"


def _spec_cache_path(source: str, headers: Optional[dict],
                     cache_key: Optional[str] = None) -> Path:
    """One file per (url, auth identity, optional cache key).

    The auth part keeps two accounts' specs apart without storing the secret
    itself — only a short hash. ``cache_key`` (``--cache-key``) lets the caller
    keep several variants of the same URL apart on purpose: two API versions
    behind one URL, or a spec that differs by query string the URL does not
    show. It is hashed into the name, never written verbatim, so a key that
    happens to contain a path separator cannot escape the cache directory.
    """
    key_src = source
    if headers:
        # Hash the header names+values so a token change busts the cache, but
        # the file name never contains the secret.
        import hashlib

        joined = "&".join(f"{k}={v}" for k, v in sorted(headers.items()))
        key_src = source + "\n" + hashlib.sha256(joined.encode()).hexdigest()[:12]
    if cache_key:
        key_src = key_src + "\ncachekey:" + cache_key
    import hashlib as _h

    return _spec_cache_dir() / (_h.sha256(key_src.encode()).hexdigest()[:24] + ".json")


def _cache_get(source: str, headers: Optional[dict], ttl: int,
               cache_key: Optional[str] = None) -> Optional[dict]:
    """Return a cached resolved spec, or None on miss/expiry/corruption."""
    if ttl <= 0:
        return None
    path = _spec_cache_path(source, headers, cache_key)
    try:
        if not path.is_file():
            return None
        import time

        age = time.time() - path.stat().st_mtime
        if age > ttl:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        spec = payload.get("spec") if isinstance(payload, dict) else None
        return spec if isinstance(spec, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        # A corrupt cache entry must behave exactly like a miss, never crash.
        return None


def _cache_put(source: str, headers: Optional[dict], spec: dict,
               cache_key: Optional[str] = None) -> None:
    """Best-effort write. A cache failure must never fail the command."""
    try:
        path = _spec_cache_path(source, headers, cache_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile
        import time

        payload = {"source": source, "fetched_at": time.time(), "spec": spec}
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


def load_spec(
    source: str,
    headers: Optional[dict] = None,
    timeout: int = 30,
    cache_ttl: Optional[int] = None,
    refresh: bool = False,
    cache_key: Optional[str] = None,
) -> dict:
    """Load and $ref-resolve an OpenAPI spec from a URL or a local path.

    JSON is always supported. YAML is supported only when PyYAML happens to be
    installed; otherwise a YAML source raises ``ValueError`` with an actionable
    message rather than an ImportError traceback.

    Remote specs are cached under mcptoon's cache root for ``cache_ttl`` seconds
    (default from ``MCPTOON_OPENAPI_TTL`` or 3600). Local files are never
    cached — they are already on disk. ``refresh=True`` bypasses the cache.
    ``cache_key`` namespaces the cache entry when the same URL serves more than
    one spec variant.
    """
    if cache_ttl is None:
        try:
            cache_ttl = int(os.environ.get("MCPTOON_OPENAPI_TTL", "3600"))
        except ValueError:
            cache_ttl = 3600

    if _is_url(source) and not refresh:
        cached = _cache_get(source, headers, cache_ttl, cache_key)
        if cached is not None:
            return cached

    if _is_url(source):
        raw = _http_get(source, headers, timeout)
    else:
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"spec not found: {source}")
        raw = path.read_text(encoding="utf-8")

    try:
        spec = json.loads(raw)
    except json.JSONDecodeError:
        try:
            import yaml  # optional; guarded so zero-deps stays true
        except ImportError:
            raise ValueError(
                "spec is not valid JSON and PyYAML is not installed. "
                "Convert the spec to JSON, or `pip install pyyaml` to read YAML."
            ) from None
        try:
            spec = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            # A file that is neither JSON nor YAML (a README, a typo'd path to
            # some other doc) must produce one clear sentence, not a traceback.
            raise ValueError(
                f"not a JSON or YAML document: {exc.__class__.__name__}"
            ) from None

    if not isinstance(spec, dict) or "paths" not in spec:
        raise ValueError("spec must be a JSON/YAML object containing 'paths'")

    spec = resolve_refs(spec)
    if _is_url(source):
        _absolutize_servers(spec, source)
        # Store the *resolved* spec so a cache hit skips $ref work too.
        if cache_ttl > 0:
            _cache_put(source, headers, spec, cache_key)
    return spec


# ---------------------------------------------------------------------------
# Command extraction
# ---------------------------------------------------------------------------


@dataclass
class Param:
    """One callable parameter of an operation."""

    name: str            # CLI flag name, kebab-case, no leading dashes
    wire_name: str       # the name the API expects
    py_type: type
    required: bool
    description: str
    location: str        # path | query | header | body | file
    choices: Optional[list] = None
    schema: dict = field(default_factory=dict)


@dataclass
class Operation:
    """One callable command derived from a path+method pair."""

    name: str
    method: str
    path: str
    description: str
    params: list = field(default_factory=list)
    has_body: bool = False
    content_type: Optional[str] = None

    def flags_help(self) -> list[str]:
        lines = []
        for p in self.params:
            req = " (required)" if p.required else ""
            lines.append(f"  --{p.name}  {p.description}{req}")
        return lines


@dataclass
class CallResult:
    status: int
    body: Any          # parsed JSON when possible, else raw text
    ok: bool
    content_type: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "ok": self.ok,
            "content_type": self.content_type,
            "body": self.body,
        }


def _merge_parameters(path_level: Any, op_level: Any) -> list[dict]:
    """Path-item parameters apply to every operation; the operation wins on
    a ``(name, in)`` collision but cannot remove an inherited one."""
    merged: dict[tuple[str, str], dict] = {}
    for group in (path_level, op_level):
        if not isinstance(group, list):
            continue
        for param in group:
            if not isinstance(param, dict) or "name" not in param:
                continue
            merged[(param["name"], param.get("in", "query"))] = param
    return list(merged.values())


def _operation_name(path: str, method: str, details: dict) -> str:
    op_id = details.get("operationId")
    if isinstance(op_id, str) and op_id.strip():
        return to_kebab(op_id)
    slug = path.strip("/").replace("/", "-").replace("{", "").replace("}", "")
    return f"{method}-{slug}" if slug else method


def _unique_name(name: str, method: str, reserved: set, used: set) -> str:
    if name not in used:
        return name
    candidate = f"{name}-{method}"
    if candidate not in reserved and candidate not in used:
        return candidate
    suffix = 2
    while True:
        numbered = f"{candidate}-{suffix}"
        if numbered not in reserved and numbered not in used:
            return numbered
        suffix += 1


def extract_commands(spec: dict) -> list[Operation]:
    """Walk ``paths`` and return one Operation per path+method pair."""
    operations: list[Operation] = []
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return operations

    reserved: set[str] = set()
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, details in item.items():
            if method in _HTTP_METHOD_SET and isinstance(details, dict):
                reserved.add(_operation_name(path, method, details))

    used: set[str] = set()
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters")
        for method, details in item.items():
            if method not in _HTTP_METHOD_SET or not isinstance(details, dict):
                continue

            name = _unique_name(
                _operation_name(path, method, details), method, reserved, used
            )
            used.add(name)
            desc = (
                details.get("summary")
                or details.get("description")
                or f"{method.upper()} {path}"
            )
            params: list[Param] = []

            for raw in _merge_parameters(shared, details.get("parameters")):
                schema = _collapse_nullable(raw.get("schema", {}))
                py_type, suffix = _schema_type(schema)
                params.append(
                    Param(
                        name=to_kebab(str(raw["name"])),
                        wire_name=str(raw["name"]),
                        py_type=py_type,
                        required=bool(raw.get("required", False)),
                        description=(raw.get("description") or raw["name"]) + suffix,
                        location=raw.get("in", "query"),
                        choices=schema.get("enum"),
                        schema=schema,
                    )
                )

            body_params, has_body, content_type = _extract_body(details)
            params.extend(body_params)

            operations.append(
                Operation(
                    name=name,
                    method=method,
                    path=path,
                    description=str(desc),
                    params=params,
                    has_body=has_body,
                    content_type=content_type,
                )
            )
    return operations


def _extract_body(details: dict) -> tuple[list[Param], bool, Optional[str]]:
    """Turn a requestBody into flat flags.

    Content negotiation: multipart with any binary field stays multipart (those
    fields become file paths); otherwise application/json is preferred; a
    multipart without binary also works. Nothing else is supported — an
    unsupported body type yields no body flags rather than a wrong request.
    """
    body = details.get("requestBody")
    if not isinstance(body, dict):
        return [], False, None
    content = body.get("content")
    if not isinstance(content, dict):
        return [], False, None

    mp_schema = (content.get("multipart/form-data") or {}).get("schema") or {}
    json_schema = (content.get("application/json") or {}).get("schema") or {}
    mp_props = mp_schema.get("properties") if isinstance(mp_schema, dict) else {}
    mp_props = mp_props if isinstance(mp_props, dict) else {}
    has_binary = any(
        isinstance(p, dict) and p.get("format") == "binary" for p in mp_props.values()
    )

    if has_binary:
        schema, content_type = mp_schema, "multipart/form-data"
    elif isinstance(json_schema, dict) and json_schema.get("properties"):
        schema, content_type = json_schema, None
    elif mp_props:
        schema, content_type = mp_schema, "multipart/form-data"
    else:
        return [], False, None

    if not isinstance(schema, dict):
        return [], False, content_type
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        return [], False, content_type
    required = set(schema.get("required") or [])

    params: list[Param] = []
    for prop_name, prop_schema in properties.items():
        prop_schema = _collapse_nullable(prop_schema)
        if content_type == "multipart/form-data" and prop_schema.get("format") == "binary":
            params.append(
                Param(
                    name=to_kebab(str(prop_name)),
                    wire_name=str(prop_name),
                    py_type=str,
                    required=str(prop_name) in required,
                    description=(prop_schema.get("description") or prop_name)
                    + " (file path)",
                    location="file",
                    choices=prop_schema.get("enum"),
                    schema=prop_schema,
                )
            )
        else:
            py_type, suffix = _schema_type(prop_schema)
            params.append(
                Param(
                    name=to_kebab(str(prop_name)),
                    wire_name=str(prop_name),
                    py_type=py_type,
                    required=str(prop_name) in required,
                    description=(prop_schema.get("description") or prop_name) + suffix,
                    location="body",
                    choices=prop_schema.get("enum"),
                    schema=prop_schema,
                )
            )
    return params, bool(params), content_type


# ---------------------------------------------------------------------------
# Calling
# ---------------------------------------------------------------------------


def _find_base_url(spec: dict) -> str:
    servers = spec.get("servers")
    if isinstance(servers, list) and servers:
        url = servers[0].get("url") if isinstance(servers[0], dict) else None
        if isinstance(url, str) and url:
            return url.rstrip("/")
    # Swagger 2.0
    host = spec.get("host")
    if isinstance(host, str) and host:
        base = spec.get("basePath") or ""
        scheme = (spec.get("schemes") or ["https"])[0]
        return f"{scheme}://{host}{base}".rstrip("/")
    return ""


def _build_request(
    spec: dict,
    op: Operation,
    flags: dict,
    headers: Optional[dict],
    base_url_override: Optional[str] = None,
) -> tuple[str, str, dict, Optional[bytes], Optional[str]]:
    """Assemble (method, url, headers, body_bytes, content_type)."""
    base = base_url_override or _find_base_url(spec)
    path = op.path
    query: list[tuple[str, str]] = []
    hdrs = dict(headers or {})
    body_fields: dict[str, Any] = {}
    files: dict[str, str] = {}

    by_name = {p.name: p for p in op.params}
    for flag_name, value in flags.items():
        p = by_name.get(flag_name)
        if p is None:
            # Unknown flag: pass through as a query param rather than dropping
            # it silently — an API may accept params the spec forgot.
            query.append((flag_name, str(value)))
            continue
        if value is None:
            continue
        if p.location == "path":
            path = path.replace("{" + p.wire_name + "}", urllib.parse.quote(str(value)))
        elif p.location == "header":
            hdrs[p.wire_name] = str(value)
        elif p.location == "body":
            body_fields[p.wire_name] = _coerce(value, p.py_type)
        elif p.location == "file":
            files[p.wire_name] = str(value)
        else:  # query
            if isinstance(value, (list, tuple)):
                for item in value:
                    query.append((p.wire_name, str(item)))
            else:
                query.append((p.wire_name, str(_coerce(value, p.py_type))))

    url = base + path
    if query:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(query)

    body_bytes: Optional[bytes] = None
    content_type = op.content_type

    if files:
        boundary = "----mcptoonBoundary7d91f"
        parts: list[bytes] = []
        for fname, fpath in files.items():
            data = Path(fpath).read_bytes()
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{fname}"; '
                f'filename="{Path(fpath).name}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n".encode()
                + data
                + b"\r\n"
            )
        for k, v in body_fields.items():
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
                f"{v}\r\n".encode()
            )
        body_bytes = b"".join(parts) + f"--{boundary}--\r\n".encode()
        content_type = f"multipart/form-data; boundary={boundary}"
    elif body_fields:
        body_bytes = json.dumps(body_fields).encode("utf-8")
        content_type = content_type or "application/json"

    if content_type:
        hdrs["Content-Type"] = content_type
    if "Accept" not in hdrs:
        hdrs["Accept"] = "application/json"

    return op.method.upper(), url, hdrs, body_bytes, content_type


def call(
    spec: dict,
    command_name: str,
    flags: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = 30,
    base_url: Optional[str] = None,
) -> CallResult:
    """Invoke one operation. Raises KeyError if the command is unknown."""
    ops = {op.name: op for op in extract_commands(spec)}
    if command_name not in ops:
        raise KeyError(
            f"unknown command {command_name!r}; "
            f"known: {', '.join(sorted(ops)[:10])}"
            + (" ..." if len(ops) > 10 else "")
        )
    op = ops[command_name]
    method, url, hdrs, body, _ = _build_request(
        spec, op, flags or {}, headers, base_url
    )

    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        ctype = exc.headers.get("Content-Type", "") if exc.headers else ""
        status = exc.code
        parsed = _maybe_json(raw)
        return CallResult(status=status, body=parsed, ok=False, content_type=ctype)

    return CallResult(status=status, body=_maybe_json(raw), ok=True, content_type=ctype)


def _maybe_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"<{len(raw)} bytes of binary data>"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


# ---------------------------------------------------------------------------
# CLI entry:  mcptoon openapi <action> ...
# ---------------------------------------------------------------------------

_OPENAPI_USAGE = """\
mcptoon openapi — drive any OpenAPI / Swagger REST API like an MCP server

Usage:
    mcptoon openapi list    <spec> [--json] [--compact] [--search KW]
    mcptoon openapi inspect <spec> <command> [--json]
    mcptoon openapi call    <spec> <command> [param=value ...] [--json] [--raw]
                            [--head N] [--destructive] [--no-check]
    mcptoon openapi help

  <spec> is a URL (http/https) or a local .json/.yaml path.
  Parameters are the operation's own — run `inspect` to see them.
  A destructive-sounding operation (delete/drop/…) is refused unless
  --destructive is passed; responses are scanned for prompt injection and
  leaked credentials unless --no-check is passed.

Examples:
    mcptoon openapi list    https://petstore3.swagger.io/api/v3/openapi.json
    mcptoon openapi inspect https://petstore3.swagger.io/api/v3/openapi.json get-pet-by-id
    mcptoon openapi call    https://petstore3.swagger.io/api/v3/openapi.json get-pet-by-id pet-id=2

Auth (repeatable; value supports env: and file: prefixes):
    mcptoon openapi list <spec> --auth-header "Authorization:env:API_TOKEN"
"""


def _parse_auth_headers(args: list[str]) -> tuple[list[str], dict]:
    """Pull `--auth-header K:V` out of args, resolving env:/file: prefixes."""
    headers: dict = {}
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--auth-header" and i + 1 < len(args):
            raw = args[i + 1]
            i += 2
            key, _, val = raw.partition(":")
            key, val = key.strip(), val.strip()
            if val.startswith("env:"):
                import os

                val = os.environ.get(val[4:], "")
            elif val.startswith("file:"):
                val = Path(val[5:]).read_text(encoding="utf-8").strip()
            if key:
                headers[key] = val
            continue
        rest.append(a)
        i += 1
    return rest, headers


def _extract_auth_flag(args: list[str]) -> tuple[Optional[str], list[str]]:
    """Pull `--auth oauth:<name>` (or `--auth=...`) out of args."""
    rest: list[str] = []
    spec: Optional[str] = None
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


def run(rest: list[str], fmt: str = "auto", head_n: int = 0, max_chars: int = 0,
        full: bool = False) -> None:
    """CLI entry point for `mcptoon openapi ...`."""
    action = rest[0] if rest else ""
    args = rest[1:]

    if action in ("", "help", "-h", "--help"):
        print(_OPENAPI_USAGE)
        return

    if action not in ("list", "inspect", "call"):
        print(f"mcptoon: unknown openapi action {action!r}", file=sys.stderr)
        print(_OPENAPI_USAGE, file=sys.stderr)
        sys.exit(1)

    args, headers = _parse_auth_headers(args)

    # `--auth oauth:<name>` resolves a stored OAuth token into a header.
    auth_spec, args = _extract_auth_flag(args)
    if auth_spec:
        try:
            headers.update(_oauth_headers(auth_spec))
        except (ValueError, OSError) as exc:
            print(f"mcptoon openapi: {exc}", file=sys.stderr)
            sys.exit(1)

    # `--refresh` re-downloads the spec, ignoring the cache.
    refresh = "--refresh" in args
    if refresh:
        args = [a for a in args if a != "--refresh"]

    # `--cache-ttl N` / `--cache-key K` / `--base-url U`. All three are
    # mcptoon-level switches, not API parameters, so they are consumed here
    # before the spec is loaded — otherwise they would ride along into the
    # operation's own parameter parsing and be sent to the API.
    cache_ttl: Optional[int] = None
    cache_key: Optional[str] = None
    base_url: Optional[str] = None
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--cache-ttl" and i + 1 < len(args):
            try:
                cache_ttl = int(args[i + 1])
            except ValueError:
                print(f"mcptoon openapi: --cache-ttl needs an integer, "
                      f"got {args[i + 1]!r}", file=sys.stderr)
                sys.exit(1)
            i += 2
            continue
        if a.startswith("--cache-ttl="):
            try:
                cache_ttl = int(a.split("=", 1)[1])
            except ValueError:
                print(f"mcptoon openapi: --cache-ttl needs an integer, "
                      f"got {a.split('=', 1)[1]!r}", file=sys.stderr)
                sys.exit(1)
            i += 1
            continue
        if a == "--cache-key" and i + 1 < len(args):
            cache_key = args[i + 1]
            i += 2
            continue
        if a.startswith("--cache-key="):
            cache_key = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--base-url" and i + 1 < len(args):
            base_url = args[i + 1]
            i += 2
            continue
        if a.startswith("--base-url="):
            base_url = a.split("=", 1)[1]
            i += 1
            continue
        cleaned.append(a)
        i += 1
    args = cleaned

    if not args:
        print(f"mcptoon openapi {action}: missing <spec>", file=sys.stderr)
        sys.exit(1)
    source = args[0]
    remaining = args[1:]

    try:
        spec = load_spec(source, headers=headers or None, refresh=refresh,
                         cache_ttl=cache_ttl, cache_key=cache_key)
    except (ValueError, FileNotFoundError, urllib.error.URLError) as exc:
        print(f"mcptoon openapi: {exc}", file=sys.stderr)
        sys.exit(1)

    if action == "list":
        _emit_list(spec, remaining, fmt)
    elif action == "inspect":
        _emit_inspect(spec, remaining, fmt)
    else:
        _emit_call(spec, remaining, fmt, headers, base_url=base_url)


def _emit_list(spec: dict, args: list[str], fmt: str) -> None:
    compact = "--compact" in args or fmt == "compact"
    as_json = "--json" in args or fmt == "json"

    # `--search <kw>` filters by name or description, case-insensitively.
    # It is a local filter, not a network round-trip.
    search = None
    for i, a in enumerate(args):
        if a == "--search" and i + 1 < len(args):
            search = args[i + 1]
            break
        if a.startswith("--search="):
            search = a.split("=", 1)[1]
            break

    ops = extract_commands(spec)
    if search:
        needle = search.lower()
        ops = [
            op for op in ops
            if needle in op.name.lower() or needle in (op.description or "").lower()
        ]
        if not ops:
            print(f"mcptoon openapi list: nothing matches {search!r}", file=sys.stderr)
            return

    if as_json:
        payload = [
            {
                "name": op.name,
                "description": op.description,
                "method": op.method.upper(),
                "path": op.path,
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.py_type.__name__,
                        "required": p.required,
                        "description": p.description,
                        "location": p.location,
                        "choices": p.choices,
                    }
                    for p in op.params
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
        print(f"  {op.name:<32} {op.method.upper():<6} {op.description}")


def _emit_inspect(spec: dict, args: list[str], fmt: str) -> None:
    if not args:
        print("mcptoon openapi inspect: missing <command>", file=sys.stderr)
        sys.exit(1)
    target = to_kebab(args[0])
    ops = {op.name: op for op in extract_commands(spec)}
    op = ops.get(target)
    if op is None:
        print(f"mcptoon openapi inspect: unknown command {args[0]!r}", file=sys.stderr)
        sys.exit(1)
    if "--json" in args or fmt == "json":
        print(json.dumps({
            "name": op.name,
            "method": op.method.upper(),
            "path": op.path,
            "description": op.description,
            "parameters": [
                {"name": p.name, "type": p.py_type.__name__, "required": p.required,
                 "description": p.description, "location": p.location, "choices": p.choices}
                for p in op.params
            ],
        }, ensure_ascii=False))
        return
    print(f"{op.name}  {op.method.upper()} {op.path}")
    print(f"  {op.description}")
    if op.params:
        print("  options:")
        for line in op.flags_help():
            print(line)
    else:
        print("  (no parameters)")


def _emit_call(spec: dict, args: list[str], fmt: str, headers: dict,
               base_url: Optional[str] = None) -> None:
    if not args:
        print("mcptoon openapi call: missing <command>", file=sys.stderr)
        sys.exit(1)
    command = to_kebab(args[0])
    raw_flags = args[1:]

    ops = {op.name: op for op in extract_commands(spec)}
    op = ops.get(command)
    if op is None:
        print(f"mcptoon openapi call: unknown command {args[0]!r}", file=sys.stderr)
        sys.exit(1)

    # mcptoon's own output switches are not API parameters. They must be
    # consumed here, or they would be forwarded to the remote API as query
    # params — `--smart` on a call once produced `?smart=true` on the wire.
    as_json = "--json" in raw_flags or fmt == "json"
    as_raw = "--raw" in raw_flags
    is_destructive = "--destructive" in raw_flags
    skip_checks = "--no-check" in raw_flags
    head_limit = 0
    flags: dict = {}
    i = 0
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
                print(f"mcptoon openapi call: --head needs an integer, "
                      f"got {raw_flags[i + 1]!r}", file=sys.stderr)
                sys.exit(1)
            i += 2
            continue
        if a.startswith("--head="):
            try:
                head_limit = int(a.split("=", 1)[1])
            except ValueError:
                print(f"mcptoon openapi call: --head needs an integer, "
                      f"got {a.split('=', 1)[1]!r}", file=sys.stderr)
                sys.exit(1)
            i += 1
            continue
        if a.startswith("--"):
            key = a[2:]
            # Both `name value` and `name=value` work.
            if "=" in key:
                key, _, val = key.partition("=")
                flags[key] = val
                i += 1
            elif i + 1 < len(raw_flags) and not raw_flags[i + 1].startswith("--"):
                flags[key] = raw_flags[i + 1]
                i += 2
            else:
                flags[key] = "true"
                i += 1
        elif "=" in a:
            # Bare `name=value` form: keeps the API's own parameter names out
            # of mcptoon's global flag namespace entirely.
            key, _, val = a.partition("=")
            flags[to_kebab(key)] = val
            i += 1
        else:
            i += 1

    # ── The same safety gate every MCP call goes through (router.call_tool) ──
    # This path used to bypass all three layers, so an OpenAPI operation named
    # `delete-user` ran without confirmation and a poisoned response reached
    # the agent unscanned. Reuse router's checks rather than reimplement them,
    # so the two paths cannot drift.
    from . import router as _router

    if not is_destructive:
        danger = _router._check_dangerous("openapi", command, flags)
        if danger:
            print(f"mcptoon openapi call: refused — {danger}. "
                  f"Pass --destructive to run it anyway.", file=sys.stderr)
            sys.exit(1)

    try:
        result = call(spec, command, flags, headers=headers or None,
                      base_url=base_url)
    except KeyError as exc:
        print(f"mcptoon openapi call: {exc}", file=sys.stderr)
        sys.exit(1)

    # ── Response scanning: prompt injection + credential leak ──
    if not skip_checks:
        poison = _router._check_poisoning(result.body)
        if poison:
            print(f"mcptoon openapi call: response may contain prompt "
                  f"injection: {poison}. Pass --no-check to see it anyway.",
                  file=sys.stderr)
            sys.exit(1)
        leak = _router._check_credential_leak(result.body)
        if leak:
            print(f"mcptoon openapi call: response may contain exposed "
                  f"credentials: {leak}. Pass --no-check to see it anyway.",
                  file=sys.stderr)
            sys.exit(1)

    # ── Usage accounting, same ledger the MCP path writes ──
    try:
        from . import usage as _usage

        _usage.track_call("openapi", command, ok=result.ok, payload=result.body)
    except Exception:
        pass  # accounting is a courtesy; never fail a call over it

    body = result.body
    if head_limit > 0 and isinstance(body, list):
        # Same contract as the MCP path: --head slices a top-level array so a
        # huge response can be previewed instead of dumped whole.
        body = body[:head_limit]

    if as_raw and isinstance(body, str):
        print(body)
        return
    if as_json or isinstance(body, (dict, list)):
        print(json.dumps(
            {"status": result.status, "ok": result.ok,
             "content_type": result.content_type, "body": body},
            ensure_ascii=False, indent=None,
        ))
        return
    print(body)
    if not result.ok:
        sys.exit(1)
