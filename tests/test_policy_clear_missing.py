# Copyright 2025-2026 cxh
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

"""`policy clear` on a missing policy must error, not fake a "Cleared" report.

Reported 2026-10-18: `policy clear s nosuchtool` printed
"Cleared: s:nosuchtool uses the default behaviour." and exited 0 even though
nothing was set — so `mcptoon policy clear s t && echo done` lied.
"""
import io
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from mcptoon import cli
from mcptoon import config as cfg


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with patch.object(cli.sys, "argv", ["mcptoon", *argv]), \
            redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return out.getvalue(), err.getvalue(), code


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPTOON_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MCPTOON_COMPRESSION_FILE", str(tmp_path / "compression.json"))
    monkeypatch.delenv("MCPTOON_SERVERS", raising=False)
    cfg.add_server("s", {"transport": "stdio", "command": ["x"], "args": []})


def test_clear_missing_policy_errors(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    out, err, code = _run(["policy", "clear", "s", "nosuchtool"])
    assert code == 1
    assert "Cleared" not in out
    assert "No policy set for s:nosuchtool" in err


def test_clear_existing_policy_succeeds(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    cfg.save_policies({"s:tool": "raw"})
    out, err, code = _run(["policy", "clear", "s", "tool"])
    assert code == 0
    assert "Cleared: s:tool" in out
    assert "s:tool" not in cfg.list_policies()
