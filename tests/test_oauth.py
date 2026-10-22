"""Tests for the OAuth client.

No real network: the token endpoint is stubbed with `patch.object(_post_form)`.
One test exercises the real local callback server on a loopback socket.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcptoon import oauth


def _capture(fn, *a, **kw):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        fn(*a, **kw)
    return out.getvalue(), err.getvalue()


class _Isolated(unittest.TestCase):
    """Point the token dir at a throwaway folder for each test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        import mcptoon.config as cfg
        self._old_state = cfg.STATE_DIR
        cfg.STATE_DIR = Path(self.tmp)

    def tearDown(self):
        import mcptoon.config as cfg
        cfg.STATE_DIR = self._old_state
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestPKCE(unittest.TestCase):
    def test_verifier_and_challenge_shape(self):
        v, c = oauth.generate_pkce()
        self.assertTrue(43 <= len(v) <= 128)
        # challenge is base64url sha256, no padding
        self.assertEqual(len(c), 43)
        self.assertNotIn("=", c)

    def test_challenge_is_sha256_of_verifier(self):
        import base64
        import hashlib
        v, c = oauth.generate_pkce()
        expect = base64.urlsafe_b64encode(
            hashlib.sha256(v.encode()).digest()
        ).decode().rstrip("=")
        self.assertEqual(c, expect)

    def test_two_calls_differ(self):
        self.assertNotEqual(oauth.generate_pkce()[0], oauth.generate_pkce()[0])


