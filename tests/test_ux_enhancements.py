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
import sys
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


def _run_config(argv):
    """Drive `_cmd_config`, capturing (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            cli._cmd_config(argv, "auto")
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

    def test_json_output_is_pure_data_and_carries_a_call_hint_field(self):
        # `--json` must stay parseable JSON. The human `→` prose line is not
        # appended, but the same next step is available *as data* in `call_hint`
        # so an agent driving the CLI can reach the call too (enhancement #1).
        hit = [{"server": "s", "name": "t", "description": "d",
                "params": "a:string*", "score": 1.0}]
        out = io.StringIO()
        with patch("mcptoon.manifest.search_tools", return_value=hit), \
                redirect_stdout(out):
            cli._cmd_search(["t"], "json", 0, 0, False)
        parsed = json.loads(out.getvalue())
        assert isinstance(parsed, list)
        assert parsed[0]["call_hint"] == "→ mcptoon call s t '{\"a\": \"...\"}'"
        # No bare prose line: the only "mcptoon call" text is inside the field.
        assert "→ mcptoon call" not in out.getvalue().split('"call_hint"')[0]


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


# ─── 4. call warns about a missing required arg (cache-only) ────────────────

class TestCallMissingArgWarning:
    def _schema(self):
        return {"inputSchema": {"properties": {"a": {"type": "number"},
                                               "b": {"type": "number"}},
                                "required": ["a", "b"]}}

    def test_missing_arg_is_named_before_the_call(self):
        with patch("mcptoon.cache.get_cached_tools_any_age",
                   return_value=([{"name": "add", **self._schema()}], 1.0)), \
                patch.object(cli, "call_tool", return_value={"ok": True}):
            code, out, err = _run_call(["srv", "add", '{"a":1}'])
        assert "Warning: missing required argument(s) for add: b" in err
        assert "mcptoon inspect srv add" in err

    def test_no_warning_when_all_required_args_present(self):
        with patch("mcptoon.cache.get_cached_tools_any_age",
                   return_value=([{"name": "add", **self._schema()}], 1.0)), \
                patch.object(cli, "call_tool", return_value={"ok": True}):
            code, out, err = _run_call(["srv", "add", '{"a":1,"b":2}'])
        assert "Warning" not in err

    def test_no_warning_when_schema_not_cached(self):
        # Cache miss must never block or spawn a server — it just says nothing.
        with patch("mcptoon.cache.get_cached_tools_any_age", return_value=(None, None)), \
                patch.object(cli, "call_tool", return_value={"ok": True}):
            code, out, err = _run_call(["srv", "add", '{"a":1}'])
        assert "Warning" not in err


# ─── 5. completion --install --dry previews without writing ─────────────────

class TestCompletionDry:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
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

    def test_dry_prints_target_and_block_but_writes_nothing(self, home):
        code, out, err = self._run(["bash", "--install", "--dry"])
        assert code == 0
        assert "Would install" in out
        assert cli._COMPLETION_BEGIN in out
        assert "No file was changed" in out
        assert not (home / ".bashrc").exists(), "--dry must not create the rc file"

    def test_dry_reports_replace_when_block_exists(self, home):
        rc = home / ".bashrc"
        rc.write_text(cli._completion_block("old body"), encoding="utf-8")
        code, out, err = self._run(["bash", "--install", "--dry"])
        assert code == 0
        assert "replace the existing block" in out
        # File is untouched: still the old body, not regenerated.
        assert "old body" in rc.read_text(encoding="utf-8")


# ─── 6. manifest --format prints a save hint only on a TTY ──────────────────

class TestManifestExportSaveHint:
    def _run(self, isatty, fmt="auto"):
        out, err = io.StringIO(), io.StringIO()
        manifest = {"srv": [{"name": "t", "description": "d", "inputSchema": {}}]}
        with patch("mcptoon.manifest.get_manifest", return_value=manifest), \
                patch.object(sys, "stdout", new=out), \
                redirect_stderr(err):
            out.isatty = lambda: isatty  # type: ignore[attr-defined]
            cli._cmd_manifest([], fmt, 0, 0, False, export_format="openai")
        return out.getvalue(), err.getvalue()

    def test_save_hint_shown_on_a_terminal(self):
        out, err = self._run(isatty=True)
        assert "Save to a file: mcptoon manifest --format openai > tools.json" in err

    def test_no_hint_when_redirected(self):
        out, err = self._run(isatty=False)
        assert "Save to a file" not in err
        # The export itself is still emitted on stdout.
        assert out.strip().startswith("[") or out.strip().startswith("{")


# ─── 7. policy set states the scope in plain words ──────────────────────────

class TestPolicyScopeHint:
    def _run(self, argv):
        out = io.StringIO()
        with patch("mcptoon.config.load_config", return_value={"img": {"transport": "stdio"}}), \
                patch("mcptoon.cache.get_cached_tools_any_age",
                      return_value=([{"name": "generate"}], 1.0)), \
                redirect_stdout(out):
            try:
                cli._cmd_policy(argv, "auto", 0, 0, False)
            except SystemExit:
                pass
        return out.getvalue()

    def test_wildcard_tool_says_every_tool(self):
        out = self._run(["set", "img", "*", "raw"])
        assert "Scope: every tool on 'img'" in out

    def test_single_tool_says_only_that_tool(self):
        out = self._run(["set", "img", "generate", "raw"])
        assert "Scope: only 'img:generate'" in out


# ─── 8. toggle states the undo command ──────────────────────────────────────

class TestToggleRestoreHint:
    def _run(self, argv):
        out = io.StringIO()
        with patch("mcptoon.config.load_config", return_value={"srv": {"transport": "stdio"}}), \
                patch("mcptoon.cache.get_cached_tools_any_age",
                      return_value=([{"name": "echo"}], 1.0)), \
                redirect_stdout(out):
            try:
                cli._cmd_toggle(argv, "auto")
            except SystemExit:
                pass
        return out.getvalue()

    def test_disable_shows_re_enable_command(self):
        # Toggle to disabled, then read the hint.
        out = self._run(["srv", "echo"])
        assert "DISABLED" in out
        assert "Re-enable it:     mcptoon toggle srv echo" in out
        # Toggle back so the persisted state does not leak into other tests.
        self._run(["srv", "echo"])


# ─── 9. stats / report say --head does not apply ────────────────────────────

class TestAggregateHeadNote:
    def test_stats_notes_head_does_not_apply(self):
        out = io.StringIO()
        manifest = {"srv": [{"name": "t", "description": "d", "inputSchema": {}}]}
        with patch("mcptoon.manifest.get_manifest", return_value=manifest), \
                redirect_stdout(out):
            cli._cmd_stats([], "auto", head_n=1)
        assert "note: --head 1 does not apply to this dashboard" in out.getvalue()

    def test_stats_is_silent_about_head_when_none_given(self):
        out = io.StringIO()
        manifest = {"srv": [{"name": "t", "description": "d", "inputSchema": {}}]}
        with patch("mcptoon.manifest.get_manifest", return_value=manifest), \
                redirect_stdout(out):
            cli._cmd_stats([], "auto", head_n=0)
        assert "--head" not in out.getvalue()


# ─── 10. config get suggests the nearest key ────────────────────────────────

class TestConfigGetDidYouMean:
    def test_near_miss_key_gets_a_suggestion(self):
        code, out, err = _run_config(["get", "footr"])
        assert code == 1
        assert "Unknown setting: footr" in err
        assert "did you mean: footer" in err

    def test_far_key_has_no_suggestion(self):
        code, out, err = _run_config(["get", "zzzzzz"])
        assert code == 1
        assert "did you mean" not in err

