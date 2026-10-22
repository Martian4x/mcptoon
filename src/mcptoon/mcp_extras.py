"""MCP resources and prompts as CLI commands (zero-dependency).

The MCP spec has three pillars: tools, resources, and prompts. mcptoon has
always driven tools; this module adds the other two, so a server that exposes
data as *resources* or instructions as *prompts* is fully usable — not just the
part that happens to be a tool.

    mcptoon resources list   [server] [--json] [--search KW]
    mcptoon resources templates [server] [--json]
    mcptoon resources read   <server> <uri> [--json] [--raw]
    mcptoon prompts list     [server] [--json] [--search KW]
    mcptoon prompts get      <server> <name> [arg=value ...] [--json]
    mcptoon prompts help

Design constraints that shaped this module
------------------------------------------
* **Raw protocol shapes, not flattened content.** A resource read returns a
  `contents` array that may carry text OR a base64 blob plus a mimeType.
  Flattening it (as `call` does for tools) would throw away the mimeType and
  uri, so this module keeps the result object intact and lets the caller
  decide. `--raw` prints just the text for the common text case.
* **A server that lacks the capability is not an error.** `resources/list`
  against a tools-only server raises METHOD_NOT_FOUND; we catch it and print
  one clear sentence instead of a traceback.
* **Server is optional when there is only one.** With a single configured
  server, `mcptoon resources list` works without naming it.

Public surface
--------------
    run(rest, fmt="auto", head_n=0, max_chars=0, full=False) -> None
"""
from __future__ import annotations

import base64
import json
import sys
from typing import Optional

__all__ = ["run"]


_USAGE = """\
mcptoon resources / prompts — the other two MCP pillars (besides tools)

Usage:
    mcptoon resources list      [server] [--json] [--search KW]
    mcptoon resources templates [server] [--json]
    mcptoon resources read      <server> <uri> [--json] [--raw]
    mcptoon prompts list        [server] [--json] [--search KW]
    mcptoon prompts get         <server> <name> [arg=value ...] [--json] [--raw]

  [server] is optional when exactly one server is configured.
  <uri>    is a resource URI (see `resources list`); templates show the shape
           of parameterised URIs.
  <name>   is a prompt name (see `prompts list`). Arguments are the prompt's
           own — `prompts list --json` shows them.

Examples:
    mcptoon resources list
    mcptoon resources read files file:///etc/hostname
    mcptoon prompts list --search summarize
    mcptoon prompts get my-server summarize topic=AI style=brief
"""


def _load_servers() -> dict:
    from .config import load_config

    return load_config() or {}


def _pick_server(args: list, servers: dict) -> tuple[Optional[str], list]:
    """Take the server name off the front of args.

    Returns (server_name, remaining_args). If the first arg is not a known
    server and exactly one server is configured, the server is inferred and
    the arg is left in place (so `resources list --json` works with one server).
    """
    from .config import resolve_server_name

    if args and not args[0].startswith("--"):
        candidate = resolve_server_name(args[0])
        if candidate in servers:
            return candidate, args[1:]
    if len(servers) == 1:
        return next(iter(servers)), args
    return None, args


def _fail(msg: str) -> None:
    print(f"mcptoon: {msg}", file=sys.stderr)
    sys.exit(1)


def _capability_error(exc: Exception, kind: str) -> str:
    """Turn a protocol error into one actionable sentence."""
    text = str(exc)
    low = text.lower()
    if "method not found" in low or "not found" in low or "-32601" in low:
        return (f"this server does not support {kind} "
                f"(it may be a tools-only server)")
    if "unknown_server" in low:
        return text
    return text


def _emit(obj, as_json: bool, raw: bool = False) -> None:
    if as_json or isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=None))
    else:
        print(obj)


# ---------------------------------------------------------------------------
# resources
# ---------------------------------------------------------------------------


