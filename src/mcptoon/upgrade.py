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

"""`mcptoon upgrade` — bring the installed mcptoon itself up to date.

Distinct from `mcptoon update`, which refreshes the *tool surface* captured from
your MCP servers (a server added a tool, changed an argument). `upgrade` is about
the mcptoon program on disk: you installed 0.8.20, a newer release exists on
PyPI, and nothing tells you. This command asks PyPI for the latest version,
compares it with what is installed, and — on confirmation — upgrades in place.

Why a command and not a background nag: an agent-facing CLI must stay silent
unless asked. `upgrade --check` is the scriptable probe (exit 1 when a newer
release exists), so a shell profile or CI job can surface it; the bare command is
the human path and always confirms before touching the install.

Zero dependencies, like the rest of mcptoon: the PyPI lookup is a single
``urllib`` GET and the version compare is hand-rolled (no ``packaging`` import).

CLI:
    mcptoon upgrade              Ask PyPI, show the gap, confirm, then upgrade
    mcptoon upgrade --check      Report only; exit 1 if a newer release exists
    mcptoon upgrade --yes        Upgrade without the confirmation prompt
    mcptoon upgrade --json       Machine-readable result
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

__all__ = [
    "current_version",
    "parse_version",
    "is_newer",
    "fetch_latest",
    "upgrade_command",
    "read_cache",
    "footer_hint",
    "maybe_refresh_cache",
    "run",
]

PYPI_JSON = "https://pypi.org/pypi/mcptoon/json"
CACHE_NAME = "upgrade-check.json"


def current_version() -> str:
    """The version this interpreter has installed (not what PyPI serves)."""
    from . import __version__

    return __version__ or "0"


def parse_version(text: str) -> tuple:
    """A tolerant version key: ``(numeric_parts, is_final)``.

    Handles the shapes that actually reach us — ``0.8.28``, ``1.2``, ``0.8.29rc1``,
    ``0.9.0.post1`` — without pulling in ``packaging``. Numeric dotted parts become
    a tuple of ints; anything after the last digit (``rc1``, ``.post1``, ``.dev0``)
    marks a pre-release, which sorts *below* the plain release of the same numbers
    so ``0.9.0rc1 < 0.9.0``. Unparseable input sorts lowest and never looks newer.
    """
    if not isinstance(text, str):
        return ((), False)
    s = text.strip().lower()
    # Strip a leading 'v' (tags sometimes leak a "v0.8.28").
    if s.startswith("v"):
        s = s[1:]
    parts: list[int] = []
    i = 0
    n = len(s)
    while i < n and (s[i].isdigit() or s[i] == "."):
        j = i
        while j < n and s[j].isdigit():
            j += 1
        if j == i:  # a dot with no digits
            break
        parts.append(int(s[i:j]))
        i = j
        if i < n and s[i] == ".":
            i += 1
        else:
            break
    if not parts:
        return ((), False)
    is_final = i >= n  # nothing but numbers/dots consumed ⇒ a plain release
    return (tuple(parts), is_final)


def is_newer(latest: str, current: str) -> bool:
    """True when ``latest`` is a strictly newer release than ``current``."""
    lk, ck = parse_version(latest), parse_version(current)
    # Compare numeric tuples, padding the shorter so (0,8) < (0,8,1).
    ln, cn = lk[0], ck[0]
    width = max(len(ln), len(cn))
    ln_p = ln + (0,) * (width - len(ln))
    cn_p = cn + (0,) * (width - len(cn))
    if ln_p != cn_p:
        return ln_p > cn_p
    # Same numbers: a final release beats a pre-release of the same numbers.
    return lk[1] and not ck[1]


def fetch_latest(timeout: float = 10.0) -> tuple[str | None, str | None]:
    """Return ``(version, None)`` from PyPI, or ``(None, error)`` on any failure.

    Never raises: a machine behind a firewall or offline must get a clean message,
    not a traceback. The User-Agent names mcptoon so PyPI can attribute the call.
    """
    try:
        req = urllib.request.Request(
            PYPI_JSON,
            headers={"User-Agent": f"mcptoon/{current_version()} upgrade-check"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        version = (data.get("info") or {}).get("version")
        if not version:
            return None, "PyPI response had no version field"
        return str(version), None
    except Exception as e:  # URLError, timeout, JSON, anything
        return None, f"{type(e).__name__}: {e}"[:200]


def _detect_installer() -> str:
    """Guess how this mcptoon was installed: ``"uv"`` or ``"pip"``.

    ``uv`` is preferred only when it is on PATH *and* the running interpreter sits
    under a uv-managed tool directory — otherwise a pip install upgraded via uv
    would create a second copy. Override with ``MCPTOON_UPGRADE_CMD`` for odd
    setups (a vendored wheel, a system package manager, a frozen app).
    """
    if os.environ.get("MCPTOON_UPGRADE_CMD"):
        return "custom"
    exe = sys.executable.replace("\\", "/").lower()
    if shutil.which("uv") and ("/uv/tools/" in exe or "/uv/python/" in exe or "/uv/cache/" in exe):
        return "uv"
    return "pip"


def upgrade_command(installer: str, version: str) -> list[str]:
    """The argv that upgrades mcptoon to ``version`` (or the latest) for ``installer``.

    Pure so the tests pin exactly what would run, and so ``--check``/dry paths can
    show the command without executing it. The custom override is split with
    ``shlex``-style whitespace (quotes are honoured by the shell when it runs, but
    we run argv directly, so keep it simple: whitespace split).
    """
    if installer == "custom":
        cmd = os.environ.get("MCPTOON_UPGRADE_CMD", "")
        return cmd.split() if cmd.strip() else [sys.executable, "-m", "pip", "install", "--upgrade", "mcptoon"]
    if installer == "uv":
        # `uv tool upgrade` for tool installs; `uv pip install --upgrade` covers
        # venv installs. `uv tool upgrade` is a no-op for a venv, so prefer the
        # pip form only when no tool of that name exists.
        return ["uv", "tool", "upgrade", "mcptoon"]
    return [sys.executable, "-m", "pip", "install", "--upgrade", "mcptoon"]


def _cache_path():
    """Where the last `upgrade --check` result is remembered (read by the footer)."""
    from . import config
    return config.STATE_DIR / CACHE_NAME


def write_cache(latest, err, *, checked_at=None) -> None:
    """Persist the last check so `footer-facts` can hint without a network call.

    Skipped under pytest unless a test opts in with ``MCPTOON_UPGRADE_CACHE`` — a
    normal test run must not leave a cache that later flips another footer test's
    output. Production is unaffected.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("MCPTOON_UPGRADE_CACHE"):
        return
    try:
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "installed": current_version(),
            "latest": latest,
            "error": err,
            "checked_at": time.time() if checked_at is None else checked_at,
        }
        p.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


