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

"""The first-run guidance must recommend a server that actually lists tools.

Reported 2026-10-17: the 30-second README step 4 taught
`mcptoon call everything echo ...`, but `everything` is only added by
`mcptoon demo --keep` — a clean machine gets `Server 'everything' not
configured`, exit 1. And `quickstart` recommended installing `everything`, which
DEVELOPERS.md itself notes lists **zero tools** in a minimal Python image. Both
now point at the built-in zero-dependency `demo-server`, whose `echo_message`
tool is real. These pin the guidance and that the documented call actually works.
"""
import io
from contextlib import redirect_stdout
from pathlib import Path

from mcptoon import cli
from mcptoon import demo_server

REPO = Path(__file__).resolve().parents[1]


def test_readme_step4_does_not_require_a_server_quickstart_never_adds():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "mcptoon call everything echo" not in readme, \
        "README still teaches `everything`, which quickstart never adds"
    assert "mcptoon add demo --stdio python -m mcptoon demo-server" in readme
    assert "mcptoon call demo echo_message" in readme


def test_zh_readme_matches_the_english_step():
    readme = (REPO / "README.zh-CN.md").read_text(encoding="utf-8")
    assert "mcptoon call everything echo" not in readme
    assert "mcptoon call demo echo_message" in readme


def test_quickstart_points_at_the_builtin_demo_server(monkeypatch, tmp_path):
    """The empty-machine guidance names demo-server first, everything only as an aside."""
    from mcptoon import config as cfg
    from mcptoon import discover

    monkeypatch.setattr(cfg, "_settings_file", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "list_servers", lambda: [])
    monkeypatch.setattr(discover, "auto_discover", lambda *a, **k: discover.DiscoveryResult())
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            cli._cmd_quickstart([], "auto")
        except SystemExit:
            pass
    out = buf.getvalue()
    assert "mcptoon add demo --stdio python -m mcptoon demo-server" in out
    # The zero-tool reference server may be mentioned, but not as the primary step.
    demo_pos = out.find("add demo")
    everything_pos = out.find("server-everything")
    assert demo_pos != -1
    assert everything_pos == -1 or everything_pos > demo_pos


def test_the_documented_tool_exists_in_the_demo_server():
    names = [t.get("name") for t in demo_server.TOOLS]
    assert "echo_message" in names
