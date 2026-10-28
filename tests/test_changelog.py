"""The changelog keeps up with the version: its newest entry is the version in the package."""

from __future__ import annotations

import re

from catalog_sync import __version__
from tests.conftest import REPO_ROOT

CHANGELOG = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
ENTRY = re.compile(r"(?m)^## (?P<version>\S+) - (?P<date>\d{4}-\d{2}-\d{2})$")


def newest_entry() -> str:
    found = list(ENTRY.finditer(CHANGELOG))
    assert found, "the changelog has no `## <version> - <date>` entry"
    end = found[1].start() if len(found) > 1 else len(CHANGELOG)
    return CHANGELOG[found[0].start() : end]


def test_the_newest_entry_is_the_version_of_the_package() -> None:
    newest = ENTRY.search(CHANGELOG)
    assert newest is not None and newest["version"] == __version__


def test_the_newest_entry_links_the_documents_it_points_to() -> None:
    entry = newest_entry()
    links = re.findall(r"\]\((docs/[\w.-]+\.md)\)", entry)
    assert {"docs/acceptance.md", "docs/data-contract.md"} <= set(links)
    assert [link for link in links if not (REPO_ROOT / link).is_file()] == []


def test_the_known_limitations_are_the_sharp_edges_of_the_data_contract() -> None:
    contract = (REPO_ROOT / "docs" / "data-contract.md").read_text(encoding="utf-8")
    edges = contract.split("## 14. Known sharp edges", 1)[1]
    in_contract = len(re.findall(r"(?m)^- ", edges))
    listed = (
        newest_entry().split("### Known limitations", 1)[1].split("Outside the data contract", 1)[0]
    )
    assert in_contract == len(re.findall(r"(?m)^- ", listed)), "list the same edges in both places"


def test_the_entry_says_that_connectors_for_real_platforms_are_not_released() -> None:
    entry = newest_entry()
    assert "`connectors/`" in entry and "not released" in entry
