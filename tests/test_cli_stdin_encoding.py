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
import os
import subprocess
import sys
from pathlib import Path

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


def test_utf8_payload_survives_a_real_gbk_console():
    """End-to-end: a *real* non-UTF-8 stdin in a child process, not a fake stream.

    The tests above swap `sys.stdin` for a Python object, which proves the code path
    but not that Python would actually bind stdin to the console code page. This one
    launches a child with UTF-8 mode off and stdin announced as GBK (cp936), writes
    the UTF-8 payload bytes to its pipe, and asks it to print what `_read_stdin_text`
    decoded. If the fix regresses, the child prints mojibake (or raises) and the
    assertion fires — on Linux, macOS and Windows alike, because the encoding is
    driven by PYTHONIOENCODING, not by the host's locale.
    """
    child = (
        "import sys\n"
        "from mcptoon import cli\n"
        "sys.stdout.buffer.write(cli._read_stdin_text().encode('utf-8'))\n"
    )
    env = os.environ.copy()
    env["PYTHONUTF8"] = "0"          # turn UTF-8 mode off, so stdin honours the codec
    env["PYTHONIOENCODING"] = "gbk"  # bind stdin/stdout to cp936, like a Chinese console
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    proc = subprocess.run(
        [sys.executable, "-c", child],
        input=PAYLOAD.encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout.decode("utf-8") == PAYLOAD


# ── the *stdout* side: a GBK console must not crash a command that prints glyphs ──
def _run_under_gbk(args, home):
    """Run `python -m mcptoon <args>` in a child whose stdout is cp936/GBK.

    UTF-8 mode off + PYTHONIOENCODING=gbk binds stdout to cp936, exactly like a
    default Chinese Windows console — the environment where 0.8.29 raised
    `UnicodeEncodeError` on `✓ 🎉 ⚠ ❌`. MCPTOON_HOME points at an empty temp dir so
    the command stays fast and never touches a real config.
    """
    env = os.environ.copy()
    env["PYTHONUTF8"] = "0"
    env["PYTHONIOENCODING"] = "gbk"
    env["MCPTOON_HOME"] = str(home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env.pop("PYTHONIOENCODING_OVERRIDE", None)
    return subprocess.run(
        [sys.executable, "-m", "mcptoon", *args],
        capture_output=True, env=env, timeout=120,
    )


def test_glyph_printing_commands_do_not_crash_on_a_gbk_console(tmp_path):
    """`status`, `footer-facts` and `doctor` print ✓/⚠/🎉/✗ — on cp936 they used to
    raise `UnicodeEncodeError` and exit with a raw traceback. After the startup
    encoding guard they must complete without a traceback."""
    for args in (["status"], ["footer-facts"], ["doctor"]):
        proc = _run_under_gbk(args, tmp_path)
        err = proc.stderr.decode("utf-8", "replace")
        assert "UnicodeEncodeError" not in err, f"{args} crashed on GBK: {err}"
        assert "Traceback" not in err, f"{args} raised on GBK: {err}"
        # doctor legitimately exits 1 with no config; a crash would exit 1 too, so
        # the traceback check above is what distinguishes them. status/footer exit 0.
        if args[0] in ("status", "footer-facts"):
            assert proc.returncode == 0, err


def test_glyph_survives_to_a_piped_gbk_console(tmp_path):
    """Piped (not a tty) output is switched to UTF-8, so a glyph reaches downstream
    intact instead of being dropped or crashing."""
    proc = _run_under_gbk(["footer-facts"], tmp_path)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    # footer-facts prints the 🎉 mark; in a pipe we force utf-8, so its bytes survive.
    assert "🎉".encode() in proc.stdout


def test_harden_stream_encoding_never_raises():
    """The guard is a safety net: a stream without `reconfigure`, or one that raises
    when reconfigured, must be skipped silently — never fail the command."""
    from mcptoon import cli

    class NoReconfigure:
        def write(self, _):
            pass

        def flush(self):
            pass

    class RaisingStream:
        def reconfigure(self, **_):
            raise OSError("cannot reconfigure")

        def isatty(self):
            return False

    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = NoReconfigure(), RaisingStream()
        cli._harden_stream_encoding()  # must not raise
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
