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

"""`--stdin` must decode UTF-8, whatever the console code page says.

Found on 2026-10-16 pushing patched plugin sources to GitHub: five files containing
Chinese went through `mcptoon call github push_files --stdin`, and every one of them
landed as mojibake. The proof was arithmetic rather than eyeballs — recomputing
`git hash-object` over `utf8_bytes.decode("cp936", errors="replace")` reproduced the
blob SHA GitHub reported for all five, while the one file that happened to be pure
ASCII came back byte-identical. Nothing in the CLI's output hinted at it: the payload
was already destroyed before `json.loads` ever saw it.

Windows binds `sys.stdin` to the active code page, so on a Chinese console a UTF-8
payload is re-decoded as cp936 on the way in, and `errors="replace"` makes the damage
irreversible (U+FFFD where the bytes went unmappable). `demo_server.py` already
reconfigures its stdio for this exact reason, with a comment; the CLI's two `--stdin`
readers in `_cmd_call` never got the same treatment.

These tests pin the fix's two halves: a UTF-8 payload survives a cp936 console, and a
payload genuinely written in the console code page is still preserved — because a
caller piping from PowerShell 5.1 is not doing anything wrong either.
"""
import io
import json
import sys

from mcptoon import cli

NOTE = "中文测试"
PAYLOAD = json.dumps({"note": NOTE}, ensure_ascii=False)


class ConsoleStdin:
    """`sys.stdin` as Windows hands it to us: text decoded from a given code page."""

    def __init__(self, raw, encoding):
        self._raw = raw
        self.encoding = encoding

    @property
    def buffer(self):
        raw = self._raw

        class _Buffer:
            def read(self):
                return raw

        return _Buffer()

    def read(self):
        return self._raw.decode(self.encoding, errors="replace")


def _read_with(raw, encoding):
    saved = sys.stdin
    try:
        sys.stdin = ConsoleStdin(raw, encoding)
        return json.loads(cli._read_stdin_text())["note"]
    finally:
        sys.stdin = saved


def test_utf8_payload_survives_a_cp936_console():
    assert _read_with(PAYLOAD.encode("utf-8"), "cp936") == NOTE


def test_utf8_payload_survives_a_cp1252_console():
    assert _read_with(PAYLOAD.encode("utf-8"), "cp1252") == NOTE


def test_console_encoded_payload_is_still_preserved():
    # A caller piping from a legacy console sends cp936 bytes; that is not UTF-8, so the
    # fallback decodes it the way the stream claims and nothing is lost either way.
    assert _read_with(PAYLOAD.encode("cp936"), "cp936") == NOTE


def test_ascii_payload_is_unchanged():
    assert _read_with(b'{"note": "plain"}', "cp936") == "plain"


def test_empty_stdin_reads_as_empty_string():
    saved = sys.stdin
    try:
        sys.stdin = ConsoleStdin(b"", "cp936")
        assert cli._read_stdin_text() == ""
    finally:
        sys.stdin = saved


def test_replaced_stream_without_buffer_still_works():
    saved = sys.stdin
    try:
        sys.stdin = io.StringIO(PAYLOAD)
        assert json.loads(cli._read_stdin_text())["note"] == NOTE
    finally:
        sys.stdin = saved
