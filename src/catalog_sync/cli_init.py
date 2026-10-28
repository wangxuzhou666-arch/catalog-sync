"""The ``init`` command: a wizard that ends in a working ``catalog-sync.toml``.

At a terminal it asks; with ``--answers FILE`` it reads the answers from a TOML file instead, and
with ``--non-interactive`` it never asks (a question without an answer is an error naming the key),
which is what a CI job or a provisioning script wants.
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from catalog_sync.config import LOCAL_CONFIG_NAME
from catalog_sync.errors import ConfigError
from catalog_sync.wizard.flow import Wizard, run

if TYPE_CHECKING:
    from catalog_sync.cli import Context


def add_init_command(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "init",
        help="set up a source, a sink and a profile step by step, and write catalog-sync.toml",
        description="Asks which connector, where the service is, the compliance declaration "
        "field by field, how to log in; runs the login, inspects the source and pulls a small "
        "sample you can look at; asks where the records go; then writes the config and prints "
        f"the command to run. Writes {LOCAL_CONFIG_NAME} (or --config FILE) last, and no secret "
        "in it: tokens and passwords are environment variables that the file only names.",
    )
    parser.add_argument(
        "--answers",
        type=Path,
        metavar="FILE.toml",
        help="read the answers from this TOML file (keys as the questions: connector, name, "
        "[settings], [compliance], [login], [sample], [[sinks]]); ask only what it lacks",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="never ask: a question with no answer and no default is an error (for CI)",
    )
    parser.add_argument(
        "--force", action="store_true", help="replace the config file if it already exists"
    )
    parser.set_defaults(run_command=cmd_init, loads_config=False)


def cmd_init(args: argparse.Namespace, context: Context) -> int:
    config_path: Path = args.config or Path.cwd() / LOCAL_CONFIG_NAME
    return run(
        Wizard(
            config_path=config_path,
            answers=_read_answers(args.answers) if args.answers else {},
            is_interactive=not args.non_interactive,
            may_overwrite=args.force,
        ),
        context,
    )


def _read_answers(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read the answers file {path.name}: {exc.strerror}") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"the answers file {path.name} is not valid TOML: {exc}") from exc
