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

"""`mcptoon <command> --help` must show that command's own usage — and only it.

Reported 2026-10-17: `skills --help`, `docs --help` and `quickstart --help` were
byte-for-byte identical to the global `--help`. A first fix printed the command's
own usage *followed by the whole global reference*, which overshot: `skills --help`
became 10,814 chars, LONGER than `mcptoon --help` (8,234) — a subcommand page that
buries the command you asked about. The settled contract is: own usage, a compact
`Options for '<cmd>':` line, and a one-line pointer to `mcptoon --help` — and the
result is strictly SHORTER than the global page. These pin all of that.
"""
import io
import sys
from unittest.mock import patch

import pytest

from mcptoon import cli

POINTER = "run 'mcptoon --help'"


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
    # It points at the global page instead of reprinting it.
    assert POINTER in out, f"{command} --help has no pointer to 'mcptoon --help'"
    # The whole point of the second fix: the subcommand page is not longer than
    # the front door it used to duplicate.
    assert len(out) < len(global_help), (
        f"{command} --help is {len(out)} chars, not shorter than the global "
        f"{len(global_help)} — it is re-dumping the full reference again"
    )
    # And it is not an empty stub: there is real content before the pointer line.
    before_pointer = out.split(POINTER)[0].strip()
    assert before_pointer, f"{command} --help prints nothing but the pointer"


def test_unknown_command_help_falls_back_to_global(global_help):
    # A bogus command name must not crash the usage lookup; it just shows global help.
    out = _capture(["totallymadeup", "--help"])
    assert out == global_help

