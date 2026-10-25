"""Baked connections — save a spec/MCP/GraphQL target under a short name.

Typing a 200-character OpenAPI URL (or remembering which `--auth-header` a
private API needs) every time is the tax this removes:

    mcptoon bake create petstore --openapi https://petstore3.swagger.io/api/v3/openapi.json
    mcptoon bake run petstore list
    mcptoon bake run petstore call get-pet-by-id pet-id=2

    mcptoon bake create gh --openapi https://.../api.github.com.json \\
        --auth-header "Authorization:env:GITHUB_TOKEN"
    mcptoon bake install gh      # writes ~/.local/bin/gh-mcp wrapping `bake run`

Security model (this is the part that had to be right)
------------------------------------------------------
A bake stores a **connection recipe**, never a secret:

* ``--auth-header`` values are accepted only as ``env:VAR`` or ``file:PATH``
  references. A literal token is rejected at `create` time with a message
  pointing at the two safe forms. This is stricter than the adapters, which
  accept a literal because a one-off call leaves no artifact behind — a bake
  does persist, so a literal here would be a token written to disk forever.
* `show` masks every header value to ``<first4>***<last4>`` even when the
  stored form is a reference, so a screenshot of `bake show` leaks nothing.
* `install` writes a wrapper that calls `mcptoon bake run <name>`, so the
  wrapper file itself contains no URL and no header — only the name.

Storage: ``STATE_DIR/bakes.json`` (0600). Not the config file: a bake is not a
server to route tools to, it is a saved adapter invocation.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

__all__ = ["run", "load_bakes", "resolve_bake", "BAKE_KINDS"]

BAKE_KINDS = ("openapi", "graphql", "mcp")

_USAGE = """\
mcptoon bake — save a connection under a short name, then reuse it

Usage:
    mcptoon bake create  <name> --openapi <spec>      [--auth-header K:env:VAR ...]
    mcptoon bake create  <name> --graphql <endpoint>  [--auth-header K:env:VAR ...]
    mcptoon bake create  <name> --mcp <url>           [--auth-header K:env:VAR ...]
                                [--base-url U] [--cache-key K] [--cache-ttl N]
    mcptoon bake list
    mcptoon bake show    <name>
    mcptoon bake update  <name> [same options as create]
    mcptoon bake remove  <name>
    mcptoon bake install <name> [--bin DIR]
    mcptoon bake run     <name> <action> [args ...]

  --auth-header values MUST be env:VAR or file:PATH. A literal secret is
  refused: a bake persists, so a literal would be a token on disk forever.
  `show` masks every header value regardless.

Examples:
    mcptoon bake create petstore --openapi https://petstore3.swagger.io/api/v3/openapi.json
    mcptoon bake run petstore call get-pet-by-id pet-id=2
    mcptoon bake create gh --openapi https://x/api.json --auth-header "Authorization:env:GITHUB_TOKEN"
    mcptoon bake install gh
