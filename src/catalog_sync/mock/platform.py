"""The reference receiver: a stand-in for your platform that implements docs/platform-contract.md.

``catalog-sync mock platform serve`` runs it. It exists for three reasons: to try the HTTP sink
with nothing of your own to point it at, to give the contract suite a real platform to run
against, and to be read by whoever builds the real receiving side. It checks every rule of the
contract on the way in (so a sink bug is a 4xx here, not a quiet corruption) and stores into
SQLite with the partial-upsert contract of the other sinks (see platform_store.py).

Like ``MockService`` it is a pure function from a request to a response; ``platform_server.py``
is the HTTP skin around it. What it answers:

* ``POST /api/v1/ingest``: a batch (the body of the HTTP sink's protocol, see sinks/http_wire.py)
  with an ``Authorization: Bearer`` token and an ``Idempotency-Key``. 200 with the counts; 400
  malformed JSON or no key; 401 wrong token; 413 body too large (the server); 422 the request
  breaks the contract (the answer says which rule).
* ``GET /health``: 200, no token needed, for a container's health check.
"""

from __future__ import annotations

import hmac
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from catalog_sync.mock.platform_store import Ingest, PlatformStore
from catalog_sync.mock.service import Request, Response
from catalog_sync.models import JsonObject
from catalog_sync.schema import FieldKind
from catalog_sync.sinks.base import Batch, BatchOrigin
from catalog_sync.sinks.http_wire import IDEMPOTENCY_HEADER, PROTOCOL_VERSION, record_key
from catalog_sync.snapshot import sha256_hex

