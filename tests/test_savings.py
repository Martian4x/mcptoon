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

"""`mcptoon savings` — milestones derived from the SAME estimate as the footer.

The one thing this screen must never do is invent a second, drifting savings
number. Every figure here comes from `footer._cumulative_estimate`, so these
tests pin that: the milestone total must equal the footer's cumulative estimate,
and the milestone math must be monotonic and honest.
"""
import io
import json
from contextlib import redirect_stdout

from mcptoon import savings


# ── the math ────────────────────────────────────────────────────────────────
def test_milestones_are_the_ones_at_or_below_the_total():
    assert savings.milestones(0) == []
    assert savings.milestones(500_000) == []
    assert savings.milestones(1_000_000) == [1_000_000]
    assert savings.milestones(2_500_000_000) == [1_000_000, 10_000_000,
                                                 100_000_000, 1_000_000_000]


def test_next_milestone_is_the_first_above_the_total():
    assert savings.next_milestone(0) == 1_000_000
    assert savings.next_milestone(1_000_000) == 10_000_000
    assert savings.next_milestone(999_999) == 1_000_000
    assert savings.next_milestone(10 ** 12) is None  # past the last tracked


def test_fmt_tokens_is_compact():
    assert savings._fmt_tokens(1_900_000_000) == "1.9B"
    assert savings._fmt_tokens(950_000) == "950K"
    assert savings._fmt_tokens(2_500_000) == "2.5M"
    assert savings._fmt_tokens(12_000_000_000) == "12B"


# ── consistency with the footer ─────────────────────────────────────────────
def test_total_equals_the_footer_cumulative_estimate():
    """One number, two screens: `savings` must not compute its own."""
    from mcptoon import footer
    facts = footer.facts()
    est = footer._cumulative_estimate(facts)
    data = savings.build()
    expected = est[1] if est else 0
    assert data["total_tokens"] == expected


# ── rendering ───────────────────────────────────────────────────────────────
def _render(data, lng):
    return savings.render(data, lng=lng)


def test_render_shows_crossed_milestones_and_next_target():
    data = {"caliber": "tiktoken cl100k_base", "total_tokens": 2_500_000_000,
            "calls": 4000, "milestones_hit": savings.milestones(2_500_000_000),
            "next": savings.next_milestone(2_500_000_000), "first_call_ts": None}
    en = _render(data, "en")
    assert "1B tokens" in en
    assert "Next milestone: 10B" in en
    assert "routed calls" in en
    zh = _render(data, "zh")
    assert "已跨过的里程碑" in zh
    assert "下一个里程碑" in zh


def test_render_is_honest_about_being_a_lower_bound():
    data = {"caliber": "tiktoken cl100k_base", "total_tokens": 2_500_000_000,
            "calls": 4000, "milestones_hit": [], "next": None, "first_call_ts": None}
    en = _render(data, "en")
    assert "lower-bound" in en or "at least" in en


def test_render_handles_no_savings_gracefully():
    data = {"caliber": "tiktoken cl100k_base", "total_tokens": 0, "calls": 0,
            "milestones_hit": [], "next": 1_000_000, "first_call_ts": None}
    en = _render(data, "en")
    assert "No savings recorded yet" in en
    zh = _render(data, "zh")
    assert "还没有记录任何节省" in zh


def test_human_terms_use_big_units_only():
    """A wall of analogies is noise — show the two biggest that fit."""
    lines = savings._yardsticks(2_500_000_000, "en")
    assert len(lines) <= 2
    assert all(l.startswith("≈ ") for l in lines)


# ── CLI wiring ──────────────────────────────────────────────────────────────
def test_run_json_is_parseable():
    buf = io.StringIO()
    with redirect_stdout(buf):
        savings.run(["--json"], fmt="json")
    data = json.loads(buf.getvalue())
    assert "total_tokens" in data
    assert "next" in data


def test_savings_is_a_known_command():
    from mcptoon import cli
    assert "savings" in cli._COMPLETION_COMMANDS
    assert "savings" in cli._FOOTER_SILENT_COMMANDS
    assert "savings" in cli._WELCOME_EXEMPT