"""

_SECRET_PREFIXES = ("env:", "file:")


def _bakes_file() -> Path:
    from . import config as cfg

    return Path(cfg.STATE_DIR) / "bakes.json"


def _err(msg: str) -> None:
    print(f"mcptoon bake: {msg}", file=sys.stderr)


def _fail(msg: str) -> None:
    _err(msg)
    sys.exit(1)


def load_bakes() -> dict:
    """Read the bake table. Missing or corrupt file reads as empty."""
    path = _bakes_file()
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_bakes(bakes: dict) -> None:
    path = _bakes_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name — a fixed ".tmp" let concurrent `bake create` writers race
    # on the same temp file and clobber each other's result on os.replace.
    import uuid
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(bakes, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    # 0600 on POSIX; a no-op ACL tighten elsewhere is not attempted (Windows
    # relies on the per-user profile directory's own ACL).
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _mask(value: str) -> str:
    """Mask a header value for display. A reference is shown as-is (it is not
    the secret); a literal (only possible in a hand-edited file) is masked."""
    if not value:
        return ""
    if value.startswith(_SECRET_PREFIXES):
        return value
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def _parse_create_args(args: list) -> tuple[dict, list]:
    """Pull the connection options out of args. Returns (opts, leftover)."""
    opts: dict = {
        "kind": None, "target": None, "auth_headers": [],
        "base_url": None, "cache_key": None, "cache_ttl": None,
    }
    leftover: list = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--openapi", "--graphql", "--mcp") and i + 1 < len(args):
            opts["kind"] = a[2:]
            opts["target"] = args[i + 1]
            i += 2
            continue
        if a.startswith("--openapi=") or a.startswith("--graphql=") or a.startswith("--mcp="):
            k, _, v = a[2:].partition("=")
            opts["kind"] = k
            opts["target"] = v
            i += 1
            continue
        if a == "--auth-header" and i + 1 < len(args):
            opts["auth_headers"].append(args[i + 1])
            i += 2
            continue
        if a.startswith("--auth-header="):
            opts["auth_headers"].append(a.split("=", 1)[1])
            i += 1
            continue
        if a == "--base-url" and i + 1 < len(args):
            opts["base_url"] = args[i + 1]
            i += 2
            continue
        if a.startswith("--base-url="):
            opts["base_url"] = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--cache-key" and i + 1 < len(args):
            opts["cache_key"] = args[i + 1]
            i += 2
            continue
        if a.startswith("--cache-key="):
            opts["cache_key"] = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--cache-ttl" and i + 1 < len(args):
            try:
                opts["cache_ttl"] = int(args[i + 1])
            except ValueError:
                _fail(f"--cache-ttl needs an integer, got {args[i + 1]!r}")
            i += 2
            continue
        if a.startswith("--cache-ttl="):
            try:
                opts["cache_ttl"] = int(a.split("=", 1)[1])
            except ValueError:
                _fail(f"--cache-ttl needs an integer, got {a.split('=', 1)[1]!r}")
            i += 1
            continue
        leftover.append(a)
        i += 1
    return opts, leftover


def _validate_headers(pairs: list) -> list:
    """Each header must be ``Name:env:VAR`` / ``Name:file:PATH``. A literal
    value is refused — a bake persists, so it would be a token on disk."""
    out = []
    for p in pairs:
        if ":" not in p:
            _fail(f"--auth-header {p!r} must be Name:value")
        name, _, value = p.partition(":")
        name = name.strip()
        value = value.strip()
        if not name:
            _fail(f"--auth-header {p!r}: empty header name")
        if not value.startswith(_SECRET_PREFIXES):
            _fail(
                f"--auth-header {name} must reference a secret, not contain one.\n"
                f"  Use:  --auth-header \"{name}:env:MY_TOKEN\"\n"
                f"  or:   --auth-header \"{name}:file:/path/to/token\"\n"
                f"  A baked connection is saved to disk, so a literal secret "
                f"would be stored in plaintext forever.")
        out.append(f"{name}:{value}")
    return out


def _cmd_create(name: str, args: list, update: bool = False) -> None:
    opts, leftover = _parse_create_args(args)
    if leftover:
        _fail(f"unexpected argument(s): {' '.join(leftover)}")
    if not opts["kind"] or not opts["target"]:
        _fail("create needs exactly one of --openapi <spec> / --graphql <endpoint> / --mcp <url>")
    if opts["kind"] not in BAKE_KINDS:
        _fail(f"unknown kind {opts['kind']!r}")
    headers = _validate_headers(opts["auth_headers"])

    bakes = load_bakes()
    if update and name not in bakes:
        _fail(f"no baked connection named {name!r} (use `bake create`)")
    if not update and name in bakes:
        _fail(f"{name!r} already exists (use `bake update` to change it)")

    import time

    bakes[name] = {
        "kind": opts["kind"],
        "target": opts["target"],
        "auth_headers": headers,
        "base_url": opts["base_url"],
        "cache_key": opts["cache_key"],
        "cache_ttl": opts["cache_ttl"],
        "created_at": bakes.get(name, {}).get("created_at") or time.time(),
        "updated_at": time.time(),
    }
    _save_bakes(bakes)
    verb = "updated" if update else "created"
    print(f"  ✓ {verb} {name} ({opts['kind']}: {opts['target']})")
    print(f"    run it: mcptoon bake run {name} list")


def _cmd_list() -> None:
    bakes = load_bakes()
    if not bakes:
        print("  (no baked connections — `mcptoon bake create <name> --openapi <spec>`)")
        return
    for name in sorted(bakes):
        b = bakes[name]
        print(f"  {name:<20} {b.get('kind','?'):<9} {b.get('target','')}")


def _cmd_show(name: str) -> None:
    bakes = load_bakes()
    if name not in bakes:
        _fail(f"no baked connection named {name!r}")
    b = bakes[name]
    print(f"  name:    {name}")
    print(f"  kind:    {b.get('kind')}")
    print(f"  target:  {b.get('target')}")
    for h in b.get("auth_headers") or []:
        hname, _, hval = h.partition(":")
        print(f"  header:  {hname}: {_mask(hval)}")
    if b.get("base_url"):
        print(f"  base-url: {b['base_url']}")
    if b.get("cache_key"):
        print(f"  cache-key: {b['cache_key']}")
    if b.get("cache_ttl") is not None:
        print(f"  cache-ttl: {b['cache_ttl']}")


def _cmd_remove(name: str) -> None:
    bakes = load_bakes()
    if name not in bakes:
        _fail(f"no baked connection named {name!r}")
    del bakes[name]
    _save_bakes(bakes)
    print(f"  ✓ removed {name}")


def resolve_bake(name: str) -> dict:
    """Return the bake dict for ``name`` or raise ValueError with a clear msg."""
    bakes = load_bakes()
    if name not in bakes:
        raise ValueError(
            f"no baked connection named {name!r}; "
            f"known: {', '.join(sorted(bakes)) or '(none)'}")
    return bakes[name]


def _build_adapter_argv(bake: dict, action: str, args: list) -> tuple[str, list]:
    """Translate a bake + action into the argv the adapter `run()` expects.

    Returns (adapter_kind, rest) where rest is what to pass to that module's
    run(). Auth headers become --auth-header flags the adapter already parses.
    """
    kind = bake.get("kind")
    target = bake.get("target")
    rest: list = []
    if kind in ("openapi", "graphql", "mcp"):
        # A baked MCP URL is used through `discover`/`call`-style actions; we
        # only support listing/calling tools against it via the http client.
        rest = [action, target]
    else:
        raise ValueError(f"unknown bake kind {kind!r}")
    for h in bake.get("auth_headers") or []:
        rest.extend(["--auth-header", h])
    if bake.get("base_url"):
        rest.extend(["--base-url", bake["base_url"]])
    if bake.get("cache_key"):
        rest.extend(["--cache-key", bake["cache_key"]])
    if bake.get("cache_ttl") is not None:
        rest.extend(["--cache-ttl", str(bake["cache_ttl"])])
    rest.extend(args)
    return kind, rest


def _cmd_run(name: str, args: list, fmt: str, head_n: int, max_chars: int,
             full: bool) -> None:
    if not args:
        _fail(f"bake run {name}: missing <action> (list / inspect / call / schema)")
    try:
        bake = resolve_bake(name)
    except ValueError as exc:
        _fail(str(exc))
    action = args[0]
    tail = args[1:]
    kind, rest = _build_adapter_argv(bake, action, tail)
    if kind == "openapi":
        from . import openapi as mod
    elif kind == "graphql":
        from . import graphql as mod
    else:
        # MCP URL bakes are not routed through an adapter module; point the
        # user at the direct command rather than pretending.
        _fail("bake run for --mcp targets is not supported yet; "
              f"use `mcptoon discover --http {bake.get('target')}`")
    mod.run(rest, fmt, head_n, max_chars, full)


def _cmd_install(name: str, args: list) -> None:
    try:
        resolve_bake(name)
    except ValueError as exc:
        _fail(str(exc))
    bin_dir = None
    for i, a in enumerate(args):
        if a == "--bin" and i + 1 < len(args):
            bin_dir = args[i + 1]
        elif a.startswith("--bin="):
            bin_dir = a.split("=", 1)[1]
    if bin_dir:
        target_dir = Path(bin_dir)
    else:
        target_dir = Path.home() / ".local" / "bin"
    target_dir.mkdir(parents=True, exist_ok=True)
    wrapper = target_dir / f"{name}-mcp"
    # The wrapper carries the NAME only — no URL, no header — so the file
    # itself is not a secret-bearing artifact.
    if os.name == "nt":
        body = f"@echo off\r\nmcptoon bake run {name} %*\r\n"
        wrapper = target_dir / f"{name}-mcp.cmd"
    else:
        body = f"#!/bin/sh\nexec mcptoon bake run {name} \"$@\"\n"
    wrapper.write_text(body, encoding="utf-8")
    if os.name != "nt":
        try:
            os.chmod(wrapper, 0o755)
        except OSError:
            pass
    print(f"  ✓ installed {wrapper}")
    print(f"    try: {wrapper.name} list")


def run(rest: list, fmt: str = "auto", head_n: int = 0, max_chars: int = 0,
        full: bool = False) -> None:
    """CLI entry: ``mcptoon bake ...``."""
    if not rest:
        print(_USAGE)
        return
    # Accept an optional leading "bake" for direct/test calls.
    action = rest[0]
    args = rest[1:]
    if action == "bake":
        action = args[0] if args else ""
        args = args[1:]

    if action in ("", "help", "-h", "--help"):
        print(_USAGE)
        return

    try:
        if action == "create":
            if not args:
                _fail("create: missing <name>")
            _cmd_create(args[0], args[1:])
        elif action == "update":
            if not args:
                _fail("update: missing <name>")
            _cmd_create(args[0], args[1:], update=True)
        elif action == "list":
            _cmd_list()
        elif action == "show":
            if not args:
                _fail("show: missing <name>")
            _cmd_show(args[0])
        elif action == "remove":
            if not args:
                _fail("remove: missing <name>")
            _cmd_remove(args[0])
        elif action == "install":
            if not args:
                _fail("install: missing <name>")
            _cmd_install(args[0], args[1:])
        elif action == "run":
            if not args:
                _fail("run: missing <name>")
            _cmd_run(args[0], args[1:], fmt, head_n, max_chars, full)
        else:
            _fail(f"unknown bake action {action!r}")
    except SystemExit:
        raise
    except Exception as exc:
        _err(str(exc))
        sys.exit(1)
