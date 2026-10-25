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

"""Tests for `mcptoon upgrade` — bringing the installed CLI up to date.

Three layers, deliberately:

  * pure version logic (`parse_version` / `is_newer`) — the decision "is there a
    newer release" must not depend on the network or the environment;
  * the upgrade argv (`upgrade_command`) — pin exactly what would run per installer;
  * `run()` end to end with the network and subprocess mocked — the exit codes and
    messages a shell script depends on (`--check` exit 1 on drift, exit 2 offline).

No live PyPI call: a test that needs the internet is a test that fails on a plane.
The one property that ties it to reality — `current_version()` equals the package's
own `__version__` — is asserted directly.
"""
import json
import os
import sys

_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from mcptoon import upgrade as upgrade_mod  # noqa: E402


# ─── pure: version parsing and comparison ───

class TestParseVersion:
    def test_plain_release(self):
        assert upgrade_mod.parse_version("0.8.28") == ((0, 8, 28), True)

    def test_two_part(self):
        assert upgrade_mod.parse_version("1.2") == ((1, 2), True)

    def test_leading_v(self):
        assert upgrade_mod.parse_version("v0.8.28") == ((0, 8, 28), True)

    def test_rc_is_not_final(self):
        assert upgrade_mod.parse_version("0.8.29rc1") == ((0, 8, 29), False)

    def test_post_is_not_final(self):
        assert upgrade_mod.parse_version("0.9.0.post1") == ((0, 9, 0), False)

    def test_garbage_is_empty_and_never_newer(self):
        assert upgrade_mod.parse_version("not-a-version") == ((), False)
        assert upgrade_mod.parse_version("") == ((), False)
        assert upgrade_mod.parse_version(None) == ((), False)


class TestIsNewer:
    def test_strictly_newer(self):
        assert upgrade_mod.is_newer("0.8.29", "0.8.28") is True

    def test_same_is_not_newer(self):
        assert upgrade_mod.is_newer("0.8.28", "0.8.28") is False

    def test_older_is_not_newer(self):
        assert upgrade_mod.is_newer("0.8.27", "0.8.28") is False

    def test_padding_short_tuple(self):
        # 0.9 should be newer than 0.8.28 (9 > 8), and 0.8 < 0.8.1.
        assert upgrade_mod.is_newer("0.9", "0.8.28") is True
        assert upgrade_mod.is_newer("0.8.1", "0.8") is True
        assert upgrade_mod.is_newer("0.8", "0.8.1") is False

    def test_final_beats_rc_of_same_numbers(self):
        assert upgrade_mod.is_newer("0.9.0", "0.9.0rc1") is True
        assert upgrade_mod.is_newer("0.9.0rc1", "0.9.0") is False

    def test_garbage_never_wins(self):
        assert upgrade_mod.is_newer("garbage", "0.8.28") is False
        assert upgrade_mod.is_newer("0.8.28", "garbage") is True


# ─── pure: the argv that would run ───

class TestUpgradeCommand:
    def test_pip(self):
        cmd = upgrade_mod.upgrade_command("pip", "0.8.29")
        assert cmd[:3] == [sys.executable, "-m", "pip"]
        assert cmd[3:] == ["install", "--upgrade", "mcptoon"]

    def test_uv(self):
        assert upgrade_mod.upgrade_command("uv", "0.8.29") == ["uv", "tool", "upgrade", "mcptoon"]

    def test_custom_from_env(self, monkeypatch):
        monkeypatch.setenv("MCPTOON_UPGRADE_CMD", "pipx upgrade mcptoon")
        assert upgrade_mod.upgrade_command("custom", "0.8.29") == ["pipx", "upgrade", "mcptoon"]

    def test_custom_without_env_falls_back_to_pip(self, monkeypatch):
        monkeypatch.delenv("MCPTOON_UPGRADE_CMD", raising=False)
        cmd = upgrade_mod.upgrade_command("custom", "0.8.29")
        assert "pip" in cmd and cmd[-1] == "mcptoon"


# ─── ties to reality ───

def test_current_version_matches_package():
    import mcptoon
    assert upgrade_mod.current_version() == mcptoon.__version__


# ─── run(): exit codes and messages ───

