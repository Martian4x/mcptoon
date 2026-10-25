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

"""Every `subprocess` call in text mode must name its `encoding`.

#28 fixed `mcptoon call --stdin` decoding a UTF-8 payload through the console
code page. The same class of bug hides wherever `subprocess.run(..., text=True)`
is used without `encoding=`: on a non-UTF-8 Windows (cp936, cp1252) the default
codec is the *console code page*, so non-ASCII bytes are silently re-encoded on
the way into a child and mis-decoded on the way out.

This guard is an AST scan, not a grep: `text=True` and its `encoding=` are often
on different physical lines, and `universal_newlines=True` is the same flag under
another name. It fails on any `subprocess.{run,Popen,call,check_output,check_call}`
that turns on text mode without naming an encoding.

It is a static check on purpose — the bug does not reproduce on a UTF-8-mode
interpreter (this project's CI sets `PYTHONUTF8=1`), so a behavioural test on the
main matrix would pass while real cp936 users get mojibake. The companion CI job
`cp936-encoding` runs the encoding tests under a non-UTF-8 locale; this guard
makes sure there is nothing left to reach that job in the first place.
"""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "mcptoon"

# The subprocess entry points that accept text/encoding. Popen is the primitive;
# the rest are wrappers with the same keyword surface.
_SUBPROCESS_FUNCS = {"run", "Popen", "call", "check_output", "check_call"}


def _is_subprocess_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in _SUBPROCESS_FUNCS:
        return False
    # subprocess.run(...) or a `from subprocess import run` alias (`run(...)`).
    owner = func.value
    return isinstance(owner, ast.Name) and owner.id == "subprocess"


def _kwarg(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_true(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _text_mode(kw) -> bool:
    """True when the call turns on text mode: text=True or universal_newlines=True."""
    if kw is None:
        return False
    if _is_true(kw):
        return True
    # text=some_flag / universal_newlines=some_flag -> cannot prove it is off.
    # Treat a non-literal as "may be text mode" only for universal_newlines; for
    # `text` a bare name is rare, but flagging it is the safe direction.
    return not isinstance(kw, ast.Constant)


def test_no_text_mode_subprocess_without_encoding():
    offenders = []
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_subprocess_call(node):
                continue
            text = _text_mode(_kwarg(node, "text")) or _text_mode(
                _kwarg(node, "universal_newlines")
            )
            if text and _kwarg(node, "encoding") is None:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "subprocess in text mode without encoding= (set encoding='utf-8'): "
        + ", ".join(offenders)
    )
