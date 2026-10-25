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

"""mcptoon exec — run code so only its stdout reaches the context (zero dependencies).

Why this exists
---------------
Every other mcptoon command *brings data back*: ``mcptoon call`` forwards a tool
result, ``mcptoon manifest`` lists tools, ``mcptoon docs resolve`` returns paths.
That is the right shape when the answer is small. It is the wrong shape when the
answer is one number hiding behind a large pile of data — counting lines across
50 files, summing a log, grepping a tree. Fetching the pile costs context the
model cannot get back; the model only ever needed the number.

``exec`` flips the direction: the *code* goes to the data and only stdout comes
back. One script replaces ten round trips, and the bytes it would have dragged
into the window never leave the subprocess.

**What "sandbox" means here — and does not mean.** This is a *context* sandbox,
not a security sandbox. The subprocess has the same filesystem, network and
credential access as the shell you launched mcptoon from; it is your machine and
your code. What is contained is the *data*: raw stdout stays out of the context
window unless it is small enough to be worth it. Do not read "sandbox" as
"isolated" — it is not, and this module will not pretend otherwise.

The lossless contract still holds
---------------------------------
``mcptoon`` promises that compression hides information but never destroys it
(see :mod:`mcptoon.ccr`). ``exec`` keeps that promise: when stdout is larger than
the budget, the **full** stdout is written to the retrieve store and the context
gets a head/tail view carrying a handle. ``mcptoon retrieve <handle>`` gets the
rest back. An ``exec`` that printed 4 MB is therefore still fully recoverable —
it just does not cost 4 MB of context to find out it was 4 MB.

Runtimes are discovered, not assumed
------------------------------------
A language is offered only when its interpreter is actually on PATH. Advertising
"12 languages" and failing on 9 of them is the kind of claim this project keeps
deleting. ``mcptoon exec --langs`` prints exactly what this machine can run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

# ─── Defaults ───

#: Seconds before the child is killed. Long enough for a real batch job, short
#: enough that a runaway loop does not hang a terminal forever.
DEFAULT_TIMEOUT = 120

#: Bytes of stdout the context is allowed to see before the view switches to
#: head/tail + handle. 8 KB is roughly where a result stops being "the answer"
#: and starts being "the data".
DEFAULT_MAX_OUTPUT = 8192

#: Lines of stderr echoed back. stderr is diagnostic, not the answer — a Python
#: traceback is worth 40 lines, a compiler dump is not worth 4,000.
STDERR_TAIL_LINES = 40

#: Bytes of stderr kept at all (before the line cap), so a single monstrous
#: stderr line cannot blow the budget on its own.
STDERR_MAX_BYTES = 8192


class ExecError(Exception):
    """A failure that is the caller's problem (unknown language, bad file)."""


@dataclass
class ExecResult:
    """One run's outcome. ``stdout`` is already reduced to what the context sees."""

    ok: bool
    exit_code: int | None
    lang: str
    stdout: str
    stderr_tail: str
    elapsed_ms: int
    #: True when stdout exceeded the budget and ``stdout`` is a head/tail view.
    truncated: bool = False
    #: Full stdout byte count, before any reduction.
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    #: Retrieve handle for the full stdout when ``truncated``; None otherwise.
    handle: str | None = None
    #: Why the run failed, when it did not simply exit non-zero.
    failure: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "lang": self.lang,
            "stdout": self.stdout,
            "stderr_tail": self.stderr_tail,
            "elapsed_ms": self.elapsed_ms,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "truncated": self.truncated,
        }
        if self.handle:
            d["handle"] = self.handle
        if self.failure:
            d["failure"] = self.failure
        if self.notes:
            d["notes"] = self.notes
        return d


# ─── Runtime registry ───
#
# Each entry answers two questions: is the interpreter here, and how do I hand it
# a program as text? ``code_flag`` is the "run this string" switch; ``file_flag``
# is "run this path". Both are argv lists — nothing is ever joined into a shell
# string, so a payload containing quotes, ``;`` or ``$()`` is data, not syntax.

