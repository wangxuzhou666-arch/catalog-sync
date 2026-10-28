"""Loading a normalised file into one configured sink.

The order is fixed: build the sink from its config, apply the sensitive-field policy to the batch,
open, back up (unless it is a dry run), upsert, close. A dry run reports what would happen and
leaves the target, and the backups directory, exactly as they were.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from catalog_sync import registry
from catalog_sync.config import Config, SinkConfig, Workspace, expand_sink_paths
from catalog_sync.errors import ConfigError
from catalog_sync.normalize.output import NormalizedFile
from catalog_sync.sinks.base import BackupReceipt, BatchOrigin, Sink, WriteReport
from catalog_sync.sinks.records import record_batch
from catalog_sync.sinks.sensitivity import (
    SensitivePolicy,
    apply_policy,
    split_policy,
    workspace_salt,
)


@dataclass(frozen=True)
class LoadOutcome:
    sink_name: str
    kind: str
    report: WriteReport
    backup: BackupReceipt | None  # None for a dry run
    policy: SensitivePolicy
    sensitive_fields: tuple[str, ...]  # the owned fields the policy applied to
    is_dry_run: bool


def sink_config(config: Config | None, name: str) -> SinkConfig:
    if config is None:
        raise ConfigError(f"sink {name!r} needs a config file defining [sinks.{name}]")
    try:
        return config.sinks[name]
    except KeyError:
        known = ", ".join(sorted(config.sinks)) or "none"
        raise ConfigError(f"unknown sink {name!r} (defined in the config: {known})") from None


def load_normalized(
    sink_entry: SinkConfig,
    normalized: NormalizedFile,
    *,
    base_dir: Path,
    workspace: Workspace,
    salt_path: Path,
    dry_run: bool,
) -> LoadOutcome:
    policy, options = split_policy(sink_entry.options)
    sink: Sink = registry.sink_factory(sink_entry.kind)(
        expand_sink_paths(options, workspace), base_dir=base_dir
    )
    batch = apply_policy(
        record_batch(
            normalized.records,
            normalized.schema,
            owned=normalized.owned,
            keep_existing_if_null=normalized.keep_existing_if_null,
            is_full_snapshot=normalized.is_full_snapshot,
            origin=BatchOrigin(
                normalized.schema.entity, normalized.schema.schema_version, normalized.run_id
            ),
        ),
        normalized.sensitive,
        policy,
        lambda: workspace_salt(salt_path),
    )
    affected = tuple(sorted(normalized.sensitive & normalized.owned))
    sink.open()
    try:
        receipt = None if dry_run else sink.backup()
        report = sink.upsert(batch, dry_run=dry_run)
    finally:
        sink.close()
    return LoadOutcome(
        sink_name=sink_entry.name,
        kind=sink_entry.kind,
        report=report,
        backup=receipt,
        policy=policy,
        sensitive_fields=affected,
        is_dry_run=dry_run,
    )
