# Copyright 2025 cxh
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

"""Regression tests: the gateway must never be its own upstream server.

Measured 2026-10-12: an agent's config legitimately carries a `mcptoon` entry
(written by `mcptoon sync --self`). `mcptoon import` / `init --auto` read that
config and, with no self-filter, merged the entry back into mcptoon's OWN
server list. The next `serve` then launched a second `serve`, which read the
same list again — 294 live processes and 10.4 GB RAM, still climbing, with no
error and no log line.

These tests pin every layer of the guard so the bomb cannot come back:
  * `config.load_config` drops a self-entry (read path)
  * `config.save_config` refuses to persist one (write path)
  * `discover.scan_import_client` / `scan_import_file` / `auto_discover` drop it
  * the matcher keys on whole argv tokens, so `python -m mcptoon demo-server`
    is NOT a false positive (that mistake was caught by
    `test_cli_smoke.py::TestAgentLoadsTheGateway`).
"""
import json
from unittest.mock import patch

import pytest

from mcptoon import config as cfg
from mcptoon import discover as disc


# The exact argv an agent config carries for the gateway.
SELF_ENTRY = {"command": "C:\\Program Files\\Python312\\python.exe",
              "args": ["-m", "mcptoon", "serve"]}
# A real, unrelated server — must never be dropped.
OTHER_ENTRY = {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]}
# The false-positive trap: "demo-server" contains the substring "serve".
DEMO_SERVER_ENTRY = {"command": "C:\\Program Files\\Python312\\python.exe",
                     "args": ["-m", "mcptoon", "demo-server"]}


class TestIsSelfEntry:
    def test_named_mcptoon_is_self(self):
        assert cfg._is_self_entry("mcptoon", SELF_ENTRY) is True

    def test_reserved_name_alone_is_enough(self):
        # Even a bare entry under the reserved name is refused.
        assert cfg._is_self_entry("mcptoon", {"command": "anything"}) is True

    def test_renamed_self_entry_still_caught(self):
        # The argv gives it away even under a different name.
        assert cfg._is_self_entry("my-gateway", SELF_ENTRY) is True

    def test_ordinary_server_is_not_self(self):
        assert cfg._is_self_entry("memory", OTHER_ENTRY) is False

    def test_demo_server_is_not_self(self):
        # `mcptoon demo-server` is a different subcommand; a substring test on
        # "serve" wrongly matched it and dropped a working server.
        assert cfg._is_self_entry("demo", DEMO_SERVER_ENTRY) is False

    def test_http_server_is_not_self(self):
        assert cfg._is_self_entry("remote", {"url": "https://example.com/mcp"}) is False

    def test_non_dict_is_not_self(self):
        assert cfg._is_self_entry("x", None) is False
        assert cfg._is_self_entry("x", "nonsense") is False

    def test_case_and_path_insensitive(self):
        e = {"command": ["C:\\Python312\\MCPTOON.EXE"], "args": ["SERVE"]}
        assert cfg._is_self_entry("whatever", e) is True


class TestStripSelfEntries:
    def test_drops_only_the_self_entry(self):
        servers = {"filesystem": OTHER_ENTRY, "mcptoon": SELF_ENTRY, "memory": OTHER_ENTRY}
        assert sorted(cfg.strip_self_entries(servers)) == ["filesystem", "memory"]

    def test_empty_and_non_dict_are_safe(self):
        assert cfg.strip_self_entries({}) == {}
        assert cfg.strip_self_entries(None) == {}
        assert cfg.strip_self_entries([1, 2]) == {}


class TestLoadConfigDropsSelf:
    def test_self_entry_in_config_file_never_survives_a_read(self, tmp_path, monkeypatch):
        """The fork bomb needs the entry to reach a running `serve`; the read
        path is what stops it, so a hand-edited file cannot re-arm the bomb."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"servers": {
            "mcptoon": SELF_ENTRY, "memory": OTHER_ENTRY,
        }}), encoding="utf-8")
        monkeypatch.setattr(cfg, "_config_file", lambda: cfg_file)
        monkeypatch.setattr(cfg, "_config_file_toml", lambda: tmp_path / "nope.toml")
        monkeypatch.delenv("MCPTOON_SERVERS", raising=False)
        # Keep project-local files out of the picture.
        monkeypatch.chdir(tmp_path)

        loaded = cfg.load_config()
        assert "mcptoon" not in loaded
        assert "memory" in loaded


class TestSaveConfigRefusesSelf:
    def test_self_entry_is_not_written_to_disk(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        monkeypatch.setattr(cfg, "_config_file", lambda: cfg_file)
        monkeypatch.setattr(cfg, "_config_file_toml", lambda: tmp_path / "nope.toml")

        cfg.save_config({"mcptoon": SELF_ENTRY, "memory": OTHER_ENTRY})

        written = json.loads(cfg_file.read_text(encoding="utf-8"))["servers"]
        assert "mcptoon" not in written
        assert "memory" in written


class TestImportScannersDropSelf:
    def test_scan_import_client_drops_self(self, tmp_path, monkeypatch):
        home = tmp_path
        (home / ".cursor").mkdir(parents=True, exist_ok=True)
        (home / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {
            "mcptoon": SELF_ENTRY, "memory": OTHER_ENTRY,
        }}), encoding="utf-8")
        monkeypatch.setattr(disc, "_home", lambda: home)

        names = [f["name"] for f in disc.scan_import_client("cursor")]
        assert "mcptoon" not in names
        assert "memory" in names

    def test_scan_import_file_drops_self(self, tmp_path):
        p = tmp_path / "exported.json"
        p.write_text(json.dumps({"mcpServers": {
            "mcptoon": SELF_ENTRY, "memory": OTHER_ENTRY,
        }}), encoding="utf-8")

        names = [f["name"] for f in disc.scan_import_file(p)]
        assert "mcptoon" not in names
        assert "memory" in names

    def test_auto_discover_drops_self(self, tmp_path, monkeypatch):
        home = tmp_path
        (home / ".cursor").mkdir(parents=True, exist_ok=True)
        (home / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {
            "mcptoon": SELF_ENTRY, "memory": OTHER_ENTRY,
        }}), encoding="utf-8")
        monkeypatch.setattr(disc, "_home", lambda: home)

        result = disc.auto_discover(scan_configs=True, detect_env=False,
                                    detect_local=False, probe_network=False)
        assert "mcptoon" not in result.servers
        assert "memory" in result.servers
