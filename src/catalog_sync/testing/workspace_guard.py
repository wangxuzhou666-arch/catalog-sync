"""Keeps the test run away from the user's real default workspace.

Two layers, because either alone has a hole:

* An autouse fixture (a conftest.py) points ``CATALOG_SYNC_HOME`` at a fresh directory for each
  test, so a command that forgets ``--workspace`` still lands in a throw-away place.
* ``RealWorkspaceGuard`` is the net under it: it fingerprints the real default workspace when the
  session starts and fails the run if anything there was created, changed or removed by the end.
  A test that clears the variable, or a code path that resolves the default another way, still
  cannot leave data in the user's own cache unnoticed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from catalog_sync.config import DEFAULT_HOME

# rel path -> (mtime_ns, size). None means the directory does not exist at all.
Fingerprint = dict[str, tuple[int, int]] | None


def real_default_workspace() -> Path:
    """Where a run with no flag, no variable and no config would put its state."""
    return DEFAULT_HOME.expanduser()


def fingerprint(root: Path) -> Fingerprint:
    if not os.path.lexists(root):
        return None
    entries: dict[str, tuple[int, int]] = {}
    for folder, subfolders, files in os.walk(root):
        for name in [*subfolders, *files]:
            path = Path(folder, name)
            stat = path.lstat()
            entries[str(path.relative_to(root))] = (stat.st_mtime_ns, stat.st_size)
    root_stat = root.lstat()
    entries["."] = (root_stat.st_mtime_ns, 0)
    return entries


def changes(before: Fingerprint, after: Fingerprint) -> list[str]:
    """What differs between two fingerprints, one short line each."""
    if before == after:
        return []
    if before is None:
        return [f"created ({len(after or {})} entries)"]
    if after is None:
        return ["removed"]
    lines = [f"created: {name}" for name in sorted(after.keys() - before.keys())]
    lines += [f"removed: {name}" for name in sorted(before.keys() - after.keys())]
    kept = sorted(before.keys() & after.keys())
    return [*lines, *(f"modified: {name}" for name in kept if before[name] != after[name])]


class RealWorkspaceGuard:
    """A pytest plugin: fail the session if the real default workspace changed during it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._before: Fingerprint = None

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        del session
        self._before = fingerprint(self.root)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        del exitstatus
        found = changes(self._before, fingerprint(self.root))
        if not found:
            return
        shown = "\n".join(f"  {line}" for line in found[:20])
        session.config.get_terminal_writer().line(
            f"\nFAILED: the test run touched the real default workspace {self.root}\n{shown}\n"
            "A test or command ran without an isolated workspace "
            "(see catalog_sync.testing.workspace_guard).",
            red=True,
            bold=True,
        )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
