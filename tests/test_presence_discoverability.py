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

"""The two onboarding surfaces must say where to SEE the savings, not only that
there are savings.

Measured gap (2026-10-19): `welcome` and `quickstart` both explained the token
saving and both pointed at `mcptoon status`, but neither told a user how to keep
the badge visible. A tool whose only evidence is a number you have to go run a
command to see is a tool that gets deleted once the novelty wears off. `statusline`
existed since 0.8.22 and appeared in neither surface.
"""
import io
from contextlib import redirect_stdout

from mcptoon import cli
from mcptoon import welcome

_FACTS = {
    "servers": 7, "tools": 209,
    "tokens_full": 640_606, "tokens_slim": 56_504,
    "tokens_saved": 584_102, "gateway_tokens": 2_499, "gateway_pct": 99.6,
}


def _render(lng):
    return welcome.render(_FACTS, skills=472, lng=lng)


# ── welcome card ────────────────────────────────────────────────────────────
def test_welcome_points_at_statusline_when_there_are_servers():
    for lng in ("en", "zh"):
        text = _render(lng)
        assert "mcptoon statusline" in text, f"welcome ({lng}) never mentions the badge"


def test_welcome_labels_the_badge_row_in_both_languages():
    assert "See it" in _render("en")
    assert "看得见" in _render("zh")


def test_welcome_omits_the_badge_row_with_no_servers():
    """No servers means nothing to watch — the row would be noise."""
    facts = {"servers": 0, "tools": 0}
    for lng in ("en", "zh"):
        text = welcome.render(facts, skills=None, lng=lng)
        assert "mcptoon statusline" not in text


def test_welcome_still_names_a_next_step_after_adding_the_badge_row():
    """The badge row must not push out the existing guidance."""
    text = _render("en")
    assert "mcptoon status" in text
    assert "mcptoon skills list" in text


# ── quickstart celebration ──────────────────────────────────────────────────
def _celebration(tool_count, servers, unreachable=0):
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._quickstart_celebration(tool_count, servers, unreachable)
    return buf.getvalue()


def test_quickstart_celebration_offers_the_status_bar():
    out = _celebration(209, 7)
    assert "mcptoon statusline --install" in out


def test_quickstart_celebration_keeps_its_existing_next_steps():
    out = _celebration(37, 5)
    assert "37 tools ready across 5 servers" in out
    assert "mcptoon manifest --slim" in out
    assert "mcptoon doctor" in out
