"""Where the records go: which sinks, and their settings, asked from their declarations.

A secret is never asked for. A setting that holds one (a Postgres connection string, a platform's
token) is declared as the *name of an environment variable*, and the answer is checked to look
like a name; if it looks like a secret it is refused and not repeated. The wizard also says, in
passing, when that variable is not set in the shell it runs in, without reading its value.
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from catalog_sync import registry
from catalog_sync.config import Workspace, expand_sink_paths
from catalog_sync.connector_info import SettingKind
from catalog_sync.errors import ConfigError
from catalog_sync.sinks.kinds import SINK_KINDS, SinkKind
from catalog_sync.sinks.sensitivity import OPTION as SENSITIVE_OPTION
from catalog_sync.sinks.sensitivity import SensitivePolicy, split_policy
from catalog_sync.wizard.prompts import Parser, Prompter, choice, yes_no
from catalog_sync.wizard.steps import ask_setting

_SINK_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


@dataclass(frozen=True)
class SinkChoice:
    name: str
    kind: str
    options: Mapping[str, object]  # the asked settings, typed; sensitive_fields included


def choose_sinks(
    prompter: Prompter,
    *,
    entity: str,
    sensitive: Collection[str],
    base_dir: Path,
    workspace: Workspace,
) -> list[SinkChoice]:
    """One or more sinks. From an answers file: the `[[sinks]]` tables, in order. At the terminal:
    as many as the person adds."""
    prompter.say("Sinks:")
    for number, kind in enumerate(SINK_KINDS, start=1):
        prompter.say(f"  {number}. {kind.name} - {kind.summary}")
    chosen: list[SinkChoice] = []
    for entry in _entries(prompter):
        chosen.append(_collect(entry, chosen, entity, sorted(sensitive), base_dir, workspace))
    return chosen


def _entries(prompter: Prompter) -> Iterator[Prompter]:
    scripted = prompter.children("sinks")
    if scripted:
        yield from scripted
        return
    if not prompter.is_interactive:
        raise ConfigError(
            "no answer for 'sinks'. Add at least one [[sinks]] table (kind, name and its "
            "settings) to the answers file: this run is not interactive"
        )
    while True:
        yield prompter.child("sinks")
        if not prompter.ask("more_sinks", "Add another sink? (yes/no)", yes_no, default="no"):
            return


def _collect(
    entry: Prompter,
    taken: list[SinkChoice],
    entity: str,
    sensitive: list[str],
    base_dir: Path,
    workspace: Workspace,
) -> SinkChoice:
    while True:
        kind = _kind(entry)
        name = entry.ask(
            "name",
            "Name for this sink in the config",
            _sink_name({choice_.name for choice_ in taken}),
            default=_free_name(kind.name, {choice_.name for choice_ in taken}),
        )
        options = _ask_options(entry, kind, entity)
        if sensitive:
            options[SENSITIVE_OPTION] = _ask_policy(entry, kind.name, sensitive)
        try:
            _check(kind.name, options, base_dir, workspace)
        except ConfigError as exc:
            if not entry.is_interactive:
                raise
            entry.say(f"  {exc}")
            continue
        return SinkChoice(name, kind.name, options)


def _kind(entry: Prompter) -> SinkKind:
    by_name = {kind.name: kind for kind in SINK_KINDS}
    return by_name[entry.ask("kind", "Which sink? (name or number)", choice(list(by_name)))]


def _ask_options(entry: Prompter, kind: SinkKind, entity: str) -> dict[str, object]:
    options: dict[str, object] = {}
    for spec in kind.settings:
        if not spec.is_asked:
            continue
        default = f"{entity}s" if spec.key == "table" else None
        value = ask_setting(entry, f"{kind.name} sink", spec, default=default)
        if value is None:
            continue
        options[spec.key] = value
        if spec.kind is SettingKind.ENV_NAME:
            _remind_about_variable(entry, str(value))
    return options


def _remind_about_variable(entry: Prompter, name: str) -> None:
    if not os.environ.get(name):
        entry.say(f"  note: ${name} is not set in this shell; set it before the load runs")


def _ask_policy(entry: Prompter, kind: str, sensitive: list[str]) -> str:
    policies = [policy.value for policy in SensitivePolicy]
    return entry.ask(
        SENSITIVE_OPTION,
        f"The mapping marks {', '.join(sensitive)} as sensitive: {'/'.join(policies)} for {kind}?",
        choice(policies),
        default=SensitivePolicy.DROP.value,
        help_text="  drop leaves it out, hash stores a salted sha256, keep stores it as it is.",
    )


def _check(kind: str, options: Mapping[str, object], base_dir: Path, workspace: Workspace) -> None:
    """Build the sink from its options, as `load` will: whatever it would refuse is found now.

    `{workspace}/` is expanded by the resolver `load` uses, so a path it would refuse
    (`{workspace}x/db`) is refused here and not after the file is written."""
    _, sink_options = split_policy(options)
    registry.sink_factory(kind)(expand_sink_paths(sink_options, workspace), base_dir=base_dir)


def _sink_name(taken: set[str]) -> Parser[str]:
    def parse(text: str) -> str:
        word = text.strip()
        if _SINK_NAME.fullmatch(word) is None:
            raise ConfigError(
                "use lowercase letters, digits, '-' and '_', starting with a letter or digit"
            )
        if word in taken:
            raise ConfigError(f"there is already a sink called {word}")
        return word

    return parse


def _free_name(wanted: str, taken: set[str]) -> str:
    if wanted not in taken:
        return wanted
    number = 2
    while f"{wanted}-{number}" in taken:
        number += 1
    return f"{wanted}-{number}"
