"""SQLite checkpoint: what each run has done, so a killed run can continue where it stopped.

Two scopes live here:

* per run: the run row, one row per item with a status for each stage, and a small key/value
  "frontier" that a source uses to remember how far its enumeration got;
* across runs: the version at which each item was last pulled completely, which is what makes
  the next run incremental.

The database holds bookkeeping only. Raw responses live in the run snapshot (snapshot.py).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from catalog_sync.errors import CatalogSyncError

_SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE runs (
    run_id        TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    status        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    list_complete INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE items (
    run_id    TEXT NOT NULL REFERENCES runs(run_id),
    source_id TEXT NOT NULL,
    ordinal   INTEGER NOT NULL,
    version   TEXT NOT NULL,
    PRIMARY KEY (run_id, source_id)
);
CREATE TABLE item_stages (
    run_id     TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    stage      TEXT NOT NULL,
    status     TEXT NOT NULL,
    error      TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, source_id, stage),
    FOREIGN KEY (run_id, source_id) REFERENCES items(run_id, source_id)
);
CREATE TABLE frontier (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    key    TEXT NOT NULL,
    value  TEXT NOT NULL,
    PRIMARY KEY (run_id, key)
);
CREATE TABLE item_versions (
    source     TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    version    TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
"""

_RECORD_VERSION = (
    "INSERT INTO item_versions (source, source_id, version, run_id, updated_at) "
    "VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT (source, source_id) DO UPDATE SET "
    "version = excluded.version, run_id = excluded.run_id, "
    "updated_at = excluded.updated_at"
)


class Stage(StrEnum):
    LIST = "list"
    DETAIL = "detail"
    ASSETS = "assets"


class StageStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    SKIPPED = "skipped"  # unchanged since the last complete pull, nothing to fetch
    FAILED = "failed"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    COMPLETE_WITH_FAILURES = "complete_with_failures"
    FUSED = "fused"
    INTERRUPTED = "interrupted"
    PURGING = "purging"  # a cache purge has claimed the run and is deleting its directory


@dataclass(frozen=True)
class RunRow:
    run_id: str
    source: str
    status: RunStatus
    started_at: str
    finished_at: str | None
    list_complete: bool


@dataclass(frozen=True)
class StoredPull:
    """The last complete pull of an item: its version, and the run holding its detail and assets."""

    version: str
    run_id: str


@dataclass(frozen=True)
class ItemRow:
    source_id: str
    ordinal: int
    version: str
    stages: Mapping[Stage, StageStatus]

    @property
    def is_fully_pulled(self) -> bool:
        return all(s in (StageStatus.DONE, StageStatus.SKIPPED) for s in self.stages.values())


@dataclass(frozen=True)
class FailureRow:
    source_id: str
    stage: Stage
    error: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