def _run(rest, fmt="auto"):
    """Run upgrade.run and return (exit_code, stdout).

    The cache file is redirected to a throwaway dir so a test's `--check` never
    leaves a record that could flip a later `footer-facts` test's output.
    """
    import io
    import contextlib
    import tempfile
    from pathlib import Path
    tmp = tempfile.mkdtemp(prefix="mcptoon-upg-cache-")
    old = upgrade_mod._cache_path
    upgrade_mod._cache_path = lambda: Path(tmp) / "upgrade-check.json"
    buf = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf):
            try:
                upgrade_mod.run(rest, fmt)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0
    finally:
        upgrade_mod._cache_path = old
    return code, buf.getvalue()


class TestRun:
    def test_up_to_date_returns_zero(self, monkeypatch):
        monkeypatch.setattr(upgrade_mod, "current_version", lambda: "0.8.28")
        monkeypatch.setattr(upgrade_mod, "fetch_latest", lambda **k: ("0.8.28", None))
        code, out = _run([])
        assert code == 0
        assert "up to date" in out

    def test_newer_with_check_exits_one(self, monkeypatch):
        monkeypatch.setattr(upgrade_mod, "current_version", lambda: "0.8.28")
        monkeypatch.setattr(upgrade_mod, "fetch_latest", lambda **k: ("0.8.29", None))
        code, out = _run(["--check"])
        assert code == 1
        assert "0.8.29" in out and "newer" in out

    def test_network_error_exits_two(self, monkeypatch):
        monkeypatch.setattr(upgrade_mod, "current_version", lambda: "0.8.28")
        monkeypatch.setattr(upgrade_mod, "fetch_latest", lambda **k: (None, "URLError: offline"))
        code, out = _run(["--check"])
        assert code == 2
        assert "could not check PyPI" in out

    def test_yes_upgrades_without_prompt(self, monkeypatch):
        monkeypatch.setattr(upgrade_mod, "current_version", lambda: "0.8.28")
        monkeypatch.setattr(upgrade_mod, "fetch_latest", lambda **k: ("0.8.29", None))
        monkeypatch.setattr(upgrade_mod, "_detect_installer", lambda: "pip")
        calls = {}

        class FakeCompleted:
            returncode = 0

        def fake_run(cmd, check=False):
            calls["cmd"] = cmd
            return FakeCompleted()

        monkeypatch.setattr(upgrade_mod.subprocess, "run", fake_run)
        code, out = _run(["--yes"])
        assert code == 0
        assert calls["cmd"][-1] == "mcptoon"
        assert "upgraded" in out

    def test_yes_failure_exits_one(self, monkeypatch):
        monkeypatch.setattr(upgrade_mod, "current_version", lambda: "0.8.28")
        monkeypatch.setattr(upgrade_mod, "fetch_latest", lambda **k: ("0.8.29", None))
        monkeypatch.setattr(upgrade_mod, "_detect_installer", lambda: "pip")

        class FakeCompleted:
            returncode = 1

        monkeypatch.setattr(upgrade_mod.subprocess, "run", lambda cmd, check=False: FakeCompleted())
        code, out = _run(["--yes"])
        assert code == 1
        assert "unchanged" in out

    def test_declining_prompt_changes_nothing(self, monkeypatch):
        monkeypatch.setattr(upgrade_mod, "current_version", lambda: "0.8.28")
        monkeypatch.setattr(upgrade_mod, "fetch_latest", lambda **k: ("0.8.29", None))
        monkeypatch.setattr(upgrade_mod, "_detect_installer", lambda: "pip")
        monkeypatch.setattr("builtins.input", lambda *a: "n")

        def boom(cmd, check=False):
            raise AssertionError("must not run the upgrade after a declined prompt")

        monkeypatch.setattr(upgrade_mod.subprocess, "run", boom)
        code, out = _run([])
        assert code == 0
        assert "cancelled" in out

    def test_json_output_is_parseable(self, monkeypatch):
        monkeypatch.setattr(upgrade_mod, "current_version", lambda: "0.8.28")
        monkeypatch.setattr(upgrade_mod, "fetch_latest", lambda **k: ("0.8.29", None))
        code, out = _run(["--check"], fmt="json")
        assert code == 1
        data = json.loads(out)
        assert data["installed"] == "0.8.28"
        assert data["latest"] == "0.8.29"
        assert data["newer_available"] is True


# ─── footer cache: hint + throttled refresh ───

