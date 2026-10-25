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

"""`mcptoon remove` must also drop that server's schema cache.

Reported 2026-10-17: removing a server from config left its per-server cache file
on disk forever — `add demo; manifest; remove demo` still showed 1 file under
`cache/schema_cache/`. `cache.clear_server_cache()` already existed (and `uninstall`
used it); `remove` simply never called it. These pin both doors that remove a
server: `mcptoon remove <name>` and `mcptoon install --remove <name>`.
"""
import io
import sys
from unittest.mock import patch

from mcptoon import cache
from mcptoon import config as cfg


def _run(argv):
    from mcptoon import cli
    buf = io.StringIO()
    try:
        with patch.object(sys, "argv", ["mcptoon", *argv]), patch("sys.stdout", buf):
            cli.main()
    except SystemExit:
        pass
    return buf.getvalue()


def _seed(server="demo"):
    cfg.save_config({server: {"type": "stdio", "command": "python", "args": []}})
    cache.set_cached_tools(server, [{"name": "echo", "description": "e"}])
    return cache._server_file(server)


def test_remove_clears_the_schema_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("MCPTOON_CACHE_DIR", raising=False)
    cfg.apply_home(tmp_path)
    f = _seed("demo")
    assert f.exists(), "precondition: cache file should exist before remove"
    _run(["remove", "demo"])
    assert not f.exists(), "remove left the server's schema cache on disk"
    assert "demo" not in cfg.load_config()


def test_remove_of_unknown_server_still_errors(tmp_path, monkeypatch):
    # A miss must still be exit 1 (unchanged behaviour) — clearing a nonexistent
    # cache must not turn a typo'd remove into a success.
    monkeypatch.delenv("MCPTOON_CACHE_DIR", raising=False)
    cfg.apply_home(tmp_path)
    from mcptoon import cli
    with patch.object(sys, "argv", ["mcptoon", "remove", "nope"]):
        try:
            cli.main()
            raised = False
        except SystemExit as e:
            raised = True
            assert e.code == 1
    assert raised, "remove of a missing server must exit 1"


def test_install_remove_clears_the_schema_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("MCPTOON_CACHE_DIR", raising=False)
    cfg.apply_home(tmp_path)
    # `install --remove` reads the installed registry, so seed that path too.
    from mcptoon import installer
    f = _seed("demo")
    installed = installer._load_installed()
    installed["demo"] = {"name": "demo"}
    installer._save_installed(installed)
    assert f.exists()
    _run(["install", "--remove", "demo"])
    assert not f.exists(), "install --remove left the server's schema cache on disk"
