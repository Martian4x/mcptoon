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

"""Exit-code and input-validation fixes reported 2026-10-17.

- `mcptoon call` printed a tool's own error (`{"error": true, ...}`) and exited 0,
  so `call ... && next` ran on a failed tool.
- `mcptoon add --header "no-colon"` dropped the header silently and printed a
  cheerful "Added"; auth then failed later with no clue why.
- `mcptoon doctor` printed "✗ … cannot start" and "N issue(s)" yet exited 0, so a
  CI health check read a broken machine as healthy.
"""
import io
import os
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from mcptoon import cli
from mcptoon import config as cfg
from mcptoon import manifest as manifest_mod


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


# ── call: a tool-level error must be a non-zero exit ─────────────────────────

def test_call_tool_error_exits_nonzero():
    fake = {"error": True, "message": "Input validation error: 'url' is required"}
    with patch.object(cli, "call_tool", return_value=fake):
        out, err, code = _run(["call", "srv", "tool", "{}"])
    assert code == 1, "a tool that returned an error must not exit 0"
    assert "url" in out or "url" in err


def test_call_success_exits_zero():
    with patch.object(cli, "call_tool", return_value={"content": "ok"}):
        out, err, code = _run(["call", "srv", "tool", "{}"])
    assert code == 0


def test_call_payload_with_error_field_is_not_a_failure():
    # A tool may legitimately return a payload that merely contains an "error"
    # key; only {error: True, message: ...} (the MCP isError shape) is a failure.
    with patch.object(cli, "call_tool", return_value={"error": "not a failure shape"}):
        out, err, code = _run(["call", "srv", "tool", "{}"])
    assert code == 0


def test_call_auto_tool_error_exits_nonzero():
    fake = {"error": True, "message": "boom"}
    with patch("mcptoon.router.call_tool_auto", return_value=fake):
        out, err, code = _run(["call", "--auto", "tool", "{}"])
    assert code == 1


# ── add: a malformed --header is refused, not dropped ────────────────────────

def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPTOON_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("MCPTOON_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.delenv("MCPTOON_SERVERS", raising=False)


def test_add_header_without_colon_is_rejected(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    out, err, code = _run(["add", "svc", "--http", "https://x.example/mcp",
                           "--header", "BearerNoColon"])
    assert code != 0
    assert "Key: Value" in err
    assert "svc" not in cfg.list_servers(), "a rejected header must not write the server"


def test_add_header_with_colon_is_accepted(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    out, err, code = _run(["add", "svc", "--http", "https://x.example/mcp",
                           "--header", "Authorization: Bearer abc"])
    assert code == 0
    assert cfg.get_server_config("svc")["headers"] == {"Authorization": "Bearer abc"}


# ── doctor: issues must be a non-zero exit ───────────────────────────────────

def _run_doctor(tools_by_server, tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")
    cache_dir = tmp_path / "cache"
    servers = {name: {"transport": "stdio"} for name in tools_by_server}
    code = 0
    with patch.object(cfg, "CONFIG_FILE", config_file), \
         patch.object(cfg, "CACHE_DIR", cache_dir), \
         patch.object(cfg, "load_config", return_value=servers), \
         patch.object(cfg, "has_config_file", return_value=True), \
         patch.object(manifest_mod, "get_server_tools",
                      side_effect=lambda name, use_cache=True: tools_by_server[name]), \
         patch.dict(os.environ, {"MCPTOON_NO_STAR_HINT": "1", "CI": ""}), \
         redirect_stdout(io.StringIO()):
        try:
            cli._cmd_doctor([])
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code


def test_doctor_broken_server_exits_nonzero(tmp_path):
    code = _run_doctor({
        "good": [{"name": "a"}],
        "bad": [{"error": "RESPONSE_TIMEOUT: went silent"}],
    }, tmp_path)
    assert code == 1


def test_doctor_all_healthy_exits_zero(tmp_path):
    code = _run_doctor({"good": [{"name": "a"}, {"name": "b"}]}, tmp_path)
    assert code == 0


def test_doctor_no_config_exits_nonzero(tmp_path):
    buf = io.StringIO()
    code = 0
    with patch.object(cfg, "CONFIG_FILE", tmp_path / "missing.json"), \
         patch.object(cfg, "CACHE_DIR", tmp_path / "cache"), \
         patch.object(cfg, "has_config_file", return_value=False), \
         redirect_stdout(buf):
        try:
            cli._cmd_doctor([])
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    assert code == 1
