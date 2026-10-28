"""The tool that runs a README's quickstart, and the README's own quickstart block (R.7).

The extraction is the part that can silently go wrong (a block nobody runs looks exactly like a
block that passes), so it gets rejected inputs, accepted inputs and a switched-off variant.
"""

from __future__ import annotations

import shlex
import textwrap
from pathlib import Path

import pytest

import run_readme
from catalog_sync import cli
from run_readme import NoRunnableBlock, extract_commands, main
from tests.conftest import REPO_ROOT

README = REPO_ROOT / "docs" / "README.md"
README_ZH = REPO_ROOT / "docs" / "README.zh-CN.md"


def readme(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


BLOCK = readme(
    """
    Intro text.

    <!-- ci:run -->
    ```console
    $ echo one
    some output the command would print
    $ echo two \\
          --flag
    ```
    """
)


# --- extraction -----------------------------------------------------------------------------------


def test_commands_are_taken_in_order_and_output_lines_are_skipped() -> None:
    assert extract_commands(BLOCK) == ["echo one", "echo two --flag"]


def test_a_blank_line_between_the_marker_and_the_fence_is_fine() -> None:
    text = readme("<!-- ci:run -->\n\n```console\n$ echo a\n```\n")
    assert extract_commands(text) == ["echo a"]


def test_every_marked_block_counts_and_unmarked_blocks_do_not() -> None:
    text = readme(
        """
        ```console
        $ echo not marked
        ```
        <!-- ci:run -->
        ```console
        $ echo first
        ```
        <!-- ci:run -->
        ```sh
        $ echo second
        ```
        """
    )
    assert extract_commands(text) == ["echo first", "echo second"]


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        ("```console\n$ echo a\n```\n", "no <!-- ci:run --> marker"),
        ("<!-- ci:run -->\nsome prose\n```console\n$ echo a\n```\n", "not followed by a fenced"),
        ("<!-- ci:run -->\n", "not followed by a fenced"),
        ("<!-- ci:run -->\n```console\n$ echo a\n", "never closed"),
        ("<!-- ci:run -->\n```console\nnothing to run\n```\n", "has no `$ ` command"),
        ("<!-- ci:run -->\n```console\n$ echo a \\\n```\n", "ends in a backslash"),
    ],
)
def test_a_readme_without_a_runnable_block_is_refused(text: str, complaint: str) -> None:
    with pytest.raises(NoRunnableBlock, match=complaint.replace("$", r"\$").replace("`", ".")):
        extract_commands(text)


# --- running --------------------------------------------------------------------------------------