@dataclass(frozen=True)
class Runtime:
    name: str
    aliases: tuple[str, ...]
    exe: tuple[str, ...]          # argv prefix that launches the interpreter
    code_flag: tuple[str, ...]    # how to pass inline source
    file_flag: tuple[str, ...]    # how to pass a file path
    ext: str                      # temp-file suffix for inline source


def _pwsh_exe() -> str | None:
    for name in ("pwsh", "powershell"):
        p = shutil.which(name)
        if p:
            return p
    return None


def available_runtimes() -> list[Runtime]:
    """Every runtime this machine can actually run, in a stable order."""
    out: list[Runtime] = []

    out.append(Runtime(
        "python", ("py", "python3"), (sys.executable,), ("-c",), (), ".py",
    ))

    node = shutil.which("node")
    if node:
        out.append(Runtime(
            "javascript", ("js", "node"), (node,), ("-e",), (), ".mjs",
        ))

    sh = _pwsh_exe()
    if sh:
        out.append(Runtime(
            "powershell", ("pwsh", "ps", "shell"), (sh, "-NoProfile", "-NonInteractive"),
            ("-Command",), ("-File",), ".ps1",
        ))

    bash = shutil.which("bash")
    if bash:
        out.append(Runtime(
            "bash", ("sh",), (bash,), ("-c",), (), ".sh",
        ))

    ruby = shutil.which("ruby")
    if ruby:
        out.append(Runtime("ruby", ("rb",), (ruby,), ("-e",), (), ".rb"))

    return out


def resolve_runtime(lang: str) -> Runtime:
    """Find the runtime for ``lang`` or raise :class:`ExecError` naming the options."""
    key = lang.strip().lower()
    runtimes = available_runtimes()
    for rt in runtimes:
        if key == rt.name or key in rt.aliases:
            return rt
    known = ", ".join(rt.name for rt in runtimes) or "(none detected)"
    raise ExecError(f"unknown language '{lang}'. On this machine: {known}")


# ─── Output reduction ───

def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _head_tail(text: str, budget: int) -> str:
    """Keep both ends of an over-budget string.

    Head-only is the wrong cut for program output: the first N bytes of a log are
    usually the boring part, and the last N bytes are the answer. Splitting the
    budget keeps the shape (how it started, how it ended) in one view. The marker
    in the middle says what was elided so a reader never mistakes the join for
    real adjacent text.
    """
    if len(text) <= budget:
        return text
    marker = f"\n... [mcptoon exec: {len(text)} chars total, middle elided — mcptoon retrieve {{handle}} for the rest] ...\n"
    # The marker names a handle placeholder; the caller substitutes the real one.
    keep = max(0, budget - len(marker))
    head = keep // 2
    tail = keep - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def reduce_stdout(text: str, *, max_output: int, lang: str,
                  cwd: str | None = None) -> tuple[str, bool, str | None]:
    """Return ``(visible_text, truncated, handle)``.

    Under budget: everything is visible, nothing is stored. Over budget: the full
    text is stored (so it is still recoverable) and the visible view is a head/tail
    split carrying the handle.
    """
    if max_output <= 0 or len(text.encode("utf-8")) <= max_output:
        return text, False, None

    handle = None
    try:
        from . import ccr as _ccr
        handle = _ccr.store_ex(text, server="mcptoon", tool=f"exec:{lang}").handle
    except Exception:
        # A broken store must not fail the run; it must also not imply the full
        # output is retrievable. Leaving handle None says exactly that.
        handle = None

    view = _head_tail(text, max_output)
    if handle:
        view = view.replace("{handle}", handle)
    else:
        view = view.replace(" — mcptoon retrieve {handle} for the rest", "")
    return view, True, handle


# ─── The run ───

