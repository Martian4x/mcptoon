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

"""`mcptoon statusline --install/--uninstall/--check` wiring Claude Code's badge.

Why these tests exist: the badge shipped in 0.8.22 but nothing put it in front of
a user, so "I installed mcptoon and cannot tell it is doing anything" stayed true.
Wiring it means editing `~/.claude/settings.json` — the file that also holds the
user's model, theme and permissions. Every test here pins one promise that makes
that edit safe to run:

* only the `statusLine` key is added, nothing else changes
* a re-run is idempotent (no second entry, no file churn)
* a different statusLine is refused, never overwritten
* `--uninstall` is byte-reversible and deletes the file only when it was ours alone
* unreadable JSON is refused instead of clobbered
* `--check` exits non-zero when not wired, so `doctor`/scripts can gate on it

All of it runs against a redirected `USERPROFILE`, never the real profile.
"""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from mcptoon import mode


class _Sandbox(unittest.TestCase):
    """Point USERPROFILE at a temp dir for the duration of one test."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"USERPROFILE": str(self.home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    @property
    def settings(self) -> Path:
        return self.home / ".claude" / "settings.json"

    def write_settings(self, data) -> None:
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        text = data if isinstance(data, str) else json.dumps(data)
        self.settings.write_text(text, encoding="utf-8")

    def read_settings(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))


class TestInstall(_Sandbox):
    def test_install_writes_only_the_statusline_key(self):
        self.write_settings({"model": "opus", "theme": "dark"})
        result = mode.install_statusline()
        self.assertEqual(result["action"], "installed")
        data = self.read_settings()
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["statusLine"],
                         {"type": "command", "command": "mcptoon statusline"})

    def test_install_creates_file_and_parent_dir_when_absent(self):
        self.assertFalse(self.settings.exists())
        result = mode.install_statusline()
        self.assertEqual(result["action"], "installed")
        self.assertTrue(self.settings.exists())
        self.assertEqual(self.read_settings()["statusLine"]["command"],
                         "mcptoon statusline")

    def test_install_is_idempotent(self):
        self.write_settings({"model": "opus"})
        mode.install_statusline()
        first = self.settings.read_text(encoding="utf-8")
        result = mode.install_statusline()
        self.assertEqual(result["action"], "already")
        self.assertFalse(result["changed"])
        self.assertEqual(self.settings.read_text(encoding="utf-8"), first)

    def test_install_refuses_to_clobber_a_different_statusline(self):
        self.write_settings({"statusLine": {"type": "command", "command": "~/my-bar.sh"}})
        result = mode.install_statusline()
        self.assertEqual(result["action"], "refused")
        self.assertFalse(result["changed"])
        self.assertEqual(self.read_settings()["statusLine"]["command"], "~/my-bar.sh")

    def test_install_refuses_unreadable_json(self):
        self.write_settings("{ this is not json ")
        result = mode.install_statusline()
        self.assertEqual(result["action"], "error")
        self.assertIn("not valid JSON", result["error"])
        # untouched: still the broken bytes the user had, not a rewrite
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{ this is not json ")

    def test_install_refuses_non_object_top_level(self):
        self.write_settings([1, 2, 3])
        result = mode.install_statusline()
        self.assertEqual(result["action"], "error")

    def test_dry_run_writes_nothing(self):
        self.write_settings({"model": "opus"})
        before = self.settings.read_text(encoding="utf-8")
        result = mode.install_statusline(dry=True)
        self.assertEqual(result["action"], "would-install")
        self.assertEqual(self.settings.read_text(encoding="utf-8"), before)

    def test_backup_is_written_once_and_never_clobbered(self):
        self.write_settings({"model": "opus"})
        mode.install_statusline()
        bak = self.settings.with_suffix(".json.bak")
        self.assertTrue(bak.exists())
        original = bak.read_text(encoding="utf-8")
        # a second install after a user edit must not overwrite the first snapshot
        mode.uninstall_statusline()
        mode.install_statusline()
        self.assertEqual(bak.read_text(encoding="utf-8"), original)


class TestUninstall(_Sandbox):
    def test_uninstall_restores_other_keys_and_removes_ours(self):
        self.write_settings({"model": "opus", "theme": "dark"})
        mode.install_statusline()
        result = mode.uninstall_statusline()
        self.assertEqual(result["action"], "removed")
        data = self.read_settings()
        self.assertNotIn("statusLine", data)
        self.assertEqual(data, {"model": "opus", "theme": "dark"})

    def test_uninstall_deletes_file_when_it_held_only_our_key(self):
        mode.install_statusline()
        self.assertTrue(self.settings.exists())
        result = mode.uninstall_statusline()
        self.assertEqual(result["action"], "removed-file")
        self.assertFalse(self.settings.exists())

    def test_uninstall_leaves_a_foreign_statusline_alone(self):
        self.write_settings({"statusLine": {"type": "command", "command": "~/my-bar.sh"}})
        result = mode.uninstall_statusline()
        self.assertEqual(result["action"], "not-ours")
        self.assertEqual(self.read_settings()["statusLine"]["command"], "~/my-bar.sh")

    def test_uninstall_when_absent_is_a_no_op(self):
        result = mode.uninstall_statusline()
        self.assertEqual(result["action"], "absent")
        self.assertFalse(result["changed"])

    def test_install_uninstall_roundtrip_is_byte_identical(self):
        original = '{\n  "model": "opus",\n  "theme": "dark"\n}\n'
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(original, encoding="utf-8")
        mode.install_statusline()
        mode.uninstall_statusline()
        # Byte-for-byte, not just value-for-value. A previous version of this test
        # accepted "data is the same, whitespace may differ" — and that is exactly
        # the bug it then failed to catch: `json.dumps` reflowed a 4-space file into
        # 2-space and exploded `["Bash"]` onto three lines (measured 2026-10-19).
        self.assertEqual(self.settings.read_text(encoding="utf-8"), original)

    def test_roundtrip_is_byte_identical_across_styles(self):
        """Every layout a real settings file is written in must round-trip exactly."""
        originals = [
            '{\n  "model": "opus",\n  "theme": "dark"\n}\n',              # 2-space
            '{\n    "model": "opus",\n    "permissions": {\n'
            '        "allow": ["Bash"]\n    }\n}\n',                        # 4-space + array
            '{"model":"opus","theme":"dark"}',                             # minified
            '{"model": "opus"}',                                           # no trailing NL
            '{\n\t"model": "opus",\n\t"theme": "dark"\n}\n',               # tabs
            '{\n  "x": {"statusLine": {"command": "mcptoon statusline"}},'
            '\n  "model": "opus"\n}\n',                                    # nested same key
            '{\n  "备注": "测试",\n  "model": "opus"\n}\n',                 # CJK, non-ASCII
            '{"model": "opus"}\n\n\n',                                     # trailing blanks
        ]
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        for original in originals:
            with self.subTest(original=original):
                with open(self.settings, "wb") as fh:
                    fh.write(original.encode("utf-8"))
                # a stale .bak from a previous subTest must not leak across cases
                bak = self.settings.with_suffix(".json.bak")
                if bak.exists():
                    bak.unlink()
                mode.install_statusline()
                installed = json.loads(self.settings.read_text(encoding="utf-8"))
                self.assertEqual(installed["statusLine"]["command"],
                                 "mcptoon statusline")
                mode.uninstall_statusline()
                with open(self.settings, "rb") as fh:
                    self.assertEqual(fh.read(), original.encode("utf-8"))

    def test_roundtrip_preserves_crlf_line_endings(self):
        """A Windows file must not come back with LF line endings."""
        original = b'{\r\n  "model": "opus"\r\n}\r\n'
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        with open(self.settings, "wb") as fh:
            fh.write(original)
        mode.install_statusline()
        mode.uninstall_statusline()
        with open(self.settings, "rb") as fh:
            self.assertEqual(fh.read(), original)

    def test_pre_existing_empty_object_is_restored_not_deleted(self):
        """`{}` was the user's file, not ours — uninstall must leave `{}` behind."""
        self.write_settings("{}")
        mode.install_statusline()
        result = mode.uninstall_statusline()
        self.assertTrue(self.settings.exists(),
                        "a file that existed before we filled it must not be deleted")
        self.assertEqual(result["action"], "removed")
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{}")

    def test_fills_a_present_but_empty_statusline_without_duplicating_it(self):
        """`{"statusLine": {}}` or `{"statusLine": {"type": "command"}}` get filled."""
        for original in ('{\n  "statusLine": {},\n  "model": "opus"\n}\n',
                         '{\n  "statusLine": {"type": "command"},\n  "model": "opus"\n}\n'):
            with self.subTest(original=original):
                self.write_settings(original)
                result = mode.install_statusline()
                self.assertEqual(result["action"], "installed")
                raw = self.settings.read_text(encoding="utf-8")
                self.assertEqual(raw.count('"statusLine"'), 1, "must not duplicate the key")
                data = self.read_settings()
                self.assertEqual(data["statusLine"],
                                 {"type": "command", "command": "mcptoon statusline"})
                self.assertEqual(data["model"], "opus")