def make_repo(tmp_path: Path, readme_text: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text(readme_text, encoding="utf-8")
    (repo / "data.txt").write_text("payload\n", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "ignored.txt").write_text("x", encoding="utf-8")
    return repo


def run(repo: Path, tmp_path: Path, *extra: str) -> int:
    return main(
        [
            str(repo / "README.md"),
            "--repo",
            str(repo),
            "--scratch",
            str(tmp_path / "scratch"),
            *extra,
        ]
    )


def test_the_commands_run_in_a_copy_with_their_own_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(
        tmp_path,
        readme(
            "<!-- ci:run -->\n```console\n$ cp data.txt out.txt\n"
            '$ echo "$CATALOG_SYNC_HOME" > home.txt\n```\n'
        ),
    )
    assert run(repo, tmp_path, "--expect", "out.txt") == 0
    scratch = tmp_path / "scratch"
    assert (scratch / "out.txt").read_text(encoding="utf-8") == "payload\n"
    assert not (repo / "out.txt").exists(), "nothing is written next to the sources"
    assert (scratch / "home.txt").read_text(encoding="utf-8").strip() == str(
        scratch / ".catalog-sync-home"
    )
    assert not (scratch / ".venv").exists(), "caches and environments are not copied"
    assert "2 command(s) ran" in capsys.readouterr().out


def test_a_workspace_set_in_the_callers_shell_never_reaches_the_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demo's sinks write to `{workspace}/out`: if the caller's own CATALOG_SYNC_HOME won, a
    demo run on a developer's machine would fill their real workspace with the demo's files."""
    real_home = tmp_path / "real-workspace"
    monkeypatch.setenv("CATALOG_SYNC_HOME", str(real_home))
    repo = make_repo(
        tmp_path,
        readme(
            "<!-- ci:run -->\n```console\n"
            '$ echo "$CATALOG_SYNC_HOME" > home.txt\n'
            '$ mkdir -p "$CATALOG_SYNC_HOME/out"\n'
            '$ echo demo > "$CATALOG_SYNC_HOME/out/x.db"\n```\n'
        ),
    )
    assert run(repo, tmp_path) == 0
    scratch = tmp_path / "scratch"
    assert (scratch / "home.txt").read_text(encoding="utf-8").strip() == str(
        scratch / ".catalog-sync-home"
    )
    assert (scratch / ".catalog-sync-home" / "out" / "x.db").is_file()
    assert not real_home.exists(), "the caller's workspace was not touched"


@pytest.mark.parametrize("path", [README, README_ZH], ids=lambda p: p.name)
def test_no_quickstart_command_can_leave_the_isolated_workspace(path: Path) -> None:
    """`--workspace` beats the environment variable, and a command that sets the variable itself or
    reaches for the home directory would write outside the scratch copy."""
    escapes = ("--workspace", "CATALOG_SYNC_HOME", "$HOME", "~/", Path.home().as_posix())
    for command in quickstart(path):
        assert not any(escape in command for escape in escapes), command


def test_a_failing_command_stops_the_run_and_fails_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(
        tmp_path,
        readme(
            "<!-- ci:run -->\n```console\n$ echo before > a.txt\n$ false\n"
            "$ echo after > b.txt\n```\n"
        ),
    )
    assert run(repo, tmp_path) == 1
    scratch = tmp_path / "scratch"
    assert (scratch / "a.txt").exists() and not (scratch / "b.txt").exists()
    assert "failed with exit code 1" in capsys.readouterr().err


def test_a_command_that_fails_in_the_middle_of_a_pipeline_line_still_fails_the_run(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path, readme("<!-- ci:run -->\n```console\n$ false && echo hidden\n```\n"))
    assert run(repo, tmp_path) == 1


def test_a_missing_expected_file_fails_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path, readme("<!-- ci:run -->\n```console\n$ echo hi\n```\n"))
    assert run(repo, tmp_path, "--expect", "out/catalog.db") == 1
    assert "did not produce out/catalog.db" in capsys.readouterr().err


def test_a_readme_with_no_block_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path, "# nothing runnable\n")
    assert run(repo, tmp_path) == 2
    assert "no <!-- ci:run --> marker" in capsys.readouterr().err
    assert not (tmp_path / "scratch").exists(), "nothing is copied for a README that cannot run"


def test_a_scratch_directory_with_something_in_it_is_refused(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, readme("<!-- ci:run -->\n```console\n$ echo hi\n```\n"))
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "leftover").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not empty"):
        run(repo, tmp_path)


def test_without_the_failure_check_the_failing_readme_would_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the test above: the failure is detected by the exit-code check."""
    repo = make_repo(tmp_path, readme("<!-- ci:run -->\n```console\n$ false\n```\n"))
    real = run_readme.subprocess.run

    def forgiving(*args: object, **kwargs: object) -> object:
        result = real(*args, **kwargs)  # type: ignore[call-overload]
        result.returncode = 0
        return result

    monkeypatch.setattr(run_readme.subprocess, "run", forgiving)
    assert run(repo, tmp_path) == 0


# --- the real README ------------------------------------------------------------------------------


def quickstart(path: Path) -> list[str]:
    return extract_commands(path.read_text(encoding="utf-8"))


def test_the_readme_has_a_runnable_quickstart_that_ends_in_a_pipeline_run() -> None:
    commands = quickstart(README)
    assert len(commands) >= 4
    assert any(command.startswith("uv run catalog-sync run ") for command in commands)


CATALOG_SYNC_COMMANDS = [c for c in quickstart(README) if c.startswith("uv run catalog-sync ")]


@pytest.mark.parametrize("command", CATALOG_SYNC_COMMANDS)
def test_every_catalog_sync_command_of_the_quickstart_is_accepted_by_the_parser(
    command: str,
) -> None:
    parsed = cli.build_parser().parse_args(shlex.split(command)[3:])
    assert callable(parsed.run_command)


def test_the_chinese_readme_runs_the_very_same_commands() -> None:
    assert quickstart(README_ZH) == quickstart(README)
