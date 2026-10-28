"""The pull pipeline: enumerate, diff against what was pulled before, fetch details, fetch assets.

Stages run one after another over the whole item set (not item by item), which is what lets
``--stage`` run a single stage and lets a resumed run pick up exactly the unfinished work.

Crash safety comes from write order, not from transactions spanning the filesystem and SQLite:
for every unit of work the blob lands in the snapshot first and the status flips to ``done``
afterwards. A kill in between costs at most one repeated fetch of the unit that was in flight;
a unit whose status says ``done`` is never fetched again.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from catalog_sync.errors import (
    ConfigError,
    FuseTripped,
    ItemFailed,
    SourceShapeError,
)
from catalog_sync.models import JsonObject
from catalog_sync.redact import redact_text
from catalog_sync.snapshot import RunSnapshot, canonical_json, sha256_hex
from catalog_sync.sources.base import AssetRef, ListedItem, Source
from catalog_sync.state import (
    Checkpoint,
    FailureRow,
    RunStatus,
    Stage,
    StageStatus,
    StateStore,
)

ALL_STAGES: tuple[Stage, ...] = (Stage.LIST, Stage.DETAIL, Stage.ASSETS)
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5


@dataclass
class _Fetches:
    """What this invocation actually asked the source for (a resumed run starts again at zero)."""

    detail: int = 0
    assets: int = 0


@dataclass(frozen=True)
class PullReport:
    run_id: str
    source: str
    status: RunStatus
    item_count: int
    stage_counts: Mapping[Stage, Mapping[StageStatus, int]]
    failures: tuple[FailureRow, ...]
    is_truncated: bool  # enumeration stopped early because of max_items
    detail_fetches: int
    asset_fetches: int


def effective_version(item: ListedItem) -> str:
    """The change-detection token for an item: the source's own, else a hash of the listing row."""
    if item.version is not None:
        return item.version
    return "sha256:" + sha256_hex(canonical_json(item.payload))


