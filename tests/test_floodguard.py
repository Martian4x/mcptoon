"""The per-agent flood guard for the gateway's search tools.

The bug this pins (context-mode #769, reproduced conceptually here): one global
counter makes ten concurrent subagents share a single budget, so a legitimate
fan-out trips a guard that was only meant to catch one actor spamming. Bucketing
per key is the fix; these tests prove the bucketing and the three states
(normal / soft-capped / blocked) all behave.
"""

from __future__ import annotations

import unittest

from mcptoon.floodguard import FloodGuard


class FloodGuardTests(unittest.TestCase):
    def _guard(self, **kw):
        return FloodGuard(window_ms=1000, soft_cap=2, block_after=4, **kw)

    def test_first_calls_are_normal(self):
        g = self._guard()
        d = g.record("a", 0)
        self.assertEqual(d.count, 1)
        self.assertFalse(d.soft_capped)
        self.assertFalse(d.blocked)

    def test_soft_cap_after_threshold(self):
        g = self._guard()
        for t in range(2):
            g.record("a", t)
        d = g.record("a", 2)  # 3rd call, past soft_cap=2
        self.assertTrue(d.soft_capped)
        self.assertFalse(d.blocked)

    def test_block_after_threshold(self):
        g = self._guard()
        for t in range(4):
            g.record("a", t)
        d = g.record("a", 4)  # 5th call, past block_after=4
        self.assertTrue(d.blocked)

    def test_keys_are_independent(self):
        """Fan-out: ten agents × 2 calls must not trip a 4-call budget."""
        g = self._guard()
        for key in [f"agent{i}" for i in range(10)]:
            for _ in range(2):
                d = g.record(key, 0)
                self.assertFalse(d.blocked, f"{key} wrongly blocked on fan-out")

    def test_window_resets(self):
        g = self._guard()
        for t in range(5):
            g.record("a", t)
        # A call after the window has elapsed starts a fresh count.
        d = g.record("a", 2000)
        self.assertEqual(d.count, 1)
        self.assertFalse(d.blocked)

    def test_eviction_fails_open(self):
        """Over the key ceiling, the oldest bucket is dropped — never a false block."""
        g = self._guard(max_keys=2)
        g.record("a", 0)
        g.record("b", 0)
        g.record("c", 0)  # evicts the oldest ("a")
        self.assertLessEqual(g.size(), 2)
        d = g.record("a", 0)  # a fresh window, not a blocked one
        self.assertFalse(d.blocked)

    def test_message_names_the_tool_and_a_next_step(self):
        g = self._guard()
        for t in range(5):
            d = g.record("a", t)
        msg = d.message("mcptoon_kb_search")
        self.assertIn("mcptoon_kb_search", msg)
        self.assertIn("blocked", msg.lower())

    def test_reset(self):
        g = self._guard()
        g.record("a", 0)
        self.assertEqual(g.size(), 1)
        g.reset()
        self.assertEqual(g.size(), 0)


if __name__ == "__main__":
    unittest.main()
