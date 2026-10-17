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

"""One rounding rule for every savings percentage — floored, never a fake 100%.

Reported 2026-10-17: `mcptoon status` printed 640,834 → 2,499 tokens as
"省 100%". The true figure is 99.61%; `{gw_pct:.0f}` rounded it up. That is both
mathematically wrong (nothing is free) and inconsistent with the skills line on
the same screen, which floors to 99.99%. Four surfaces carried the same `:.0f`
bug: the footer tools line, `status`, `welcome` and `report`. `bench` had the
mirror-image bug — it rounded to 3 decimals then formatted `:.1f`, so its skills
pointer printed 100.0% while `footer-facts` said 99.99%. `display_pct` is now the
single whole-number rule; these pin it and the surfaces that use it.
"""
import io
from contextlib import redirect_stdout

import pytest

from mcptoon import footer as footer_mod


def test_display_pct_floors_not_rounds():
    # 99.61% saved must read "99", never "100".
    assert footer_mod.display_pct(640_834 - 2_499, 640_834) == "99"
    # A tiny pointer against a huge catalog: 99.9962% -> "99", not "100".
    assert footer_mod.display_pct(1_030_589, 1_030_628) == "99"
    # Only a genuine total (never happens for the gateway) would read 100.
    assert footer_mod.display_pct(100, 100) == "100"
    # Nothing to divide -> empty string (caller omits the figure).
    assert footer_mod.display_pct(5, 0) == ""
    assert footer_mod.display_pct(0, 100) == ""


def _facts_with_gateway():
    return {
        "servers": 1, "tools": 209, "tokens_full": 640_834,
        "tokens_slim": 56_504, "tokens_saved": 584_330, "savings_pct": 91.2,
        "gateway_tokens": 2_499, "gateway_saved": 638_335, "gateway_pct": 99.6,
        "token_caliber": "tiktoken cl100k_base", "calls_recorded": 0,
    }


@pytest.mark.parametrize("lng", ["en", "zh"])
def test_footer_line_never_claims_100(lng):
    out = footer_mod.line(_facts_with_gateway(), lng)
    assert "100%" not in out, out
    assert ("99%" in out) or ("99%" in out), out


def test_status_screen_never_claims_100(monkeypatch, tmp_path):
    from mcptoon import cli
    from mcptoon import bench as bench_mod
    from mcptoon import config as cfg
    from mcptoon import manifest as manifest_mod

    monkeypatch.setattr(manifest_mod, "get_manifest", lambda use_cache=True: {
        "srv": [{"name": "t", "description": "d" * 200}],
    })
    monkeypatch.setattr(cfg, "_settings_file", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "list_servers", lambda: ["srv"])
    monkeypatch.setattr(cli, "_gateway_wired_in", lambda: False)
    monkeypatch.setattr(bench_mod, "_tokenizer",
                        lambda: (lambda s: len(s), "tiktoken cl100k_base", True))
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._cmd_status([], fmt="human")
    out = buf.getvalue()
    assert "100%" not in out, out
