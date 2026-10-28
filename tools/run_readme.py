"""Run the commands of a README's quickstart exactly as written, in a scratch copy of the repo.

A quickstart that nobody runs rots: a renamed option, a missing step. Mark the fenced block that
holds the commands with an HTML comment on the line above it::

    <!-- ci:run -->
    ```console
    $ uv sync
    $ uv run catalog-sync run demo
    ```

and this tool runs every ``$ `` line of it, in order, with ``bash -e``, from a copy of the
repository's files made in ``--scratch`` (so nothing is written next to your sources). A line
ending in a backslash continues on the next line; lines that do not start with ``$ `` are output
samples and are skipped. The workspace is moved into the scratch directory with
``CATALOG_SYNC_HOME`` so that the run never touches the real one; the commands are not altered.

Exit codes: 0 every command succeeded (and every ``--expect`` file exists afterwards), 1 a command
failed or an expected file is missing, 2 the README has no runnable block.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

MARKER = "<!-- ci:run -->"
PROMPT = "$ "
_SKIPPED_DIRECTORIES = frozenset({".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"})


class NoRunnableBlock(Exception):
    """The README does not contain a marked block of commands."""


def extract_commands(readme_text: str) -> list[str]:
    """The commands of every block that follows a marker, in the order they appear."""
    lines = readme_text.splitlines()
    commands: list[str] = []
    found = False
    for index, line in enumerate(lines):
        if line.strip() != MARKER:
            continue
        found = True
        commands += _block_after(lines, index + 1)
    if not found:
        raise NoRunnableBlock(f"no {MARKER} marker in the README")
    return commands


def _block_after(lines: Sequence[str], start: int) -> list[str]:
    position = start
    while position < len(lines) and not lines[position].strip():
        position += 1
    if position >= len(lines) or not lines[position].lstrip().startswith("```"):
        raise NoRunnableBlock(f"the {MARKER} marker is not followed by a fenced block")
    commands: list[str] = []
    parts: list[str] = []  # the lines of a command that continues over several
    is_continued = False
    for line in lines[position + 1 :]:
        if line.strip().startswith("```"):
            break
        if not is_continued and not line.startswith(PROMPT):
            continue  # an output sample
        text = line if is_continued else line[len(PROMPT) :]
        is_continued = text.rstrip().endswith("\\")
        parts.append(text.rstrip().removesuffix("\\").strip())
        if not is_continued:
            commands.append(" ".join(parts))
            parts = []
    else:
        raise NoRunnableBlock("the fenced block after a marker is never closed")
    if is_continued:
        raise NoRunnableBlock("the last command of the block ends in a backslash")
    if not commands:
        raise NoRunnableBlock(f"the block after a {MARKER} marker has no `$ ` command")
    return commands


def copy_repository(repo: Path, scratch: Path) -> None:
    """Copy the project files (tracked, or untracked but not ignored) into `scratch`."""
    scratch.mkdir(parents=True, exist_ok=True)
    if any(scratch.iterdir()):
        raise ValueError(f"the scratch directory {scratch.name} is not empty")
    for relative in _project_files(repo):
        target = scratch / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / relative, target)


def _project_files(repo: Path) -> list[Path]:
    if (repo / ".git").exists():
        listing = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            check=True,
        ).stdout.decode()
        return sorted(
            Path(name) for name in listing.split("\0") if name and (repo / name).is_file()
        )
    return sorted(
        path.relative_to(repo)
        for path in repo.rglob("*")
        if path.is_file() and not _SKIPPED_DIRECTORIES & set(path.relative_to(repo).parts)
    )


def run_commands(commands: Sequence[str], scratch: Path, home: Path) -> int:
    """Run each command in `scratch`; stop at the first that fails. Returns 0 or its exit code."""
    # A fresh shell would not have the caller's virtual environment; `uv` warns about a mismatch.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env["CATALOG_SYNC_HOME"] = str(home)
    for command in commands:
        print(f"$ {command}", flush=True)
        result = subprocess.run(["bash", "-e", "-c", command], cwd=scratch, env=env, check=False)
        if result.returncode != 0:
            print(
                f"run_readme: the command above failed with exit code {result.returncode}",
                file=sys.stderr,
            )
            return result.returncode
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a README's marked quickstart in a scratch copy."
    )
    parser.add_argument("readme", type=Path)
    parser.add_argument("--repo", type=Path, default=Path(), help="repository root (default: .)")
    parser.add_argument("--scratch", required=True, type=Path, help="an empty or new directory")
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="PATH",
        help="a file the commands must have produced, relative to the scratch copy",
    )
    args = parser.parse_args(argv)
    try:
        commands = extract_commands(args.readme.read_text(encoding="utf-8"))
    except NoRunnableBlock as exc:
        print(f"run_readme: {exc}", file=sys.stderr)
        return 2
    copy_repository(args.repo, args.scratch)
    if run_commands(commands, args.scratch, args.scratch / ".catalog-sync-home") != 0:
        return 1
    missing = [path for path in args.expect if not (args.scratch / path).exists()]
    if missing:
        print(f"run_readme: the commands did not produce {', '.join(missing)}", file=sys.stderr)
        return 1
    print(f"run_readme: {len(commands)} command(s) ran")
    return 0


if __name__ == "__main__":
    sys.exit(main())
