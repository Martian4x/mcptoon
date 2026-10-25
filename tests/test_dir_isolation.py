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

"""`mcptoon --dir X` must keep *every* write inside X.

Reported 2026-10-17: `--dir X` moved config and the skills index into X, but the
schema cache still landed in the real `~/.cache/mcptoon`, because
`cache._cache_dir()` had bound `config.CACHE_DIR` at import and `apply_home()`
only rebound `config.CACHE_DIR`. So an "isolated" run leaked cache files out of
X. `cache._cache_dir()` now reads `config.CACHE_DIR` live; these pin the whole
chain after `apply_home`.
"""
from pathlib import Path

from mcptoon import cache
from mcptoon import config as cfg


def test_cache_dir_follows_apply_home(tmp_path):
    cfg.apply_home(tmp_path)
    got = cache._cache_dir()
    assert str(got).startswith(str(tmp_path)), \
        f"cache._cache_dir() escaped --dir: {got} not under {tmp_path}"
    assert str(cache._server_file("demo")).startswith(str(tmp_path))
    assert str(cache._legacy_file()).startswith(str(tmp_path))


def test_explicit_cache_dir_env_wins_without_apply_home(tmp_path, monkeypatch):
    # Without `--dir`, a caller that sets MCPTOON_CACHE_DIR directly keeps control
    # of the root (tests, bench, CI). `apply_home` deliberately overrides the env
    # so `--dir` isolation always wins — see the next test.
    monkeypatch.setenv("MCPTOON_CACHE_DIR", str(tmp_path / "explicit"))
    assert cache._cache_dir() == tmp_path / "explicit"


def test_apply_home_pins_the_cache_env(tmp_path, monkeypatch):
    monkeypatch.delenv("MCPTOON_CACHE_DIR", raising=False)
    cfg.apply_home(tmp_path)
    assert Path(__import__("os").environ["MCPTOON_CACHE_DIR"]).resolve() == \
        (tmp_path / "cache").resolve()
