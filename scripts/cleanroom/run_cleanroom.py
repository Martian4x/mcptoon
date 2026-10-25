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

"""Run mcptoon end to end inside a clean room, then prove the real machine is untouched.

A clean room is a throwaway directory that stands in for `$HOME` / `$APPDATA` and
every `MCPTOON_*` path. Inside it we seed all twelve agent hosts (see
`seed_agents.py`), a small real skill catalog, and a real local MCP server, then
drive the real `mcptoon` binary through the surfaces that matter:

    skills index / list / resolve / sync / doctor   -- the skill subsystem
    sync --self / status / off / off-round-trip      -- the gateway lifecycle
    manifest / call                                  -- the MCP proxy runtime

Every check runs the shipped code against real files in the clean room. Before and
after, the developer's own sensitive files are hashed: if any changed, the run
fails loudly -- the point of a clean room is that nothing leaks out of it.

    python scripts/cleanroom/run_cleanroom.py            # throwaway root
    python scripts/cleanroom/run_cleanroom.py --root DIR  # keep the root for poking
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import seed_agents  # noqa: E402

GATEWAY = "mcptoon"
USER_SERVER = "user-tool"
# The clean room ships its own real stdio MCP server, so `manifest`/`call` have
# something to talk to without installing or fetching anything.
_LOCAL_SERVER = Path(__file__).resolve().parent / "local_mcp_server.py"


# ── the real machine's files that must not move ────────────────────────────

def _sensitive_files() -> list[Path]:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    appdata = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
    return [
        home / ".claude.json",
        home / ".cursor" / "mcp.json",
        home / ".gemini" / "GEMINI.md",
        home / ".config" / "opencode" / "AGENTS.md",
        home / ".claude" / "CLAUDE.md",
        home / ".mcptoon" / "config.json",
        home / ".mcptoon" / "settings.json",
        home / ".mcptoon" / ".selfheal",
        appdata / "Claude" / "claude_desktop_config.json",
        appdata / "Code" / "User" / "settings.json",
        home / ".codeium" / "windsurf" / "mcp_config.json",
    ]


def _hash_all(paths: list[Path]) -> dict[str, str]:
    return {str(p): (hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "(absent)")
            for p in paths}


# ── clean-room construction ────────────────────────────────────────────────

_SKILLS = {
    "cleanroom-alpha": ("Clean-room alpha skill", "Use when the task is alpha."),
    "cleanroom-beta": ("Clean-room beta skill", "Use when the task is beta."),
    "cleanroom-gamma": ("Clean-room gamma skill", "Use when the task is gamma."),
}


def _seed_skill_source(root: Path) -> Path:
    """A tiny real catalog: three skills, each a directory with a SKILL.md."""
    src = root / "skills-source"
    for slug, (title, desc) in _SKILLS.items():
        d = src / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {slug}\ndescription: {desc}\n---\n\n# {title}\n\nBody.\n",
            encoding="utf-8")
    return src


def _clean_env(root: Path) -> dict:
    env = dict(os.environ)
    home = root / "home"
    env.update({
        "USERPROFILE": str(home),
        "HOME": str(home),
        "APPDATA": str(root / "appdata"),
        "MCPTOON_HOME": str(root / "mcptoon"),
        "MCPTOON_SKIP_SELF_HEAL": "1",
        "MCPTOON_SKILLS_ROOTS": os.pathsep.join(
            str(root / "skills-source")),
        "MCPTOON_SKILLS_VIEWS": os.pathsep.join(
            str(root / "views" / n) for n in ("claude", "codex", "cursor")),
        "MCPTOON_SKILLS_DERIVED": f"roo={root / 'views' / 'roo'}",
        "MCPTOON_SKILLS_INDEX": str(root / "mcptoon" / "skills-index.json"),
        "MCPTOON_SKILLS_LEDGER": str(root / "mcptoon" / "skill_versions.json"),
        "MCPTOON_FOOTER_STATE_FILE": str(root / "mcptoon" / "footer-state.json"),
    })
    return env


def _mcptoon(env: dict, cwd: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "mcptoon", *args],
                          capture_output=True, text=True, env=env, cwd=str(cwd),
                          timeout=timeout)


# ── host config readers ────────────────────────────────────────────────────

def _servers(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if path.name == "settings.json":
        return (data.get("mcp") or {}).get("servers") or {}
    return data.get("mcpServers") or {}


def _json_hosts(seeded: dict[str, dict]) -> list[tuple[str, Path]]:
    """(host_id, global config path) for every host that carries servers as JSON.

    `seeded` comes from the seeder, so the paths are the ones mcptoon itself
    resolved -- not a second copy of the same rules that could drift.
    """
    out = []
    for host_id, info in seeded.items():
        if info["shape"] in ("mcpServers", "mcp.servers"):
            out.append((host_id, info["paths"][0]))
    return out


def _project_legs(seeded: dict[str, dict]) -> list[tuple[str, Path]]:
    """Extra (non-first) config paths -- Cursor's project-level file lives here."""
    return [(h, p) for h, info in seeded.items() for p in info["paths"][1:]]


