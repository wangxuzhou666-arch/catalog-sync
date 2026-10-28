"""The test run must never touch the real default workspace (catalog_sync.testing.workspace_guard).

The guard is proven the hard way: a nested pytest session whose tests do touch the watched folder
has to fail, and one whose tests do not has to pass.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from catalog_sync import cli
from catalog_sync.config import DEFAULT_HOME, HOME_ENV_VAR
from catalog_sync.testing.workspace_guard import changes, fingerprint, real_default_workspace
from tests.conftest import MOCK_SNAPSHOT_DIR, REPO_ROOT

pytest_plugins = ["pytester"]


# --- the per-test home ----------------------------------------------------------------------------


def test_every_test_starts_with_a_workspace_home_of_its_own() -> None:
    # Reads the variable instead of asking for the fixture: this must hold for a test that never
    # mentions it, which is what makes it autouse.
    home = Path(os.environ[HOME_ENV_VAR])
    assert not home.exists(), "a fresh home does not exist yet, like a first run"
    assert home.parent.is_dir() and real_default_workspace() not in home.parents


def test_a_test_may_set_its_own_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "mine"))
    assert os.environ[HOME_ENV_VAR] == str(tmp_path / "mine")


def test_a_command_without_workspace_flag_lands_in_the_isolated_home() -> None:
    """The case the fixture exists for: no ``--workspace``, no config. Without the fixture this
    would create the user's real ``~/.local/share/catalog-sync``."""
    code = cli.main(["pull", "replay", "--option", f"path={MOCK_SNAPSHOT_DIR}", "--stage", "list"])
    assert code == 0
    assert (Path(os.environ[HOME_ENV_VAR]) / "state.sqlite").is_file()


def test_the_real_default_is_the_one_the_code_resolves() -> None:
    assert real_default_workspace() == DEFAULT_HOME.expanduser()


# --- fingerprint and changes ----------------------------------------------------------------------


def test_a_missing_folder_has_no_fingerprint_and_a_created_one_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    assert fingerprint(root) is None and changes(None, fingerprint(root)) == []
    root.mkdir()
    assert changes(None, fingerprint(root)) == ["created (1 entries)"]


def test_a_new_file_a_changed_file_and_a_removed_file_are_each_reported(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "cache").mkdir(parents=True)
    (root / "state.sqlite").write_text("one", encoding="utf-8")
    (root / "gone.txt").write_text("x", encoding="utf-8")
    before = fingerprint(root)
    assert changes(before, fingerprint(root)) == []

    (root / "state.sqlite").write_text("longer", encoding="utf-8")
    (root / "cache" / "new.json").write_text("{}", encoding="utf-8")
    (root / "gone.txt").unlink()
    found = changes(before, fingerprint(root))
    assert "created: cache/new.json" in found
    assert "removed: gone.txt" in found
    assert "modified: state.sqlite" in found


def test_removing_the_whole_folder_is_a_change(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    before = fingerprint(root)
    root.rmdir()
    assert changes(before, fingerprint(root)) == ["removed"]


# --- the session guard ----------------------------------------------------------------------------


def run_guarded_session(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, test_body: str, *, existing: bool
) -> pytest.RunResult:
    """A nested session that watches ``watched`` with the real guard class and runs one test."""
    watched = pytester.path / "watched"
    if existing:
        watched.mkdir()
        (watched / "state.sqlite").write_text("before", encoding="utf-8")
    pytester.makeconftest(
        f"""
        from pathlib import Path
        from catalog_sync.testing.workspace_guard import RealWorkspaceGuard

        WATCHED = Path({str(watched)!r})

        def pytest_configure(config):
            config.pluginmanager.register(RealWorkspaceGuard(WATCHED), "guard")
        """
    )
    pytester.makepyfile(
        test_inner=f"from pathlib import Path\nWATCHED = Path({str(watched)!r})\n"
        + textwrap.dedent(test_body)
    )
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    return pytester.runpytest_subprocess("-p", "no:cacheprovider")


def test_a_session_whose_tests_leave_the_folder_alone_passes(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_guarded_session(
        pytester, monkeypatch, "def test_idle():\n    assert True\n", existing=True
    )
    result.assert_outcomes(passed=1)
    assert result.ret == 0


def test_a_session_that_creates_the_folder_fails_even_though_every_test_passed(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = """
        def test_writes_where_it_should_not():
            WATCHED.mkdir()
            (WATCHED / "state.sqlite").write_text("x")
        """
    result = run_guarded_session(pytester, monkeypatch, body, existing=False)
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*touched the real default workspace*", "*created*"])


def test_a_session_that_modifies_a_file_in_the_folder_fails(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = """
        def test_overwrites():
            (WATCHED / "state.sqlite").write_text("after, and longer")
        """
    result = run_guarded_session(pytester, monkeypatch, body, existing=True)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*modified: state.sqlite*"])
