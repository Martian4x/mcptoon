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

"""Three CLI contract fixes reported 2026-10-17.

- `mcptoon inspect <server>` (one arg) is documented in its own usage block as
  "list all tools", but the code required two args and exited 1 — the documented
  path was dead.
- `mcptoon help <command>` ignored the argument and always printed the global page,
  disagreeing with `mcptoon <command> --help`.
- `mcptoon --version --json` printed a bare line; CI/scripts want structured output.
"""
import io
import json
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from mcptoon import cli


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


# ── inspect <server> lists its tools ─────────────────────────────────────────

_FAKE_TOOLS = [
    {"name": "read_file", "description": "Read a file", "inputSchema": {}},
    {"name": "write_file", "description": "Write a file", "inputSchema": {}},
]


def test_inspect_server_lists_all_tools():
    with patch.object(cli.manifest_mod, "get_server_tools", return_value=_FAKE_TOOLS):
        out, err, code = _run(["inspect", "filesystem"])
    assert code == 0
    assert "read_file" in out and "write_file" in out


def test_inspect_server_unreachable_exits_nonzero():
    err_entry = [{"error": "connection refused"}]
    with patch.object(cli.manifest_mod, "get_server_tools", return_value=err_entry), \
            patch.object(cli.manifest_mod, "explain_error", return_value=("no answer", "check the server")):
        out, err, code = _run(["inspect", "ghost"])
    assert code != 0
    assert "ghost" in err


def test_inspect_without_args_is_an_error():
    out, err, code = _run(["inspect"])
    assert code != 0
    assert "Usage: mcptoon inspect" in err


# ── help <command> routes to the command's own page ──────────────────────────

def test_help_command_shows_command_page_not_global():
    out, err, code = _run(["help", "manifest"])
    text = out + err
    assert "mcptoon manifest" in text
    assert "one gateway for all your MCP tools" not in text, \
        "`help manifest` must not dump the whole global page"


def test_help_without_argument_still_shows_global():
    out, err, code = _run(["help"])
    assert "one gateway for all your MCP tools" in out


def test_help_matches_the_command_dash_dash_help_route():
    via_help, _, _ = _run(["help", "manifest"])
    via_flag, _, _ = _run(["manifest", "--help"])
    assert via_help.strip() == via_flag.strip(), \
        "the two documented routes to a command's help must agree"


# ── --version --json ─────────────────────────────────────────────────────────

def test_version_json_is_structured():
    out, err, code = _run(["--version", "--json"])
    assert code == 0
    data = json.loads(out)
    assert data["version"]
    assert data["python"]
    assert data["platform"]


def test_plain_version_is_unchanged():
    out, err, code = _run(["--version"])
    assert out.strip().startswith("mcptoon ")
    assert "{" not in out, "plain --version must stay a single human line"
