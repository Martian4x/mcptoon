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

"""Tests for `mcptoon serve --background` / `--status` / `--stop`.

The whole value of these flags is that a process outlives the shell, so the tests
do two kinds of thing:

  * pure state logic against the pid file (`_pid_alive`, `_read_pid`) — including
    the stale-file case a crash or reboot leaves behind;
  * a REAL detached process that `--stop` then kills, because a mocked stop that
    never signals anything proves nothing about whether the stop works.

The pid file lives under the temp STATE_DIR that conftest.py pins, so none of
this touches the developer's real ~/.mcptoon.
"""
import io
import contextlib
import os
import sys

import pytest

_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from mcptoon import serve  # noqa: E402


@pytest.fixture()
def clean_pidfile():
    """Make sure each test starts with no background pid file."""
    pid_file, log_file = serve._bg_paths()
    for p in (pid_file, log_file):
        try:
            p.unlink()
        except OSError:
            pass
    yield
    for p in (pid_file, log_file):
        try:
            p.unlink()
        except OSError:
            pass


def _fast_clock():
    """A monotonic stand-in for serve.time.time that jumps 1s per call.

    The startup wait loop is `while time.time() < deadline` with a 10s budget; a
    real clock would make the "never binds" test wait the full 10 seconds. This
    exhausts the budget in a handful of iterations so the test stays instant.
    """
    state = {"t": 0.0}

    def tick():
        state["t"] += 1.0
        return state["t"]

    return tick


def _capture(fn, *a, **k):
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            rv = fn(*a, **k)
            if isinstance(rv, int):
                code = rv
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 0
    return code, buf.getvalue()


# ─── pure: liveness and pid file ───

class TestPidAlive:
    def test_own_pid_is_alive(self):
        assert serve._pid_alive(os.getpid()) is True

    def test_absurd_pid_is_dead(self):
        assert serve._pid_alive(2_000_000_000) is False

    def test_nonpositive_is_dead(self):
        assert serve._pid_alive(0) is False
        assert serve._pid_alive(-5) is False


class TestReadPid:
    def test_missing_file_is_none(self, clean_pidfile):
        assert serve._read_pid() is None

    def test_live_pid_round_trips(self, clean_pidfile):
        pid_file, _ = serve._bg_paths()
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        assert serve._read_pid() == os.getpid()

    def test_stale_file_is_cleared(self, clean_pidfile):
        pid_file, _ = serve._bg_paths()
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("2000000000", encoding="utf-8")  # dead pid
        assert serve._read_pid() is None
        assert not pid_file.exists(), "a stale pid file must be removed"

    def test_garbage_file_is_cleared(self, clean_pidfile):
        pid_file, _ = serve._bg_paths()
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("not-a-pid", encoding="utf-8")
        assert serve._read_pid() is None

    def test_identity_match_is_trusted(self, clean_pidfile):
        import json
        pid_file, _ = serve._bg_paths()
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        rec = {"pid": os.getpid(), "created": serve._proc_create_time(os.getpid())}
        pid_file.write_text(json.dumps(rec), encoding="utf-8")
        assert serve._read_pid() == os.getpid()

    def test_identity_mismatch_is_not_trusted(self, clean_pidfile):
        """The safety net: same pid, different creation time ⇒ NOT ours, refuse.

        This is the regression for the bug that killed an unrelated process: a
        recycled pid must never be handed to --stop. The record is cleared and
        None returned, so nothing downstream can kill that pid.
        """
        import json
        pid_file, _ = serve._bg_paths()
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        rec = {"pid": os.getpid(), "created": 0}  # alive pid, bogus creation time
        pid_file.write_text(json.dumps(rec), encoding="utf-8")
        assert serve._read_pid() is None
        assert not pid_file.exists()


# ─── status / stop messages ───

