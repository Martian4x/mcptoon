"""OAuth 2.0 client for mcptoon (zero-dependency).

Gives `mcptoon openapi` / `mcptoon graphql` the ability to talk to APIs that
require OAuth instead of a static key. Three grant types, no third-party code:

    mcptoon auth login    <name> --auth-url U --token-url U --client-id ID
                            [--scope "a b"] [--redirect-port 8765]
                            [--device]          # headless / manual callback
    mcptoon auth creds    <name> --token-url U --client-id ID [--scope "a b"]
                            [--client-secret env:VAR]   # client credentials
    mcptoon auth status   <name>
    mcptoon auth token    <name>              # print the access token (raw)
    mcptoon auth refresh  <name>
    mcptoon auth logout   <name>
    mcptoon auth help

Then use it:

    mcptoon openapi call <spec> <cmd> --auth oauth:<name>
    mcptoon graphql call <endpoint> <cmd> --auth oauth:<name>

Design constraints that shaped this module
------------------------------------------
* **Zero third-party dependencies.** `hashlib`, `secrets`, `base64`, `http.server`,
  `urllib` — all stdlib. No `requests-oauthlib`, no `authlib`.
* **Tokens live outside the repo, owner-only.** Stored under mcptoon's state
  dir with 0600 permissions. Never printed except by the explicit `token`
  subcommand (and never logged by the adapter).
* **Refresh is automatic on 401, exactly once.** A retry loop that can spin is
  worse than a clear failure, so the adapter refreshes once and, if the retry
  still fails, reports the original error.
* **The authorization URL is the product, not a side effect.** `login` prints
  the URL *before* waiting, so a headless box can print it, let the user open it
  elsewhere, and paste the callback back — the standard out-of-band flow.

Public surface
--------------
    generate_pkce() -> (verifier, challenge)
    build_authorize_url(cfg, state, challenge) -> str
    exchange_code(cfg, code, verifier) -> TokenSet
    client_credentials(cfg) -> TokenSet
    refresh_token(cfg, token_set) -> TokenSet
    load_token(name) -> Optional[TokenSet]
    save_token(name, token_set) -> None
    get_access_token(name, force_refresh=False) -> str
    run(rest, fmt="auto", head_n=0, max_chars=0, full=False) -> None
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

__all__ = [
    "TokenSet",
    "generate_pkce",
    "build_authorize_url",
    "exchange_code",
    "client_credentials",
    "refresh_token",
    "load_token",
    "save_token",
    "get_access_token",
    "run",
]


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------


@dataclass
class TokenSet:
    access_token: str
    token_type: str = "Bearer"
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None      # absolute epoch seconds
    scope: Optional[str] = None

    def is_expired(self, leeway: int = 60) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - leeway)

    def header(self) -> dict:
        return {"Authorization": f"{self.token_type} {self.access_token}"}


def _state_dir() -> Path:
    """Token dir under mcptoon's state root, resolved live so `--dir` works."""
    from . import config as _config

    return Path(_config.STATE_DIR) / "oauth"