class TestFooterCache:
    def _cache(self, tmp_path, monkeypatch, **rec):
        p = tmp_path / "upgrade-check.json"
        monkeypatch.setattr(upgrade_mod, "_cache_path", lambda: p)
        return p

    def test_hint_shown_when_newer_and_fresh(self, tmp_path, monkeypatch):
        p = self._cache(tmp_path, monkeypatch)
        import time as _t
        p.write_text(json.dumps({
            "installed": "0.8.28", "latest": "0.8.29",
            "error": None, "checked_at": _t.time(),
        }), encoding="utf-8")
        monkeypatch.setattr(upgrade_mod, "current_version", lambda: "0.8.28")
        hint = upgrade_mod.footer_hint()
        assert hint and "0.8.29" in hint

    def test_no_hint_when_up_to_date(self, tmp_path, monkeypatch):
        p = self._cache(tmp_path, monkeypatch)
        import time as _t
        p.write_text(json.dumps({
            "installed": "0.8.28", "latest": "0.8.28",
            "error": None, "checked_at": _t.time(),
        }), encoding="utf-8")
        monkeypatch.setattr(upgrade_mod, "current_version", lambda: "0.8.28")
        assert upgrade_mod.footer_hint() is None

    def test_no_hint_when_stale(self, tmp_path, monkeypatch):
        p = self._cache(tmp_path, monkeypatch)
        import time as _t
        p.write_text(json.dumps({
            "installed": "0.8.28", "latest": "0.9.0",
            "error": None, "checked_at": _t.time() - 100 * 3600,
        }), encoding="utf-8")
        monkeypatch.setattr(upgrade_mod, "current_version", lambda: "0.8.28")
        assert upgrade_mod.footer_hint() is None

    def test_no_hint_when_errored(self, tmp_path, monkeypatch):
        p = self._cache(tmp_path, monkeypatch)
        import time as _t
        p.write_text(json.dumps({
            "installed": "0.8.28", "latest": None,
            "error": "URLError", "checked_at": _t.time(),
        }), encoding="utf-8")
        assert upgrade_mod.footer_hint() is None

    def test_no_hint_after_we_upgraded(self, tmp_path, monkeypatch):
        """Cache recorded the OLD install; after upgrading it must not nag."""
        p = self._cache(tmp_path, monkeypatch)
        import time as _t
        p.write_text(json.dumps({
            "installed": "0.8.27", "latest": "0.8.28",
            "error": None, "checked_at": _t.time(),
        }), encoding="utf-8")
        monkeypatch.setattr(upgrade_mod, "current_version", lambda: "0.8.28")
        assert upgrade_mod.footer_hint() is None

    def test_no_cache_no_hint(self, tmp_path, monkeypatch):
        self._cache(tmp_path, monkeypatch)  # path set, file absent
        assert upgrade_mod.footer_hint() is None

    def test_refresh_is_skipped_under_pytest(self, monkeypatch):
        """maybe_refresh_cache must never spawn a child during the test suite."""
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")

        def boom(*a, **k):
            raise AssertionError("must not spawn a background check under pytest")

        monkeypatch.setattr(upgrade_mod.subprocess, "Popen", boom)
        upgrade_mod.maybe_refresh_cache()  # no raise

    def test_refresh_throttled_when_fresh(self, tmp_path, monkeypatch):
        p = self._cache(tmp_path, monkeypatch)
        import time as _t
        p.write_text(json.dumps({
            "installed": "0.8.28", "latest": "0.8.28",
            "error": None, "checked_at": _t.time(),
        }), encoding="utf-8")
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

        def boom(*a, **k):
            raise AssertionError("fresh cache ⇒ no refresh spawn")

        monkeypatch.setattr(upgrade_mod.subprocess, "Popen", boom)
        upgrade_mod.maybe_refresh_cache()  # no raise

    def test_refresh_spawns_when_stale(self, tmp_path, monkeypatch):
        p = self._cache(tmp_path, monkeypatch)
        p.write_text(json.dumps({
            "installed": "0.8.28", "latest": "0.8.28",
            "error": None, "checked_at": 0,
        }), encoding="utf-8")
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        seen = {}

        class P:
            pass

        def fake_popen(cmd, **kw):
            seen["cmd"] = cmd
            return P()

        monkeypatch.setattr(upgrade_mod.subprocess, "Popen", fake_popen)
        upgrade_mod.maybe_refresh_cache()
        assert seen["cmd"][:4] == [sys.executable, "-m", "mcptoon", "upgrade"]
        assert "--check" in seen["cmd"] and "--quiet" in seen["cmd"]
