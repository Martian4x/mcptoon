"""`mcptoon exec` — code runs, only its stdout enters the context.

The behaviour worth pinning, and why each one is a real failure if it regresses:

* **stdout is the answer, stderr is a tail.** A Python traceback is 40 useful
  lines; a compiler dump is thousands of useless ones. If stderr came back whole
  the command would defeat its own purpose.
* **Over budget is not lost.** This is the same lossless contract as
  ``compressor.compress_with_ccr``: what the context does not see must be
  retrievable. A 50 KB print must hand back a handle that returns all 50 KB,
  byte-identical — otherwise ``exec`` is a data shredder wearing a savings badge.
* **A crash, a timeout and a non-zero exit are three different answers.** "The
  program ran and said no" must not look like "the program never ran".
* **Only runtimes that exist are advertised.** Claiming a language this machine
  cannot run is the exact kind of unverifiable claim the rest of the project
  deletes on sight.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from mcptoon import exec_sandbox


class RuntimeDiscoveryTests(unittest.TestCase):
    def test_python_is_always_available(self):
        names = [rt["name"] for rt in exec_sandbox.languages()]
        self.assertIn("python", names)

    def test_every_advertised_runtime_actually_resolves(self):
        # A name in --langs that resolve_runtime() then rejects would be a lie.
        for rt in exec_sandbox.available_runtimes():
            self.assertEqual(exec_sandbox.resolve_runtime(rt.name).name, rt.name)

    def test_unknown_language_names_the_alternatives(self):
        with self.assertRaises(exec_sandbox.ExecError) as ctx:
            exec_sandbox.resolve_runtime("cobol")
        self.assertIn("cobol", str(ctx.exception))
        self.assertIn("python", str(ctx.exception))

    def test_aliases_resolve(self):
        self.assertEqual(exec_sandbox.resolve_runtime("py").name, "python")
        self.assertEqual(exec_sandbox.resolve_runtime("PYTHON").name, "python")


class RunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.dict(os.environ, {"MCPTOON_CCR_DIR": self._tmp.name})
        p.start()
        self.addCleanup(p.stop)

    def test_stdout_only(self):
        r = exec_sandbox.run("python", "print('hello')")
        self.assertTrue(r.ok)
        self.assertEqual(r.stdout.strip(), "hello")
        self.assertEqual(r.exit_code, 0)

    def test_stderr_is_captured_separately_and_capped(self):
        code = "import sys\nfor i in range(500): sys.stderr.write('line %d\\n' % i)"
        r = exec_sandbox.run("python", code)
        self.assertTrue(r.ok)
        self.assertEqual(r.stdout, "")
        # The tail, not the head: line 499 must be present, line 0 must not.
        self.assertIn("line 499", r.stderr_tail)
        self.assertNotIn("line 0\n", r.stderr_tail)
        self.assertLessEqual(len(r.stderr_tail.splitlines()), exec_sandbox.STDERR_TAIL_LINES)

    def test_nonzero_exit_is_not_ok_but_is_still_a_result(self):
        r = exec_sandbox.run("python", "raise SystemExit(3)")
        self.assertFalse(r.ok)
        self.assertEqual(r.exit_code, 3)
        self.assertIsNone(r.failure)  # it ran; it just said no

    def test_timeout_is_reported_as_such(self):
        r = exec_sandbox.run("python", "import time; time.sleep(10)", timeout=1)
        self.assertFalse(r.ok)
        self.assertIsNotNone(r.failure)
        self.assertIn("timed out", r.failure)

    def test_under_budget_is_not_truncated(self):
        r = exec_sandbox.run("python", "print('x' * 100)", max_output=8192)
        self.assertFalse(r.truncated)
        self.assertIsNone(r.handle)
        self.assertIn("x" * 100, r.stdout)

    def test_over_budget_keeps_both_ends_and_stores_the_rest(self):
        r = exec_sandbox.run("python", "print('A' * 2000 + 'B' * 2000)", max_output=500)
        self.assertTrue(r.truncated)
        self.assertGreater(r.stdout_bytes, 4000)
        # Head and tail both survive; the middle is what got elided.
        self.assertIn("A" * 100, r.stdout)
        self.assertIn("B" * 100, r.stdout)
        self.assertLess(len(r.stdout), 900)

    def test_over_budget_handle_returns_the_full_output(self):
        """The lossless contract: nothing the context did not see is lost."""
        from mcptoon import ccr

        payload = "".join(f"line {i:05d}\n" for i in range(2000))
        prog = f"import sys; sys.stdout.write({payload!r})"
        r = exec_sandbox.run("python", prog, max_output=500)
        self.assertTrue(r.truncated)
        self.assertIsNotNone(r.handle, "over-budget stdout must be retrievable")

        got = ccr.retrieve(r.handle)
        self.assertIsNotNone(got, "the handle must resolve")
        # ccr returns the stored value itself (a str here). Windows translates
        # the child's "\n" to "\r\n" on the pipe, so compare with newlines
        # normalised rather than pretending the platform does not exist.
        text = got if isinstance(got, str) else json.loads(got)
        self.assertEqual(text.replace("\r\n", "\n"), payload)

    def test_file_mode_runs_a_path(self):
        path = os.path.join(self._tmp.name, "script.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("print('from file')\n")
        r = exec_sandbox.run("python", file_path=path)
        self.assertTrue(r.ok)
        self.assertEqual(r.stdout.strip(), "from file")

    def test_missing_file_is_a_caller_error(self):
        with self.assertRaises(exec_sandbox.ExecError):
            exec_sandbox.run("python", file_path=os.path.join(self._tmp.name, "nope.py"))

    def test_nothing_to_run_is_a_caller_error(self):
        with self.assertRaises(exec_sandbox.ExecError):
            exec_sandbox.run("python")

    def test_payload_is_data_not_shell_syntax(self):
        # Quotes, semicolons and $() must reach the interpreter verbatim — they
        # are a program, never a shell line.
        r = exec_sandbox.run("python", "print('a;b$(c)`d`')")
        self.assertTrue(r.ok)
        self.assertEqual(r.stdout.strip(), "a;b$(c)`d`")


class RedactionOfResultTests(unittest.TestCase):
    def test_as_dict_is_json_serialisable(self):
        r = exec_sandbox.run("python", "print('ok')")
        json.dumps(r.as_dict())  # must not raise

    def test_failure_field_only_present_when_it_happened(self):
        ok = exec_sandbox.run("python", "print('ok')")
        self.assertNotIn("failure", ok.as_dict())


if __name__ == "__main__":
    unittest.main()
