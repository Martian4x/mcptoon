# Copyright 2025-2026 activeing123
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Manifest fetch tests — pool lifecycle, not discovery correctness.

`get_manifest` / `get_server_tools` build an `MCPClientPool` per server. The pool
spawns child processes (npx/node stdio servers), so it MUST be closed even when
`list_tools` raises — otherwise every failed server leaks a child. The old code
called `pool.close()` *after* `list_tools`, so a server that died mid-fetch
skipped it (measured 2026-10-17: pools=1, close()=0). These pin the `with` fix.
"""
from unittest.mock import patch

from mcptoon import manifest as manifest_mod


class _FakePool:
    """Records construction and close; `list_tools` raises to simulate a dead server."""

    instances = 0
    closed = 0

    def __init__(self, servers):
        type(self).instances += 1

    def list_tools(self, name):
        raise RuntimeError("server died mid-fetch")

    def close(self):
        type(self).closed += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _reset():
    _FakePool.instances = 0
    _FakePool.closed = 0


def test_get_server_tools_closes_pool_when_list_tools_raises():
    _reset()
    with patch.object(manifest_mod, "MCPClientPool", _FakePool), \
         patch.object(manifest_mod, "load_config", lambda: {"srv": {"command": "x"}}), \
         patch.object(manifest_mod.cache_mod, "get_cached_tools", lambda s: None), \
         patch.object(manifest_mod.cache_mod, "set_cached_tools", lambda s, t: False):
        result = manifest_mod.get_server_tools("srv")
    assert result and "error" in result[0], "a dead server must surface as an error entry"
    assert _FakePool.instances == 1
    assert _FakePool.closed == 1, "pool.close() must run even though list_tools raised"


def test_get_manifest_closes_pool_when_list_tools_raises():
    _reset()
    with patch.object(manifest_mod, "MCPClientPool", _FakePool), \
         patch.object(manifest_mod, "load_config", lambda: {"srv": {"command": "x"}}), \
         patch.object(manifest_mod.cache_mod, "get_cached_tools", lambda s: None), \
         patch.object(manifest_mod.cache_mod, "set_cached_tools", lambda s, t: False):
        manifest = manifest_mod.get_manifest(use_cache=False)
    assert "srv" in manifest and "error" in manifest["srv"][0]
    assert _FakePool.closed == _FakePool.instances, "every pool built must be closed"