def _pointer_hosts(seeded: dict[str, dict]) -> list[tuple[str, Path]]:
    return [(h, info["paths"][0]) for h, info in seeded.items()
            if info["shape"] == "pointer"]


# ── checks ─────────────────────────────────────────────────────────────────

class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, ok, detail))

    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.rows)

    def print(self) -> None:
        print("\n" + "=" * 72)
        print("CLEAN-ROOM REPORT")
        print("=" * 72)
        for name, ok, detail in self.rows:
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))
        print("=" * 72)
        print("RESULT:", "ALL PASS" if self.ok() else "FAILURES ABOVE")
        print("=" * 72)


def main() -> int:
    ap = argparse.ArgumentParser(description="Clean-room end-to-end run.")
    ap.add_argument("--root", type=Path, default=None,
                    help="clean-room root (default: a throwaway temp dir)")
    args = ap.parse_args()

    keep = args.root is not None
    root = args.root if keep else Path(tempfile.mkdtemp(prefix="mcptoon-cleanroom-"))
    root = root.resolve()
    if root.exists() and keep:
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    rep = Report()
    real_before = _hash_all(_sensitive_files())

    print(f"clean room: {root}")
    print(f"real-machine files watched: {len(real_before)}")

    # 1) seed all twelve hosts + a real skill catalog
    seeded = seed_agents.seed(root, USER_SERVER)
    src = _seed_skill_source(root)
    (root / "views").mkdir(parents=True, exist_ok=True)
    rep.add("seed 12 agent hosts", len(seeded) == 12, f"{len(seeded)} hosts")

    env = _clean_env(root)
    cwd = root / "cwd"          # neutral cwd (Cursor's project-level path lives here)

    # 2) skills: index -> list -> resolve
    r = _mcptoon(env, cwd, "skills", "index", str(src))
    idx = root / "mcptoon" / "skills-index.json"
    rep.add("skills index", r.returncode == 0 and idx.exists(),
            f"exit {r.returncode}, index={'yes' if idx.exists() else 'no'}")

    r = _mcptoon(env, cwd, "skills", "list")
    listed = sum(1 for s in _SKILLS if s in r.stdout)
    rep.add("skills list shows the catalog", listed == len(_SKILLS),
            f"{listed}/{len(_SKILLS)} skills in output")

    r = _mcptoon(env, cwd, "skills", "resolve", "the alpha task", "--k", "3")
    rep.add("skills resolve finds the right skill", "cleanroom-alpha" in r.stdout,
            f"exit {r.returncode}")

    # 3) skills sync: link the catalog into the view roots
    views = [str(root / "views" / n) for n in ("claude", "codex", "cursor")]
    for v in views:
        Path(v).mkdir(parents=True, exist_ok=True)
    r = _mcptoon(env, cwd, "skills", "sync", str(src), *views)
    synced = sum(1 for v in views
                 if (Path(v) / "cleanroom-alpha").exists()
                 or (Path(v) / "cleanroom-alpha" / "SKILL.md").exists())
    rep.add("skills sync populates the views", r.returncode == 0 and synced == len(views),
            f"exit {r.returncode}, views filled {synced}/{len(views)}")

    # 4) skills doctor: read-only health check, exit 0 when clean
    r = _mcptoon(env, cwd, "skills", "doctor", str(src), *views)
    rep.add("skills doctor is green on a clean room", r.returncode == 0,
            f"exit {r.returncode}")

    # 5) gateway lifecycle: sync --self -> status -> off -> round trip
    # mcptoon only mounts a gateway when there is at least one server to carry;
    # an empty config is skipped by design. Put one real local server in first.
    cfg = root / "mcptoon" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"servers": {"echo": {
        "transport": "stdio",
        "command": [sys.executable],
        "args": [str(_LOCAL_SERVER)],
    }}}), encoding="utf-8")

    json_hosts = [p for _, p in _json_hosts(seeded)]          # 6 MCP hosts
    project_legs = [p for _, p in _project_legs(seeded)]      # cursor project file
    # pointer files: the six CLI-leg hosts, plus Claude Code's memory file
    claude_mem = root / "home" / ".claude" / "CLAUDE.md"
    ptr_hosts = [p for _, p in _pointer_hosts(seeded)] + [claude_mem]

    r = _mcptoon(env, cwd, "sync", "--self")
    present = [p for p in json_hosts if GATEWAY in _servers(p)]
    rep.add("sync --self writes the gateway to every JSON host",
            r.returncode == 0 and len(present) == len(json_hosts),
            f"exit {r.returncode}, {len(present)}/{len(json_hosts)} hosts")

    ptr_present = [p for p in ptr_hosts
                   if p.exists() and "Skill catalog (mcptoon)" in p.read_text(encoding="utf-8")]
    rep.add("sync --self writes the pointer to every CLI host",
            len(ptr_present) == len(ptr_hosts),
            f"{len(ptr_present)}/{len(ptr_hosts)} pointer files")

    r = _mcptoon(env, cwd, "status")
    rep.add("status reports the gateway present", "yes" in r.stdout.lower(),
            f"exit {r.returncode}")

    # What `off` must not touch: every host's *own* servers, entry for entry.
    # The gateway is the only key that may disappear; `echo` and `user-tool` are
    # the user's and must survive with identical config (the reversibility promise).
    def _own_servers(path: Path) -> dict:
        return {k: v for k, v in _servers(path).items() if k != GATEWAY}

    all_configs = json_hosts + project_legs
    before_own = {str(p): _own_servers(p) for p in all_configs}

    r = _mcptoon(env, cwd, "off")
    gone = [p for p in json_hosts if GATEWAY not in _servers(p)]
    legs_cleaned = [p for p in project_legs if GATEWAY not in _servers(p)]
    rep.add("off removes the gateway from every JSON host",
            len(gone) == len(json_hosts) and len(legs_cleaned) == len(project_legs),
            f"{len(gone)}/{len(json_hosts)} global, {len(legs_cleaned)}/{len(project_legs)} project")
    users_intact = all(_own_servers(p) == before_own[str(p)] for p in all_configs)
    rep.add("off leaves every host's own servers identical", users_intact,
            f"{len(all_configs)} configs compared entry-for-entry")
    no_residue = not any(p.exists() and p.stat().st_size == 0 for p in ptr_hosts)
    ptr_gone = all(
        (not p.exists()) or "Skill catalog (mcptoon)" not in p.read_text(encoding="utf-8")
        for p in ptr_hosts)
    rep.add("off removes every pointer block", ptr_gone, "")
    rep.add("off leaves no empty-file residue", no_residue, "")

    # round trip: the first off left every host gateway-free; a second off must
    # leave it exactly the same (idempotent), and no file may appear or vanish.
    watched = all_configs + ptr_hosts
    first_off = {str(p): (p.read_bytes() if p.exists() else None) for p in watched}
    _mcptoon(env, cwd, "sync", "--self")
    _mcptoon(env, cwd, "off")
    second_off = {str(p): (p.read_bytes() if p.exists() else None) for p in watched}
    rep.add("off is idempotent (second off == first off)", second_off == first_off,
            "")

    # 6) MCP proxy runtime: a real local server, listed and called.
    # The manifest assertion checks the *tool name*, not the substring "echo" --
    # "echo" also appears in a failure message that quotes the server path, which
    # is how a broken manifest first looked like a pass in the Linux container
    # (2026-10-11). A green here means the server actually answered tools/list.
    r = _mcptoon(env, cwd, "manifest")
    manifest_ok = (r.returncode == 0 and "PROCESS_DIED" not in r.stdout + r.stderr
                   and "echo" in r.stdout)
    rep.add("manifest lists a real local server's tools", manifest_ok,
            f"exit {r.returncode}")
    r = _mcptoon(env, cwd, "call", "echo", "echo", '{"message":"cleanroom"}')
    call_ok = r.returncode == 0 and "cleanroom" in r.stdout
    rep.add("call proxies a real tool call", call_ok,
            f"exit {r.returncode}" + ("" if call_ok else f", out={r.stdout.strip()[:80]!r}"))

    # 7) the real machine must be untouched
    real_after = _hash_all(_sensitive_files())
    drift = [k for k in real_before if real_before[k] != real_after.get(k)]
    rep.add("real machine untouched (sha256 before/after)", not drift,
            f"{len(real_before)} files, drift={drift or 'none'}")

    rep.print()
    if not keep:
        shutil.rmtree(root, ignore_errors=True)
    else:
        print(f"\nclean room kept at: {root}")
    return 0 if rep.ok() else 1


if __name__ == "__main__":
    raise SystemExit(main())
