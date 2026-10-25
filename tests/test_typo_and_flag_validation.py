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

"""Mistyped commands and non-integer flags must fail loudly.

Reported 2026-10-18:
- `mcptoon listt` matched "list" inside the word and ran `manifest`;
  `mcptoon healthh` matched "health" and ran the health check — both exit 0.
- `--head abc` / `--head=xyz` was swallowed silently (head_n stayed 0 =
  unlimited), so a scripted `--head $N` with a bad value looked like it worked.
"""
import io
import json
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from mcptoon import cli
from mcptoon import manifest as manifest_mod


def _run(argv, search=None):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    patches = [patch.object(cli.sys, "argv", ["mcptoon", *argv])]
    if search is not None:
        patches.append(patch.object(manifest_mod, "search_tools", return_value=search))
    for p in patches:
        p.start()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
    finally:
        for p in patches:
            p.stop()
    return out.getvalue(), err.getvalue(), code


# ── mistyped commands ────────────────────────────────────────────────────────

def test_listt_is_unknown_not_manifest():
    out, err, code = _run(["listt"])
    assert code == 1
    assert "unknown command 'listt'" in err
    assert "did you mean" in err
    assert "list" in err
    # Must NOT have run manifest/list (no server listing on stdout).
    assert "[stdio]" not in out and "[http]" not in out


def test_healthh_is_unknown_not_health_check():
    out, err, code = _run(["healthh"])
    assert code == 1
    assert "unknown command 'healthh'" in err
    assert "Health check" not in out


def test_real_command_still_works():
    # A real command with no args must still run (list has no sub-args).
    out, err, code = _run(["list"])
    assert "Configured servers" in out or "No servers configured" in out


def test_genuine_single_token_phrase_still_routes():
    # A lone phrase that is NOT close to a command still reaches keyword routing.
    out, err, code = _run(["有什么工具"])
    # It routes to manifest/list; the point is it is NOT rejected as unknown.
    assert "unknown command" not in err


# ── non-integer flags ────────────────────────────────────────────────────────

def test_head_non_integer_errors():
    out, err, code = _run(["search", "file", "--head", "abc"], search=[{"server": "s", "name": "t", "description": "d", "params": "", "score": 1.0}])
    assert code == 1
    assert "--head needs an integer" in err


def test_head_equals_non_integer_errors():
    out, err, code = _run(["search", "file", "--head=xyz"], search=[])
    assert code == 1
    assert "--head needs an integer" in err


def test_max_chars_non_integer_errors():
    out, err, code = _run(["search", "file", "--max-chars", "nope"], search=[])
    assert code == 1
    assert "--max-chars needs an integer" in err


def test_head_valid_integer_still_works():
    rows = [{"server": "s", "name": f"t{i}", "description": "d", "params": "", "score": 1.0}
            for i in range(5)]
    out, err, code = _run(["search", "file", "--head", "2", "--json"], search=rows)
    assert code == 0
    assert len(json.loads(out)) == 2


def test_select_top_non_integer_errors():
    out, err, code = _run(["select", "file", "--top", "abc"])
    assert code == 1
    assert "--top needs an integer" in err