TOKEN_ENV = "PLATFORM_TOKEN"  # noqa: S105 — the name of an environment variable
# Demo-only default, like the Mock SaaS's account: the server refuses to listen beyond loopback
# while it is in place, so it never protects anything but a local demo.
DEMO_TOKEN = "demo-platform-token"  # noqa: S105 — demo-only default, not a secret
INGEST_PATH = "/api/v1/ingest"
HEALTH_PATH = "/health"
MAX_BODY_BYTES = 16 * 1024 * 1024
_ENTITY = re.compile(r"[a-z][a-z0-9_]{0,40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_JSON = "application/json"
_HTTP_OK = 200
_HTTP_BAD_REQUEST = 400
_HTTP_UNAUTHORIZED = 401
_HTTP_NOT_FOUND = 404
_HTTP_METHOD_NOT_ALLOWED = 405
_HTTP_UNPROCESSABLE = 422
_BEARER = "Bearer "


class Rejected(Exception):
    """A request the platform refuses, with the status and the rule it broke."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class PlatformEvent:
    """One thing the platform did, for its log and for tests. Identifiers and counts only."""

    kind: str  # "ingested", "replayed" or "rejected"
    status: int
    key: str | None
    run_id: str | None
    batch: str | None  # "2/3"
    records: int
    detail: str = ""

    def line(self) -> str:
        key = f" key {self.key[:12]}" if self.key else ""
        place = f" run {self.run_id} request {self.batch}" if self.run_id and self.batch else ""
        tail = f": {self.detail}" if self.detail else ""
        return f"{self.kind}{key}{place}, {self.records} record(s), HTTP {self.status}{tail}"


class PlatformService:
    def __init__(
        self,
        db_path: Path,
        *,
        token: str,
        on_event: Callable[[PlatformEvent], None] | None = None,
    ) -> None:
        self.store = PlatformStore(db_path)
        self._token = token
        self._on_event = on_event
        self._lock = threading.Lock()  # one writer at a time, like the database behind it
        self._events: list[PlatformEvent] = []

    @property
    def events(self) -> list[PlatformEvent]:
        with self._lock:
            return list(self._events)

    def handle(self, request: Request) -> Response:
        if request.path == HEALTH_PATH and request.method == "GET":
            return _json(_HTTP_OK, {"status": "ok"})
        if request.path != INGEST_PATH:
            return _json(_HTTP_NOT_FOUND, {"error": "no such endpoint"})
        if request.method != "POST":
            return _json(_HTTP_METHOD_NOT_ALLOWED, {"error": "POST only"}, (("Allow", "POST"),))
        try:
            return self._ingest(request)
        except Rejected as refusal:
            self._record(
                PlatformEvent("rejected", refusal.status, None, None, None, 0, refusal.message)
            )
            headers = (
                (("WWW-Authenticate", "Bearer"),) if refusal.status == _HTTP_UNAUTHORIZED else ()
            )
            return _json(refusal.status, {"error": refusal.message}, headers)

    def _ingest(self, request: Request) -> Response:
        self._check_token(request)
        key = request.header(IDEMPOTENCY_HEADER)
        if not key:
            raise Rejected(_HTTP_BAD_REQUEST, f"the {IDEMPOTENCY_HEADER} header is required")
        if key != sha256_hex(request.body):  # the exact bytes received, not a re-encoding of them
            raise Rejected(
                _HTTP_UNPROCESSABLE,
                f"the {IDEMPOTENCY_HEADER} must be the sha256 of the request body, as the "
                "contract says",
            )
        ingest = parse_ingest(_decode(request.body), key)
        with self._lock:
            outcome = self.store.ingest(ingest)
        batch = f"{ingest.batch_index + 1}/{ingest.batch_count}"
        self._record(
            PlatformEvent(
                "replayed" if outcome.is_replay else "ingested",
                _HTTP_OK,
                key,
                ingest.run_id,
                batch,
                len(ingest.batch.rows),
                "answered from the stored result, nothing applied" if outcome.is_replay else "",
            )
        )
        return _json(_HTTP_OK, outcome.response)

    def _check_token(self, request: Request) -> None:
        sent = request.header("Authorization") or ""
        offered = sent.removeprefix(_BEARER) if sent.startswith(_BEARER) else ""
        if not hmac.compare_digest(offered.encode(), self._token.encode()):
            raise Rejected(_HTTP_UNAUTHORIZED, "missing or wrong bearer token")

    def _record(self, event: PlatformEvent) -> None:
        with self._lock:
            self._events.append(event)
        if self._on_event is not None:
            self._on_event(event)


# --- reading a request --------------------------------------------------------------------------


def _decode(body: bytes) -> JsonObject:
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise Rejected(_HTTP_BAD_REQUEST, "the body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise Rejected(_HTTP_BAD_REQUEST, "the body must be a JSON object")
    return payload


def parse_ingest(payload: Mapping[str, object], key: str) -> Ingest:
    """Check a request against the contract and turn it into something to apply."""
    if payload.get("protocol") != PROTOCOL_VERSION:
        raise _unprocessable(f"'protocol' must be {PROTOCOL_VERSION}")
    run_id = _text(payload, "run_id")
    schema = _mapping(payload, "schema")
    entity = _text(schema, "entity")
    if _ENTITY.fullmatch(entity) is None:
        raise _unprocessable(f"schema.entity must match {_ENTITY.pattern}")
    version = _whole_number(schema, "version", "schema.version")
    position = _mapping(payload, "batch")
    index = _whole_number(position, "index", "batch.index")
    count = _whole_number(position, "count", "batch.count")
    if not 0 <= index < count:
        raise _unprocessable("batch.index must be at least 0 and less than batch.count")
    is_full_snapshot = payload.get("is_full_snapshot")
    if not isinstance(is_full_snapshot, bool):
        raise _unprocessable("'is_full_snapshot' must be true or false")
    owned = _owned_kinds(payload)
    keep = _text_list(payload, "keep_existing_if_null")
    rows = [_row(entry, owned) for entry in _list(payload, "records")]
    try:
        batch = Batch(
            rows,
            owned=frozenset(owned),
            kinds=owned,
            keep_existing_if_null=frozenset(keep),
            origin=BatchOrigin(entity, version, run_id),
        )
    except ValueError as exc:
        raise _unprocessable(str(exc)) from exc
    return Ingest(key, run_id, entity, index, count, is_full_snapshot, batch)


def _owned_kinds(payload: Mapping[str, object]) -> dict[str, FieldKind]:
    declared = _mapping(payload, "owned")
    kinds = {kind.value: kind for kind in FieldKind}
    owned: dict[str, FieldKind] = {}
    for name, kind in declared.items():
        if not isinstance(kind, str) or kind not in kinds:
            raise _unprocessable(f"owned.{name} must be one of {', '.join(kinds)}")
        owned[name] = kinds[kind]
    return owned


def _row(entry: object, owned: Mapping[str, FieldKind]) -> dict[str, object]:
    if not isinstance(entry, dict):
        raise _unprocessable("every record must be a JSON object")
    source, source_id = _text(entry, "source"), _text(entry, "source_id")
    fields = _mapping(entry, "fields")
    if set(fields) != set(owned):
        raise _unprocessable(
            f"record {source_id!r}: 'fields' must hold exactly the owned fields {sorted(owned)}"
        )
    claimed = entry.get("idempotency_key")
    if not isinstance(claimed, str) or _SHA256.fullmatch(claimed) is None:
        raise _unprocessable(f"record {source_id!r}: 'idempotency_key' must be a sha256 in hex")
    if claimed != record_key(source, source_id, fields):
        raise _unprocessable(
            f"record {source_id!r}: 'idempotency_key' is not the sha256 of its identity and "
            "owned values"
        )
    return {"source": source, "source_id": source_id, **fields}


def _unprocessable(message: str) -> Rejected:
    return Rejected(_HTTP_UNPROCESSABLE, message)


def _text(parent: Mapping[str, object], name: str) -> str:
    value = parent.get(name)
    if not isinstance(value, str) or not value:
        raise _unprocessable(f"'{name}' must be a non-empty string")
    return value


def _whole_number(parent: Mapping[str, object], name: str, where: str) -> int:
    value = parent.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _unprocessable(f"{where} must be a non-negative integer")
    return value


def _mapping(parent: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = parent.get(name)
    if not isinstance(value, dict):
        raise _unprocessable(f"'{name}' must be a JSON object")
    return value


def _list(parent: Mapping[str, object], name: str) -> list[object]:
    value = parent.get(name)
    if not isinstance(value, list):
        raise _unprocessable(f"'{name}' must be a list")
    return value


def _text_list(parent: Mapping[str, object], name: str) -> list[str]:
    values = _list(parent, name)
    if not all(isinstance(value, str) for value in values):
        raise _unprocessable(f"'{name}' must be a list of strings")
    return [value for value in values if isinstance(value, str)]


def _json(status: int, payload: object, headers: tuple[tuple[str, str], ...] = ()) -> Response:
    return Response(
        status, json.dumps(payload, separators=(",", ":")).encode("utf-8"), _JSON, headers
    )