def _build_argv(rt: Runtime, code: str | None, file_path: str | None,
                tmpdir: str) -> tuple[list[str], str | None]:
    """Return ``(argv, temp_file_to_clean)`` for one invocation."""
    if file_path is not None:
        argv = list(rt.exe) + list(rt.file_flag) + [file_path]
        return argv, None

    if rt.code_flag == ("-File",):
        # PowerShell's -Command mangles multiline quoting in ways that differ by
        # version; a temp script file is the one form that round-trips a program
        # verbatim. Same reason the CLI itself avoids long inline pwsh strings.
        fd, path = tempfile.mkstemp(suffix=rt.ext, dir=tmpdir)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(code or "")
        return list(rt.exe) + ["-File", path], path

    return list(rt.exe) + list(rt.code_flag) + [code or ""], None


def run(lang: str, code: str | None = None, *, file_path: str | None = None,
        timeout: int = DEFAULT_TIMEOUT, max_output: int = DEFAULT_MAX_OUTPUT,
        cwd: str | None = None, env: dict[str, str] | None = None) -> ExecResult:
    """Run ``code`` (or ``file_path``) in ``lang`` and return the reduced result.

    Never raises for anything the child did — a crash, a timeout and a non-zero
    exit all come back as a populated :class:`ExecResult`. It raises
    :class:`ExecError` only when the request itself cannot be honoured (unknown
    language, missing file), because those are not "the program failed", they are
    "there was no program to run".
    """
    rt = resolve_runtime(lang)

    if file_path is not None:
        if not os.path.isfile(file_path):
            raise ExecError(f"file not found: {file_path}")
        file_path = os.path.abspath(file_path)
    elif code is None:
        raise ExecError("nothing to run: pass code or --file")

    if timeout <= 0:
        raise ExecError("--timeout must be a positive number of seconds")

    child_env = dict(os.environ)
    if env:
        child_env.update(env)

    started = time.time()
    tmpdir = tempfile.mkdtemp(prefix="mcptoon-exec-")
    temp_script: str | None = None
    try:
        argv, temp_script = _build_argv(rt, code, file_path, tmpdir)
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                env=child_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
            )
            out_raw, err_raw, code_ = proc.stdout, proc.stderr, proc.returncode
            failure = None
        except subprocess.TimeoutExpired as exc:
            out_raw = exc.stdout or b""
            err_raw = exc.stderr or b""
            code_ = None
            failure = f"timed out after {timeout}s (process killed)"
        except FileNotFoundError as exc:
            raise ExecError(f"interpreter not runnable: {exc}") from exc
    finally:
        if temp_script and os.path.exists(temp_script):
            try:
                os.unlink(temp_script)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    elapsed_ms = int((time.time() - started) * 1000)
    out_text = _decode(out_raw)
    err_text = _decode(err_raw)

    visible, truncated, handle = reduce_stdout(
        out_text, max_output=max_output, lang=rt.name, cwd=cwd,
    )

    err_bytes = len(err_raw)
    err_tail = err_text
    if err_bytes > STDERR_MAX_BYTES:
        err_tail = _decode(err_raw[-STDERR_MAX_BYTES:])
    lines = err_tail.splitlines()
    if len(lines) > STDERR_TAIL_LINES:
        err_tail = "\n".join(lines[-STDERR_TAIL_LINES:])

    return ExecResult(
        ok=(code_ == 0 and failure is None),
        exit_code=code_,
        lang=rt.name,
        stdout=visible,
        stderr_tail=err_tail,
        elapsed_ms=elapsed_ms,
        truncated=truncated,
        stdout_bytes=len(out_raw),
        stderr_bytes=err_bytes,
        handle=handle,
        failure=failure,
    )


def languages() -> list[dict[str, str]]:
    """What this machine can run, for ``mcptoon exec --langs``."""
    return [
        {"name": rt.name, "aliases": ", ".join(rt.aliases), "exe": rt.exe[0]}
        for rt in available_runtimes()
    ]
