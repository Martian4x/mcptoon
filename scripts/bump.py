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

"""Bump every version reference in the tree from one argument.

The release ritual has always said "the version lives in five places", but the
guards know it lives in more: `softwareVersion` in the two landing pages and the
`vX.Y.Z · N passed` line in DEVELOPERS.md / ROADMAP.md are also checked against
`pyproject.toml`. Missing any one of them is how 0.7.6 shipped with
`mcptoon --version` printing 0.7.5, and how a release goes red on
`test_registry_sync` / `test_footprint_claims` after the tag is already pushed.

This script reads the current version from `pyproject.toml`, rewrites every
guarded reference to the new one, and (if `CHANGELOG.md` still has an
`## [Unreleased]` heading) renames that heading to the new version + date. It is
idempotent and prints exactly what it changed. Run the two guard suites after it
(`python -m pytest tests/test_registry_sync.py tests/test_footprint_claims.py
tests/test_gemini_manifest.py`) — that is what proves the tree is consistent.

Usage:
    python scripts/bump.py 0.8.22                 # bump + stamp CHANGELOG today
    python scripts/bump.py 0.8.22 --date 2026-10-18
    python scripts/bump.py 0.8.22 --dry-run       # show changes, write nothing
"""
from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (file, regex, replacement, label). The regex has exactly one capture group: the
# literal text *before* the version (including any opening quote). The replacement
# re-emits that group then the new version with the same trailing quote, so a
# quoted field stays quoted and an unquoted `vX.Y.Z` stays unquoted. Keeping the
# quote in the replacement is not cosmetic: dropping it produces `version = 0.8.22`
# and TOML/JSON refuse to parse (this bit the first draft).
_TARGETS = [
    ("pyproject.toml", r'(?m)^(version\s*=\s*)"{old}"', r'\g<1>"{new}"', "project version"),
    ("src/mcptoon/__init__.py", r'(__version__\s*=\s*)"{old}"', r'\g<1>"{new}"', "__version__"),
    ("gemini-extension.json", r'("version"\s*:\s*)"{old}"', r'\g<1>"{new}"', "gemini manifest version"),
    ("claude-code-plugin/.claude-plugin/plugin.json", r'("version"\s*:\s*)"{old}"', r'\g<1>"{new}"', "claude plugin manifest version"),
    ("server.json", r'("version"\s*:\s*)"{old}"', r'\g<1>"{new}"', "server.json version (x2)"),
    ("docs/index.html", r'("softwareVersion"\s*:\s*)"{old}"', r'\g<1>"{new}"', "landing softwareVersion"),
    ("docs/index-zh.html", r'("softwareVersion"\s*:\s*)"{old}"', r'\g<1>"{new}"', "landing-zh softwareVersion"),
    ("docs/index.html", r"(GitHub v){old}\b", r"\g<1>{new}", "landing GitHub badge"),
    ("docs/index-zh.html", r"(GitHub v){old}\b", r"\g<1>{new}", "landing-zh GitHub badge"),
    ("DEVELOPERS.md", r"(?m)(v){old}(?=\s*[·|])", r"\g<1>{new}", "DEVELOPERS version line"),
    ("ROADMAP.md", r"(?m)(v){old}(?=\s*[·|])", r"\g<1>{new}", "ROADMAP version line"),
]


def current_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if not m:
        sys.exit("bump: no version found in pyproject.toml")
    return m.group(1)


def _parse(v: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", v)
    if not m:
        sys.exit(f"bump: version {v!r} is not X.Y.Z")
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def main() -> int:
    ap = argparse.ArgumentParser(description="Bump every version reference from one argument.")
    ap.add_argument("new_version", help="target version, e.g. 0.8.22")
    ap.add_argument("--date", default=datetime.date.today().isoformat(),
                    help="date for the CHANGELOG heading (default: today)")
    ap.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    args = ap.parse_args()

    old = current_version()
    new = args.new_version
    if _parse(new) <= _parse(old):
        sys.exit(f"bump: {new} is not greater than the current {old}")
    if new == old:
        sys.exit(f"bump: already at {old}")

    changes: list[str] = []
    # Group by file so each file is read once and written once. A file may carry
    # several patterns (server.json has two version fields; each landing page has
    # softwareVersion + a badge), so apply every matching pattern to the same text.
    per_file: dict[str, list[tuple[str, str, int]]] = {}
    for rel, pattern, repl, label in _TARGETS:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        rx = re.compile(pattern.format(old=re.escape(old)))
        _new_text, n = rx.subn(repl.format(new=new), text)
        if n:
            per_file.setdefault(rel, []).append((pattern, repl, n))
            changes.append(f"  {rel}: {label}  ({n}x) {old} -> {new}")
        else:
            changes.append(f"  {rel}: {label}  [NOT FOUND - pattern missed?]")

    # Apply: for each file, run every pattern that hit it against one live text.
    for rel, hits in per_file.items():
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        for pattern, repl, _n in hits:
            rx = re.compile(pattern.format(old=re.escape(old)))
            text = rx.sub(repl.format(new=new), text)
        if not args.dry_run:
            path.write_text(text, encoding="utf-8")

    # CHANGELOG: rename the Unreleased heading, if present.
    changelog = ROOT / "CHANGELOG.md"
    cl = changelog.read_text(encoding="utf-8")
    if "## [Unreleased]" in cl:
        cl_new = cl.replace("## [Unreleased]", f"## [{new}] - {args.date}", 1)
        changes.append(f"  CHANGELOG.md: [Unreleased] -> [{new}] - {args.date}")
        if not args.dry_run:
            changelog.write_text(cl_new, encoding="utf-8")
    else:
        changes.append("  CHANGELOG.md: no [Unreleased] heading (write the entry by hand)")

    verb = "would change" if args.dry_run else "changed"
    print(f"bump {old} -> {new}  ({verb}):")
    print("\n".join(changes))
    if any("NOT FOUND" in c for c in changes):
        print("\nWARNING: a pattern missed - the file layout may have changed; check by hand.")
        return 2
    if not args.dry_run:
        print("\nNext: python -m pytest tests/test_registry_sync.py "
              "tests/test_footprint_claims.py tests/test_gemini_manifest.py -q")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