class TestAuthorizeUrl(unittest.TestCase):
    def test_contains_all_required_params(self):
        cfg = oauth.OAuthConfig(
            name="x", auth_url="https://p.test/authorize",
            token_url="https://p.test/token", client_id="cid",
            redirect_uri="http://127.0.0.1:8765/callback", scope="read write",
        )
        url = oauth.build_authorize_url(cfg, "STATE", "CHALLENGE")
        self.assertIn("response_type=code", url)
        self.assertIn("client_id=cid", url)
        self.assertIn("state=STATE", url)
        self.assertIn("code_challenge=CHALLENGE", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("scope=read+write", url)

    def test_appends_with_ampersand_when_query_exists(self):
        cfg = oauth.OAuthConfig(
            name="x", auth_url="https://p.test/authorize?tenant=abc",
            client_id="cid", redirect_uri="http://127.0.0.1:1/cb",
        )
        url = oauth.build_authorize_url(cfg, "S", "C")
        self.assertIn("?tenant=abc&", url)


class TestTokenSet(unittest.TestCase):
    def test_no_expiry_never_expires(self):
        self.assertFalse(oauth.TokenSet(access_token="a").is_expired())

    def test_expiry_respects_leeway(self):
        ts = oauth.TokenSet(access_token="a", expires_at=time.time() + 30)
        self.assertTrue(ts.is_expired(leeway=60))     # inside leeway
        self.assertFalse(ts.is_expired(leeway=5))

    def test_header(self):
        ts = oauth.TokenSet(access_token="abc", token_type="Bearer")
        self.assertEqual(ts.header(), {"Authorization": "Bearer abc"})


class TestStorage(_Isolated):
    def test_token_roundtrip(self):
        ts = oauth.TokenSet(access_token="AT", refresh_token="RT",
                            expires_at=time.time() + 3600, scope="read")
        oauth.save_token("svc", ts)
        back = oauth.load_token("svc")
        self.assertEqual(back.access_token, "AT")
        self.assertEqual(back.refresh_token, "RT")
        self.assertEqual(back.scope, "read")

    def test_missing_token_returns_none(self):
        self.assertIsNone(oauth.load_token("nope"))

    def test_corrupt_token_file_returns_none(self):
        oauth.save_token("svc", oauth.TokenSet(access_token="AT"))
        path = oauth._token_path("svc")
        path.write_text("{ broken", encoding="utf-8")
        self.assertIsNone(oauth.load_token("svc"))

    def test_config_roundtrip(self):
        cfg = oauth.OAuthConfig(name="svc", auth_url="https://a", token_url="https://t",
                                client_id="cid", scope="read")
        oauth.save_config(cfg)
        back = oauth.load_config("svc")
        self.assertEqual(back.auth_url, "https://a")
        self.assertEqual(back.client_id, "cid")

    def test_delete_token(self):
        oauth.save_token("svc", oauth.TokenSet(access_token="AT"))
        self.assertTrue(oauth._delete_token("svc"))
        self.assertIsNone(oauth.load_token("svc"))

    def test_token_file_is_not_world_readable_on_posix(self):
        if os.name == "nt":
            self.skipTest("POSIX permission bits")
        oauth.save_token("svc", oauth.TokenSet(access_token="AT"))
        mode = oauth._token_path("svc").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class TestGrants(_Isolated):
    def _cfg(self):
        return oauth.OAuthConfig(name="svc", auth_url="https://a", token_url="https://t",
                                 client_id="cid", client_secret="sec", scope="read")

    def test_exchange_code_sends_verifier(self):
        captured = {}

        def fake_post(url, form, timeout=30):
            captured.update(form)
            return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

        with patch.object(oauth, "_post_form", side_effect=fake_post):
            ts = oauth.exchange_code(self._cfg(), "CODE", "VERIFIER")
        self.assertEqual(captured["grant_type"], "authorization_code")
        self.assertEqual(captured["code"], "CODE")
        self.assertEqual(captured["code_verifier"], "VERIFIER")
        self.assertEqual(ts.access_token, "AT")
        self.assertIsNotNone(ts.expires_at)

    def test_client_credentials_grant(self):
        captured = {}

        def fake_post(url, form, timeout=30):
            captured.update(form)
            return {"access_token": "AT-CC", "token_type": "Bearer"}

        with patch.object(oauth, "_post_form", side_effect=fake_post):
            ts = oauth.client_credentials(self._cfg())
        self.assertEqual(captured["grant_type"], "client_credentials")
        self.assertEqual(captured["client_secret"], "sec")
        self.assertEqual(ts.access_token, "AT-CC")
        self.assertIsNone(ts.expires_at)   # no expires_in

    def test_refresh_keeps_old_refresh_token_when_omitted(self):
        def fake_post(url, form, timeout=30):
            return {"access_token": "AT-NEW", "expires_in": 3600}  # no refresh_token

        ts = oauth.TokenSet(access_token="OLD", refresh_token="RT-OLD")
        with patch.object(oauth, "_post_form", side_effect=fake_post):
            fresh = oauth.refresh_token(self._cfg(), ts)
        self.assertEqual(fresh.access_token, "AT-NEW")
        self.assertEqual(fresh.refresh_token, "RT-OLD")

    def test_refresh_without_refresh_token_raises(self):
        with self.assertRaises(ValueError):
            oauth.refresh_token(self._cfg(), oauth.TokenSet(access_token="A"))

    def test_token_error_response_is_a_valueerror(self):
        import urllib.error
        from io import BytesIO

        def fake_urlopen(*a, **kw):
            raise urllib.error.HTTPError(
                "https://t", 400, "Bad Request", {},
                BytesIO(json.dumps({"error": "invalid_grant",
                                    "error_description": "code expired"}).encode()),
            )
        with patch.object(oauth.urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(ValueError) as ctx:
                oauth.client_credentials(self._cfg())
        self.assertIn("code expired", str(ctx.exception))


class TestGetAccessToken(_Isolated):
    def _cfg(self):
        return oauth.OAuthConfig(name="svc", token_url="https://t", client_id="cid")

    def test_no_token_raises_actionable(self):
        with self.assertRaises(ValueError) as ctx:
            oauth.get_access_token("svc")
        self.assertIn("mcptoon auth login", str(ctx.exception))

    def test_valid_token_returned_as_is(self):
        oauth.save_token("svc", oauth.TokenSet(access_token="AT",
                                               expires_at=time.time() + 3600))
        self.assertEqual(oauth.get_access_token("svc"), "AT")

    def test_expired_token_refreshes(self):
        oauth.save_config(self._cfg())
        oauth.save_token("svc", oauth.TokenSet(
            access_token="OLD", refresh_token="RT", expires_at=time.time() - 10))

        def fake_post(url, form, timeout=30):
            return {"access_token": "FRESH", "expires_in": 3600}

        with patch.object(oauth, "_post_form", side_effect=fake_post):
            self.assertEqual(oauth.get_access_token("svc"), "FRESH")
        # the refreshed token was persisted
        self.assertEqual(oauth.load_token("svc").access_token, "FRESH")

    def test_expired_without_refresh_raises(self):
        oauth.save_config(self._cfg())
        oauth.save_token("svc", oauth.TokenSet(
            access_token="OLD", expires_at=time.time() - 10))
        with self.assertRaises(ValueError) as ctx:
            oauth.get_access_token("svc")
        self.assertIn("expired", str(ctx.exception))


class TestCallbackServer(unittest.TestCase):
    def test_receives_code(self):
        import threading
        import urllib.request

        def hit():
            time.sleep(0.5)
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:8871/callback?code=THE_CODE&state=ST",
                    timeout=5).read()
            except Exception:
                pass

        threading.Thread(target=hit, daemon=True).start()
        code = oauth._wait_for_callback(8871, "ST", timeout=15)
        self.assertEqual(code, "THE_CODE")

    def test_state_mismatch_raises(self):
        import threading
        import urllib.request

        def hit():
            time.sleep(0.5)
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:8872/callback?code=C&state=WRONG",
                    timeout=5).read()
            except Exception:
                pass

        threading.Thread(target=hit, daemon=True).start()
        with self.assertRaises(ValueError) as ctx:
            oauth._wait_for_callback(8872, "EXPECTED", timeout=15)
        self.assertIn("state mismatch", str(ctx.exception))

    def test_provider_error_in_callback_raises(self):
        import threading
        import urllib.request

        def hit():
            time.sleep(0.5)
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:8873/callback?error=access_denied"
                    "&error_description=user%20said%20no", timeout=5).read()
            except Exception:
                pass

        threading.Thread(target=hit, daemon=True).start()
        with self.assertRaises(ValueError) as ctx:
            oauth._wait_for_callback(8873, "S", timeout=15)
        self.assertIn("user said no", str(ctx.exception))


