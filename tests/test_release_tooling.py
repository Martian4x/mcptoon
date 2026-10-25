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

"""Guards for the release helpers in `scripts/`.

`bump.py` and `sync_claims.py` exist because doing these edits by hand shipped
stale versions and stale test totals more than once. A helper that silently stops
matching the tree is worse than no helper — the release goes out with a value the
guards will later reject, after the tag is pushed. So these tests fail loudly if a
pattern no longer finds its target, or if the rewrite mangles a spelling.
"""
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _current_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'(?m)^version\s*=\s*"([^"]+)"', text).group(1)


def _bump(v: str) -> str:
    a, b, c = (int(x) for x in v.split("."))
    return f"{a}.{b}.{c + 1}"


def test_bump_dry_run_matches_every_target():
    """If a target file's layout changes, bump.py must say so, not miss it silently."""
    target = _bump(_current_version())
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "bump.py"), target, "--dry-run"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "NOT FOUND" not in proc.stdout, proc.stdout


def test_bump_keeps_quotes_on_quoted_fields(tmp_path):
    """The first draft dropped the closing quote and produced `version = 0.8.22`."""
    mod = _load("bump")
    text = 'version = "0.8.21"\n'
    pattern, repl, _label = mod._TARGETS[0][1], mod._TARGETS[0][2], mod._TARGETS[0][3]
    rx = re.compile(pattern.format(old=re.escape("0.8.21")))
    out = rx.sub(repl.format(new="0.8.22"), text)
    assert out == 'version = "0.8.22"\n'


def test_sync_claims_rewrites_every_spelling():
    mod = _load("sync_claims")
    cases = [
        ("badge tests-2097%20passed ok", "badge tests-2099%20passed ok"),
        ("2097 passed · 2 skipped", "2099 passed · 2 skipped"),
        ("2097 passed + 2 skipped", "2099 passed + 2 skipped"),
        ("2097 passed, 2 skipped", "2099 passed, 2 skipped"),
        ("# 2097 tests, green expected", "# 2099 tests, green expected"),
        ("共 2097 个测试", "共 2099 个测试"),
        ("v0.8.21 · 2097 passed + 2 skipped", "v0.8.21 · 2099 passed + 2 skipped"),
    ]
    for src, want in cases:
        out, n = mod.rewrite(src, 2099)
        assert out == want, (src, out)
        assert n == 1, (src, n)
    # token figures are not suite totals
    assert mod.rewrite("8,282 tokens", 2099) == ("8,282 tokens", 0)
    # already-correct text is untouched and reports zero changes
    assert mod.rewrite("2099 passed · 2 skipped", 2099) == ("2099 passed · 2 skipped", 0)