def _cmd_resources(action: str, args: list, fmt: str) -> None:
    from . import router

    servers = _load_servers()
    if not servers:
        _fail("no servers configured; run `mcptoon add` or `mcptoon discover` first")

    server, args = _pick_server(args, servers)
    if server is None:
        _fail("multiple servers configured; name one: "
              f"mcptoon resources {action} <server> ...")

    as_json = "--json" in args or fmt == "json"
    pool = router._get_pool()

    if action == "list":
        search = None
        for i, a in enumerate(args):
            if a == "--search" and i + 1 < len(args):
                search = args[i + 1]
            elif a.startswith("--search="):
                search = a.split("=", 1)[1]
        try:
            items = pool.list_resources(server)
        except Exception as exc:
            _fail(_capability_error(exc, "resources"))
        if search:
            needle = search.lower()
            items = [
                r for r in items
                if needle in (r.get("uri", "") or "").lower()
                or needle in (r.get("name", "") or "").lower()
                or needle in (r.get("description", "") or "").lower()
            ]
        if as_json:
            _emit(items, True)
            return
        if not items:
            print(f"  (no resources{' matching ' + repr(search) if search else ''})")
            return
        for r in items:
            uri = r.get("uri", "")
            name = r.get("name", "")
            mime = r.get("mimeType", "")
            desc = (r.get("description") or "").strip().split("\n")[0]
            print(f"  {uri:<44} {mime:<20} {name or desc}")
        return

    if action == "templates":
        try:
            items = pool.list_resource_templates(server)
        except Exception as exc:
            _fail(_capability_error(exc, "resource templates"))
        if as_json:
            _emit(items, True)
            return
        if not items:
            print("  (no resource templates)")
            return
        for t in items:
            print(f"  {t.get('uriTemplate', ''):<44} {t.get('name', '')}")
        return

    if action == "read":
        if not args:
            _fail("resources read: missing <uri>")
        uri = args[0]
        try:
            result = pool.read_resource(server, uri)
        except Exception as exc:
            _fail(_capability_error(exc, f"resource {uri!r}"))
        # `--raw` is consumed by the GLOBAL parser and arrives as fmt="raw",
        # not as a literal in args — so check both. (Same trap exists for any
        # format flag; `--json` likewise arrives as fmt="json".)
        as_raw = "--raw" in args or fmt == "raw"
        contents = result.get("contents") if isinstance(result, dict) else None
        if as_raw and isinstance(contents, list):
            for c in contents:
                if not isinstance(c, dict):
                    continue
                if c.get("text") is not None:
                    print(c["text"])
                elif c.get("blob"):
                    # Binary payload. Write raw bytes when stdout is a real
                    # stream; when it is redirected to a text sink (StringIO,
                    # a pipe wrapper, a test capture) there is no `.buffer`,
                    # so fall back to base64 rather than crash. This matters:
                    # `--raw` on a binary resource used to raise AttributeError
                    # the moment anything wrapped stdout.
                    raw = base64.b64decode(c["blob"])
                    buf = getattr(sys.stdout, "buffer", None)
                    if buf is not None:
                        buf.write(raw)
                    else:
                        sys.stdout.write(base64.b64encode(raw).decode("ascii") + "\n")
            return
        _emit(result, as_json)
        return

    _fail(f"unknown resources action {action!r}")


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


def _cmd_prompts(action: str, args: list, fmt: str) -> None:
    from . import router

    servers = _load_servers()
    if not servers:
        _fail("no servers configured; run `mcptoon add` or `mcptoon discover` first")

    server, args = _pick_server(args, servers)
    if server is None:
        _fail("multiple servers configured; name one: "
              f"mcptoon prompts {action} <server> ...")

    as_json = "--json" in args or fmt == "json"
    pool = router._get_pool()

    if action == "list":
        search = None
        for i, a in enumerate(args):
            if a == "--search" and i + 1 < len(args):
                search = args[i + 1]
            elif a.startswith("--search="):
                search = a.split("=", 1)[1]
        try:
            items = pool.list_prompts(server)
        except Exception as exc:
            _fail(_capability_error(exc, "prompts"))
        if search:
            needle = search.lower()
            items = [
                p for p in items
                if needle in (p.get("name", "") or "").lower()
                or needle in (p.get("description", "") or "").lower()
            ]
        if as_json:
            _emit(items, True)
            return
        if not items:
            print(f"  (no prompts{' matching ' + repr(search) if search else ''})")
            return
        for p in items:
            name = p.get("name", "")
            desc = (p.get("description") or "").strip().split("\n")[0]
            print(f"  {name:<32} {desc}")
        return

    if action == "get":
        if not args:
            _fail("prompts get: missing <name>")
        name = args[0]
        # Everything after the name is a prompt argument (name=value).
        arguments: dict = {}
        for a in args[1:]:
            if a.startswith("--"):
                continue
            if "=" in a:
                k, _, v = a.partition("=")
                arguments[k] = v
        try:
            result = pool.get_prompt(server, name, arguments or None)
        except Exception as exc:
            _fail(_capability_error(exc, f"prompt {name!r}"))
        # `--raw` arrives as fmt="raw" (global parser consumed it), not in args.
        as_raw = "--raw" in args or fmt == "raw"
        messages = result.get("messages") if isinstance(result, dict) else None
        if as_raw and isinstance(messages, list):
            for m in messages:
                if not isinstance(m, dict):
                    continue
                content = m.get("content")
                if isinstance(content, dict) and content.get("text") is not None:
                    print(content["text"])
                elif isinstance(content, str):
                    print(content)
            return
        _emit(result, as_json)
        return

    _fail(f"unknown prompts action {action!r}")


