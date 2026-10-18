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

"""Three UX affordances that turn a dead end into a next step.

None of these is a bug fix; each removes a place where the CLI made the user
do work the CLI already knows how to do:

1. `search` stopped at `params: message:string*` — the user still had to assemble
   the `call` JSON by hand and spot the `*` marking required arguments. A hit now
   prints a ready-to-edit `→ mcptoon call ...` line.
2. `call` with malformed JSON printed a raw Python `JSONDecodeError` and nothing
   else. It now follows with the correct invocation (required args filled in from
   the tool schema) and the `inspect` pointer.
3. `completion` only printed a script and told the user to paste it somewhere.
   `--install` / `--uninstall` now write into the right rc file, idempotently,
   inside markers so uninstall removes only what mcptoon added.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from mcptoon import cli


# ─── helpers ────────────────────────────────────────────────────────────────

def _run_cmd_search(argv, results):
    out = io.StringIO()
    with patch("mcptoon.manifest.search_tools", return_value=results), \
            redirect_stdout(out):
        cli._cmd_search(argv, "auto", 0, 0, False)
    return out.getvalue()


def _run_call(argv):
    """Drive `_cmd_call`, capturing (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            cli._cmd_call(argv, "json", 0, 0, False)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    return code, out.getvalue(), err.getvalue()


# ─── 1. search prints a copyable call line ──────────────────────────────────

class TestSearchCallHint:
    def test_required_arg_is_filled_into_a_callable_line(self):
        hit = [{"server": "filesystem", "name": "read_file", "description": "read",
                "params": "path:string*,encoding:string", "score": 1.0}]
        out = _run_cmd_search(["read"], hit)
        assert "→ mcptoon call filesystem read_file" in out
        # Only the required `path` is in the skeleton; the optional encoding is not.
        assert '{"path": "..."}' in out
        assert "encoding" not in out.split("→ mcptoon call")[1].splitlines()[0]

    def test_the_printed_call_line_is_valid_pasteable_json(self):
        hit = [{"server": "db", "name": "query", "description": "sql",
                "params": "sql:string*,limit:number", "score": 1.0}]
        out = _run_cmd_search(["sql"], hit)
        line = next(l for l in out.splitlines() if "→ mcptoon call" in l)
        # Extract the JSON between the quotes and prove it parses.
        payload = line[line.index("'") + 1:line.rindex("'")]
        parsed = json.loads(payload)
        assert parsed == {"sql": "..."}

    def test_numeric_required_arg_uses_a_number_not_a_string(self):
        hit = [{"server": "s", "name": "t", "description": "d",
                "params": "count:number*,flag:boolean", "score": 1.0}]
        out = _run_cmd_search(["t"], hit)
        line = next(l for l in out.splitlines() if "→ mcptoon call" in l)
        payload = line[line.index("'") + 1:line.rindex("'")]
        parsed = json.loads(payload)
        assert parsed == {"count": 0}

    def test_no_required_args_shows_an_empty_object(self):
        hit = [{"server": "s", "name": "ping", "description": "d",
                "params": "verbose:boolean", "score": 1.0}]
        out = _run_cmd_search(["ping"], hit)
        assert "→ mcptoon call s ping '{}'" in out

    def test_the_call_hint_is_not_printed_in_machine_formats(self):
        # `--json` must stay pure JSON; the human hint lives only on the human path.
        hit = [{"server": "s", "name": "t", "description": "d",
                "params": "a:string*", "score": 1.0}]
        out = io.StringIO()
        with patch("mcptoon.manifest.search_tools", return_value=hit), \
                redirect_stdout(out):
            cli._cmd_search(["t"], "json", 0, 0, False)
        parsed = json.loads(out.getvalue())
        assert isinstance(parsed, list)
        assert "mcptoon call" not in out.getvalue()


# ─── 2. call JSON parse error gains a correct-form hint ─────────────────────

class TestCallJsonErrorHint:
    def _schema(self):
        return {"inputSchema": {"properties": {"path": {"type": "string"}},
                                "required": ["path"]}}

    def test_bad_json_shows_the_correct_form_and_inspect_pointer(self):
        with patch("mcptoon.manifest.inspect_tool", return_value=self._schema()):
            code, out, err = _run_call(["fs", "read_file", "{bad"])
        assert code == 1
        assert "Error parsing JSON args" in err
        assert "Correct form: mcptoon call fs read_file '{\"path\": \"...\"}'" in err
        assert "mcptoon inspect fs read_file" in err

    def test_hint_falls_back_when_the_schema_is_unavailable(self):
        with patch("mcptoon.manifest.inspect_tool", return_value=None):
            code, out, err = _run_call(["fs", "read_file", "{bad"])
        assert code == 1
        assert "Correct form: mcptoon call fs read_file" in err
        # Generic placeholder because there was no schema to fill in.
        assert '{"arg":"value"}' in err

    def test_auto_branch_shows_a_generic_correct_form(self):
        with patch("mcptoon.manifest.inspect_tool", return_value=None):
            code, out, err = _run_call(["--auto", "read_file", "{bad"])
        assert code == 1
        assert "Correct form: mcptoon call <server> <tool>" in err
        assert "mcptoon inspect <server> <tool>" in err


# ─── 3. completion --install / --uninstall ──────────────────────────────────

class TestCompletionInstall:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        """Redirect Path.home() to an isolated dir for the rc-file writes."""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        return tmp_path

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with redirect_stdout(out), redirect_stderr(err):
                cli._cmd_completion(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def test_install_writes_the_block_into_the_rc_file(self, home):
        code, out, err = self._run(["bash", "--install"])
        assert code == 0
        rc = home / ".bashrc"
        assert rc.exists()
        text = rc.read_text(encoding="utf-8")
        assert cli._COMPLETION_BEGIN in text
        assert cli._COMPLETION_END in text
        assert "complete -F _mcptoon_complete mcptoon" in text

    def test_install_is_idempotent(self, home):
        self._run(["bash", "--install"])
        rc = home / ".bashrc"
        first = rc.read_text(encoding="utf-8")
        code, out, err = self._run(["bash", "--install"])
        assert code == 0
        assert "already installed" in out
        assert rc.read_text(encoding="utf-8") == first

    def test_install_preserves_existing_user_content(self, home):
        rc = home / ".bashrc"
        rc.write_text("alias ll='ls -l'\nexport FOO=1\n", encoding="utf-8")
        self._run(["bash", "--install"])
        text = rc.read_text(encoding="utf-8")
        assert "alias ll='ls -l'" in text
        assert "export FOO=1" in text
        assert cli._COMPLETION_BEGIN in text

    def test_uninstall_removes_only_the_block(self, home):
        rc = home / ".bashrc"
        rc.write_text("alias ll='ls -l'\n", encoding="utf-8")
        self._run(["bash", "--install"])
        code, out, err = self._run(["bash", "--uninstall"])
        assert code == 0
        text = rc.read_text(encoding="utf-8")
        assert cli._COMPLETION_BEGIN not in text
        assert "complete -F _mcptoon_complete mcptoon" not in text
        assert "alias ll='ls -l'" in text

    def test_uninstall_when_absent_is_a_noop(self, home):
        code, out, err = self._run(["bash", "--uninstall"])
        assert code == 0
        assert "not installed" in out

    def test_install_with_no_shell_detects_one(self, home, monkeypatch):
        # On this platform (Windows) detection defaults to PowerShell; either way
        # it must write *some* profile and not error out.
        code, out, err = self._run(["--install"])
        assert code == 0
        assert "installed in" in out

    def test_plain_completion_still_prints_the_script_without_markers(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cli._cmd_completion(["bash"])
        text = out.getvalue()
        assert "complete -F _mcptoon_complete mcptoon" in text
        assert cli._COMPLETION_BEGIN not in text

    def test_unknown_shell_still_errors_with_install(self, home):
        code, out, err = self._run(["tcsh", "--install"])
        assert code == 1
        assert "Unknown shell" in err
