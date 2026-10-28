"""The order of the wizard, from the first question to the file it writes.

1. which connector, its settings, the compliance block (nothing contacts anything yet);
2. log in, look at the source (``inspect``) and pull a small sample, and show it normalised;
3. which sinks the records go to;
4. write ``catalog-sync.toml`` (last, so that a config that was never shown to work is not left
   behind) and print the command to run.

The login, the look at the source, the sample pull and the normalisation are the real commands
with their own audit lines; the wizard adds one of its own, "init", with the compliance
declaration's hash and what it wrote.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from catalog_sync.audit import AuditScope, audited
from catalog_sync.cli_pipeline import (
    audit_log,
    normalize_with_audit,
    print_pull_summary,
    pull_with_audit,
)
from catalog_sync.cli_sources import (
    build_source,
    credential_fingerprint,
    describe_source,
    print_inspection,
)
from catalog_sync.compliance import parse_compliance
from catalog_sync.config import LOCAL_CONFIG_NAME, parse_config
from catalog_sync.errors import ConfigError
from catalog_sync.fsutil import atomic_write_bytes
from catalog_sync.normalize.mapping import load_mapping
from catalog_sync.puller import PullReport
from catalog_sync.sources.base import Inspectable
from catalog_sync.state import Stage
from catalog_sync.wizard.preview import preview_lines
from catalog_sync.wizard.prompts import Prompter
from catalog_sync.wizard.sinks import SinkChoice, choose_sinks
from catalog_sync.wizard.steps import (
    SourceChoice,
    ask_compliance,
    ask_sample_size,
    ask_settings,
    choose_connector,
    confirm_sample,
    log_in,
    name_the_source,
)
from catalog_sync.wizard.tomlwrite import Table, dumps

if TYPE_CHECKING:
    from catalog_sync.cli import Context

_WORLD_READABLE = 0o644  # no secret is in the file, and a container's user must be able to read it
_PREAMBLE = (
    "Written by `catalog-sync init`. This file holds no secret: a token or a password is an\n"
    "environment variable, and the options below only give its name. Edit it freely; the\n"
    "documented options are in the README (Configuration reference)."
)


@dataclass(frozen=True)
class Wizard:
    """What a run of ``init`` was asked to do."""

    config_path: Path
    answers: Mapping[str, object]
    is_interactive: bool
    may_overwrite: bool


def run(wizard: Wizard, context: Context) -> int:
    _refuse_to_overwrite(wizard)
    prompter = Prompter(wizard.answers, interactive=wizard.is_interactive)
    today = context.clock().date()
    with audited(audit_log(context), "init") as scope:
        prompter.say("catalog-sync init: set up a source, a sink and a profile.\n")
        source = _source(prompter, scope)
        declaration = _declaration(prompter, source, today, scope)
        draft = _draft_context(context, wizard.config_path, source, declaration)
        preview = _try_it_out(prompter, draft, source, scope)
        sinks = choose_sinks(
            prompter,
            entity=preview.entity,
            sensitive=preview.sensitive,
            base_dir=wizard.config_path.resolve().parent,
            workspace=context.workspace,
        )
        scope.sinks.extend(choice.name for choice in sinks)
        text = _config_text(source, declaration, sinks, preview, today)
        _refuse_to_overwrite(wizard)  # the wizard can take minutes; the file may have appeared
        _write(wizard.config_path, text)
        scope.counts["sinks"] = len(sinks)
    _say_what_next(prompter, wizard.config_path, source, sinks)
    return 0


def _refuse_to_overwrite(wizard: Wizard) -> None:
    if wizard.config_path.exists() and not wizard.may_overwrite:
        raise ConfigError(
            f"{wizard.config_path} already exists; init will not overwrite it. Use --force to "
            "replace it, or --config FILE to write another"
        )


# --- 1. the source and its declaration ----------------------------------------------------------


def _source(prompter: Prompter, scope: AuditScope) -> SourceChoice:
    plugin, info = choose_connector(prompter)
    name = name_the_source(prompter, plugin)
    options, credential_options = ask_settings(prompter, plugin, info)
    scope.source = name
    return SourceChoice(plugin, info, name, options, credential_options)


def _declaration(
    prompter: Prompter, source: SourceChoice, today: date, scope: AuditScope
) -> dict[str, object]:
    label = f"sources.{source.name}"
    declaration = ask_compliance(prompter, source.info, source.options, label=label, today=today)
    # The whole, by the gate's own parser; the field checks cannot see across fields.
    scope.compliance_sha256 = parse_compliance(declaration, label=label, today=today).digest
    return declaration


# --- 2. log in, look, sample, preview -----------------------------------------------------------


@dataclass(frozen=True)
class Tried:
    entity: str
    sensitive: frozenset[str]
    mapping: Path


def _draft_context(
    context: Context, config_path: Path, source: SourceChoice, declaration: Mapping[str, object]
) -> Context:
    """A context whose config holds just this source, so that the real commands can run on it
    before anything is written."""
    document = _document(source, declaration, sinks=[], mapping=source.info.mapping)
    config = parse_config(document, base_dir=config_path.resolve().parent, where="the new config")
    return replace(context, config=config)


def _try_it_out(
    prompter: Prompter, context: Context, source: SourceChoice, scope: AuditScope
) -> Tried:
    prompter.say("\nLogging in:")
    log_in(prompter, context, source.info.credential)
    prompter.say("\nLooking at the source (inspect):")
    _inspect(context, source, scope.compliance_sha256)
    size = ask_sample_size(prompter)
    prompter.say(f"\nPulling a sample of {size} item(s):")
    outcome = pull_with_audit(
        context,
        source.name,
        {},
        stages=(Stage.LIST, Stage.DETAIL, Stage.ASSETS),
        resume=None,
        max_items=size,
        incremental=False,
    )
    report = outcome.report
    print_pull_summary(report)
    _refuse_a_failed_sample(report)
    scope.run_id = report.run_id
    scope.counts["sample_items"] = report.item_count
    spec = load_mapping(source.info.mapping)
    normalized, file = normalize_with_audit(
        context,
        source_names=[source.name, source.plugin],
        run_ref=report.run_id,
        spec=spec,
        out=None,
    )
    prompter.say("\nThe sample, normalised (secrets masked, sensitive fields left out):")
    for line in preview_lines(
        normalized.records, file.schema, owned=file.owned, sensitive=file.sensitive
    ):
        prompter.say(line)
    confirm_sample(prompter)
    return Tried(file.schema.entity, file.sensitive, source.info.mapping)


def _inspect(context: Context, source: SourceChoice, compliance_sha256: str | None) -> None:
    """The real `inspect`, with an audit line of its own: it contacts the service."""
    with audited(audit_log(context), "inspect", source=source.name) as scope:
        scope.compliance_sha256 = compliance_sha256
        scope.credential_hash = credential_fingerprint(
            describe_source(source.name, {}, context), context
        )
        built = build_source(source.name, {}, context)
        scope.source = built.name
        if not isinstance(built, Inspectable):
            print(f"source: {source.name} ({built.name}) does not support inspect; going on.")
            scope.counts["inspectable"] = 0
            return
        report = built.inspect(None)
        if report.total is not None:
            scope.counts["total"] = report.total
        scope.counts["folders"] = len(report.folders)
        print_inspection(source.name, built.name, report)


def _refuse_a_failed_sample(report: PullReport) -> None:
    if report.failures:
        first = report.failures[0]
        raise ConfigError(
            f"the sample pull had {len(report.failures)} failed item(s) (first: {first.stage} "
            f"{first.source_id}: {first.error}); nothing was written. Look at `catalog-sync "
            f"runs show {report.run_id}`, fix the cause and run init again"
        )


# --- 4. the file --------------------------------------------------------------------------------


def _mapping_entry(source: SourceChoice, mapping: Path) -> dict[str, str]:
    """The profile's ``mapping`` key, left out when it names the connector's own mapping: the
    profile resolves that by itself, and an absolute path inside an installed package would break
    the file the day the package moves."""
    return {} if mapping == source.info.mapping else {"mapping": str(mapping)}


def _document(
    source: SourceChoice,
    declaration: Mapping[str, object],
    *,
    sinks: Sequence[SinkChoice],
    mapping: Path,
) -> dict[str, object]:
    """The config as the loader reads it: nested tables."""
    document: dict[str, object] = {
        "sources": {
            source.name: {
                "plugin": source.plugin,
                "credential": source.info.credential,
                **source.options,
                "compliance": dict(declaration),
            }
        },
    }
    if sinks:  # a profile needs somewhere to load into; the draft has none yet
        document["profiles"] = {
            source.name: {
                "source": source.name,
                **_mapping_entry(source, mapping),
                "sinks": [choice.name for choice in sinks],
                "incremental": True,
            }
        }
        document["sinks"] = {
            choice.name: {"kind": choice.kind, **choice.options} for choice in sinks
        }
    if source.credential_options:
        document["credentials"] = {source.info.credential: dict(source.credential_options)}
    return document


def _config_text(
    source: SourceChoice,
    declaration: Mapping[str, object],
    sinks: Sequence[SinkChoice],
    tried: Tried,
    today: date,
) -> str:
    tables = [
        Table(
            ("sources", source.name),
            [
                ("plugin", source.plugin),
                ("credential", source.info.credential),
                *source.options.items(),
            ],
            note=f"The source. Its other options keep their defaults ({source.plugin}, README).",
        ),
        Table(
            ("sources", source.name, "compliance"),
            list(declaration.items()),
            note=(
                "What you declared. The tool refuses to contact the service without it; it checks\n"
                f"that it is there and consistent, not that it is true. Written {today:%Y-%m-%d}."
            ),
        ),
    ]
    if source.credential_options:
        tables.append(
            Table(
                ("credentials", source.info.credential),
                list(source.credential_options.items()),
                note="How to log in to it: `catalog-sync login` and `catalog-sync auth status`.",
            )
        )
    tables += [
        Table(("sinks", choice.name), [("kind", choice.kind), *choice.options.items()])
        for choice in sinks
    ]
    tables.append(
        Table(
            ("profiles", source.name),
            [
                ("source", source.name),
                *_mapping_entry(source, tried.mapping).items(),
                ("sinks", [choice.name for choice in sinks]),
                ("incremental", True),
            ],
            note=(
                "What `catalog-sync run` does: pull, normalize and load. The mapping is the\n"
                'one that ships with the connector; add `mapping = "path/to/copy.yaml"` to use\n'
                "your own."
            ),
        )
    )
    return dumps(tables, preamble=_PREAMBLE)


def _write(path: Path, text: str) -> None:
    """Check that the text is a config the loader accepts, then write it. Atomic, so a reader
    never sees half a file, and readable by others: it holds no secret."""
    parse_config(tomllib.loads(text), base_dir=path.resolve().parent, where=path.name)
    atomic_write_bytes(path, text.encode("utf-8"))
    path.chmod(_WORLD_READABLE)


# --- the end ------------------------------------------------------------------------------------


def _say_what_next(
    prompter: Prompter, config_path: Path, source: SourceChoice, sinks: Sequence[SinkChoice]
) -> None:
    flag = (
        ""
        if config_path.name == LOCAL_CONFIG_NAME and config_path.parent == Path.cwd()
        else f" --config {config_path}"
    )
    prompter.say(f"\nWrote {config_path}.")
    variables = _variables(sinks)
    if variables:
        prompter.say(
            "Set before running (their values never go in the file): "
            + ", ".join(f"${v}" for v in variables)
        )
    prompter.say("\nRun it:")
    prompter.say(f"  catalog-sync{flag} run {source.name}")
    prompter.say(
        "\nTo run it on a schedule, call that same command from cron or a systemd timer, e.g."
    )
    prompter.say(f"  0 3 * * 1  catalog-sync{flag} run {source.name}")
    prompter.say(
        f"The login expires: `catalog-sync auth status {source.info.credential}` says when."
    )


def _variables(sinks: Sequence[SinkChoice]) -> list[str]:
    names = [
        str(value)
        for choice in sinks
        for key, value in choice.options.items()
        if key.endswith("_env") and isinstance(value, str)
    ]
    return sorted(dict.fromkeys(names))