class StateStore:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] = _utc_now) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        try:
            self._init_schema()
        except BaseException:
            self._conn.close()  # a half-built store must not leak its connection
            raise

    def _init_schema(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == _SCHEMA_VERSION:
            return
        if version != 0:
            raise CatalogSyncError(
                f"state database has schema version {version}, this build expects {_SCHEMA_VERSION}"
            )
        with self._conn:
            self._conn.executescript(_DDL)
            self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _now(self) -> str:
        return self._clock().isoformat()

    # --- runs -------------------------------------------------------------------------------

    def create_run(self, run_id: str, source: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO runs (run_id, source, status, started_at) VALUES (?, ?, ?, ?)",
                (run_id, source, RunStatus.RUNNING, self._now()),
            )

    def get_run(self, run_id: str) -> RunRow:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise CatalogSyncError(f"no such run: {run_id}")
        return _run_row(row)

    def list_runs(self) -> list[RunRow]:
        rows = self._conn.execute("SELECT * FROM runs ORDER BY started_at DESC, run_id DESC")
        return [_run_row(row) for row in rows]

    def latest_run(self, source: str, *, settled_only: bool = False) -> RunRow | None:
        """The newest run of `source`. `settled_only` skips runs still marked running (their
        snapshot is being written, or the process died) and runs being purged: neither is a
        consistent copy to read."""
        row = self._conn.execute(
            "SELECT * FROM runs WHERE source = ? AND (? = 0 OR status NOT IN (?, ?)) "
            "ORDER BY started_at DESC, run_id DESC LIMIT 1",
            (source, settled_only, RunStatus.RUNNING, RunStatus.PURGING),
        ).fetchone()
        return None if row is None else _run_row(row)

    def reopen_run(self, run_id: str) -> None:
        """Mark a run running again so it can be resumed. A run a purge has claimed stays gone."""
        with self._conn:
            reopened = self._conn.execute(
                "UPDATE runs SET status = ?, finished_at = NULL WHERE run_id = ? AND status != ?",
                (RunStatus.RUNNING, run_id, RunStatus.PURGING),
            )
        if reopened.rowcount != 1:
            raise CatalogSyncError(f"run {run_id} is being deleted by a cache purge; pull afresh")

    def finish_run(self, run_id: str, status: RunStatus) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
                (status, self._now(), run_id),
            )

    def mark_list_complete(self, run_id: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE runs SET list_complete = 1 WHERE run_id = ?", (run_id,))

    def checkpoint(self, run_id: str) -> Checkpoint:
        return Checkpoint(self._conn, run_id, self._now)

    def delete_run(self, run_id: str) -> None:
        """Forget a run's bookkeeping. Refuses a run that items still depend on.

        ``item_versions.run_id`` names the run holding an unchanged item's raw data, so removing
        that run would leave the next incremental pull trusting data that is no longer there. The
        check and the deletes share one write transaction, so a pull that records a version
        cannot slip in between them.
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self.require_unreferenced(run_id)
            self._conn.execute("DELETE FROM item_stages WHERE run_id = ?", (run_id,))
            self._conn.execute("DELETE FROM items WHERE run_id = ?", (run_id,))
            self._conn.execute("DELETE FROM frontier WHERE run_id = ?", (run_id,))
            deleted = self._conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            if deleted.rowcount != 1:
                raise CatalogSyncError(f"no such run: {run_id}")

    def mark_purging(self, run_id: str, *, was: RunStatus) -> None:
        """Claim a run for deletion: from now on nothing can resume it.

        The no-dependents check and the status change share one write transaction, so a pull that
        resumes the run or records a version in it cannot slip between the check and the
        directory delete that follows. `was` is the status the purge was planned with; a run
        that has moved on since (a pull reopened it) is refused instead of deleted under it.
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self.require_unreferenced(run_id)
            row = self._conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise CatalogSyncError(f"no such run: {run_id}")
            if row["status"] != was:
                raise CatalogSyncError(
                    f"run {run_id} is now {row['status']}, not {was} as when the purge was "
                    "planned; nothing was deleted"
                )
            self._conn.execute(
                "UPDATE runs SET status = ? WHERE run_id = ?", (RunStatus.PURGING, run_id)
            )

    def require_unreferenced(self, run_id: str) -> None:
        held = self._conn.execute(
            "SELECT COUNT(*) FROM item_versions WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        if held:
            raise CatalogSyncError(
                f"run {run_id} cannot be deleted: {held} item(s) still depend on its data"
            )

    def dependents(self) -> dict[str, int]:
        """For each run that holds the current raw data of some items: how many items."""
        rows = self._conn.execute(
            "SELECT run_id, COUNT(*) AS items FROM item_versions GROUP BY run_id"
        )
        return {row["run_id"]: row["items"] for row in rows}

    # --- versions across runs ---------------------------------------------------------------

    def stored_version(self, source: str, source_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT version FROM item_versions WHERE source = ? AND source_id = ?",
            (source, source_id),
        ).fetchone()
        return None if row is None else row["version"]

    def stored_pull(self, source: str, source_id: str) -> StoredPull | None:
        row = self._conn.execute(
            "SELECT version, run_id FROM item_versions WHERE source = ? AND source_id = ?",
            (source, source_id),
        ).fetchone()
        return None if row is None else StoredPull(row["version"], row["run_id"])

    def record_version(self, source: str, source_id: str, version: str, run_id: str) -> None:
        with self._conn:
            self._conn.execute(_RECORD_VERSION, (source, source_id, version, run_id, self._now()))


class Checkpoint:
    """The part of the state database that belongs to one run.

    Sources receive this in `enumerate` and may only use the frontier methods. The puller uses
    the rest.
    """

    def __init__(self, conn: sqlite3.Connection, run_id: str, now: Callable[[], str]) -> None:
        self._conn = conn
        self._now = now
        self.run_id = run_id

    # --- frontier ---------------------------------------------------------------------------

    def get_frontier(self, key: str) -> object | None:
        row = self._conn.execute(
            "SELECT value FROM frontier WHERE run_id = ? AND key = ?", (self.run_id, key)
        ).fetchone()
        return None if row is None else json.loads(row["value"])

    def set_frontier(self, key: str, value: object) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO frontier (run_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT (run_id, key) DO UPDATE SET value = excluded.value",
                (self.run_id, key, json.dumps(value, sort_keys=True, allow_nan=False)),
            )

    # --- items ------------------------------------------------------------------------------

    def has_item(self, source_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM items WHERE run_id = ? AND source_id = ?", (self.run_id, source_id)
        ).fetchone()
        return row is not None

    def count_items(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM items WHERE run_id = ?", (self.run_id,))
        return int(row.fetchone()[0])

    def add_item(
        self,
        source_id: str,
        version: str,
        *,
        detail: StageStatus,
        assets: StageStatus,
    ) -> None:
        """Register a freshly listed item. The list stage is done by definition."""
        now = self._now()
        stages = {Stage.LIST: StageStatus.DONE, Stage.DETAIL: detail, Stage.ASSETS: assets}
        with self._conn:
            self._conn.execute(
                "INSERT INTO items (run_id, source_id, ordinal, version) VALUES (?, ?, "
                "(SELECT COALESCE(MAX(ordinal), -1) + 1 FROM items WHERE run_id = ?), ?)",
                (self.run_id, source_id, self.run_id, version),
            )
            self._conn.executemany(
                "INSERT INTO item_stages (run_id, source_id, stage, status, attempts, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (self.run_id, source_id, stage, status, 0, now)
                    for stage, status in stages.items()
                ],
            )

    def item(self, source_id: str) -> ItemRow:
        rows = self._conn.execute(
            "SELECT i.source_id, i.ordinal, i.version, s.stage, s.status "
            "FROM items i JOIN item_stages s USING (run_id, source_id) "
            "WHERE i.run_id = ? AND i.source_id = ?",
            (self.run_id, source_id),
        ).fetchall()
        if not rows:
            raise CatalogSyncError(f"run {self.run_id} has no item {source_id!r}")
        return _item_row(rows)

    def items(self) -> Iterator[ItemRow]:
        rows = self._conn.execute(
            "SELECT i.source_id, i.ordinal, i.version, s.stage, s.status "
            "FROM items i JOIN item_stages s USING (run_id, source_id) "
            "WHERE i.run_id = ? ORDER BY i.ordinal",
            (self.run_id,),
        ).fetchall()
        per_item: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            per_item.setdefault(row["source_id"], []).append(row)
        for item_rows in per_item.values():
            yield _item_row(item_rows)

    def pending(self, stage: Stage, *, after: Stage | None = None) -> list[str]:
        """Items whose `stage` still has to run, in listing order. Failed ones are retried.

        With `after`, only items whose `after` stage already succeeded qualify: assets are not
        fetched for an item whose detail is still missing, so the item is retried as a whole.
        """
        rows = self._conn.execute(
            "SELECT i.source_id FROM items i JOIN item_stages s USING (run_id, source_id) "
            "WHERE i.run_id = ? AND s.stage = ? AND s.status IN (?, ?) "
            "AND (? IS NULL OR EXISTS (SELECT 1 FROM item_stages d WHERE d.run_id = i.run_id "
            "AND d.source_id = i.source_id AND d.stage = ? AND d.status IN (?, ?))) "
            "ORDER BY i.ordinal",
            (
                self.run_id,
                stage,
                StageStatus.PENDING,
                StageStatus.FAILED,
                after,
                after,
                StageStatus.DONE,
                StageStatus.SKIPPED,
            ),
        )
        return [row["source_id"] for row in rows]

    def mark(
        self, source_id: str, stage: Stage, status: StageStatus, error: str | None = None
    ) -> None:
        with self._conn:
            self._update_stage(source_id, stage, status, error)

    def mark_done(self, source_id: str, stage: Stage, *, source: str) -> None:
        """Mark a stage done and, if that finishes the item, record its version, atomically.

        Two commits would let a kill between them leave an item whose stages all say done but whose
        stored version is old: the next incremental pull would fetch it all again.
        """
        with self._conn:
            self._update_stage(source_id, stage, StageStatus.DONE, None)
            row = self.item(source_id)
            if row.is_fully_pulled:
                # Only a complete pull moves the stored version; a half-pulled item stays "changed".
                self._conn.execute(
                    _RECORD_VERSION, (source, source_id, row.version, self.run_id, self._now())
                )

    def _update_stage(
        self, source_id: str, stage: Stage, status: StageStatus, error: str | None
    ) -> None:
        """The stage row update, left to the caller's transaction."""
        cursor = self._conn.execute(
            "UPDATE item_stages SET status = ?, error = ?, updated_at = ?, "
            "attempts = attempts + 1 "
            "WHERE run_id = ? AND source_id = ? AND stage = ?",
            (status, error, self._now(), self.run_id, source_id, stage),
        )
        if cursor.rowcount != 1:
            raise CatalogSyncError(f"run {self.run_id} has no item {source_id!r}")

    def failures(self) -> list[FailureRow]:
        rows = self._conn.execute(
            "SELECT i.source_id, s.stage, s.error FROM items i "
            "JOIN item_stages s USING (run_id, source_id) "
            "WHERE i.run_id = ? AND s.status = ? ORDER BY i.ordinal, s.stage",
            (self.run_id, StageStatus.FAILED),
        )
        return [
            FailureRow(row["source_id"], Stage(row["stage"]), row["error"] or "") for row in rows
        ]

    def stage_counts(self) -> dict[Stage, dict[StageStatus, int]]:
        counts: dict[Stage, dict[StageStatus, int]] = {stage: {} for stage in Stage}
        rows = self._conn.execute(
            "SELECT stage, status, COUNT(*) AS n FROM item_stages WHERE run_id = ? "
            "GROUP BY stage, status",
            (self.run_id,),
        )
        for row in rows:
            counts[Stage(row["stage"])][StageStatus(row["status"])] = row["n"]
        return counts


def _run_row(row: sqlite3.Row) -> RunRow:
    return RunRow(
        run_id=row["run_id"],
        source=row["source"],
        status=RunStatus(row["status"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        list_complete=bool(row["list_complete"]),
    )


def _item_row(rows: list[sqlite3.Row]) -> ItemRow:
    first = rows[0]
    return ItemRow(
        source_id=first["source_id"],
        ordinal=first["ordinal"],
        version=first["version"],
        stages={Stage(r["stage"]): StageStatus(r["status"]) for r in rows},
    )
