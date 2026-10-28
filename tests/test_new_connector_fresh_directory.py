"""The steps `new-connector` prints, run word for word in a directory that has nothing in it.

The other tests of the scaffold run the generated package with this checkout's own virtual
environment, which already has catalog-sync installed, so they could not notice that a *fresh*
directory's `uv sync` has no way to find catalog-sync while it is not on a package index. This one
leaves that environment behind: it takes the commands the tool prints (not a copy of them kept
here, so that the text and the behaviour cannot drift apart) and runs them in a shell, in a new
directory, with no virtual environment active. uv's cache is enough to run it without a network
when the dependencies were fetched before, and a CI runner fetches them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed"),
]

_TIMEOUT_S = 600
_TRAILING_COMMENT = re.compile(r"\s+#\s.*$")


def printed_steps(output: str) -> list[str]:
    """The indented lines under `next:`, without their trailing comments."""
    lines = output.splitlines()
    steps: list[str] = []
    for line in lines[lines.index("next:") + 1 :]:
        if not line.startswith("  "):
            break
        steps.append(_TRAILING_COMMENT.sub("", line.strip()))
    return steps


# CI sets UV_FROZEN so that `uv run` never re-resolves this repository's uv.lock. The generated
# project has no lock of its own: with UV_FROZEN=1 its `uv sync` stops at "Unable to find lockfile",
# which a developer's machine (the variable unset) never shows. So it must not be inherited here.
_NOT_FOR_A_FRESH_PROJECT = {
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "UV_FROZEN",
    "UV_LOCKED",
    "UV_PROJECT_ENVIRONMENT",  # else the generated project installs itself into this one's venv
}


def fresh_env() -> dict[str, str]:
    """No active environment: the generated project must build and lock its own."""
    kept = {k: v for k, v in os.environ.items() if k not in _NOT_FOR_A_FRESH_PROJECT}
    return {**kept, "PYTHONDONTWRITEBYTECODE": "1"}


def generate_with_the_cli(target: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    env = {**fresh_env(), "PYTHONPATH": str(REPO_ROOT / "src")}  # this checkout, whatever .pth does
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "catalog_sync",
            "new-connector",
            "acme",
            "--dir",
            str(target),
            *extra,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_printed_steps_work_in_a_fresh_directory(tmp_path: Path) -> None:
    target = tmp_path / "work" / "acme"
    generated = generate_with_the_cli(target)
    assert generated.returncode == 0, generated.stderr
    steps = printed_steps(generated.stdout)
    assert steps[0] == f"cd {target}"
    assert "uv sync && uv run pytest -q" in steps, f"the steps changed: {steps}"

    # One shell per step, as typing them one after the other would be, so that a step that fails
    # is seen failing (inside a single `bash -e` script, the left side of `&&` never stops it).
    working_dir = Path.cwd()
    for step in steps:
        if step.startswith("cd "):
            working_dir = Path(step.removeprefix("cd "))
            continue
        result = subprocess.run(
            ["bash", "-e", "-c", step],
            cwd=working_dir,
            env=fresh_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_S,
        )
        assert result.returncode == 0, (
            f"`{step}` failed in a fresh directory\n--- stdout\n{result.stdout}\n"
            f"--- stderr\n{result.stderr}"
        )
    assert (target / ".venv").is_dir(), "the steps must have built the package's own environment"
