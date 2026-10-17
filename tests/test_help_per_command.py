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

"""`mcptoon <command> --help` must show that command's own usage.

Reported 2026-10-17: `skills --help`, `docs --help` and `quickstart --help` were
byte-for-byte identical to the global `--help`. A user asking how to use one
subcommand got the whole front door and no per-command flags. The fix prints the
command's own usage block first, then the full reference. These pin that the
per-command output is *not* the general page verbatim and *does* carry usage.
"""
import io
import sys
from unittest.mock import patch

import pytest

from mcptoon import cli


def _capture(argv):
    buf = io.StringIO()
    try:
        with patch.object(sys, "argv", ["mcptoon", *argv]), patch("sys.stdout", buf):
            cli.main()
    except SystemExit:
        pass
    return buf.getvalue()


@pytest.fixture(scope="module")
def global_help():
    return _capture(["--help"])


@pytest.mark.parametrize("command", ["skills", "docs", "quickstart", "status", "select", "policy"])
def test_per_command_help_differs_from_global(command, global_help):
    out = _capture([command, "--help"])
    assert out != global_help, f"{command} --help is still the global page verbatim"
    assert "Usage:" in out, f"{command} --help carries no usage block"
    # ...and still offers the full reference so flags remain discoverable.
    assert "Output flags:" in out


def test_unknown_command_help_falls_back_to_global(global_help):
    # A bogus command name must not crash the usage lookup; it just shows global help.
    out = _capture(["totallymadeup", "--help"])
    assert out == global_help