def read_cache():
    """Return the cached check dict, or None if absent/unreadable."""
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return None


def footer_hint(*, max_age_hours: float = 48.0) -> str | None:
    """A one-line 'new version available' hint for the footer, or None.

    Reads only the cache written by `upgrade --check` — never the network, so it is
    safe in the per-turn footer. Returns None when there is no fresh-enough record,
    the last check errored, or the installed version is already current.
    """
    c = read_cache()
    if not c or c.get("error") or not c.get("latest"):
        return None
    try:
        age = time.time() - float(c.get("checked_at") or 0)
    except (TypeError, ValueError):
        return None
    if age > max_age_hours * 3600:
        return None
    installed = current_version()
    if str(c.get("installed")) != installed:
        return None  # we upgraded since the check; the record is about the old install
    if not is_newer(str(c["latest"]), installed):
        return None
    return (f"  新版 mcptoon {c['latest']} 可用（当前 {installed}）"
            f" · mcptoon upgrade")


def maybe_refresh_cache(*, ttl_hours: float = 24.0) -> None:
    """If the cache is stale, spawn a detached `upgrade --check --quiet` to refresh.

    Fire-and-forget: never blocks the caller, never touches the network inline, and
    silently does nothing when a recent record already exists. The child is
    detached so a slow or offline PyPI can never delay the footer.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return  # never spawn a background check from inside the test suite
    c = read_cache()
    if c and (time.time() - float(c.get("checked_at") or 0)) < ttl_hours * 3600:
        return
    try:
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
        else:
            kw["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, "-m", "mcptoon", "upgrade", "--check", "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **kw,
        )
    except Exception:
        pass


def _render_gap(current: str, latest: str, *, checked: bool) -> str:
    return (
        f"  installed  {current}\n"
        f"  latest     {latest}\n"
        f"  a newer release is available.\n"
        + ("  run 'mcptoon upgrade' to install it (or 'mcptoon upgrade --yes')."
           if checked else "")
    )


def run(rest: list, fmt: str = "auto") -> None:
    """Usage: mcptoon upgrade [--check] [--yes] [--json]

    Compare the installed mcptoon with the latest release on PyPI.

    --check   Report only; do not upgrade. Exit 1 when a newer release exists,
              0 when up to date, 2 when PyPI could not be reached.
    --yes     Skip the confirmation prompt and upgrade immediately.
    --json    Machine-readable result instead of the human report.

    Exit codes (bare / --yes mode): 0 up to date or upgraded, 1 upgrade failed.
    """
    check = any(a in ("--check", "--dry", "--dry-run") for a in rest)
    assume_yes = any(a in ("--yes", "-y") for a in rest)
    quiet = any(a in ("--quiet", "-q") for a in rest)

    current = current_version()
    latest, err = fetch_latest()

    # Every explicit check updates the cache the footer reads. A --quiet run is the
    # detached refresher, so it is the most important writer; a normal run writes too.
    write_cache(latest, err)

    if fmt == "json":
        payload = {
            "installed": current,
            "latest": latest,
            "newer_available": bool(latest and is_newer(latest, current)),
            "error": err,
            "checked": check,
        }

    if err:
        if fmt == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        elif not quiet:
            print(f"  could not check PyPI for the latest mcptoon: {err}")
            print("  (this is a network problem, not an 'up to date' result)")
        sys.exit(2)

    newer = is_newer(latest, current)

    if not newer:
        if fmt == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        elif not quiet:
            print(f"  mcptoon {current} is up to date (latest on PyPI: {latest}).")
        return

    if check:
        if fmt == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        elif not quiet:
            print(_render_gap(current, latest, checked=True))
        sys.exit(1)

    # Upgrade path.
    installer = _detect_installer()
    cmd = upgrade_command(installer, latest)

    if fmt == "json":
        # --json + real upgrade is unusual; report intent, still upgrade below.
        pass
    else:
        print(_render_gap(current, latest, checked=False))
        print(f"  source: {installer} — {' '.join(cmd)}")

    if not assume_yes and fmt != "json":
        try:
            answer = input("  proceed? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("  cancelled — nothing changed.")
            return

    if fmt == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    try:
        rc = subprocess.run(cmd, check=False).returncode
    except Exception as e:
        print(f"  upgrade failed to start: {type(e).__name__}: {e}")
        sys.exit(1)

    if rc != 0:
        print(f"  upgrade command exited {rc}. The installed version is unchanged.")
        print(f"  You can run it yourself: {' '.join(cmd)}")
        sys.exit(1)

    print(f"  upgraded. Re-run 'mcptoon --version' to confirm {latest} is active.")