def _token_path(name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return _state_dir() / f"{safe}.json"


def _restrict(path: Path) -> None:
    """Owner-only permissions. POSIX chmod 600; Windows best-effort ACL."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name == "nt":
        try:
            import subprocess

            user = os.environ.get("USERNAME", "")
            if user:
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r",
                     "/grant:r", f"{user}:F"],
                    capture_output=True, timeout=10,
                )
        except Exception:
            pass


def save_token(name: str, ts: TokenSet) -> None:
    path = _token_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name: a fixed ".tmp" meant two concurrent `auth refresh`/`login`
    # for the same name shared one scratch file — the loser's os.replace raised
    # FileNotFoundError, or an interleaved write stored a corrupt token set.
    import tempfile
    fd, _tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tok-", suffix=".tmp")
    tmp = Path(_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(asdict(ts), ensure_ascii=False))
        _restrict(tmp)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def load_token(name: str) -> Optional[TokenSet]:
    path = _token_path(name)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("access_token"):
        return None
    return TokenSet(
        access_token=data["access_token"],
        token_type=data.get("token_type") or "Bearer",
        refresh_token=data.get("refresh_token"),
        expires_at=data.get("expires_at"),
        scope=data.get("scope"),
    )


def _delete_token(name: str) -> bool:
    path = _token_path(name)
    if path.is_file():
        try:
            path.unlink()
            return True
        except OSError:
            return False
    return False


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def generate_pkce() -> tuple[str, str]:
    """Return (verifier, challenge) for the S256 PKCE method.

    verifier: 43-128 chars from the unreserved set. challenge: base64url of its
    SHA-256 with padding stripped, per RFC 7636 §4.2.
    """
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


# ---------------------------------------------------------------------------
# OAuth config + HTTP
# ---------------------------------------------------------------------------


@dataclass
class OAuthConfig:
    name: str
    auth_url: str = ""
    token_url: str = ""
    client_id: str = ""
    client_secret: Optional[str] = None
    scope: Optional[str] = None
    redirect_uri: str = ""


def _config_path(name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return _state_dir() / f"{safe}.config.json"


def save_config(cfg: OAuthConfig) -> None:
    path = _config_path(cfg.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(cfg)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _restrict(path)


def load_config(name: str) -> Optional[OAuthConfig]:
    path = _config_path(name)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return OAuthConfig(**{k: data.get(k) for k in (
        "name", "auth_url", "token_url", "client_id", "client_secret",
        "scope", "redirect_uri",
    ) if k in data or k == "name"})


def build_authorize_url(cfg: OAuthConfig, state: str, challenge: str) -> str:
    """The URL the user opens in a browser."""
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if cfg.scope:
        params["scope"] = cfg.scope
    sep = "&" if urllib.parse.urlsplit(cfg.auth_url).query else "?"
    return cfg.auth_url + sep + urllib.parse.urlencode(params)


def _post_json(url: str, body: dict, timeout: int = 30) -> dict:
    """POST application/json; return parsed JSON. Used by DCR."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8", errors="replace")
        try:
            err = json.loads(raw.decode("utf-8", errors="replace"))
            msg = (err.get("error_description") or err.get("error")
                   or str(exc))
        except json.JSONDecodeError:
            msg = raw.decode("utf-8", errors="replace")[:200] or str(exc)
        raise ValueError(f"registration endpoint rejected the request: {msg}") from None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise ValueError("registration endpoint did not return JSON") from None