class TestPastedUrl(unittest.TestCase):
    def test_extracts_code_and_state(self):
        code, state = oauth._extract_code_from_url(
            "http://127.0.0.1:8765/callback?code=XYZ&state=S9")
        self.assertEqual((code, state), ("XYZ", "S9"))

    def test_error_url_raises(self):
        with self.assertRaises(ValueError):
            oauth._extract_code_from_url(
                "http://127.0.0.1:8765/callback?error=denied")

    def test_url_without_code_raises(self):
        with self.assertRaises(ValueError):
            oauth._extract_code_from_url("http://127.0.0.1:8765/callback")


class TestCli(_Isolated):
    def test_status_without_token(self):
        out, _ = _capture(oauth.run, ["status", "svc"])
        self.assertIn("no token", out)

    def test_creds_then_status_then_token_then_logout(self):
        def fake_post(url, form, timeout=30):
            return {"access_token": "AT-CLI", "expires_in": 3600}

        with patch.object(oauth, "_post_form", side_effect=fake_post):
            out, _ = _capture(oauth.run, [
                "creds", "svc", "--token-url", "https://t", "--client-id", "cid"])
        self.assertIn("saved", out)

        out, _ = _capture(oauth.run, ["status", "svc"])
        self.assertIn("valid for", out)

        out, _ = _capture(oauth.run, ["token", "svc"])
        self.assertEqual(out.strip(), "AT-CLI")

        out, _ = _capture(oauth.run, ["logout", "svc"])
        self.assertIn("removed", out)
        self.assertIsNone(oauth.load_token("svc"))

    def test_creds_missing_required_exits(self):
        with self.assertRaises(SystemExit):
            _capture(oauth.run, ["creds", "svc"])   # no --token-url/--client-id

    def test_unknown_action_exits(self):
        with self.assertRaises(SystemExit):
            _capture(oauth.run, ["bogus"])

    def test_help_lists_actions(self):
        out, _ = _capture(oauth.run, ["help"])
        for action in ("login", "creds", "status", "token", "refresh", "logout"):
            self.assertIn(action, out)


