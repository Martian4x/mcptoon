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

"""`sync_claims` must own the line- and module-count claims, not just test totals.

Measured drift (2026-10-19): after a refactor added a module, README still said
"31,410 lines ... 45 modules" while the tree was 31,979 lines / 46 modules. The
guard (`test_footprint_claims`) tolerated the line gap (2%) but the module count is
exact — and nothing auto-wrote either, so they only moved when someone remembered
to hand-edit five files. This pins that `sync_claims` now writes both, so the next
refactor moves them mechanically.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sync_claims  # noqa: E402


def _real_tree():
    src = ROOT / "src" / "mcptoon"
    mods = [p for p in src.rglob("*.py") if "__pycache__" not in p.parts]
    lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in mods)
    return len(mods), lines


def test_line_count_uses_the_guard_ruler():
    """sync_claims' ruler must equal the guard's, or the two disagree forever."""
    n_modules, n_lines = _real_tree()
    assert sync_claims._module_count() == n_modules
    assert sync_claims._line_count() == n_lines


def test_rewrite_moves_a_stale_line_and_module_claim():
    stale = "The codebase: **30,000 lines of Python across 40 modules**, zero deps."
    new, changes = sync_claims.rewrite_tree_claims(stale, 31_979, 46)
    assert changes == 2
    assert "31,979 lines of" in new
    assert "46 modules" in new
    assert "30,000" not in new and "40 modules" not in new


def test_rewrite_handles_the_chinese_forms():
    stale = "代码规模：**40 个模块、30,000 行 Python**"
    new, changes = sync_claims.rewrite_tree_claims(stale, 31_979, 46)
    assert changes == 2
    assert "46 个模块" in new
    assert "31,979 行" in new


def test_rewrite_handles_the_cross_line_form():
    """docs/README-details.md wraps '31,410\\nlines of' — the regex must span it."""
    stale = "45 modules, 30,000\nlines of Python"
    new, changes = sync_claims.rewrite_tree_claims(stale, 31_979, 46)
    assert "46 modules" in new
    assert "31,979\nlines of" in new


def test_no_change_when_already_correct():
    ok = "46 modules, 31,979\nlines of Python"
    new, changes = sync_claims.rewrite_tree_claims(ok, 31_979, 46)
    assert changes == 0
    assert new == ok


def test_surfaces_include_readme_details_for_the_tree_claim():
    assert "docs/README-details.md" in sync_claims.SURFACES
