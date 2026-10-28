"""Finding, gating and building the source a command works on.

A source is named either by a ``[sources.NAME]`` entry of the config or by an installed plugin
(options then come from ``--option``). Before anything is built, a source that contacts a
service must pass the compliance gate: no declaration, or pacing that contradicts it, stops the
command here, before a credential is read or a socket opened.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from catalog_sync import registry
from catalog_sync.audit import hash_token_id
from catalog_sync.cli_credentials import credential_option, resolve_credential
from catalog_sync.compliance import SourceGate, gate_source
from catalog_sync.config import SourceConfig
from catalog_sync.errors import ConfigError
from catalog_sync.sources.base import InspectReport, Source

if TYPE_CHECKING:
    from catalog_sync.cli import Context


@dataclass(frozen=True)
class ResolvedSource:
    name: str
    plugin: str
    options: dict[str, object]  # config entry merged with --option overrides
    entry: SourceConfig | None
    base_dir: Path


def describe_source(name: str, overrides: Mapping[str, str], context: Context) -> ResolvedSource:
    config = context.config
    if config is not None and name in config.sources:
        entry = config.sources[name]
        options: dict[str, object] = {**entry.options, **overrides}
        return ResolvedSource(name, entry.plugin, options, entry, config.base_dir)
    if name in registry.available(registry.SOURCES_GROUP):
        return ResolvedSource(name, name, dict(overrides), None, Path.cwd())
    known = ", ".join(registry.available(registry.SOURCES_GROUP)) or "none"
    raise ConfigError(
        f"unknown source {name!r}: not a [sources.*] entry in the config and not an installed "
        f"plugin (installed plugins: {known})"
    )


def check_gate(
    resolved: ResolvedSource, context: Context, *, should_warn: bool = False
) -> SourceGate:
    """Raises ComplianceError unless this source may be contacted. Warnings go to stderr."""
    gate = gate_source(
        resolved.name,
        resolved.entry,
        plugin=resolved.plugin,
        options=resolved.options,
        today=context.clock().date(),
    )
    if should_warn and gate.compliance is not None:
        warning = gate.compliance.tos_review_warning(context.clock().date())
        if warning:
            print(f"warning: source {resolved.name!r}: {warning}", file=sys.stderr)
    return gate


def build_source(name: str, overrides: Mapping[str, str], context: Context) -> Source:
    resolved = describe_source(name, overrides, context)
    gate = check_gate(resolved, context)
    options = dict(resolved.options)
    if gate.compliance is not None:
        # Every networked source plugin accepts this option and must hold the rate it names.
        options["max_requests_per_minute"] = gate.compliance.max_requests_per_minute
    return registry.source_factory(resolved.plugin)(
        credential_option(options, context), base_dir=resolved.base_dir
    )


def credential_fingerprint(resolved: ResolvedSource, context: Context) -> str | None:
    """A hash of the token id of the credential this source would use, for the audit log."""
    name = resolved.options.get("credential")
    if not isinstance(name, str):
        return None
    return hash_token_id(resolve_credential(name, context).load().token_id)


def print_inspection(label: str, plugin: str, report: InspectReport) -> None:
    """What `inspect` shows: the source, its total, the folder labels and the notes."""
    print(f"source: {label} ({plugin})")
    print(f"total:  {'unknown' if report.total is None else report.total}")
    if report.folders:
        print(f"folders ({len(report.folders)}):")
        for folder in report.folders:
            print(f"  {folder}")
    for note in report.notes:
        print(note)
