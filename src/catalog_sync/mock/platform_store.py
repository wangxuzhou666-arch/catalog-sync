"""The reference platform's storage: SQLite, one table per entity, partial upserts, receipts.

The rows are written by the very function the SQLite sink uses (``apply_in_transaction``), so the
reference receiver honours the same partial-upsert contract by construction: only owned fields
are touched, a null is written unless the field keeps its value, nothing is ever deleted. What it
adds is what a platform needs and a sink does not:

* ``_cs_idempotency``: the answer given to each ``Idempotency-Key``, written in the same
  transaction as the rows, so a request is applied exactly once and a repeat gets the stored
  answer. A real platform would expire these after a day or so; here they stay.
* ``_cs_run_keys`` and ``_cs_run_batches``: which records and which requests of a run have been
  seen, so that the last request of a full snapshot can say which stored rows the run did not
  contain (``absent``). Reported, never deleted.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from catalog_sync.models import JsonObject
from catalog_sync.sinks.base import Batch, row_key
from catalog_sync.sinks.sqlite import apply_in_transaction

_SCHEMA = """
CREATE TABLE IF NOT EXISTS _cs_idempotency (
    key TEXT PRIMARY KEY, response TEXT NOT NULL, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS _cs_run_keys (
    run_id TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL,
    PRIMARY KEY (run_id, source, source_id));
CREATE TABLE IF NOT EXISTS _cs_run_batches (
    run_id TEXT NOT NULL, batch_index INTEGER NOT NULL, PRIMARY KEY (run_id, batch_index));
"""


@dataclass(frozen=True)
class Ingest:
    """One validated request, ready to apply."""

    key: str
    run_id: str
    entity: str
    batch_index: int
    batch_count: int
    is_full_snapshot: bool
    batch: Batch  # never marked as a full snapshot: the store answers that across the run


@dataclass(frozen=True)
class Outcome:
    response: JsonObject
    is_replay: bool


class PlatformStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))  # owner only: it holds data
        with self._connection() as conn:
            conn.executescript(_SCHEMA)

    def ingest(self, request: Ingest) -> Outcome:
        """Apply the request once. A key already seen is answered from the stored result."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                outcome = self._ingest_in_transaction(conn, request)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return outcome

    def _ingest_in_transaction(self, conn: sqlite3.Connection, request: Ingest) -> Outcome:
        seen = conn.execute(
            "SELECT response FROM _cs_idempotency WHERE key = ?", (request.key,)
        ).fetchone()
        if seen is not None:
            return Outcome({**json.loads(seen[0]), "replayed": True}, is_replay=True)
        report = apply_in_transaction(conn, request.entity, request.batch)
        absent = self._track_run(conn, request)
        response: JsonObject = {
            "inserted": report.inserted,
            "updated": report.updated,
            "unchanged": report.unchanged,
            "replayed": False,
            "absent": [{"source": source, "source_id": sid} for source, sid in absent],
        }
        conn.execute(
            "INSERT INTO _cs_idempotency (key, response, received_at) VALUES (?, ?, ?)",
            (request.key, json.dumps(response), datetime.now(UTC).isoformat()),
        )
        return Outcome(response, is_replay=False)

    def _track_run(self, conn: sqlite3.Connection, request: Ingest) -> list[tuple[str, str]]:
        """Remember what this request contained; the request that completes a full snapshot (the
        last one, as the sink sends them in order) gets the stored rows of the run's sources that
        the run did not contain. Before the run is complete, "not in the run" would be a guess."""
        if not request.is_full_snapshot:
            return []
        run = request.run_id
        conn.executemany(
            "INSERT OR IGNORE INTO _cs_run_keys (run_id, source, source_id) VALUES (?, ?, ?)",
            [(run, *row_key(row)) for row in request.batch.rows],
        )
        conn.execute(
            "INSERT OR IGNORE INTO _cs_run_batches (run_id, batch_index) VALUES (?, ?)",
            (run, request.batch_index),
        )
        arrived = conn.execute(
            "SELECT count(*) FROM _cs_run_batches WHERE run_id = ?", (run,)
        ).fetchone()[0]
        if arrived != request.batch_count:
            return []
        return self._not_in_run(conn, request.entity, run)

    def _not_in_run(self, conn: sqlite3.Connection, table: str, run: str) -> list[tuple[str, str]]:
        stored = conn.execute(
            f'SELECT d.source, d.source_id FROM "{table}" d '
            "WHERE d.source IN (SELECT DISTINCT source FROM _cs_run_keys WHERE run_id = ?) "
            "AND NOT EXISTS (SELECT 1 FROM _cs_run_keys k WHERE k.run_id = ? "
            "AND k.source = d.source AND k.source_id = d.source_id) ORDER BY d.rowid",
            (run, run),
        )
        return [(row[0], row[1]) for row in stored]

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        # Autocommit mode: transactions are explicit, so DDL is part of them.
        with closing(sqlite3.connect(self.path, isolation_level=None)) as conn:
            yield conn