class TestStatusStop:
    def test_status_when_not_running(self, clean_pidfile):
        code, out = _capture(serve._background_status)
        assert code == 0
        assert "not running" in out

    def test_status_when_running(self, clean_pidfile):
        import json
        pid_file, _ = serve._bg_paths()
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        rec = {"pid": os.getpid(), "created": serve._proc_create_time(os.getpid())}
        pid_file.write_text(json.dumps(rec), encoding="utf-8")
        code, out = _capture(serve._background_status)
        assert code == 0
        assert "running" in out and str(os.getpid()) in out

    def test_stop_when_not_running(self, clean_pidfile):
        code, out = _capture(serve._background_stop)
        assert code == 0
        assert "nothing to stop" in out

    def test_stop_refuses_on_identity_mismatch(self, clean_pidfile, monkeypatch):
        """With a mismatched-identity pid file, --stop must do nothing at all."""
        import json
        pid_file, _ = serve._bg_paths()
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(json.dumps({"pid": os.getpid(), "created": 0}),
                            encoding="utf-8")

        def boom(*a, **k):
            raise AssertionError("must not issue any kill for an unverified pid")

        monkeypatch.setattr(serve.subprocess, "run", boom)
        code, out = _capture(serve._background_stop)
        assert code == 0
        assert "nothing to stop" in out


# ─── stop: invokes the OS kill and clears the pid file ───

class TestStopMechanism:
    def test_stop_invokes_taskkill_and_clears_pidfile(self, clean_pidfile, monkeypatch):
        """A live pid must trigger the platform kill and remove the pid file.

        Deliberately mocked: force-killing a real child inside the test runner's
        Windows job aborts the job (exit 0xC000013A), so the actual detached
        lifecycle is demonstrated live rather than inside pytest. What is pinned
        here is the contract a user relies on — the kill is issued for *that* pid
        and the bookkeeping is cleaned up.
        """
        pid_file, _ = serve._bg_paths()
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("4321", encoding="utf-8")

        killed = {}

        class FakeCompleted:
            returncode = 0

        def fake_run(cmd, **kw):
            killed["cmd"] = cmd
            return FakeCompleted()

        def fake_kill(pid, sig):
            killed["kill"] = (pid, sig)

        monkeypatch.setattr(serve.subprocess, "run", fake_run)
        # POSIX stop uses os.kill; mock it so no real signal is sent. (Windows uses
        # subprocess.run taskkill, already mocked above.) _pid_alive is fully
        # faked below, so it never reaches os.kill either.
        if os.name != "nt":
            monkeypatch.setattr(serve.os, "kill", fake_kill)
        # Alive for the initial _read_pid, gone after the kill so the wait loop ends.
        calls = {"n": 0}

        def fake_alive(pid):
            calls["n"] += 1
            return calls["n"] == 1

        monkeypatch.setattr(serve, "_pid_alive", fake_alive)

        code, out = _capture(serve._background_stop)
        assert code == 0
        assert "stopped" in out
        assert not pid_file.exists()
        if os.name == "nt":
            assert "taskkill" in killed["cmd"][0].lower()
            assert "4321" in killed["cmd"]
        else:
            assert killed["kill"] == (4321, 15)


# ─── start ───

