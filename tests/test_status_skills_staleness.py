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

"""`status` must flag a skills index that lags the live catalog.

Reported 2026-10-17: `mcptoon status` showed 464 skills (the built index) while
`bench` / `footer-facts` showed 469 (a live scan), with no hint which was
current. `status` deliberately reads the index and does not walk the filesystem,
so the fix is to *say so* when the footer's cached live count is higher, and name
the refresh command. These drive `_cmd_status` and pin both the stale hint and
its absence when the counts agree.
"""
import io
from contextlib import redirect_stdout

from mcptoon import cli
from mcptoon import bench as bench_mod
from mcptoon import config as cfg
from mcptoon import manifest as manifest_mod
from mcptoon import skills as skills_mod


def _wire(monkeypatch, tmp_path, *, index_n, live_n):
    monkeypatch.setattr(manifest_mod, "get_manifest", lambda use_cache=True: {})
    monkeypatch.setattr(cfg, "_settings_file", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "list_servers", lambda: [])
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: False)
    monkeypatch.setattr(bench_mod, "_tokenizer",
                        lambda: (lambda s: len(s), "tiktoken cl100k_base", True))
    monkeypatch.setattr(skills_mod, "load_index",
                        lambda: {"skills": [{"slug": f"s{i}"} for i in range(index_n)]})
    monkeypatch.setattr(skills_mod, "_load_skill_token_cache",
                        lambda: {"figures": {"count": live_n}})


def _status_out():
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._cmd_status([], fmt="human")
    return buf.getvalue()


def test_stale_index_is_flagged_with_the_fix(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, index_n=464, live_n=469)
    out = _status_out()
    assert "464" in out
    assert "469" in out, out
    assert "may be stale" in out
    assert "mcptoon skills index" in out


def test_no_hint_when_counts_agree(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, index_n=469, live_n=469)
    out = _status_out()
    assert "469" in out
    assert "stale" not in out, out


def _status_json():
    import json
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._cmd_status([], fmt="json")
    return json.loads(buf.getvalue())


def test_json_exposes_skills_live_and_stale_flag(monkeypatch, tmp_path):
    # The human view warns "index may be stale"; a script reading --json needs the
    # same signal, or it trusts a stale count. index 464 < live 469 -> stale True.
    _wire(monkeypatch, tmp_path, index_n=464, live_n=469)
    data = _status_json()
    assert data["skills"] == 464
    assert data["skills_live"] == 469
    assert data["skills_stale"] is True


def test_json_stale_is_false_when_counts_agree(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, index_n=469, live_n=469)
    data = _status_json()
    assert data["skills_stale"] is False