class TestLoginFlushesUrl(unittest.TestCase):
    """The authorize URL must be flushed, not block-buffered.

    Regression: under a pipe (headless box / CI / a wrapper script) Python
    block-buffers stdout, so the printed URL never appeared until the process
    exited — but the process cannot exit while it waits for the callback. The
    whole point of printing the URL first is that it is visible *before* the
    wait, so the flush is load-bearing.
    """

    def test_url_is_written_before_waiting(self):
        import subprocess
        import sys as _sys
        import os as _os

        repo = str(Path(__file__).resolve().parent.parent)
        env = dict(_os.environ)
        env["ALL_PROXY"] = ""
        env["MCPTOON_HOME"] = tempfile.mkdtemp()
        proc = subprocess.Popen(
            [_sys.executable, "-m", "mcptoon", "auth", "login", "flush-svc",
             "--auth-url", "http://127.0.0.1:9999/authorize",
             "--token-url", "http://127.0.0.1:9999/token",
             "--client-id", "cid", "--port", "8893"],
            cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env,
        )
        try:
            # Read exactly the first two lines within a few seconds. If stdout
            # is block-buffered this read would block until timeout and return "".
            import threading
            collected = {"text": ""}

            def reader():
                # The first run under a fresh MCPTOON_HOME prints a welcome
                # banner first, so read until the URL line appears or we hit a
                # cap. What matters is that it appears at all, promptly.
                for _ in range(40):
                    line = proc.stdout.readline()
                    if not line:
                        break
                    collected["text"] += line
                    if "http://127.0.0.1:9999/authorize" in collected["text"]:
                        break

            t = threading.Thread(target=reader, daemon=True)
            t.start()
            t.join(timeout=20)
            text = collected["text"]
            self.assertIn("Open this URL", text,
                          "authorize URL was not flushed to the pipe")
            self.assertIn("http://127.0.0.1:9999/authorize", text)
        finally:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass


class TestAdapterHook(_Isolated):
    """`--auth oauth:<name>` must turn into a Bearer header in both adapters."""

    def test_openapi_oauth_headers(self):
        oauth.save_token("svc", oauth.TokenSet(access_token="AT-X",
                                               expires_at=time.time() + 3600))
        from mcptoon import openapi
        headers = openapi._oauth_headers("oauth:svc")
        self.assertEqual(headers, {"Authorization": "Bearer AT-X"})

    def test_graphql_oauth_headers(self):
        oauth.save_token("svc", oauth.TokenSet(access_token="AT-Y",
                                               expires_at=time.time() + 3600))
        from mcptoon import graphql
        headers = graphql._oauth_headers("oauth:svc")
        self.assertEqual(headers, {"Authorization": "Bearer AT-Y"})

    def test_unsupported_scheme_rejected(self):
        from mcptoon import openapi
        with self.assertRaises(ValueError):
            openapi._oauth_headers("basic:svc")

    def test_extract_auth_flag(self):
        from mcptoon import openapi
        spec, rest = openapi._extract_auth_flag(
            ["list", "http://x", "--auth", "oauth:svc", "--json"])
        self.assertEqual(spec, "oauth:svc")
        self.assertEqual(rest, ["list", "http://x", "--json"])


class TestZeroDeps(unittest.TestCase):
    def test_oauth_module_imports_only_stdlib_at_module_level(self):
        import ast
        src = Path(oauth.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        allowed = {
            "base64", "hashlib", "json", "os", "re", "secrets", "ssl", "sys",
            "time", "urllib", "dataclasses", "pathlib", "typing", "__future__",
        }
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
