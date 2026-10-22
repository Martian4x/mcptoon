"""Issue #34 — MCPTOON_CALL_TIMEOUT must actually reach the MCP client.

Before the fix, `serve.py` read MCPTOON_CALL_TIMEOUT and used it only for the
worker-thread wait in `_call_with_timeout`, while the client it waited on kept
its own 30s default. The client's deadline therefore fired first and any call
longer than 30s died with `[RESPONSE_TIMEOUT] ... within 30s`, no matter what
the documented env var said.

These tests pin the plumbing, not the wall clock: they assert the value handed
to `MCPClientPool` lands on the clients it creates, that an explicit per-server
"timeout" still wins (the documented workaround must not regress), and that the
serve gateway actually passes the env var down.
"""

from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

from mcptoon import serve as serve_mod
from mcptoon.client import MCPClientPool


STDIO_CFG = {"transport": "stdio", "command": "python"}
HTTP_CFG = {"transport": "http", "url": "http://127.0.0.1:1/mcp"}


def test_pool_default_timeout_reaches_stdio_client():
    """The whole point of #34: the pool's default must not be swallowed."""
    client = MCPClientPool._make_client(STDIO_CFG, 900)
    assert client._timeout == 900


def test_pool_default_timeout_reaches_http_client():
    client = MCPClientPool._make_client(HTTP_CFG, 900)
    assert client._timeout == 900


def test_explicit_server_timeout_still_wins():
    """The config.json workaround from the issue report must keep working."""
    cfg = dict(STDIO_CFG, timeout=120)
    client = MCPClientPool._make_client(cfg, 900)
    assert client._timeout == 120


def test_no_default_keeps_historical_30s():
    """Backwards compatibility: callers that pass nothing get the old value."""
    assert MCPClientPool._make_client(STDIO_CFG)._timeout == 30
    assert MCPClientPool._make_client(STDIO_CFG, None)._timeout == 30


def test_pool_stores_and_forwards_the_timeout():
    pool = MCPClientPool({"s": STDIO_CFG}, timeout=900)
    assert pool._timeout == 900
    # _get_client is what creates real clients; stub out initialize() so the
    # test does not spawn a subprocess.
    with mock.patch.object(type(pool._make_client(STDIO_CFG)), "initialize", lambda self: None):
        client = pool._get_client("s")
    assert client._timeout == 900


def test_pool_without_timeout_is_unchanged():
    pool = MCPClientPool({"s": STDIO_CFG})
    assert pool._timeout is None
    assert MCPClientPool._make_client(STDIO_CFG, pool._timeout)._timeout == 30


def test_get_call_timeout_reads_env():
    with mock.patch.dict(os.environ, {"MCPTOON_CALL_TIMEOUT": "900"}):
        assert serve_mod._get_call_timeout() == 900


def test_get_call_timeout_default_and_bad_value():
    env = {k: v for k, v in os.environ.items() if k != "MCPTOON_CALL_TIMEOUT"}
    with mock.patch.dict(os.environ, env, clear=True):
        assert serve_mod._get_call_timeout() == serve_mod._DEFAULT_CALL_TIMEOUT
    with mock.patch.dict(os.environ, {"MCPTOON_CALL_TIMEOUT": "not-a-number"}):
        assert serve_mod._get_call_timeout() == serve_mod._DEFAULT_CALL_TIMEOUT


@pytest.mark.parametrize("site", ["init", "lazy"])
def test_serve_passes_call_timeout_into_the_pool(site):
    """Guard the two construction sites so a future edit cannot drop it again."""
    import inspect

    src = inspect.getsource(serve_mod)
    assert src.count("MCPClientPool(self._servers, timeout=_get_call_timeout())") == 2, (
        "both MCPClientPool construction sites in serve.py must forward the "
        "documented call timeout (issue #34)"
    )


# ── The shared helper: one parse for every entry point ──────────────────────

def test_default_call_timeout_helper_reads_env():
    from mcptoon.client import default_call_timeout

    with mock.patch.dict(os.environ, {"MCPTOON_CALL_TIMEOUT": "900"}):
        assert default_call_timeout() == 900
    with mock.patch.dict(os.environ, {"MCPTOON_CALL_TIMEOUT": "12.5"}):
        assert default_call_timeout() == 12.5


def test_default_call_timeout_helper_survives_garbage():
    from mcptoon.client import default_call_timeout

    for bad in ("", "abc", "9s"):
        with mock.patch.dict(os.environ, {"MCPTOON_CALL_TIMEOUT": bad}):
            assert default_call_timeout() == 30


def test_serve_helper_delegates_to_the_shared_one():
    """serve._get_call_timeout must not re-implement the parsing."""
    import inspect

    src = inspect.getsource(serve_mod._get_call_timeout)
    assert "default_call_timeout()" in src
    with mock.patch.dict(os.environ, {"MCPTOON_CALL_TIMEOUT": "900"}):
        assert serve_mod._get_call_timeout() == 900


def test_router_pool_forwards_the_env_timeout():
    """The CLI `mcptoon call` path goes through router's global pool."""
    from mcptoon import router as router_mod

    captured = {}

    class FakePool:
        def __init__(self, servers, timeout=None):
            captured["timeout"] = timeout

        def close(self):
            pass

    with mock.patch.object(router_mod, "MCPClientPool", FakePool), \
         mock.patch.object(router_mod, "_global_pool", None), \
         mock.patch.object(router_mod, "_global_pool_config", None), \
         mock.patch.object(router_mod, "load_config", lambda *a, **k: {"s": {}}), \
         mock.patch.dict(os.environ, {"MCPTOON_CALL_TIMEOUT": "900"}):
        router_mod._get_pool()
    assert captured["timeout"] == 900


# ── Real subprocess proof: the deadline actually fires ─────────────────────

SLOW_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "slow_server.py")


def _slow_cfg(**extra):
    cfg = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [SLOW_SERVER],
    }
    cfg.update(extra)
    return {"slow": cfg}


def test_pool_default_timeout_lets_a_slow_call_finish():
    """3s call under a 60s pool default must succeed (no premature timeout)."""
    pool = MCPClientPool(_slow_cfg(), timeout=60)
    try:
        out = pool.call("slow", "sleep", {"seconds": 3})
    finally:
        pool.close()
    assert "slept 3" in str(out)


def test_pool_default_timeout_aborts_a_call_that_exceeds_it():
    """4s call under a 1s pool default must fail with RESPONSE_TIMEOUT."""
    from mcptoon.client import MCPError

    pool = MCPClientPool(_slow_cfg(), timeout=1)
    try:
        with pytest.raises(MCPError) as exc:
            pool.call("slow", "sleep", {"seconds": 4})
    finally:
        pool.close()
    assert "RESPONSE_TIMEOUT" in str(exc.value)


def test_per_server_timeout_overrides_the_pool_default():
    """config.json "timeout" wins over the pool default — the documented escape."""
    from mcptoon.client import MCPError

    pool = MCPClientPool(_slow_cfg(timeout=1), timeout=60)
    try:
        with pytest.raises(MCPError) as exc:
            pool.call("slow", "sleep", {"seconds": 4})
    finally:
        pool.close()
    assert "RESPONSE_TIMEOUT" in str(exc.value)
