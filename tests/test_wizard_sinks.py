"""The sink step of ``init``: its file defaults land in the workspace, and `{workspace}/` is checked
by the resolver `load` uses, not by a copy of it.

Without this a person who pressed Enter at every question got `./out/...`, a folder in the
checkout (or in a folder a cloud service syncs), and a `{workspace}x/db` typed in the wizard was
accepted there and refused only when the first load ran.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from catalog_sync.config import Workspace
from catalog_sync.errors import ConfigError
from catalog_sync.sinks.kinds import SINK_KINDS
from catalog_sync.wizard.sinks import SinkChoice, choose_sinks
from tests.test_wizard_prompts import Terminal

FILE_SINKS = ("sqlite", "jsonl", "csv")


def run_step(
    terminal: Terminal, tmp_path: Path, answers: dict[str, object] | None = None
) -> list[SinkChoice]:
    prompter = terminal.prompter(answers, interactive=answers is None)
    return choose_sinks(
        prompter,
        entity="material",
        sensitive=[],
        base_dir=tmp_path / "config",
        workspace=Workspace(tmp_path / "home"),
    )


@pytest.mark.parametrize("kind", FILE_SINKS)
def test_a_file_sink_asked_with_its_default_writes_into_the_workspace(
    kind: str, tmp_path: Path
) -> None:
    # Nothing is answered but the kind, so every setting takes its declared default.
    answers = {"sinks": [{"kind": kind}]}
    [chosen] = run_step(Terminal(), tmp_path, answers)
    path = str(chosen.options["path"])
    assert path.startswith("{workspace}/out/"), f"{kind} defaults to {path!r}"
    assert not (tmp_path / "config").exists(), "asking creates nothing beside the config"


def test_every_file_sink_kind_declares_a_workspace_default() -> None:
    declared = {
        kind.name: spec.default
        for kind in SINK_KINDS
        for spec in kind.settings
        if spec.key == "path"
    }
    assert set(declared) == set(FILE_SINKS)
    for name, default in declared.items():
        assert str(default).startswith("{workspace}/"), f"sink {name} defaults to {default!r}"


@pytest.mark.parametrize("path", ["{workspace}", "{workspace}/", "{workspace}x/db.sqlite"])
def test_a_workspace_path_the_load_would_refuse_is_refused_in_the_answers_file(
    path: str, tmp_path: Path
) -> None:
    answers = {"sinks": [{"kind": "sqlite", "path": path}]}
    with pytest.raises(ConfigError, match=r"\{workspace\}/<file>"):
        run_step(Terminal(), tmp_path, answers)


def test_at_the_terminal_a_refused_workspace_path_is_asked_again(tmp_path: Path) -> None:
    terminal = Terminal(
        "sqlite",  # sink
        "",  # name
        "{workspace}x/db.sqlite",  # path: refused by the same rule `load` applies
        "",  # table
        "sqlite",  # the same sink is asked again, from the kind
        "",  # name
        "{workspace}/out/mine.db",  # path
        "",  # table
        "n",  # another sink?
    )
    [chosen] = run_step(terminal, tmp_path)
    assert chosen.options["path"] == "{workspace}/out/mine.db"
    assert any("{workspace}/<file>" in line for line in terminal.shown)


def test_a_path_that_is_not_in_the_workspace_is_accepted_as_it_was(tmp_path: Path) -> None:
    answers = {"sinks": [{"kind": "csv", "path": "./beside/sheet.csv"}]}
    [chosen] = run_step(Terminal(), tmp_path, answers)
    assert chosen.options["path"] == "./beside/sheet.csv"