# ---------------------------------------------------------------------------
# complete — ask the server to finish an argument value (completion/complete)
# ---------------------------------------------------------------------------


def _cmd_complete(args: list, fmt: str) -> None:
    """``mcptoon complete <server> <ref>:<arg>=<prefix>``

    ``ref`` is a prompt name, or a resource URI template when it contains
    ``://``. This is what shells/agents use to suggest legal values for a
    prompt or resource argument (e.g. a city list) instead of guessing.
    """
    from . import router

    servers = _load_servers()
    if not servers:
        _fail("no servers configured; run `mcptoon add` or `mcptoon discover` first")

    server, args = _pick_server(args, servers)
    if server is None:
        _fail("multiple servers configured; name one: "
              "mcptoon complete <server> <ref>:<arg>=<prefix>")

    spec = None
    for a in args:
        if not a.startswith("--"):
            spec = a
            break
    if not spec:
        _fail("complete: missing <ref>:<arg>=<prefix> "
              "(e.g. my-prompt:city=San)")
    # Split ref:arg=prefix. The prefix itself may contain '=' (a value like
    # "a=b=c"), so split on the FIRST '=' after the argument name, not the
    # last. The argument separator is the last ':' that is not part of '://'
    # (a resource ref like file:///{path} contains '://', which is not a
    # separator).
    if "=" not in spec:
        _fail(f"complete: {spec!r} must be <ref>:<arg>=<prefix>")
    sep = -1
    idx = 0
    while True:
        j = spec.find(":", idx)
        if j < 0:
            break
        if spec[j:j + 3] != "://":
            sep = j
        idx = j + 1
    if sep < 0:
        _fail(f"complete: {spec!r} must be <ref>:<arg>=<prefix>")
    ref = spec[:sep]
    arg_and_prefix = spec[sep + 1:]
    if "=" not in arg_and_prefix:
        _fail(f"complete: {spec!r} must be <ref>:<arg>=<prefix>")
    argument, _, prefix = arg_and_prefix.partition("=")
    if not ref or not argument:
        _fail(f"complete: {spec!r} must be <ref>:<arg>=<prefix>")

    as_json = "--json" in args or fmt == "json"
    pool = router._get_pool()
    try:
        result = pool.complete(server, ref, argument, prefix)
    except Exception as exc:
        _fail(_capability_error(exc, "argument completion"))

    completion = result.get("completion") if isinstance(result, dict) else None
    if as_json:
        _emit(result, True)
        return
    values = (completion or {}).get("values") or []
    if not values:
        print(f"  (no completions for {argument}={prefix!r})")
        return
    for v in values:
        print(f"  {v}")
    if (completion or {}).get("hasMore"):
        print("  … (more available)")


def run(rest: list, fmt: str = "auto", head_n: int = 0, max_chars: int = 0,
        full: bool = False) -> None:
    """CLI entry: ``mcptoon resources ...``, ``mcptoon prompts ...`` or
    ``mcptoon complete ...``.

    The dispatcher passes the whole tail; the first token tells us which
    pillar.
    """
    if not rest:
        print(_USAGE)
        return
    # Normalise: the CLI dispatch calls this with rest already starting at the
    # action (it strips the command word). We accept an optional leading
    # "resources"/"prompts"/"complete" for direct/test calls.
    pillar = "resources"
    action = rest[0]
    args = rest[1:]
    if action in ("resources", "prompts", "complete"):
        pillar = action
        action = args[0] if args else ""
        args = args[1:]

    if pillar == "complete":
        # `complete` has a single action; everything after the pillar word is
        # its arguments. Reassemble so `complete <server> ref:arg=pre` works.
        _cmd_complete(([action] if action else []) + args, fmt)
        return

    if action in ("", "help", "-h", "--help"):
        print(_USAGE)
        return

    try:
        if pillar == "resources":
            _cmd_resources(action, args, fmt)
        else:
            _cmd_prompts(action, args, fmt)
    except SystemExit:
        raise
    except Exception as exc:  # a protocol error must never be a raw traceback
        print(f"mcptoon {pillar}: {exc}", file=sys.stderr)
        sys.exit(1)
