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

"""A troubleshooting page must exist, be linked, and cover the errors we ship.

Reported 2026-10-17: `mcptoon skills sync` failed with "pass --allow-tracked-deletes
to proceed" but that flag appeared in zero of README / DEVELOPERS / CONTRIBUTING,
and there was no FAQ page at all. `docs/faq.md` now answers the common errors, and
the skills usage text documents the flag. These pin that both stay true.
"""
from pathlib import Path

from mcptoon import skills as skills_mod

REPO = Path(__file__).resolve().parents[1]


def test_faq_page_exists_and_readme_links_it():
    faq = REPO / "docs" / "faq.md"
    assert faq.is_file(), "docs/faq.md is missing"
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "docs/faq.md" in readme, "README does not link the FAQ page"


def test_faq_covers_the_reported_errors():
    text = (REPO / "docs" / "faq.md").read_text(encoding="utf-8")
    for needle in (
        "not configured",             # the everything / step-4 error
        "--allow-tracked-deletes",    # the skills sync gate
        "unknown command",            # exit 1
        "unknown option",             # exit 2
        "skills index",               # stale skills count
    ):
        assert needle in text, f"FAQ does not cover {needle!r}"


def test_skills_usage_documents_the_tracked_deletes_flag():
    assert "--allow-tracked-deletes" in skills_mod._skills_usage()
