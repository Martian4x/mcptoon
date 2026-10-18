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

"""Zero-match `search`/`select` in machine formats must stay parseable.

Reported 2026-10-18: the empty branch printed human text + tips for EVERY format,
so `mcptoon search q --json | jq` (and `select ... --json`) crashed on non-JSON.
Human formats keep the friendly tips; machine formats emit an empty structure.
"""
import io
import json
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from mcptoon import cli
from mcptoon import manifest as manifest_mod


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with patch.object(cli.sys, "argv", ["mcptoon", *argv]), \
         patch.object(manifest_mod, "search_tools", return_value=[]), \
         patch.object(manifest_mod, "get_manifest", return_value={}), \
         redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return out.getvalue(), err.getvalue(), code


def test_search_zero_match_json_is_empty_list():
    out, err, code = _run(["search", "zzzznomatch", "--json"])
    assert json.loads(out) == []
    assert code == 0


def test_search_zero_match_slim_is_blank_not_prose():
    out, err, code = _run(["search", "zzzznomatch", "--slim"])
    assert "No tools found" not in out
    assert out.strip() == ""
    assert code == 0


def test_search_zero_match_toon_is_not_prose():
    out, err, code = _run(["search", "zzzznomatch", "--toon"])
    assert "No tools found" not in out
    assert code == 0


def test_search_zero_match_human_keeps_tips():
    out, err, code = _run(["search", "zzzznomatch"])
    assert "No tools found matching 'zzzznomatch'." in out
    assert "install --search" in out
    assert code == 0


def test_select_zero_match_json_is_empty_list():
    out, err, code = _run(["select", "zzzznomatch", "--json"])
    assert json.loads(out) == []
    assert code == 0


def test_select_zero_match_human_keeps_tips():
    # A non-empty catalog whose ranking comes back empty is the true zero-match
    # path (distinct from "no tools installed at all").
    out, err = io.StringIO(), io.StringIO()
    with patch.object(cli.sys, "argv", ["mcptoon", "select", "zzzznomatch"]), \
         patch.object(manifest_mod, "get_manifest",
                      return_value={"s": [{"name": "t", "description": "x"}]}), \
         patch.object(manifest_mod, "rank_tools", return_value=([], "lexical")), \
         redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main()
        except SystemExit:
            pass
    assert "No tools found" in out.getvalue()
    assert "install --search" in out.getvalue()
