#!/usr/bin/env python3
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

"""Rewrite every advertised test-count claim to the number pytest actually collects.

Adding a single test file moves the suite total, and the total is stated on eight
surfaces (two README badges, the one-line "N passed · M skipped" claim on six
pages, and the prose "N tests" spelling on seven). Updating them by hand is how
this repo shipped stale totals repeatedly — 694->703->713 drifted unnoticed, and
0.8.16's author wrote 736 when the truth was 737. This script is the mechanical
answer: measure collection once, write the honest number everywhere, then run the
guard that judges those very surfaces.

The number it writes is `collected - DETERMINISTIC_SKIPS` (the "passed" count the
docs state), and it keeps the "· 2 skipped" tail. It is the same arithmetic
`tests/test_footprint_claims.py` checks, whose tolerance is `[collected-3,
collected]`; a truthful `collected-2` always lands inside it.

Usage:
    python scripts/sync_claims.py            # dry-run: show what would change
    python scripts/sync_claims.py --apply    # write, then run the guard
    python scripts/sync_claims.py --check    # exit 1 if any surface is stale
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# This box skips two deterministically (case-insensitive NTFS; the WSL-stub bash).
# Mirrors SKIP_SLACK's docstring in the guard; the guard tolerates a third
# environmental skip, so writing `collected - 2` stays inside its window.
DETERMINISTIC_SKIPS = 2

# Surfaces that state the suite total. Union of TEST_CLAIM_SURFACES and the prose
# extra (docs/tiktoken-benchmarks.md) from tests/test_footprint_claims.py.
# `docs/README-details.md` carries the line/module claim (not a test count), so it
# is included here for the line/module sync below even though it states no total.
SURFACES = (
    "README.md", "README.zh-CN.md", "DEVELOPERS.md", "ROADMAP.md",
    "docs/index.html", "docs/index-zh.html", "docs/tiktoken-benchmarks.md",
    "docs/README-details.md",
)

# The spellings the guard knows. Each has its number in group 1 (except the badge,
# where the literal prefix is kept) so the replacement rebuilds the whole match
# from parts — no fragile substring splicing.
_BADGE = re.compile(r"(?i)(Tests-)(\d+)(%20passed)")
_LIVE = re.compile(r"(\d+) passed(\s*(?:·|\+|,)\s*\d+ skipped)")
_PROSE = re.compile(r"(\d[\d,]{2,6})(\s*(?:tests?\b|个测试))")
_VERSION_LINE = re.compile(r"(v\d+\.\d+\.\d+[ ·|]+)(\d+) passed")

# Line-count and module-count claims — the numbers `sync_claims` used to leave to a
# hand edit, which is exactly how they drifted (31,339 → 31,410 found stale on
# 2026-10-19 after a refactor added a module). Both are pure numbers in a fixed
# frame; the words around them are untouched, so this respects the README freeze
# ("only the numbers move"). The guard (`test_footprint_claims`) judges them with a
# 2% tolerance on lines and exact match on modules — matching that arithmetic here
# means a written figure always lands inside the guard's window.
#
# `\s+` (not a literal space) between the number and the unit so the cross-line
# "31,410\nlines of" in docs/README-details.md is caught too.
_LINECOUNT = re.compile(r"(\d[\d,]{3,6})(\s*(?:行|lines of))")
_MODCOUNT = re.compile(r"(\d{1,3})(\s*(?:个)?\s*模块|\s*modules\b)")


def _module_count() -> int:
    """Physical module count of src/mcptoon/**/*.py — the guard's own ruler."""
    src = ROOT / "src" / "mcptoon"
    return sum(1 for p in src.rglob("*.py") if "__pycache__" not in p.parts)


def _line_count() -> int:
    """Physical line count of src/mcptoon/**/*.py — the guard's own ruler."""
    src = ROOT / "src" / "mcptoon"
    return sum(len(p.read_text(encoding="utf-8").splitlines())
               for p in src.rglob("*.py") if "__pycache__" not in p.parts)


def collected_total() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
        capture_output=True, text=True, cwd=ROOT,
    )
    n = sum(1 for line in proc.stdout.splitlines() if "::" in line)
    if n < 500:
        sys.exit(f"sync_claims: collection looks broken (only {n} tests); aborting")
    return n


