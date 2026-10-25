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


# ── empty-state conversion: point at the zero-config demo, not a dead end ────
def test_welcome_empty_state_points_at_the_demo():
    """A machine with no servers used to be told 'run quickstart' — but quickstart
    discovers servers and there are none to find, a dead end. It must offer the
    zero-config demo (which spins up its own server and shows the saving) first."""
    for lng in ("en", "zh"):
        text = welcome.render({"servers": 0, "tools": 0}, skills=None, lng=lng)
        assert "mcptoon demo" in text, f"empty state ({lng}) must offer the demo"


def test_welcome_empty_state_demo_precedes_quickstart():
    """The demo row is the cheapest first action; it must sort before discovery."""
    rows, _ = welcome._rows_and_tail(
        {"servers": 0, "tools": 0}, 0, "en", io.StringIO(), "direct")
    cmds = [cmd for _label, cmd, _desc in rows]
    assert "mcptoon demo" in cmds and "mcptoon quickstart" in cmds
    assert cmds.index("mcptoon demo") < cmds.index("mcptoon quickstart")


def test_welcome_with_servers_does_not_show_the_demo_row():
    """The demo row is only for the no-servers dead end; a wired machine has
    nothing to demo and the row would be noise."""
    text = _render("en")
    assert "mcptoon demo" not in text


# ── welcome honours the delivery mode (no saving claim before it saves) ──────
def test_welcome_direct_does_not_claim_a_saving():
    """A direct machine (installed, not wired) saves nothing — the card must say
    so, not quote the gateway 99%. This is the "credit points the wrong way" bug."""
    text = welcome.render(_FACTS, skills=472, lng="en", mode="direct")
    assert "no saving yet" in text
    assert "gateway saves" not in text
    assert "mcptoon sync --self" in text   # names the one fix


def test_welcome_alongside_does_not_claim_a_saving():
    text = welcome.render(_FACTS, skills=472, lng="en", mode="serve-alongside")
    assert "not taken over" in text and "no saving yet" in text
    assert "gateway saves" not in text
    assert "mcptoon sync --takeover" in text


def test_welcome_serve_still_shows_the_gateway_saving():
    """No regression: a genuinely saving serve machine keeps the saving row."""
    text = welcome.render(_FACTS, skills=472, lng="en", mode="serve")
    assert "gateway saves" in text


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
