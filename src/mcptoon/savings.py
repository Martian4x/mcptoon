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

"""`mcptoon savings` — turn the running total into milestones a person can feel.

`report` answers "how much, on what caliber". This answers the different question
a long-time user actually has: *has this been worth keeping?* A raw "≈1.9B tokens"
is impressive but abstract; "you crossed 1 billion" and "that is about 4,700 copies
of War and Peace" are things a person can hold. Every milestone here is derived
from the SAME estimate the footer and report use (`footer._cumulative_estimate`:
calls routed × per-turn saving) — never a second, drifting number — and each is
labelled as the lower-bound estimate it is.

Design rule (inherited from `docs/calibers.md`): the mileage figures are *analogies*,
clearly marked with `≈`, not measurements. They exist to make a real number legible,
never to replace it.
"""

from __future__ import annotations

__all__ = ["milestones", "next_milestone", "build", "render", "run"]

# Ordered token milestones. Powers of ten plus the ones people actually say out
# loud ("a million", "a billion"). Ascending so `next_milestone` can pick the
# first one above the current total.
_TOKEN_MILESTONES = (
    1_000_000,        # 1M
    10_000_000,       # 10M
    100_000_000,      # 100M
    1_000_000_000,    # 1B
    10_000_000_000,   # 10B
    100_000_000_000,  # 100B
    1_000_000_000_000,  # 1T
)

# Human-scale yardsticks, in tokens. Rough and deliberately few; each is a widely
# known unit of length so the analogy lands without explanation.
#   - A novel (~90k words ≈ 120k tokens)
#   - A 300-page non-fiction book (~150k tokens)
#   - The King James Bible (~1.2M tokens)
_YARDSTICKS = (
    ("novel", 120_000, "本小说", "novels"),
    ("book", 150_000, "本书", "books"),
    ("bible", 1_200_000, "部《圣经》", "copies of the Bible"),
)


def _fmt_tokens(n: float) -> str:
    """1_900_000_000 -> '1.9B'; 950_000 -> '950K'. One decimal, trimmed."""
    n = float(n)
    for unit, size in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= size:
            v = n / size
            return (f"{v:.1f}".rstrip("0").rstrip(".") + unit) if v < 10 else f"{v:.0f}{unit}"
    return f"{int(n):,}"


def milestones(total_tokens: int) -> list[int]:
    """Every milestone at or below the running total, ascending."""
    if total_tokens <= 0:
        return []
    return [m for m in _TOKEN_MILESTONES if m <= total_tokens]


def next_milestone(total_tokens: int) -> int | None:
    """The next milestone above the running total, or None past the last one."""
    for m in _TOKEN_MILESTONES:
        if m > total_tokens:
            return m
    return None


def _yardsticks(total_tokens: int, lng: str) -> list[str]:
    """A couple of 'that is about N novels' lines, largest unit that fits first."""
    zh = lng == "zh"
    out = []
    for _key, unit_tokens, zh_unit, en_unit in _YARDSTICKS:
        count = total_tokens // unit_tokens
        if count >= 1:
            if zh:
                out.append(f"≈ {count:,} {zh_unit}")
            else:
                out.append(f"≈ {count:,} {en_unit}")
    # Show the two biggest that fit; a wall of analogies is noise.
    return out[-2:]


def build() -> dict:
    """Everything the milestones screen needs, from the shared estimate."""
    from . import footer as footer_mod
    from . import usage as usage_mod

    facts = footer_mod.facts()
    est = footer_mod._cumulative_estimate(facts)
    total_tokens = est[1] if est else 0
    calls = est[0] if est else int(facts.get("calls_recorded") or 0)
    stats = usage_mod.get_usage_stats()
    return {
        "caliber": facts.get("token_caliber") or "tiktoken cl100k_base",
        "total_tokens": total_tokens,
        "calls": calls,
        "milestones_hit": milestones(total_tokens),
        "next": next_milestone(total_tokens),
        "first_call_ts": stats.get("first_call_ts"),
    }


def render(data: dict, lng: str = "en") -> str:
    """The milestones screen as pasteable text. One language, no escape codes."""
    zh = lng == "zh"
    total = int(data.get("total_tokens") or 0)
    calls = int(data.get("calls") or 0)
    L = []
    a = L.append

    def L_(en: str, zh_text: str) -> str:
        return zh_text if zh else en

    a(L_("mcptoon savings milestones", "mcptoon 省钱里程碑") + f"  ·  {data['caliber']}")
    a("")

    if total <= 0:
        a(L_("No savings recorded yet — route a call first (`mcptoon call ...`).",
             "还没有记录任何节省 —— 先转发一次调用（`mcptoon call ...`）。"))
        return "\n".join(L)

    a(L_(f"≈ {_fmt_tokens(total)} tokens saved so far, across {calls:,} routed calls",
         f"目前已省 ≈ {_fmt_tokens(total)} tokens，累计转发 {calls:,} 次"))
    a(L_("(lower-bound estimate: calls × per-turn saving; every routed call is",
         "（下界估算：转发次数 × 每轮省量；把每次转发都当作单独一轮，"))
    a(L_("  counted as its own turn, so the true figure is at least this)",
         "  所以真实值只会更高）"))
    a("")

    # Milestones crossed.
    hit = data.get("milestones_hit") or []
    if hit:
        a(L_("Milestones crossed:", "已跨过的里程碑："))
        for m in hit:
            a(f"     ✓ {_fmt_tokens(m)} tokens")
        a("")
    else:
        a(L_("No milestone crossed yet — the first is 1M tokens.",
             "还没跨过任何里程碑 —— 第一个是 1M tokens。"))
        a("")

    # What it means, in human units.
    yards = _yardsticks(total, lng)
    if yards:
        a(L_("In human terms:", "换算成人能懂的量："))
        for y in yards:
            a(f"     {y}")
        a("")

    # Next target, if there is one.
    nxt = data.get("next")
    if nxt:
        remaining = max(0, nxt - total)
        pct = int(total / nxt * 100) if nxt else 0
        a(L_(f"Next milestone: {_fmt_tokens(nxt)} — {_fmt_tokens(remaining)} to go ({pct}%)",
             f"下一个里程碑：{_fmt_tokens(nxt)} —— 还差 {_fmt_tokens(remaining)}（{pct}%）"))
    else:
        a(L_("You have passed every milestone this screen tracks. 🎉",
             "本屏记录的里程碑已全部跨过。🎉"))

    return "\n".join(L)


def run(rest: list[str], fmt: str = "auto", head_n: int = 0, max_chars: int = 0,
        full: bool = False) -> None:
    """CLI entry: `mcptoon savings [--json]`."""
    data = build()
    if fmt == "json":
        from . import output
        print(output.render(data, fmt="json", head_n=head_n, max_chars=max_chars,
                            full=full))
        return
    from . import config as cfg
    print(render(data, lng=cfg.resolve_lang()))
