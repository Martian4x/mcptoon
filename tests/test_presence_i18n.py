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

"""The "is mcptoon doing anything for me?" surfaces, and their language.

Three surfaces answer that question, one per host tier:

* the status-line badge (Claude Code's status bar) — `mcptoon.statusline`
* the pointer block written into AGENTS.md-style files — `sync._skill_pointer_block`
* the gateway `instructions` field (serve leg) — `serve._INSTRUCTIONS`

Each must speak the user's language, chosen by `config.resolve_lang()` and never
guessed, and each must carry the *presence* signal (calls routed / tokens saved),
not only the savings report — otherwise a user watching the badge sees "forwarded"
but never "saved", which is exactly the worry that gets a tool deleted.

Why these tests exist: the badge used to be hard-coded English, so a machine
configured for Chinese showed an English status bar. And the pointer block used to
talk only about skills, so the seven file-pointer hosts never learned the tool
schemas were being kept out of context — the one place the saving is real.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcptoon import mode, serve, sync
from mcptoon.welcome import display_width


def _badge(lng: str) -> str:
    """A deterministic badge line for one language, independent of the real cache."""
    with mock.patch.object(mode, "detect",
                           return_value={"mode": "cli", "calls": 2916}), \
         mock.patch.object(mode, "_figures",
                           return_value={"gateway_tokens": 2499,
                                         "tokens_full": 640606,
                                         "calls_recorded": 2916}), \
         mock.patch.object(mode, "_exposed", return_value=9), \
         mock.patch("mcptoon.footer._cumulative_estimate",
                    return_value=(2916, 1_900_000_000)):
        return mode.statusline(lng=lng)


class TestStatuslineLanguage(unittest.TestCase):
    def test_chinese_and_english_badges_differ(self):
        zh, en = _badge("zh"), _badge("en")
        self.assertNotEqual(zh, en, "the badge must actually change language")
        # These badges are CLI-mode (the helper patches detect() to cli), so the
        # leading figure is the CLI credit — "0 resident / full catalog kept out" —
        # not the serve "N tools" story. Both languages must still speak it.
        self.assertIn("常驻", zh)
        self.assertIn("没进上下文", zh)
        self.assertIn("省", zh)
        self.assertIn("次", zh)
        self.assertIn("resident", en)
        self.assertIn("kept out", en)
        self.assertIn("saved", en)
        self.assertIn("calls", en)

    def test_both_badges_stay_within_80_display_columns(self):
        # A CJK glyph is two columns, so len() would under-count the Chinese one.
        for lng in ("zh", "en"):
            line = _badge(lng)
            self.assertLessEqual(display_width(line), 80,
                                 f"{lng} badge overflows: {line!r}")
            self.assertNotIn("\n", line, "a wrapping badge defeats the purpose")

    def test_both_badges_carry_presence_and_savings_together(self):
        # The user's stated fear: "I see it forwarded, but not that it saved".
        # One line must answer both, in either language.
        for lng in ("zh", "en"):
            line = _badge(lng)
            self.assertRegex(line, r"≈.*(1\.9B)",
                             "the savings figure must be on the badge")
            self.assertRegex(line, r"2,916",
                             "the routed-call count must be on the badge")

    def test_language_follows_the_environment(self):
        # MCPTOON_LANG is the same escape hatch every other surface honours.
        # (Whatever the machine's catalog is, the mode tag itself is bilingual.)
        with mock.patch.dict(os.environ, {"MCPTOON_LANG": "zh"}):
            self.assertIn("无服务器", mode.statusline())
        with mock.patch.dict(os.environ, {"MCPTOON_LANG": "en"}):
            self.assertIn("no servers", mode.statusline())

    def test_statusline_is_footer_silent(self):
        # The badge is machine-consumed: a savings footer on stderr would be both
        # wasted work every prompt and a corrupting extra line for the status bar.
        from mcptoon import cli
        self.assertIn("statusline", cli._FOOTER_SILENT_COMMANDS)


class TestPointerBlockLanguage(unittest.TestCase):
    def test_english_and_chinese_bodies_differ_and_share_the_heading(self):
        en = sync._skill_pointer_block(lng="en")
        zh = sync._skill_pointer_block(lng="zh")
        self.assertNotEqual(en, zh)
        for block in (en, zh):
            self.assertTrue(block.startswith(sync._SKILL_POINTER_HEADING),
                            "the heading is the stable anchor for strip/idempotency")

    def test_both_blocks_point_at_the_presence_receipt(self):
        # The pointer hosts (Codex/Gemini/Qwen/Zed/Crush/opencode/Claude Code) read
        # this every turn. It must tell them how to show the live receipt and that
        # the saving is definition-side, in whichever language was written.
        for lng in ("en", "zh"):
            block = sync._skill_pointer_block(lng=lng)
            self.assertIn("mcptoon statusline", block)
        self.assertIn("definition-side", sync._skill_pointer_block(lng="en"))
        self.assertIn("定义侧", sync._skill_pointer_block(lng="zh"))

    def test_strip_removes_either_language_block_byte_reversibly(self):
        original = "# My notes\n\nuser content here\n"
        for write_lng, strip_lng in (("en", "en"), ("zh", "zh"),
                                     ("en", "zh"), ("zh", "en")):
            with self.subTest(write=write_lng, strip=strip_lng):
                d = Path(tempfile.mkdtemp())
                p = d / "AGENTS.md"
                p.write_text(original, encoding="utf-8")
                with mock.patch.dict(os.environ, {"MCPTOON_LANG": write_lng}):
                    sync._write_skill_pointer(p)
                self.assertIn(sync._SKILL_POINTER_HEADING,
                              p.read_text(encoding="utf-8"))
                with mock.patch.dict(os.environ, {"MCPTOON_LANG": strip_lng}):
                    sync._remove_skill_pointer(p)
                self.assertEqual(p.read_text(encoding="utf-8"), original,
                                 "a language switch must not leave the block behind")


class TestServeInstructionsPresence(unittest.TestCase):
    def test_instructions_request_the_presence_receipt(self):
        text = serve._INSTRUCTIONS
        self.assertIn("mcptoon statusline", text)
        self.assertIn("presence receipt", text)

    def test_instructions_keep_the_older_contracts_intact(self):
        # Adding the presence receipt must not drop the savings-report contract.
        text = serve._INSTRUCTIONS
        for phrase in ("mcptoon footer-facts",
                       "only when the numbers have changed",
                       "mcptoon config set footer off",
                       "do not substitute"):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