def _replace_badge(text: str, passed: int) -> tuple[str, int]:
    changes = 0

    def _f(m):
        nonlocal changes
        if m.group(2) != str(passed):
            changes += 1
        # keep the original prefix casing (`tests-` vs `Tests-`)
        return f"{m.group(1)}{passed}{m.group(3)}"

    return _BADGE.sub(_f, text), changes


def _replace_live(text: str, passed: int) -> tuple[str, int]:
    changes = 0

    def _f(m):
        nonlocal changes
        if m.group(1) != str(passed):
            changes += 1
        return f"{passed} passed{m.group(2)}"

    return _LIVE.sub(_f, text), changes


def _replace_prose(text: str, passed: int) -> tuple[str, int]:
    changes = 0

    def _f(m):
        nonlocal changes
        if m.group(1).replace(",", "") != str(passed):
            changes += 1
        return f"{passed}{m.group(2)}"

    return _PROSE.sub(_f, text), changes


def _replace_version_line(text: str, passed: int) -> tuple[str, int]:
    changes = 0

    def _f(m):
        nonlocal changes
        if m.group(2) != str(passed):
            changes += 1
        return f"{m.group(1)}{passed} passed"

    return _VERSION_LINE.sub(_f, text), changes


def rewrite(text: str, passed: int) -> tuple[str, int]:
    total = 0
    for fn in (_replace_badge, _replace_live, _replace_prose, _replace_version_line):
        text, c = fn(text, passed)
        total += c
    return text, total


def rewrite_version_line(text: str, passed: int) -> tuple[str, int]:
    changes = 0

    def _f(m):
        nonlocal changes
        if m.group(2) != str(passed):
            changes += 1
        return f"{m.group(1)}{passed} passed"

    return _VERSION_LINE.sub(_f, text), changes


def _replace_linecount(text: str, lines: int) -> tuple[str, int]:
    changes = 0
    target = f"{lines:,}"

    def _f(m):
        nonlocal changes
        if m.group(1).replace(",", "") != str(lines):
            changes += 1
        return f"{target}{m.group(2)}"

    return _LINECOUNT.sub(_f, text), changes


def _replace_modcount(text: str, modules: int) -> tuple[str, int]:
    changes = 0

    def _f(m):
        nonlocal changes
        if m.group(1) != str(modules):
            changes += 1
        return f"{modules}{m.group(2)}"

    return _MODCOUNT.sub(_f, text), changes


def rewrite_tree_claims(text: str, lines: int, modules: int) -> tuple[str, int]:
    """Sync the line- and module-count claims (numbers only, wording untouched)."""
    total = 0
    for fn, arg in ((_replace_linecount, lines), (_replace_modcount, modules)):
        text, c = fn(text, arg)
        total += c
    return text, total


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync advertised test-count claims to collection.")
    ap.add_argument("--apply", action="store_true", help="write changes, then run the guard")
    ap.add_argument("--check", action="store_true", help="exit 1 if any surface is stale")
    args = ap.parse_args()

    collected = collected_total()
    passed = collected - DETERMINISTIC_SKIPS
    print(f"collection: {collected} collected - {DETERMINISTIC_SKIPS} skipped = {passed} passed")

    n_lines = _line_count()
    n_modules = _module_count()
    print(f"tree: {n_modules} modules, {n_lines:,} physical lines (src/mcptoon/**/*.py)")

    stale = 0
    for rel in SURFACES:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        new, n = rewrite(text, passed)
        new, nt = rewrite_tree_claims(new, n_lines, n_modules)
        n += nt
        if n:
            stale += n
            print(f"  {rel}: {n} claim(s) -> passed {passed}, lines {n_lines:,}, modules {n_modules}")
            if args.apply and new != text:
                path.write_text(new, encoding="utf-8")

    if args.check:
        print("STALE" if stale else "all surfaces consistent")
        return 1 if stale else 0

    if not args.apply:
        print(f"dry-run: {stale} stale claim(s). Re-run with --apply to write.")
        return 0

    if stale == 0:
        print("nothing to change")
        return 0

    print("\nverifying with the guard...")
    guard = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_footprint_claims.py", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
    )
    return 0 if guard.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
