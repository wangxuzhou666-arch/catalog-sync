"""The messages of the HTTP sink's protocol: how a batch becomes a request and an answer is read.

This is the wire format in docs/platform-contract.md, in code. It sits apart from the sink so that
the reference receiver (``catalog-sync mock platform serve``) builds on the very same definitions
the sink does: the idempotency keys it checks are computed by the functions that computed them.

Request body (JSON)::

    {"protocol": 1, "run_id": "...", "schema": {"entity": "supplier", "version": 1},
     "batch": {"index": 0, "count": 3}, "is_full_snapshot": true,
     "owned": {"name": "text", "rating": "real"}, "keep_existing_if_null": ["rating"],
     "records": [{"source": "s", "source_id": "1", "idempotency_key": "<sha256>",
                  "fields": {"name": "Alpha", "rating": 4.5}}]}

Answer (JSON, status 200)::

    {"inserted": 1, "updated": 0, "unchanged": 0, "replayed": false,
     "absent": [{"source": "s", "source_id": "9"}]}
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from catalog_sync.errors import SinkError
from catalog_sync.http import encode_json_body
from catalog_sync.models import JsonObject, RecordKey
from catalog_sync.sinks.base import Batch, BatchOrigin, Row, WriteReport, row_key
from catalog_sync.snapshot import canonical_json, sha256_hex

PROTOCOL_VERSION = 1
IDEMPOTENCY_HEADER = "Idempotency-Key"
COUNT_NAMES = ("inserted", "updated", "unchanged")


@dataclass(frozen=True)
class Request:
    """One POST: its body, its ``Idempotency-Key`` and how many records it carries."""

    payload: JsonObject
    key: str
    record_count: int


@dataclass(frozen=True)
class Answer:
    """What the platform said about one request."""

    inserted: int
    updated: int
    unchanged: int
    absent: tuple[RecordKey, ...]
    is_replay: bool

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged


# --- building the requests ----------------------------------------------------------------------


def build_requests(batch: Batch, origin: BatchOrigin, size: int) -> Iterator[Request]:
    chunks = [batch.rows[start : start + size] for start in range(0, len(batch.rows), size)]
    for index, rows in enumerate(chunks):
        payload = build_payload(batch, origin, rows, index=index, count=len(chunks))
        yield Request(payload, request_key(payload), len(rows))


def build_payload(
    batch: Batch, origin: BatchOrigin, rows: Sequence[Row], *, index: int, count: int
) -> JsonObject:
    names = sorted(batch.owned)
    return {
        "protocol": PROTOCOL_VERSION,
        "run_id": origin.run_id,
        "schema": {"entity": origin.entity, "version": origin.schema_version},
        "batch": {"index": index, "count": count},
        "is_full_snapshot": batch.is_full_snapshot,
        "owned": {name: batch.kinds[name].value for name in names},
        "keep_existing_if_null": sorted(batch.keep_existing_if_null),
        "records": [_record(row, names) for row in rows],
    }


def _record(row: Row, names: Sequence[str]) -> JsonObject:
    source, source_id = row_key(row)
    fields = {name: row[name] for name in names}
    return {
        "source": source,
        "source_id": source_id,
        "idempotency_key": record_key(source, source_id, fields),
        "fields": fields,
    }


def record_key(source: str, source_id: str, fields: Mapping[str, object]) -> str:
    """sha256 of the record's identity and a hash of the values it owns: the same record with the
    same content always has the same key, and any change in an owned value changes it."""
    return sha256_hex(canonical_json([source, source_id, sha256_hex(canonical_json(fields))]))


def request_key(payload: Mapping[str, object]) -> str:
    """The ``Idempotency-Key`` of a request: the sha256 of the exact bytes of its body."""
    return sha256_hex(encode_json_body(payload))


# --- reading the answers ------------------------------------------------------------------------


def parse_answer(reply: Mapping[str, object], *, sent: int, where: str) -> Answer:
    """Check the platform's answer to a request that carried `sent` records."""
    inserted, updated, unchanged = (_count(reply, name, where) for name in COUNT_NAMES)
    answer_total = inserted + updated + unchanged
    if answer_total != sent:
        raise SinkError(
            f"the platform's answer to {where} accounts for {answer_total} record(s), but {sent} "
            "were sent: its counts must add up, or records were dropped without a word"
        )
    replayed = reply.get("replayed", False)
    if not isinstance(replayed, bool):
        raise SinkError(f"the platform's answer to {where} has a 'replayed' that is not true/false")
    return Answer(inserted, updated, unchanged, _absent(reply.get("absent", []), where), replayed)


def _count(reply: Mapping[str, object], name: str, where: str) -> int:
    value = reply.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SinkError(
            f"the platform's answer to {where} lacks a count: '{name}' must be a "
            f"non-negative integer, got {value!r}"
        )
    return value


def _absent(value: object, where: str) -> tuple[RecordKey, ...]:
    if not isinstance(value, list):
        raise SinkError(f"the platform's answer to {where} has an 'absent' that is not a list")
    keys: list[RecordKey] = []
    for entry in value:
        source = entry.get("source") if isinstance(entry, dict) else None
        source_id = entry.get("source_id") if isinstance(entry, dict) else None
        if not isinstance(source, str) or not isinstance(source_id, str):
            raise SinkError(
                f"the platform's answer to {where} lists an absent record without a "
                "string 'source' and 'source_id'"
            )
        keys.append((source, source_id))
    return tuple(keys)


def combine_answers(answers: Sequence[Answer]) -> WriteReport:
    """Add up the requests of one load. A replayed request was applied by an earlier one, not by
    this load: its records count as unchanged, because sending it again changed nothing."""
    inserted = sum(a.inserted for a in answers if not a.is_replay)
    updated = sum(a.updated for a in answers if not a.is_replay)
    unchanged = sum(a.unchanged if not a.is_replay else a.total for a in answers)
    absent = tuple(dict.fromkeys(key for answer in answers for key in answer.absent))
    replays = sum(1 for answer in answers if answer.is_replay)
    notes: tuple[str, ...] = ()
    if replays:
        notes = (
            f"{replays} request(s) were replays the platform had already applied (same "
            "idempotency key): counted as unchanged",
        )
    return WriteReport(inserted, updated, unchanged, absent, notes)