def merge_context(stored: JsonObject, incoming: JsonObject) -> JsonObject:
    """Fold a second sighting of an item into the first.

    List-valued entries (folder paths) are unioned in order; for any other key the first value
    wins. This is how an item that lives in two folders ends up with both paths.
    """
    merged = dict(stored)
    for key, value in incoming.items():
        current = merged.get(key)
        if key not in merged:
            merged[key] = value
        elif isinstance(current, list) and isinstance(value, list):
            merged[key] = current + [entry for entry in value if entry not in current]
    return merged


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Puller:
    source: Source
    store: StateStore
    runs_root: Path
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES
    clock: Callable[[], datetime] = _utc_now

    def pull(
        self,
        *,
        stages: Collection[Stage] = ALL_STAGES,
        resume: str | None = None,
        max_items: int | None = None,
        incremental: bool = True,
    ) -> PullReport:
        run_id, snapshot = self._open_run(resume, stages)
        checkpoint = self.store.checkpoint(run_id)
        fetches = _Fetches()
        is_truncated = False
        failures: list[FailureRow] = []
        outcome = RunStatus.INTERRUPTED  # what the run counts as unless it gets to the end
        try:
            if Stage.LIST in stages and not self.store.get_run(run_id).list_complete:
                is_truncated = self._list_stage(snapshot, checkpoint, max_items, incremental)
            if Stage.DETAIL in stages:
                self._run_stage(
                    checkpoint, Stage.DETAIL, lambda sid: self._detail(snapshot, sid, fetches)
                )
            if Stage.ASSETS in stages:
                self._run_stage(
                    checkpoint,
                    Stage.ASSETS,
                    lambda sid: self._assets(snapshot, sid, fetches),
                    after=Stage.DETAIL,
                )
            failures = checkpoint.failures()
            outcome = RunStatus.COMPLETE_WITH_FAILURES if failures else RunStatus.COMPLETE
        except FuseTripped:
            outcome = RunStatus.FUSED
            raise
        finally:
            self.store.finish_run(run_id, outcome)
        return PullReport(
            run_id=run_id,
            source=self.source.name,
            status=outcome,
            item_count=checkpoint.count_items(),
            stage_counts=checkpoint.stage_counts(),
            failures=tuple(failures),
            is_truncated=is_truncated,
            detail_fetches=fetches.detail,
            asset_fetches=fetches.assets,
        )

    # --- run bookkeeping --------------------------------------------------------------------

    def _open_run(self, resume: str | None, stages: Collection[Stage]) -> tuple[str, RunSnapshot]:
        if resume is not None:
            row = self.store.get_run(resume)
            if row.source != self.source.name:
                raise ConfigError(
                    f"run {resume} belongs to source {row.source!r}, not {self.source.name!r}"
                )
            if row.status is RunStatus.PURGING:
                raise ConfigError(f"run {resume} is being deleted by a cache purge; pull afresh")
            snapshot = RunSnapshot.open(self.runs_root / resume)
            self.store.reopen_run(resume)
            return resume, snapshot
        if Stage.LIST not in stages:
            raise ConfigError("a new run must include the list stage; use --resume to continue one")
        now = self.clock()
        run_id = f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
        snapshot = RunSnapshot.create(
            self.runs_root, run_id=run_id, source=self.source.name, created_at=now
        )
        self.store.create_run(run_id, self.source.name)
        return run_id, snapshot

    # --- list stage -------------------------------------------------------------------------

    def _list_stage(
        self,
        snapshot: RunSnapshot,
        checkpoint: Checkpoint,
        max_items: int | None,
        incremental: bool,
    ) -> bool:
        """Record every listed item. Returns True when max_items cut the enumeration short."""
        for item in self.source.enumerate(checkpoint):
            self._record_listed(snapshot, checkpoint, item, incremental)
            if max_items is not None and checkpoint.count_items() >= max_items:
                # Returning drops the suspended generator before it can advance its frontier past
                # this item, so a later resume delivers it again; recording is idempotent.
                return True
        self.store.mark_list_complete(checkpoint.run_id)
        return False

    def _record_listed(
        self, snapshot: RunSnapshot, checkpoint: Checkpoint, item: ListedItem, incremental: bool
    ) -> None:
        if checkpoint.has_item(item.source_id):
            self._merge_second_sighting(snapshot, item)
            return
        # Blob first, then the row: a crash between the two leaves a blob the next delivery
        # of this item rewrites identically (put is a no-op for identical content).
        snapshot.put_item(item)
        version = effective_version(item)
        is_unchanged = (
            incremental and self.store.stored_version(self.source.name, item.source_id) == version
        )
        status = StageStatus.SKIPPED if is_unchanged else StageStatus.PENDING
        checkpoint.add_item(item.source_id, version, detail=status, assets=status)

    def _merge_second_sighting(self, snapshot: RunSnapshot, item: ListedItem) -> None:
        stored = snapshot.get_item(item.source_id)
        merged = merge_context(stored.context, item.context)
        if merged != stored.context:
            snapshot.put_item(replace(stored, context=merged))

    # --- detail and asset stages ------------------------------------------------------------

    def _run_stage(
        self,
        checkpoint: Checkpoint,
        stage: Stage,
        work: Callable[[str], None],
        *,
        after: Stage | None = None,
    ) -> None:
        consecutive_failures = 0
        for source_id in checkpoint.pending(stage, after=after):
            try:
                work(source_id)
            except (ItemFailed, SourceShapeError) as exc:
                # Only failures confined to one item are isolated. Auth errors, config errors and
                # crashes are not caught here: they stop the run and leave it resumable.
                # Stored and later printed by `runs show`, so a secret an error message happened
                # to carry must not reach the state database.
                # ItemFailed's text starts with the item id, which every place that prints a
                # failure already prints from `source_id`: keep only the reason.
                reason = redact_text(exc.reason if isinstance(exc, ItemFailed) else str(exc))
                checkpoint.mark(source_id, stage, StageStatus.FAILED, reason)
                consecutive_failures += 1
                if consecutive_failures >= self.max_consecutive_failures:
                    raise FuseTripped(
                        f"{consecutive_failures} consecutive {stage} failures in run "
                        f"{checkpoint.run_id}; last: {source_id}: {reason}. Fix the cause, then "
                        f"continue with --resume {checkpoint.run_id}"
                    ) from exc
            else:
                checkpoint.mark_done(source_id, stage, source=self.source.name)
                consecutive_failures = 0

    def _detail(self, snapshot: RunSnapshot, source_id: str, fetches: _Fetches) -> None:
        item = snapshot.get_item(source_id)
        fetches.detail += 1
        detail = self.source.fetch_detail(item)
        if detail is not None:
            snapshot.put_detail(source_id, detail)

    def _assets(self, snapshot: RunSnapshot, source_id: str, fetches: _Fetches) -> None:
        item = snapshot.get_item(source_id)
        refs = self.source.asset_refs(item, snapshot.get_detail(source_id))
        _check_refs(source_id, refs)
        snapshot.put_asset_refs(source_id, refs)
        for ref in refs:
            if snapshot.has_asset(ref):  # fetched before a crash cut this item short
                continue
            fetches.assets += 1
            snapshot.put_asset(ref, self.source.fetch_asset(ref))


def _check_refs(source_id: str, refs: list[AssetRef]) -> None:
    roles = [ref.role for ref in refs]
    if len(set(roles)) != len(roles):
        raise SourceShapeError(f"{source_id}: asset roles are not unique: {sorted(roles)}")
    for ref in refs:
        if ref.source_id != source_id:
            raise SourceShapeError(f"{source_id}: asset ref belongs to {ref.source_id!r}")
