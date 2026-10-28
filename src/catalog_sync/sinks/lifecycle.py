"""The open / backup / upsert ordering rules every sink enforces the same way.

All four sinks (JSONL, CSV, SQLite, Postgres) share them, so they live here once: using a sink that
is not open is a programming error, and a load that was not preceded by a backup of what it is
about to change must be refused, not silently allowed.

File-backed sinks add a path with timestamped backup names (`SinkLifecycle`). The flat-file ones
(JSONL, CSV) rewrite the whole file on every load, so two loads at once would lose one of the two
updates; they also take an advisory lock for as long as they are open (`LockedFileSink`).
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from catalog_sync.errors import SinkError
from catalog_sync.fsutil import atomic_write_bytes

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


class SinkState:
    """Open and backed-up flags, and the checks that read them."""

    # What the Sink protocol asks every sink to declare; a sink that keeps its own rows can do both.
    backup_supported = True
    dry_run_predicts_counts = True

    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._is_open = False
        self._is_backed_up = False

    def open(self) -> None:
        self._is_open = True
        self._is_backed_up = False

    def close(self) -> None:
        self._is_open = False
        self._is_backed_up = False

    def _require_open(self) -> None:
        if not self._is_open:
            raise SinkError("sink is not open")

    def _mark_backed_up(self) -> None:
        self._require_open()
        self._is_backed_up = True

    def _require_backup_unless(self, *, is_dry_run: bool) -> None:
        self._require_open()
        if not is_dry_run and not self._is_backed_up:
            raise SinkError("call backup() before upsert(): loads must be recoverable")

    def _stamp(self) -> str:
        # Microseconds in the stamp: two loads within one second must not share a backup name.
        return self._clock().strftime("%Y%m%dT%H%M%S%fZ")


class SinkLifecycle(SinkState):
    def __init__(self, path: Path, clock: Callable[[], datetime] = utc_now) -> None:
        super().__init__(clock)
        self.path = path

    def _backup_path(self) -> Path:
        path = self.path.with_name(f"{self.path.name}.bak-{self._stamp()}")
        if path.exists():
            raise SinkError(f"backup file {path.name} already exists; not overwriting it")
        return path

    def _copy_file_backup(self) -> Path:
        backup_path = self._backup_path()
        atomic_write_bytes(backup_path, self.path.read_bytes())
        return backup_path


class LockedFileSink(SinkLifecycle):
    """A flat-file sink that holds ``<file>.lock`` exclusively from its backup until it closes.

    The data file is replaced by renaming a new file over it, so the lock cannot live on the data
    file itself (the lock would stay with the old inode). A second process that tries to load into
    the same file gets an error at once instead of waiting: a load can run for minutes, and "it is
    already being loaded" is something the operator should hear about. The lock is advisory: it
    stops other catalog-sync runs, not an editor.

    It is taken in `backup()`, the first call of every real load, and not in `open()`: a dry run
    only reads (the file is always replaced whole, so a read never sees half a file) and must not
    create so much as a lock file.
    """

    def __init__(self, path: Path, clock: Callable[[], datetime] = utc_now) -> None:
        super().__init__(path, clock)
        self._lock_fd: int | None = None

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.lock")

    def close(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # closing the descriptor releases the flock
            self._lock_fd = None
        super().close()

    def _mark_backed_up(self) -> None:
        self._require_open()
        if self._lock_fd is None:
            self._lock_fd = self._acquire_lock()
        super()._mark_backed_up()

    def _acquire_lock(self) -> int | None:
        if sys.platform == "win32":
            logger.warning(
                "this platform has no fcntl: %s is loaded without a lock; do not run two loads "
                "into it at the same time",
                self.path.name,
            )
            return None
        import fcntl  # POSIX only, hence not imported at module level

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise SinkError(
                f"{self.path.name} is being written by another catalog-sync process "
                f"(lock file {self.lock_path.name} is held). Wait for it to finish, then retry"
            ) from exc
        except OSError as exc:
            os.close(fd)
            # A filesystem without lock support: say so rather than run unprotected.
            raise SinkError(f"cannot lock {self.lock_path.name}: {exc.strerror}") from exc
        return fd
