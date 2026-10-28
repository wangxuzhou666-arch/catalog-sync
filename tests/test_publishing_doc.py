"""docs/publishing.md keeps up with the checks it tells the maintainer to run before a release."""

from __future__ import annotations

import tomllib

import acceptance_lab
import acceptance_release
import check_dist
from tests.conftest import REPO_ROOT

TEXT = (REPO_ROOT / "docs" / "publishing.md").read_text(encoding="utf-8")
LOCAL_STATE_DIRECTORY = ".claude"  # documents do not name the tool; the doc describes it instead


def test_the_checklist_names_the_tool_and_every_release_check_of_the_harness() -> None:
    assert "tools/check_dist.py" in TEXT
    registered = {s.id for s in acceptance_lab.registered()}
    for check in ("dist-check", "licenses", "image"):
        assert check in registered, f"{check} is no longer registered: update the document"
        assert f"`{check}`" in TEXT


def test_every_file_and_directory_the_tool_refuses_is_in_the_never_ship_list() -> None:
    directories = [*check_dist.NEVER_SHIP_DIRS, *check_dist.NEVER_SHIP_ROOT_DIRS]
    missing = [
        f"`{name}/`"
        for name in directories
        if name != LOCAL_STATE_DIRECTORY and f"`{name}/`" not in TEXT
    ]
    missing += [f"`{glob}`" for glob in check_dist.NEVER_SHIP_FILES if f"`{glob}`" not in TEXT]
    assert missing == [], f"docs/publishing.md does not list: {missing}"
    assert "`.env`" in TEXT and "`.env.example`" in TEXT


def test_every_reviewed_copyleft_exception_is_named() -> None:
    for name in acceptance_release.REVIEWED_COPYLEFT:
        assert f"`{name}`" in TEXT
    assert "`REVIEWED_COPYLEFT`" in TEXT


def test_the_sdist_exclusions_the_document_states_are_the_ones_in_pyproject() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    assert sorted(excluded) == ["/.claude", "/.pre-commit-config.yaml", "/connectors"]
    for name in ("/connectors", "/.pre-commit-config.yaml"):
        assert f"`{name}`" in TEXT
