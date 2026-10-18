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

"""CLI-layer tests for `mcptoon select`.

`test_select_engine.py` tests the ranking engines in isolation, and every one of
its cases feeds `rank_tools` a query that matches — so none of them exercised the
caller's handling of an *empty* ranking. `_cmd_select` indexed `ranked[0]` and
crashed with IndexError + a traceback on any zero-match query (reported
2026-10-17: two Chinese queries against an all-English catalog both crashed).
These tests drive the actual command function and pin the two contracts: a
zero-match prints the same tips `search` does and exits 0, and a match still
works.
"""

import json

import pytest

from mcptoon import cli
from mcptoon import config as cfg
from mcptoon import manifest as manifest_mod

CATALOG = {
    "filesystem": [
        {"name": "read_file", "description": "Read the complete contents of a file as text"},
        {"name": "search_files", "description": "Recursively search for files matching a pattern"},
    ],
    "exa": [
        {"name": "search", "description": "Search the web"},
    ],
}


def _settings(tmp_path):
    return pytest.MonkeyPatch()


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Catalog injected, settings pinned to an empty file (default lexical engine)."""
    monkeypatch.setattr(manifest_mod, "get_manifest", lambda use_cache=True: CATALOG)
    monkeypatch.setattr(cfg, "_settings_file", lambda: tmp_path / "settings.json")
    return tmp_path


def test_zero_match_prints_tips_and_exits_0(wired, capsys):
    """The regression: a zero-match query must not raise, and must exit 0."""
    with pytest.raises(SystemExit) as ei:
        cli._cmd_select(["zzzzz-no-overlap-qqq"], fmt="auto")
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "No tools found" in out
    assert "mcptoon list" in out
    assert "install --search" in out


def test_zero_match_never_raises_indexerror(wired):
    """Belt and suspenders: the old bug was an uncaught IndexError."""
    try:
        cli._cmd_select(["qqqqqqqq"], fmt="auto")
    except SystemExit:
        pass
    except IndexError as e:  # pragma: no cover - this is the failure we fixed
        raise AssertionError(f"select still crashes on zero match: {e}") from e


def test_matching_query_still_prints_results(wired, capsys):
    # A match returns normally (the human branch does not sys.exit).
    cli._cmd_select(["read", "file"], fmt="auto")
    out = capsys.readouterr().out
    assert "Top" in out and "read_file" in out
    assert "Inspect one" in out


def test_json_zero_match_is_also_clean(wired, capsys):
    """--json on a miss must emit parseable JSON (an empty list), not prose.

    Previously the zero-match branch printed human text for every format, so
    `mcptoon select q --json | jq` crashed on non-JSON (reported 2026-10-18).
    """
    with pytest.raises(SystemExit) as ei:
        cli._cmd_select(["zzzzz"], fmt="json")
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert json.loads(out) == []