class TestRobustness(_Sandbox):
    def test_settings_path_that_is_a_directory_is_a_clean_error(self):
        """No traceback, and never attempt to rewrite a directory."""
        self.settings.mkdir(parents=True)
        result = mode.install_statusline()
        self.assertEqual(result["action"], "error")
        self.assertIn("directory", result["error"])
        self.assertTrue(self.settings.is_dir(), "must not have touched it")
        st = mode.statusline_status()
        self.assertFalse(st["wired"])


class TestStatusAndCheck(_Sandbox):
    def test_status_reports_not_wired_before_install(self):
        st = mode.statusline_status()
        self.assertFalse(st["wired"])
        self.assertFalse(st["file_exists"])

    def test_status_reports_wired_after_install(self):
        mode.install_statusline()
        st = mode.statusline_status()
        self.assertTrue(st["wired"])
        self.assertEqual(st["command"], "mcptoon statusline")

    def test_status_does_not_create_a_file(self):
        mode.statusline_status()
        self.assertFalse(self.settings.exists())


class TestCommandSurface(unittest.TestCase):
    def test_statusline_command_is_the_plain_line(self):
        # No --json: the host renders stdout as the bar, so it must be one line.
        self.assertEqual(mode.statusline_command(), "mcptoon statusline")

    def test_public_api_is_exported(self):
        for name in ("statusline_command", "install_statusline",
                     "uninstall_statusline", "statusline_status"):
            self.assertIn(name, mode.__all__)


if __name__ == "__main__":
    unittest.main()
