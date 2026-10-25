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

"""Config writes must be atomic and keep a `.bak`; settings must be reversible.

Reported 2026-10-17 (config-atomic): `save_config` called `path.write_text(...)`
directly — a crash mid-write, a full disk, or a reader landing during the write
could leave a truncated/empty `config.json` (the user's whole server list, not
regenerable). It also overwrote a good file with no snapshot, the other half of the
2026-10-02 incident where a whole list was replaced "with no `.bak`". Now the write
goes through a unique temp file + `os.replace`, and the last good file is copied to
`.bak` once before the first overwrite.

Reported 2026-10-17 (config-unset): `config set` had no inverse — once you set
`lang=en` there was no supported way back to `auto` short of hand-editing JSON.
`unset` / `reset` are now real actions.
"""
import io
import json
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from mcptoon import cli
from mcptoon import config as cfg


def _cfg_env(monkeypatch, tmp_path):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("MCPTOON_CONFIG_FILE", str(cfg_file))
    monkeypatch.delenv("MCPTOON_SERVERS", raising=False)
    monkeypatch.chdir(tmp_path)
    return cfg_file


# ── atomic write + .bak ──────────────────────────────────────────────────────

def test_save_config_writes_valid_json(tmp_path, monkeypatch):
    f = _cfg_env(monkeypatch, tmp_path)
    cfg.save_config({"a": {"transport": "http", "url": "https://x/mcp"}})
    data = json.loads(f.read_text(encoding="utf-8"))
    assert "a" in data["servers"]


def test_save_config_leaves_a_bak_of_the_previous_file(tmp_path, monkeypatch):
    f = _cfg_env(monkeypatch, tmp_path)
    f.write_text('{"servers": {"old": {"transport": "http", "url": "u"}}}',
                 encoding="utf-8")
    cfg.save_config({"new": {"transport": "http", "url": "u"}})
    bak = f.with_suffix(f.suffix + ".bak")
    assert bak.exists(), "save_config overwrote a good config without a .bak"
    assert "old" in bak.read_text(encoding="utf-8")


def test_bak_is_not_clobbered_on_second_save(tmp_path, monkeypatch):
    # The first snapshot is the one closest to the original; a later save must not
    # overwrite it (that would erase the pre-edit state).
    f = _cfg_env(monkeypatch, tmp_path)
    f.write_text('{"servers": {"v1": {}}}', encoding="utf-8")
    cfg.save_config({"v2": {}})
    bak = f.with_suffix(f.suffix + ".bak")
    first = bak.read_text(encoding="utf-8")
    cfg.save_config({"v3": {}})
    assert bak.read_text(encoding="utf-8") == first, "second save clobbered the .bak"
    assert "v1" in first


def test_no_bak_when_there_was_no_prior_file(tmp_path, monkeypatch):
    f = _cfg_env(monkeypatch, tmp_path)
    cfg.save_config({"a": {}})
    assert not f.with_suffix(f.suffix + ".bak").exists()


def test_atomic_write_leaves_no_temp_files(tmp_path, monkeypatch):
    _cfg_env(monkeypatch, tmp_path)
    cfg.save_config({"a": {}})
    stragglers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".tmp-cfg-")]
    assert stragglers == [], f"temp files leaked: {stragglers}"


# ── config unset / reset ─────────────────────────────────────────────────────

def _settings_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))


def _run(argv):
    buf = io.StringIO()
    code = 0
    try:
        with patch.object(sys, "argv", ["mcptoon", *argv]), redirect_stdout(buf):
            cli.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    return buf.getvalue(), code


def test_unset_restores_default(tmp_path, monkeypatch):
    _settings_env(monkeypatch, tmp_path)
    cfg.set_setting("lang", "en")
    assert cfg.get_setting("lang") == "en"
    default = cfg.unset_setting("lang")
    assert default == "auto"
    assert cfg.get_setting("lang") == "auto"


def test_unset_of_absent_key_is_a_noop_success(tmp_path, monkeypatch):
    _settings_env(monkeypatch, tmp_path)
    assert cfg.unset_setting("lang") == "auto"
    assert cfg.get_setting("lang") == "auto"


def test_unset_unknown_key_raises(tmp_path, monkeypatch):
    _settings_env(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        cfg.unset_setting("nope")


def test_reset_clears_all_overrides(tmp_path, monkeypatch):
    _settings_env(monkeypatch, tmp_path)
    cfg.set_setting("lang", "en")
    cfg.set_setting("footer", "off")
    cfg.reset_settings()
    assert cfg.get_setting("lang") == "auto"
    assert cfg.get_setting("footer") == "on"


def test_cli_config_unset_command(tmp_path, monkeypatch):
    _settings_env(monkeypatch, tmp_path)
    cfg.set_setting("lang", "en")
    out, code = _run(["config", "unset", "lang"])
    assert code == 0
    assert "auto" in out
    assert cfg.get_setting("lang") == "auto"


def test_cli_config_unset_unknown_key_exits_1(tmp_path, monkeypatch):
    _settings_env(monkeypatch, tmp_path)
    _, code = _run(["config", "unset", "bogus"])
    assert code == 1


def test_cli_config_reset_command(tmp_path, monkeypatch):
    _settings_env(monkeypatch, tmp_path)
    cfg.set_setting("lang", "en")
    out, code = _run(["config", "reset"])
    assert code == 0
    assert cfg.get_setting("lang") == "auto"