def register_client(registration_url: str, redirect_uri: str,
                    client_name: str = "mcptoon",
                    scope: Optional[str] = None,
                    timeout: int = 30) -> dict:
    """OAuth 2.0 Dynamic Client Registration (RFC 7591).

    POSTs the minimal client metadata and returns the server's response, whose
    ``client_id`` (and ``client_secret`` for a confidential client) the caller
    persists. Nothing here is mcptoon-specific — it is the standard body a
    registration endpoint expects:

        {"client_name": ..., "redirect_uris": [...],
         "grant_types": ["authorization_code", "refresh_token"],
         "response_types": ["code"],
         "token_endpoint_auth_method": "none"}      # public client + PKCE

    ``token_endpoint_auth_method: "none"`` is the right default for a native
    CLI using PKCE — asking for a secret we then have to store is strictly
    worse. A server that insists on a confidential client returns a
    ``client_secret`` anyway, and the caller keeps it (owner-only file).
    """
    body = {
        "client_name": client_name or "mcptoon",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if scope:
        body["scope"] = scope
    resp = _post_json(registration_url, body, timeout=timeout)
    if not isinstance(resp, dict) or not resp.get("client_id"):
        raise ValueError("registration response had no client_id")
    return resp


def _post_form(url: str, form: dict, timeout: int = 30) -> dict:
    """POST application/x-www-form-urlencoded; return parsed JSON."""
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8", errors="replace")
        # OAuth error responses are JSON with an `error` field; surface it.
        try:
            err = json.loads(raw.decode("utf-8", errors="replace"))
            msg = err.get("error_description") or err.get("error") or str(exc)
        except json.JSONDecodeError:
            msg = raw.decode("utf-8", errors="replace")[:200] or str(exc)
        raise ValueError(f"token endpoint rejected the request: {msg}") from None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise ValueError("token endpoint did not return JSON") from None


def _token_from_response(payload: dict) -> TokenSet:
    if not isinstance(payload, dict) or not payload.get("access_token"):
        err = payload.get("error") if isinstance(payload, dict) else None
        raise ValueError(f"token response had no access_token (error={err!r})")
    expires_in = payload.get("expires_in")
    expires_at = time.time() + int(expires_in) if expires_in else None
    return TokenSet(
        access_token=payload["access_token"],
        token_type=payload.get("token_type") or "Bearer",
        refresh_token=payload.get("refresh_token"),
        expires_at=expires_at,
        scope=payload.get("scope"),
    )


def exchange_code(cfg: OAuthConfig, code: str, verifier: str) -> TokenSet:
    """Authorization-code grant with PKCE."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.redirect_uri,
        "client_id": cfg.client_id,
        "code_verifier": verifier,
    }
    if cfg.client_secret:
        form["client_secret"] = cfg.client_secret
    return _token_from_response(_post_form(cfg.token_url, form))


def client_credentials(cfg: OAuthConfig) -> TokenSet:
    """Client-credentials grant (machine-to-machine, no browser)."""
    form = {
        "grant_type": "client_credentials",
        "client_id": cfg.client_id,
    }
    if cfg.client_secret:
        form["client_secret"] = cfg.client_secret
    if cfg.scope:
        form["scope"] = cfg.scope
    return _token_from_response(_post_form(cfg.token_url, form))


def refresh_token(cfg: OAuthConfig, ts: TokenSet) -> TokenSet:
    """Exchange a refresh token for a fresh access token."""
    if not ts.refresh_token:
        raise ValueError("no refresh_token stored; run `mcptoon auth login` again")
    form = {
        "grant_type": "refresh_token",
        "refresh_token": ts.refresh_token,
        "client_id": cfg.client_id,
    }
    if cfg.client_secret:
        form["client_secret"] = cfg.client_secret
    fresh = _token_from_response(_post_form(cfg.token_url, form))
    # A refresh response may omit refresh_token; keep the old one in that case.
    if not fresh.refresh_token:
        fresh.refresh_token = ts.refresh_token
    return fresh


def get_access_token(name: str, force_refresh: bool = False) -> str:
    """Return a usable access token, refreshing first if it is about to expire.

    Raises ValueError with an actionable message when there is nothing to use.
    """
    ts = load_token(name)
    if ts is None:
        raise ValueError(
            f"no token stored for {name!r}; run `mcptoon auth login {name}` "
            f"or `mcptoon auth creds {name}` first"
        )
    if force_refresh or ts.is_expired():
        cfg = load_config(name)
        if cfg is None:
            raise ValueError(f"no OAuth config for {name!r}; re-run the login")
        if ts.refresh_token:
            ts = refresh_token(cfg, ts)
            save_token(name, ts)
            return ts.access_token
        if ts.is_expired():
            raise ValueError(
                f"token for {name!r} expired and there is no refresh_token; "
                f"run `mcptoon auth login {name}` again"
            )
    return ts.access_token


# ---------------------------------------------------------------------------
# Local callback server (PKCE redirect)
# ---------------------------------------------------------------------------


def _wait_for_callback(port: int, expected_state: str, timeout: int = 300) -> str:
    """Block on a one-shot local HTTP server until the redirect arrives.

    Returns the authorization code. Raises ValueError on state mismatch or if
    the provider returned an error instead of a code.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = urllib.parse.urlsplit(self.path).query
            params = urllib.parse.parse_qs(qs)
            if "code" in params:
                if params.get("state", [""])[0] != expected_state:
                    result["error"] = "state mismatch — possible CSRF, aborted"
                    self._reply("Authorization failed: state mismatch.")
                    return
                result["code"] = params["code"][0]
                self._reply("mcptoon: authorized. You can close this tab.")
            elif "error" in params:
                desc = params.get("error_description", params["error"])[0]
                result["error"] = desc
                self._reply(f"Authorization failed: {desc}")
            else:
                self._reply("mcptoon: waiting for the authorization code...")

        def _reply(self, text: str):
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass  # silence the default request logging

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and "code" not in result and "error" not in result:
            server.handle_request()
    finally:
        server.server_close()
    if "error" in result:
        raise ValueError(result["error"])
    if "code" not in result:
        raise ValueError("timed out waiting for the authorization callback")
    return result["code"]


def _extract_code_from_url(url: str) -> tuple[str, Optional[str]]:
    """Pull (code, state) out of a pasted redirect URL (out-of-band flow)."""
    qs = urllib.parse.urlsplit(url).query
    params = urllib.parse.parse_qs(qs)
    if "error" in params:
        raise ValueError(params.get("error_description", params["error"])[0])
    if "code" not in params:
        raise ValueError("that URL has no ?code= parameter")
    return params["code"][0], params.get("state", [None])[0]


# ---------------------------------------------------------------------------
# CLI entry:  mcptoon auth <action> ...
# ---------------------------------------------------------------------------

_AUTH_USAGE = """\
mcptoon auth — OAuth 2.0 login and token management

Usage:
    mcptoon auth login  <name> --auth-url U --token-url U --client-id ID
                        [--scope "a b"] [--port 8765] [--device]
    mcptoon auth login  <name> --auth-url U --token-url U --register REG_URL
                        [--client-name NAME]      # dynamic client registration
    mcptoon auth creds  <name> --token-url U --client-id ID [--scope "a b"]
                        [--client-secret env:VAR|file:PATH|VALUE]
    mcptoon auth creds  <name> --token-url U --register REG_URL [--client-name NAME]
    mcptoon auth status <name>
    mcptoon auth token  <name>
    mcptoon auth refresh <name>
    mcptoon auth logout <name>
    mcptoon auth help

  login   Authorization-code + PKCE. Opens a browser (or, with --device, prints
          the URL and waits for you to paste the redirect back — for headless
          boxes). --port sets the local callback port (default 8765).
  creds   Client-credentials grant. Machine-to-machine, no browser.
  status  Show whether a token exists and when it expires (never the token).
  token   Print the raw access token (for scripting).
  refresh Force a refresh now.

  --register URL  obtain the client_id (and any secret) via OAuth 2.0 Dynamic
                  Client Registration (RFC 7591) instead of registering by hand.
                  The returned secret is stored owner-only, never printed.

Then pass the credential to an adapter:
    mcptoon openapi call <spec> <cmd> --auth oauth:<name>
    mcptoon graphql call <endpoint> <cmd> --auth oauth:<name>
"""


def _flag(args: list, name: str, default=None) -> Optional[str]:
    """Read `--name value` or `--name=value` from args."""
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def _resolve_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if value.startswith("env:"):
        return os.environ.get(value[4:])
    if value.startswith("file:"):
        return Path(value[5:]).read_text(encoding="utf-8").strip()
    return value


def _require(name: str, value: Optional[str]) -> str:
    if not value:
        print(f"mcptoon auth: {name} is required", file=sys.stderr)
        sys.exit(1)
    return value


def _cmd_login(args: list) -> None:
    name = _require("<name>", args[0] if args and not args[0].startswith("--") else None)
    auth_url = _require("--auth-url", _flag(args, "--auth-url"))
    token_url = _require("--token-url", _flag(args, "--token-url"))
    scope = _flag(args, "--scope")
    port = int(_flag(args, "--port", "8765"))
    device = "--device" in args
    client_secret = _resolve_secret(_flag(args, "--client-secret"))

    redirect_uri = f"http://127.0.0.1:{port}/callback"

    # --register <url> performs Dynamic Client Registration instead of taking a
    # --client-id. It runs BEFORE the browser opens, because the client_id it
    # returns is what the authorize URL needs.
    register_url = _flag(args, "--register")
    client_name = _flag(args, "--client-name", "mcptoon")
    client_id = _flag(args, "--client-id")
    if register_url:
        resp = register_client(register_url, redirect_uri,
                               client_name=client_name, scope=scope)
        client_id = resp["client_id"]
        # A public client gets no secret; a confidential one does — keep it.
        if resp.get("client_secret") and not client_secret:
            client_secret = resp["client_secret"]
        print(f"mcptoon auth: registered client_id {client_id}"
              f" with {register_url}")
        sys.stdout.flush()
    if not client_id:
        _require("--client-id", None)  # raises the standard "missing" message

    cfg = OAuthConfig(name=name, auth_url=auth_url, token_url=token_url,
                      client_id=client_id, client_secret=client_secret,
                      scope=scope, redirect_uri=redirect_uri)

    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(24)
    url = build_authorize_url(cfg, state, challenge)

    print("Open this URL in a browser and authorize:\n")
    print("  " + url + "\n")
    # Flush explicitly: when stdout is a pipe (headless box, CI, a wrapper
    # script), Python block-buffers it and the URL would never appear until the
    # process exits — which it cannot do while it waits for the callback.
    sys.stdout.flush()

    if device:
        print("After you authorize, the browser will try to open a")
        print(f"{redirect_uri}?code=... page that will not load. Copy that")
        print("entire URL and paste it here.\n")
        sys.stdout.flush()
        pasted = input("Redirect URL: ").strip()
        code, got_state = _extract_code_from_url(pasted)
        if got_state and got_state != state:
            print("mcptoon auth: state mismatch — aborting", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Waiting for the browser to hit {redirect_uri} ...")
        sys.stdout.flush()
        code = _wait_for_callback(port, state)

    ts = exchange_code(cfg, code, verifier)
    save_config(cfg)
    save_token(name, ts)
    print(f"\nmcptoon auth: token saved for {name!r}"
          + (f" (expires in ~{int(ts.expires_at - time.time())}s)"
             if ts.expires_at else ""))


def _cmd_creds(args: list) -> None:
    name = _require("<name>", args[0] if args and not args[0].startswith("--") else None)
    token_url = _require("--token-url", _flag(args, "--token-url"))
    scope = _flag(args, "--scope")
    client_id = _flag(args, "--client-id")
    client_secret = _resolve_secret(_flag(args, "--client-secret"))

    # --register <url>: obtain a client_id (and possibly a secret) via DCR.
    # client_credentials has no redirect, so we register with a placeholder
    # redirect_uris of [] — RFC 7591 allows an empty list for a
    # machine-to-machine client.
    register_url = _flag(args, "--register")
    if register_url:
        resp = register_client(register_url, "",
                               client_name=_flag(args, "--client-name", "mcptoon"),
                               scope=scope)
        client_id = resp["client_id"]
        if resp.get("client_secret") and not client_secret:
            client_secret = resp["client_secret"]
        print(f"mcptoon auth: registered client_id {client_id}"
              f" with {register_url}")
    if not client_id:
        _require("--client-id", None)

    cfg = OAuthConfig(
        name=name, token_url=token_url, client_id=client_id,
        client_secret=client_secret,
        scope=scope,
    )
    ts = client_credentials(cfg)
    save_config(cfg)
    save_token(name, ts)
    print(f"mcptoon auth: client-credentials token saved for {name!r}")


def _cmd_status(args: list) -> None:
    name = _require("<name>", args[0] if args else None)
    ts = load_token(name)
    if ts is None:
        print(f"mcptoon auth: no token stored for {name!r}")
        return
    print(f"  name       : {name}")
    print(f"  token_type : {ts.token_type}")
    if ts.expires_at:
        left = int(ts.expires_at - time.time())
        state = "EXPIRED" if left <= 0 else f"valid for ~{left}s"
        print(f"  expires    : {state}")
    else:
        print("  expires    : no expiry reported")
    print(f"  refresh    : {'yes' if ts.refresh_token else 'no'}")
    if ts.scope:
        print(f"  scope      : {ts.scope}")


def _cmd_token(args: list) -> None:
    name = _require("<name>", args[0] if args else None)
    print(get_access_token(name))


def _cmd_refresh(args: list) -> None:
    name = _require("<name>", args[0] if args else None)
    ts = load_token(name)
    cfg = load_config(name)
    if ts is None or cfg is None:
        print(f"mcptoon auth: nothing to refresh for {name!r}", file=sys.stderr)
        sys.exit(1)
    fresh = refresh_token(cfg, ts)
    save_token(name, fresh)
    print(f"mcptoon auth: token refreshed for {name!r}")


def _cmd_logout(args: list) -> None:
    name = _require("<name>", args[0] if args else None)
    removed = _delete_token(name)
    print(f"mcptoon auth: {'token removed' if removed else 'no token to remove'} "
          f"for {name!r}")


def run(rest: list, fmt: str = "auto", head_n: int = 0, max_chars: int = 0,
        full: bool = False) -> None:
    """CLI entry point for `mcptoon auth ...`."""
    action = rest[0] if rest else ""
    args = rest[1:]

    if action in ("", "help", "-h", "--help"):
        print(_AUTH_USAGE)
        return

    handlers = {
        "login": _cmd_login,
        "creds": _cmd_creds,
        "status": _cmd_status,
        "token": _cmd_token,
        "refresh": _cmd_refresh,
        "logout": _cmd_logout,
    }
    handler = handlers.get(action)
    if handler is None:
        print(f"mcptoon: unknown auth action {action!r}", file=sys.stderr)
        print(_AUTH_USAGE, file=sys.stderr)
        sys.exit(1)
    try:
        handler(args)
    except (ValueError, FileNotFoundError, urllib.error.URLError, OSError) as exc:
        print(f"mcptoon auth: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Adapter hook: resolve `--auth oauth:<name>` into headers
# ---------------------------------------------------------------------------


def headers_for(name: str) -> dict:
    """Headers an adapter should send for a stored OAuth identity."""
    return {"Authorization": f"Bearer {get_access_token(name)}"}
