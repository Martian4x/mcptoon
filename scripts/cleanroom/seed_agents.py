#!/usr/bin/env python3
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

"""Seed a *clean room*: every agent host's config, inside a throwaway root.

mcptoon never launches an agent to do its job -- it reads and writes that agent's
config file. So a host does not have to be installed to be exercised; a stub file
at the path mcptoon looks for is the real interface. This script writes those
stubs for every host in `_AGENT_SPECS`, so `sync` / `off` / `skills` can be run
end to end against all of them without touching the developer's machine.

The paths are taken from mcptoon itself (`_paths_for`), not re-derived here, so a
stub lands exactly where the product looks -- if the product's path logic changes,
the seed follows it instead of silently drifting.

Usage:
    python scripts/cleanroom/seed_agents.py --root <clean-root> [--user-server NAME]

`--root` becomes the fake home; `<root>/appdata` becomes the fake APPDATA. The
script prints one line per host so a caller can see what was created.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Make `import mcptoon` work from a checkout (no install needed).
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))

# The stub server every host's config carries, so `off` has something to NOT
# delete. `command` is a real interpreter and a real script -- a host entry is a
# process spec, and using a fake command would make the "servers survive" check
# weaker than it should be.
_USER_SERVER = {
    "command": sys.executable,
    "args": ["-c", "print('cleanroom user server')"],
}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def seed(root: Path, user_server: str = "user-tool") -> dict[str, dict]:
    """Create every host's stub under ``root``.

    Returns ``{host_id: {"shape": ..., "paths": [Path, ...]}}`` -- the shape and
    the paths mcptoon itself resolved, so a caller (run_cleanroom) can check
    exactly the files the product would write, on whatever platform this runs.
    Resolving them here, with the clean env in place, is why the same seeder
    works on Windows and in a Linux container.
    """
    home = root / "home"
    appdata = root / "appdata"
    home.mkdir(parents=True, exist_ok=True)
    appdata.mkdir(parents=True, exist_ok=True)
    # Cursor's project-level path is `Path.cwd() / ".cursor/mcp.json"`. From a repo
    # checkout that would drop a `.cursor/` into the caller's directory (measured
    # 2026-10-11: seeding from the repo wrote `.cursor/mcp.json` into the repo). A
    # neutral cwd inside the clean room keeps the project-level stub contained.
    neutral = root / "cwd"
    neutral.mkdir(parents=True, exist_ok=True)
    os.chdir(neutral)

    # Point mcptoon's own path logic at the clean root *before* importing it, so
    # `_paths_for` resolves inside the clean room. The previous values are restored
    # on the way out: a caller that snapshots the real machine's files afterwards
    # must not inherit a mutated environment (that bug made a real-machine check
    # compare the clean room against itself and report false drift, 2026-10-11).
    _KEYS = ("USERPROFILE", "HOME", "APPDATA", "MCPTOON_HOME")
    saved = {k: os.environ.get(k) for k in _KEYS}
    os.environ["USERPROFILE"] = str(home)
    os.environ["HOME"] = str(home)
    os.environ["APPDATA"] = str(appdata)
    os.environ["MCPTOON_HOME"] = str(root / "mcptoon")

    try:
        from mcptoon import sync as sync_mod  # noqa: E402  (env must be set first)
        seeded = _seed_under(sync_mod._AGENT_SPECS, sync_mod._paths_for, home, user_server)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return seeded


def _seed_under(specs, paths_for, home: Path, user_server: str) -> dict[str, dict]:
    seeded: dict[str, dict] = {}
    for spec in specs:
        paths = paths_for(spec.id)
        shape = getattr(spec, "shape", "mcpServers")
        if shape == "pointer":
            # CLI-leg host: the delivery is a pointer block mcptoon appends to the
            # host's global instruction file. Seed the *directory* (or the file for
            # codex, which is detected by the file) so the host is detected; leave
            # the pointer for `sync --self` to write, so the round trip is real.
            p = paths[0]
            p.parent.mkdir(parents=True, exist_ok=True)
            if getattr(spec, "detected_by", "parent") == "file" and not p.exists():
                p.write_text("", encoding="utf-8")
            seeded[spec.id] = {"shape": shape, "paths": paths}
            continue

        for p in paths:
            # Cursor lists a global and a project-level file; seed both so the
            # project-level cleanup path (a real bug once) is exercised too.
            if shape == "mcp.servers":
                _write_json(p, {"mcp": {"servers": {user_server: dict(_USER_SERVER)}}})
            else:
                _write_json(p, {"mcpServers": {user_server: dict(_USER_SERVER)}})
        # Claude Code reads a memory file as a second leg; make its directory so
        # the pointer write has somewhere to land.
        if spec.id == "claude-code":
            (home / ".claude").mkdir(parents=True, exist_ok=True)
        seeded[spec.id] = {"shape": shape, "paths": paths}
    return seeded


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed a clean room of agent configs.")
    ap.add_argument("--root", required=True, type=Path,
                    help="clean-room root (becomes the fake home)")
    ap.add_argument("--user-server", default="user-tool",
                    help="name of the marker server each host config carries")
    args = ap.parse_args()

    seeded = seed(args.root, args.user_server)
    print(f"seeded {len(seeded)} hosts under {args.root}:")
    for h, info in seeded.items():
        print(f"   {h:16s} {info['shape']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
