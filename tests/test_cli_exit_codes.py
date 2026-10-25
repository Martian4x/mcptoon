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

"""Exit codes and streams for bad input — the shell-facing contract.

A CLI is used from scripts and `&&` chains, so "did it work?" must be answerable
from `$?` alone, and errors must be on stderr. Two violations were reported on
2026-10-17: `mcptoon notacommand` printed "Unknown command" to stdout and exited
0 (a failed step looked green), and `mcptoon serve --nonsense` printed "ignored"
then ran anyway (a typo'd format flag silently produced default output). These
drive `cli.main` end-to-end and pin the corrected contract.
"""
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


from mcptoon import cli


def _run(argv, monkeypatch=None, env=None):
    """Run cli.main() with argv, capturing (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    ctx = patch.object(sys, "argv", ["mcptoon"] + argv)
    if env and monkeypatch is not None:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    try:
        with ctx, redirect_stdout(out), redirect_stderr(err):
            cli.main()
    except SystemExit as e:
        code = e.code if e.code is not None else 0
    return code, out.getvalue(), err.getvalue()


def test_unknown_command_exits_nonzero_and_writes_stderr():
    code, out, err = _run(["definitelynotacommand"])
    assert code != 0, "an unknown command must not exit 0"
    assert "unknown command" in err.lower()
    assert "definitelynotacommand" in err
    assert "unknown command" not in out.lower(), "the error belongs on stderr, not stdout"


def test_unknown_flag_exits_2_and_does_not_run_the_command():
    # `serve --nonsense` used to print "ignored" then start serving; it must now
    # refuse before doing anything. Use `manifest` (fast, no server) as the vehicle.
    code, out, err = _run(["manifest", "--definitely-nonsense"])
    assert code == 2
    assert "unknown option" in err.lower()
    assert "--definitely-nonsense" in err


def test_unknown_flag_can_be_downgraded_to_a_warning(monkeypatch):
    # MCPTOON_STRICT_FLAGS=0 restores warn-and-continue for forward-compat piping.
    code, out, err = _run(["manifest", "--nonsense", "--help"], monkeypatch=monkeypatch,
                          env={"MCPTOON_STRICT_FLAGS": "0"})
    assert "unknown option" in err.lower()
    assert "ignored" in err.lower()


def test_a_typo_of_a_format_flag_is_caught():
    # The silent-corruption case: `--jsn` (missing o) must not silently give default.
    code, out, err = _run(["manifest", "--jsn"])
    assert code == 2
    assert "--jsn" in err