class TestBackgroundStart:
    def test_already_running_refuses(self, clean_pidfile):
        import json
        pid_file, _ = serve._bg_paths()
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        rec = {"pid": os.getpid(), "created": serve._proc_create_time(os.getpid())}
        pid_file.write_text(json.dumps(rec), encoding="utf-8")
        code, out = _capture(serve._background_start, ["--background", "--http"], ":8080")
        assert code == 1
        assert "already running" in out

    def test_start_spawns_and_writes_pid(self, clean_pidfile, monkeypatch):
        recorded = {}

        class FakeProc:
            pid = 4242

            def poll(self):
                return None  # still running after the startup wait → success

        def fake_popen(cmd, **kw):
            # On POSIX, `_write_pidfile` runs `_proc_create_time`, which itself calls
            # subprocess.run(["ps", ...]) → Popen. Only capture the *serve* spawn.
            if cmd[:2] == [sys.executable, "-m"] or "mcptoon" in cmd:
                recorded["cmd"] = cmd
                recorded["kw"] = kw
            return FakeProc()

        monkeypatch.setattr(serve.subprocess, "Popen", fake_popen)
        # First probe = pre-spawn "is the port free?" → False. Then "did we bind?" → True.
        calls = {"n": 0}

        def fake_probe(host, port, timeout=0.25):
            calls["n"] += 1
            return calls["n"] > 1

        monkeypatch.setattr(serve, "_tcp_probe", fake_probe)
        code, out = _capture(serve._background_start,
                             ["--background", "--http"], ":8080")
        assert code == 0
        # The child is the same serve invocation, minus the control flag.
        assert recorded["cmd"][:4] == [sys.executable, "-m", "mcptoon", "serve"]
        assert "--background" not in recorded["cmd"]
        assert "--http" in recorded["cmd"]
        pid_file, _ = serve._bg_paths()
        import json as _json
        rec = _json.loads(pid_file.read_text(encoding="utf-8"))
        assert rec["pid"] == 4242
        assert "created" in rec
        assert "started in the background" in out
        # Clean up the fake pid so later tests do not see it as "running".
        pid_file.unlink()

    def test_start_reports_when_child_dies_immediately(self, clean_pidfile, monkeypatch):
        """Port already taken ⇒ the child exits at once ⇒ honest failure, no pid file."""
        class DeadProc:
            pid = 4343
            returncode = 1

            def poll(self):
                return 1

        monkeypatch.setattr(serve.subprocess, "Popen", lambda cmd, **kw: DeadProc())
        monkeypatch.setattr(serve, "_tcp_probe", lambda host, port, timeout=0.25: False)
        code, out = _capture(serve._background_start,
                             ["--background", "--listen"], "127.0.0.1:9")
        assert code == 1
        assert "failed to start" in out
        pid_file, _ = serve._bg_paths()
        assert not pid_file.exists(), "a failed start must not leave a pid file behind"

    def test_start_reports_when_port_never_opens(self, clean_pidfile, monkeypatch):
        """Child stays alive but never binds (e.g. wrong port) ⇒ timeout ⇒ failure."""
        class AliveProc:
            pid = 4545

            def poll(self):
                return None

            def terminate(self):
                pass

        monkeypatch.setattr(serve.subprocess, "Popen", lambda cmd, **kw: AliveProc())
        monkeypatch.setattr(serve, "_tcp_probe", lambda host, port, timeout=0.25: False)
        monkeypatch.setattr(serve.time, "time", _fast_clock())
        code, out = _capture(serve._background_start,
                             ["--background", "--listen"], "127.0.0.1:9")
        assert code == 1
        assert "failed to start" in out
        pid_file, _ = serve._bg_paths()
        assert not pid_file.exists()

    def test_start_refuses_when_port_already_in_use(self, clean_pidfile, monkeypatch):
        """If the port is taken before we spawn, say so and do not spawn at all."""
        spawned = {"n": 0}

        def fake_popen(cmd, **kw):
            spawned["n"] += 1
            raise AssertionError("must not spawn when the port is already in use")

        monkeypatch.setattr(serve.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(serve, "_tcp_probe", lambda host, port, timeout=0.25: True)
        code, out = _capture(serve._background_start,
                             ["--background", "--listen"], "127.0.0.1:9")
        assert code == 1
        assert "already in use" in out
        assert spawned["n"] == 0
        pid_file, _ = serve._bg_paths()
        assert not pid_file.exists()


# ─── run_serve routing ───

class TestAutostart:
    def test_autostart_needs_http(self):
        code, out = _capture(serve._install_autostart, ["--install-autostart"], None)
        assert code == 2

    def test_autostart_command_strips_control_flags(self):
        cmd = serve._autostart_command(
            ["--background", "--install-autostart", "--listen", "127.0.0.1:9"], ":9")
        assert cmd[:4] == [sys.executable, "-m", "mcptoon", "serve"]
        assert "--background" not in cmd
        assert "--install-autostart" not in cmd
        assert "--listen" in cmd

    def test_install_autostart_windows_creates_task(self, monkeypatch, tmp_path):
        calls = {}

        class R:
            returncode = 0
            stdout = "SUCCESS"
            stderr = ""

        def fake_run(cmd, **kw):
            calls["cmd"] = cmd
            return R()

        monkeypatch.setattr(serve.os, "name", "nt")
        monkeypatch.setattr(serve.subprocess, "run", fake_run)
        monkeypatch.setattr(serve, "_bg_paths",
                            lambda: (tmp_path / "serve.pid", tmp_path / "serve.log"))
        code, out = _capture(serve._install_autostart,
                             ["--install-autostart", "--listen", "127.0.0.1:9"], "127.0.0.1:9")
        assert code == 0
        assert "autostart installed" in out
        assert "schtasks" in calls["cmd"].lower() and "ONLOGON" in calls["cmd"]
        # It must point the task at the space-free launcher, not an inline spaced cmd.
        launcher = tmp_path / "serve-autostart.cmd"
        assert launcher.exists()
        assert str(launcher) in calls["cmd"]
        assert "mcptoon" in launcher.read_text(encoding="utf-8")

    def test_install_autostart_reports_failure(self, monkeypatch, tmp_path):
        class R:
            returncode = 1
            stdout = ""
            stderr = "Access denied"

        monkeypatch.setattr(serve.os, "name", "nt")
        monkeypatch.setattr(serve.subprocess, "run", lambda cmd, **kw: R())
        monkeypatch.setattr(serve, "_bg_paths",
                            lambda: (tmp_path / "serve.pid", tmp_path / "serve.log"))
        code, out = _capture(serve._install_autostart,
                             ["--install-autostart", "--listen", "127.0.0.1:9"], "127.0.0.1:9")
        assert code == 1
        assert "could not create" in out


class TestRunServeRouting:
    def test_status_flag_routes_and_exits(self, clean_pidfile):
        code, out = _capture(serve.run_serve, ["--status"])
        assert code == 0
        assert "not running" in out

    def test_stop_flag_routes_and_exits(self, clean_pidfile):
        code, out = _capture(serve.run_serve, ["--stop"])
        assert code == 0
        assert "nothing to stop" in out

    def test_uninstall_autostart_routes(self, monkeypatch):
        monkeypatch.setattr(serve, "_uninstall_autostart", lambda: 0)
        code, _ = _capture(serve.run_serve, ["--uninstall-autostart"])
        assert code == 0

    def test_background_without_http_is_refused(self, clean_pidfile):
        code, out = _capture(serve.run_serve, ["--background"])
        # stdio + background has no reader — refuse with exit 2 (message on stderr).
        assert code == 2


class TestParseServeArgs:
    """`--http` used to ignore a following address: `serve --background --http :18099`
    silently kept :8080 and then failed "8080 is already in use", never mentioning the
    requested port (measured 0.8.29). It must now honour `--http <addr>` and
    `--http=<addr>`, while the bare `--http` shorthand stays :8080."""

    def test_http_takes_a_following_address(self):
        assert serve._parse_serve_args(["--background", "--http", ":18099"])[1] == ":18099"
        assert serve._parse_serve_args(["--http", "127.0.0.1:9000"])[1] == "127.0.0.1:9000"

    def test_bare_http_is_still_the_8080_shorthand(self):
        assert serve._parse_serve_args(["--http"])[1] == ":8080"
        # A following flag is not mistaken for an address.
        assert serve._parse_serve_args(["--http", "--auth", "x"])[1] == ":8080"

    def test_http_equals_form(self):
        assert serve._parse_serve_args(["--http=:1234"])[1] == ":1234"

    def test_listen_unchanged(self):
        assert serve._parse_serve_args(["--listen", ":9"])[1] == ":9"
        assert serve._parse_serve_args(["--listen=:9"])[1] == ":9"
