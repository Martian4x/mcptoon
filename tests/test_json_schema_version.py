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

"""Every script-facing `--json` payload carries `schema_version` + `mcptoon_version`.

Reported 2026-10-17: `footer-facts --json` (and the other machine views) emitted a
bare object with no version marker, so a consumer could not tell when a field was
renamed. The stable, script-consumed surfaces — footer-facts / status / stats /
usage / bench — now pass their payload through `output.stamp_schema`, which adds
`schema_version` (the shape version, bumped only on breaking change) and the CLI
`mcptoon_version`. These pin both the helper and the command outputs.
"""
import io
import json
import sys
from unittest.mock import patch

import pytest

from mcptoon import output


def _json_capture(argv):
    from mcptoon import cli
    buf = io.StringIO()
    try:
        with patch.object(sys, "argv", ["mcptoon", *argv]), patch("sys.stdout", buf):
            cli.main()
    except SystemExit:
        pass
    return buf.getvalue()


def test_stamp_schema_adds_both_keys():
    d = output.stamp_schema({"a": 1})
    assert d["schema_version"] == output.JSON_SCHEMA_VERSION
    assert "mcptoon_version" in d and d["mcptoon_version"]


def test_stamp_schema_does_not_clobber_existing():
    d = output.stamp_schema({"schema_version": 99, "mcptoon_version": "x"})
    assert d["schema_version"] == 99  # setdefault semantics
    assert d["mcptoon_version"] == "x"


@pytest.mark.parametrize("command", ["footer-facts", "status", "stats", "usage"])
def test_command_json_is_stamped(command):
    raw = _json_capture([command, "--json"])
    payload = json.loads(raw)  # must be valid JSON, not human text
    assert payload.get("schema_version") == output.JSON_SCHEMA_VERSION
    assert payload.get("mcptoon_version")


def test_bench_json_is_stamped_when_it_has_data():
    raw = _json_capture(["bench", "--json"])
    try:
        payload = json.loads(raw)
    except ValueError:
        pytest.skip("bench had no data to report on this machine (prints guidance)")
    assert payload.get("schema_version") == output.JSON_SCHEMA_VERSION
    assert payload.get("mcptoon_version")
