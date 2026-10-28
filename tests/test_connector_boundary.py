"""Nothing of a connector package ships with catalog-sync: wheel, sdist, image or entry points.

Connectors live under connectors/ as projects of their own. These tests pin the places where one
could leak back in, without naming any connector: the build configuration, the Dockerfile and its
ignore file, the root entry points, and the CI job that runs the connectors' own suites.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from tests.conftest import REPO_ROOT

PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
CI = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def copied_sources(dockerfile: str) -> list[str]:
    """What every COPY / ADD line takes from the build context (not from another stage)."""
    sources: list[str] = []
    for line in dockerfile.splitlines():
        words = line.split()
        if not words or words[0].upper() not in {"COPY", "ADD"}:
            continue
        arguments = [word for word in words[1:] if not word.startswith("--")]
        if any(word.startswith("--from") for word in words[1:]):
            continue
        sources += arguments[:-1]  # the last argument is the destination
    return sources


def test_the_scan_reads_the_sources_of_copy_lines_and_skips_other_stages() -> None:
    text = "COPY a.toml b.lock ./\nCOPY --from=builder /app/.venv /app/.venv\nCOPY src ./src\n"
    assert copied_sources(text) == ["a.toml", "b.lock", "src"]


def test_the_image_copies_nothing_from_a_connector_directory() -> None:
    sources = copied_sources(DOCKERFILE)
    assert "src" in sources, "the scan must really see the COPY lines"
    assert [s for s in sources if Path(s).parts[:1] == ("connectors",)] == []
    assert not any("connectors" in source for source in sources)


def test_the_build_context_leaves_connector_directories_out() -> None:
    patterns = [line.strip() for line in DOCKERIGNORE.splitlines()]
    assert "connectors" in patterns


def test_the_wheel_holds_the_core_package_only() -> None:
    assert PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/catalog_sync"
    ]


def test_the_sdist_leaves_connector_directories_and_local_tool_state_out() -> None:
    excluded = PYPROJECT["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    assert {"/connectors", "/.claude"} <= set(excluded)


def test_every_root_entry_point_resolves_inside_the_core_package() -> None:
    """A connector registers itself from its own package; the core wheel registers its own code."""
    groups = PYPROJECT["project"]["entry-points"]
    assert groups, "the scan must really see the entry points"
    outside = {
        f"{group}:{name}": target
        for group, entries in groups.items()
        for name, target in entries.items()
        if not target.startswith("catalog_sync.")
    }
    assert outside == {}


def test_the_ci_job_for_connectors_loops_over_the_directory_and_names_none() -> None:
    start = CI.index("\n  connectors:")
    job = CI[start : CI.index("\n  # ", start + 1)]  # up to the comment that opens the next job
    assert "connectors/*/" in job, "the connectors job does not loop over connectors/*/"
    named = re.findall(r"connectors/(?!\*)[\w.-]+", job)
    assert named == [], f"the connectors job names a connector: {named}"
    for step in ("uv sync", "uv run pytest", "uv run ruff check", "uv run mypy"):
        assert step in job, f"the connectors job does not run `{step}`"
