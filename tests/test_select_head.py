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

"""`select --head N` caps the number of top picks shown.

Reported 2026-10-18: `select "..." --head 1` still printed all `--top` results,
because only the json branch forwarded head_n to the renderer. The human path
printed every ranked tool.
"""
import io
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from mcptoon import cli
from mcptoon import manifest as manifest_mod


_MANIFEST = {
    "filesystem": [
        {"name": "read_file", "description": "read a file"},
        {"name": "write_file", "description": "write a file"},
        {"name": "list_directory", "description": "list a directory"},
        {"name": "search_files", "description": "search files"},
    ],
}


def _run_select(argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with patch.object(cli.sys, "argv", ["mcptoon", *argv]), \
         patch.object(manifest_mod, "get_manifest", return_value=_MANIFEST), \
         redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return out.getvalue(), err.getvalue(), code


def test_select_head_caps_human_output():
    out, err, code = _run_select(["select", "file", "--top", "4", "--head", "1"])
    assert out.count("(score:") == 1


def test_select_head_caps_json_output():
    import json
    out, err, code = _run_select(["select", "file", "--top", "4", "--head", "2", "--json"])
    assert len(json.loads(out)) == 2


def test_select_without_head_shows_top():
    out, err, code = _run_select(["select", "file", "--top", "3"])
    assert out.count("(score:") == 3
