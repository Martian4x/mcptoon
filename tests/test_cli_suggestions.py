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

"""A typo'd command or flag should suggest the nearest real one.

Reported 2026-10-17: `mcptoon selec` and `mcptoon manifest --jsn` failed with only
"unknown ..." — no "did you mean". Now that an unknown flag is a hard error (exit 2),
a bare failure is hostile to a typo. Both paths use `cli._closest` (difflib) to add a
"did you mean" line, while keeping the original exit codes and the base message.
"""
import io
import sys
from unittest.mock import patch

from mcptoon import cli


def _run(argv):
    """Return (stdout, stderr, exit_code) for a CLI invocation."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with patch.object(sys, "argv", ["mcptoon", *argv]), \
                patch("sys.stdout", out), patch("sys.stderr", err):
            cli.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    return out.getvalue(), err.getvalue(), code


def test_unknown_command_suggests_and_exits_1():
    _, err, code = _run(["selec"])
    assert code == 1
    assert "unknown command 'selec'" in err
    assert "did you mean" in err.lower()
    assert "select" in err


def test_unknown_flag_suggests_and_exits_2():
    _, err, code = _run(["manifest", "--jsn"])
    assert code == 2
    assert "unknown option '--jsn'" in err
    assert "did you mean" in err.lower()
    assert "--json" in err


def test_closest_helper_returns_empty_for_far_away_word():
    # A word with nothing close must not fabricate a suggestion.
    assert cli._closest("zzzzqqqq", cli._COMPLETION_COMMANDS) == []


def test_closest_helper_finds_prefix_typo():
    assert "select" in cli._closest("selec", cli._COMPLETION_COMMANDS)
