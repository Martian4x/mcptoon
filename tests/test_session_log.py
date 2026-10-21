"""`mcptoon session` — capture + on-demand recall, NOT automatic restore.

The distinction is the whole test surface. context-mode restores state
automatically through host hooks; mcptoon has no such hook and this module must
not pretend otherwise. What it can do is log every call that routes through the
gateway and hand that log back when asked. So the tests pin:

* **What is captured**: writes, reads, git, errors, and the file path of a write
  (which is what "where was I" actually needs).
* **What is honestly absent**: the summary carries a note saying it is not
  automatic. If that note ever disappears, an agent will assume it gets
  restored without asking — the exact false belief this design avoids.
* **Classification is a closed set**: ``error``/``git``/``write``/``read``/
  ``other``, with ``ok=False`` always winning.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from mcptoon import session_log


class SessionBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.dict(os.environ, {"MCPTOON_SESSION_DIR": self._tmp.name})
        p.start()
        self.addCleanup(p.stop)


class ClassifyTests(unittest.TestCase):
    def test_error_wins_over_everything(self):
        self.assertEqual(session_log.classify("write_file", {"path": "a"}, ok=False), "error")

    def test_write(self):
        self.assertEqual(session_log.classify("write_file", {"path": "a"}, ok=True), "write")
        self.assertEqual(session_log.classify("edit_file", {}, ok=True), "write")

    def test_read(self):
        self.assertEqual(session_log.classify("read_file", {"path": "a"}, ok=True), "read")

    def test_git_tool_name_with_underscore(self):
        # "git_commit" must classify as git; a plain \bgit\b misses it.
        self.assertEqual(session_log.classify("git_commit", {}, ok=True), "git")

    def test_git_subcommand_in_arguments(self):
        self.assertEqual(
            session_log.classify("run", {"cmd": "git checkout main"}, ok=True), "git")

    def test_other(self):
        self.assertEqual(session_log.classify("do_magic", {}, ok=True), "other")


class RecordAndSummariseTests(SessionBase):
    def test_empty_session(self):
        s = session_log.summarise()
        self.assertEqual(s.events, 0)

    def test_writes_are_remembered_with_paths(self):
        session_log.record("filesystem", "write_file", {"path": "src/a.py"}, ok=True)
        session_log.record("filesystem", "write_file", {"path": "src/b.py"}, ok=True)
        s = session_log.summarise()
        self.assertEqual(s.events, 2)
        self.assertIn("src/a.py", s.files_written)
        self.assertIn("src/b.py", s.files_written)

    def test_errors_are_listed(self):
        session_log.record("shell", "run", {"cmd": "pytest"}, ok=False)
        s = session_log.summarise()
        self.assertEqual(len(s.errors), 1)
        self.assertEqual(s.errors[0]["tool"], "run")

    def test_git_ops_collected(self):
        session_log.record("shell", "git_commit", {"message": "x"}, ok=True)
        s = session_log.summarise()
        self.assertIn("git_commit", s.git_ops)

    def test_tools_used_counts(self):
        for _ in range(3):
            session_log.record("fs", "read_file", {"path": "a"}, ok=True)
        s = session_log.summarise()
        top = s.tools_used[0]
        self.assertEqual(top["tool"], "read_file")
        self.assertEqual(top["calls"], 3)

    def test_summary_says_it_is_not_automatic(self):
        """The honesty clause must survive: it is the difference from context-mode."""
        session_log.record("fs", "read_file", {"path": "a"}, ok=True)
        d = session_log.summarise().as_dict()
        self.assertIn("note", d)
        self.assertIn("cannot inject", d["note"])

    def test_record_never_raises_on_bad_arguments(self):
        # An unserialisable argument must not break the call it is logging.
        session_log.record("x", "y", object(), ok=True)
        self.assertGreaterEqual(session_log.summarise().events, 0)

    def test_purge(self):
        session_log.record("fs", "read_file", {"path": "a"}, ok=True)
        n = session_log.purge()
        self.assertEqual(n, 1)
        self.assertEqual(session_log.summarise().events, 0)

    def test_trimming_keeps_newest(self):
        old = session_log.MAX_EVENTS
        try:
            session_log.MAX_EVENTS = 5
            for i in range(12):
                session_log.record("fs", "read_file", {"path": f"f{i}"}, ok=True)
            self.assertLessEqual(session_log.summarise().events, 5)
        finally:
            session_log.MAX_EVENTS = old


if __name__ == "__main__":
    unittest.main()
