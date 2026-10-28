"""The audit log: an append-only record of what the tool did, never of what it saw.

One JSON line per command that touches data (login, pull, normalize, load, run) in
``<workspace>/audit.jsonl``. A line says when, which command, which source and run, how many
records or requests, which compliance declaration was in force (its sha256), which credential was
used (a hash of its token id, enough to tell logins apart), and which sinks were written.

It never holds a token, a header, a URL, a record's content or any personal data: counts are
numbers, everything else is an identifier the operator chose. A test greps the log for a canary
token and a canary sensitive value after a full run to keep it that way.

The file is opened with O_APPEND and one write per line, so concurrent runs interleave whole
lines, and it is created 0600. Nothing in the tool rewrites or truncates it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from catalog_sync.errors import CatalogSyncError, RecordFormatError

_TOKEN_HASH_LENGTH = 16


def hash_token_id(token_id: str | None) -> str | None:
    """A short stable fingerprint of a credential's token id; the id itself is never logged."""
    if token_id is None:
        return None
    return hashlib.sha256(token_id.encode("utf-8")).hexdigest()[:_TOKEN_HASH_LENGTH]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class AuditEvent:
    command: str
    outcome: str
    source: str | None = None
    run_id: str | None = None
    profile: str | None = None
    counts: Mapping[str, int] = field(default_factory=dict)
    compliance_sha256: str | None = None
    credential_hash: str | None = None
    sinks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all(type(count) is int for count in self.counts.values()):
            raise TypeError("audit counts must be integers: the log holds numbers, not content")


class AuditLog:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self.path = path
        self._clock = clock

    def append(self, event: AuditEvent) -> None:
        line = {
            "ts": self._clock().isoformat(),
            "command": event.command,
            "outcome": event.outcome,
            "source": event.source,
            "run_id": event.run_id,
            "profile": event.profile,
            "counts": dict(sorted(event.counts.items())),
            "compliance_sha256": event.compliance_sha256,
            "credential_hash": event.credential_hash,
            "sinks": list(event.sinks),
        }
        encoded = (json.dumps(line, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)

    def read(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        lines: list[dict[str, object]] = []
        text = self.path.read_text(encoding="utf-8")
        for number, raw in enumerate(text.splitlines(), start=1):
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RecordFormatError(
                    f"{self.path.name} line {number} is not valid JSON"
                ) from exc
            if not isinstance(entry, dict):
                raise RecordFormatError(f"{self.path.name} line {number} is not a JSON object")
            lines.append(entry)
        return lines


@dataclass
class AuditScope:
    """What a command fills in while it runs; written as one line when the command ends."""

    command: str
    source: str | None = None
    run_id: str | None = None
    profile: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    compliance_sha256: str | None = None
    credential_hash: str | None = None
    sinks: list[str] = field(default_factory=list)
    outcome: str = "ok"

    def event(self) -> AuditEvent:
        return AuditEvent(
            command=self.command,
            outcome=self.outcome,
            source=self.source,
            run_id=self.run_id,
            profile=self.profile,
            counts=dict(self.counts),
            compliance_sha256=self.compliance_sha256,
            credential_hash=self.credential_hash,
            sinks=tuple(self.sinks),
        )


@contextmanager
def audited(
    log: AuditLog, command: str, *, source: str | None = None, profile: str | None = None
) -> Iterator[AuditScope]:
    """Log the command when it ends, however it ends.

    A failure is logged with the exception's class name and nothing else (its message could carry
    data) and then re-raised; a failure to write the log must not hide the real error. When the
    command succeeds, failing to write the log is itself a failure: a run that cannot be audited
    should not look finished.
    """
    scope = AuditScope(command, source=source, profile=profile)
    try:
        yield scope
    except CatalogSyncError as exc:
        scope.outcome = f"error:{type(exc).__name__}"
        with contextlib.suppress(OSError):
            log.append(scope.event())
        raise
    except BaseException:
        scope.outcome = "interrupted"
        with contextlib.suppress(OSError):
            log.append(scope.event())
        raise
    log.append(scope.event())
