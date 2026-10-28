"""The reference platform: every rule of the contract it enforces, and what it stores.

``PlatformService`` is a pure function from a request to a response, so most of this needs no
socket. The requests are built by the HTTP sink's own wire code (``build_requests``) and then
broken one rule at a time; a rule the receiver does not enforce is a test that fails here.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import stat
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from catalog_sync.errors import ConfigError
from catalog_sync.http import encode_json_body
from catalog_sync.mock.platform import (
    DEMO_TOKEN,
    HEALTH_PATH,
    INGEST_PATH,
    PlatformEvent,
    PlatformService,
)
from catalog_sync.mock.platform_server import PlatformServer
from catalog_sync.mock.service import Request, Response
from catalog_sync.schema import FieldKind
from catalog_sync.sinks.base import Batch, BatchOrigin, Row
from catalog_sync.sinks.http_wire import build_requests, record_key
from catalog_sync.sinks.sqlite import read_table
from catalog_sync.snapshot import sha256_hex

TOKEN = "platform-canary-" + "5d1e"
KINDS = {"name": FieldKind.TEXT, "rating": FieldKind.REAL, "tags": FieldKind.JSON}
ORIGIN = BatchOrigin("supplier", 1, "run-1")


def row(number: int, **fields: object) -> Row:
    defaults: dict[str, object] = {"name": f"Supplier {number}", "rating": 4.5, "tags": ["a"]}
    return {"source": "demo", "source_id": f"s{number}", **defaults, **fields}


def batch_of(
    rows: Iterable[Row],
    *,
    owned: Iterable[str] = ("name", "rating", "tags"),
    keep: Iterable[str] = (),
    is_full_snapshot: bool = False,
    run_id: str = "run-1",
) -> Batch:
    return Batch(
        list(rows),
        owned=frozenset(owned),
        kinds=KINDS,
        keep_existing_if_null=frozenset(keep),
        is_full_snapshot=is_full_snapshot,
        origin=BatchOrigin("supplier", 1, run_id),
    )


def post(
    service: PlatformService,
    body: bytes,
    *,
    key: str | None = None,
    token: str | None = TOKEN,
    path: str = INGEST_PATH,
    method: str = "POST",
) -> Response:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    headers["Idempotency-Key"] = sha256_hex(body) if key is None else key
    return service.handle(Request(method, path, headers, "platform.test", body))


def send(service: PlatformService, batch: Batch, size: int = 100) -> list[dict[str, object]]:
    """Every request of the batch, in order; the answers as dictionaries."""
    answers = []
    for request in build_requests(batch, batch.origin or ORIGIN, size):
        reply = post(service, encode_json_body(request.payload), key=request.key)
        assert reply.status == 200, reply.body
        answers.append(json.loads(reply.body))
    return answers


@pytest.fixture
def service(tmp_path: Path) -> PlatformService:
    return PlatformService(tmp_path / "platform.db", token=TOKEN)


def table(
    service: PlatformService, name: str = "supplier"
) -> dict[tuple[str, str], dict[str, object]]:
    return read_table(service.store.path, name)


def single_request(batch: Batch) -> dict[str, object]:
    [request] = list(build_requests(batch, batch.origin or ORIGIN, 100))
    return copy.deepcopy(request.payload)


# --- who may ask ----------------------------------------------------------------------------------


def test_health_needs_no_token(service: PlatformService) -> None:
    reply = service.handle(Request("GET", HEALTH_PATH, {}, "platform.test"))
    assert reply.status == 200 and json.loads(reply.body) == {"status": "ok"}


@pytest.mark.parametrize("token", [None, "", "wrong", TOKEN + "x", TOKEN[:-1]])
def test_a_missing_or_wrong_token_is_a_401_and_nothing_is_stored(
    service: PlatformService, token: str | None
) -> None:
    body = encode_json_body(single_request(batch_of([row(1)])))
    reply = post(service, body, token=token)
    assert reply.status == 401 and ("WWW-Authenticate", "Bearer") in reply.headers
    assert table(service) == {}


def test_a_token_in_another_scheme_is_not_a_bearer_token(service: PlatformService) -> None:
    body = encode_json_body(single_request(batch_of([row(1)])))
    headers = {"Authorization": f"Token {TOKEN}", "Idempotency-Key": sha256_hex(body)}
    reply = service.handle(Request("POST", INGEST_PATH, headers, "platform.test", body))
    assert reply.status == 401


def test_other_paths_and_methods_are_refused(service: PlatformService) -> None:
    assert post(service, b"{}", path="/api/v1/other").status == 404
    refused = post(service, b"{}", method="GET")
    assert refused.status == 405 and ("Allow", "POST") in refused.headers


# --- the rules of the contract --------------------------------------------------------------------


def tampered(change: Callable[[dict[str, object]], None]) -> bytes:
    payload = single_request(batch_of([row(1)]))
    change(payload)
    return encode_json_body(payload)


def set_at(path: str, value: object) -> Callable[[dict[str, object]], None]:
    def change(payload: dict[str, object]) -> None:
        *parents, last = path.split(".")
        target: object = payload
        for part in parents:
            target = target[int(part)] if isinstance(target, list) else target[part]  # type: ignore[index]
        if isinstance(target, list):
            target[int(last)] = value
        else:
            target[last] = value  # type: ignore[index]

    return change


def drop(path: str) -> Callable[[dict[str, object]], None]:
    def change(payload: dict[str, object]) -> None:
        *parents, last = path.split(".")
        target: object = payload
        for part in parents:
            target = target[int(part)] if isinstance(target, list) else target[part]  # type: ignore[index]
        del target[last]  # type: ignore[attr-defined]

    return change


@pytest.mark.parametrize(
    ("change", "complaint"),
    [
        (set_at("protocol", 2), "'protocol' must be 1"),
        (drop("run_id"), "'run_id' must be a non-empty string"),
        (set_at("schema.entity", "Bad Name"), "schema.entity must match"),
        (set_at("schema.entity", "x; drop table y"), "schema.entity must match"),
        (set_at("schema.version", "1"), "schema.version must be a non-negative integer"),
        (set_at("batch.index", 1), "batch.index must be at least 0 and less than batch.count"),
        (set_at("batch.count", 0), "batch.index must be at least 0 and less than batch.count"),
        (set_at("is_full_snapshot", "yes"), "'is_full_snapshot' must be true or false"),
        (set_at("owned.rating", "float"), "owned.rating must be one of"),
        (set_at("owned", ["name"]), "'owned' must be a JSON object"),
        (set_at("keep_existing_if_null", ["nope"]), "keep_existing_if_null must be owned"),
        (set_at("keep_existing_if_null", "name"), "'keep_existing_if_null' must be a list"),
        (set_at("records", {}), "'records' must be a list"),
        (set_at("records.0", 5), "every record must be a JSON object"),
        (drop("records.0.source_id"), "'source_id' must be a non-empty string"),
        (drop("records.0.fields.rating"), "must hold exactly the owned fields"),
        (set_at("records.0.fields.extra", 1), "must hold exactly the owned fields"),
        (set_at("records.0.idempotency_key", "abc"), "'idempotency_key' must be a sha256"),
        (set_at("records.0.idempotency_key", "0" * 64), "is not the sha256 of its identity"),
        (set_at("records.0.fields.name", "Edited"), "is not the sha256 of its identity"),
    ],
)
def test_a_request_that_breaks_a_rule_is_a_422_that_names_the_rule(
    service: PlatformService, change: Callable[[dict[str, object]], None], complaint: str
) -> None:
    reply = post(service, tampered(change))
    assert reply.status == 422, reply.body
    assert complaint in json.loads(reply.body)["error"]
    assert table(service) == {}


def rekeyed(payload: dict[str, object]) -> bytes:
    """Fix the record keys after a tamper, so that only the rule under test is broken."""
    for record in payload["records"]:  # type: ignore[attr-defined]
        record["idempotency_key"] = record_key(
            record["source"], record["source_id"], record["fields"]
        )
    return encode_json_body(payload)


def test_a_value_that_does_not_fit_its_declared_kind_is_a_422(service: PlatformService) -> None:
    payload = single_request(batch_of([row(1)]))
    payload["records"][0]["fields"]["rating"] = "high"  # type: ignore[index]
    reply = post(service, rekeyed(payload))
    assert reply.status == 422 and "'rating' is declared" in json.loads(reply.body)["error"]


def test_an_identity_field_cannot_be_owned(service: PlatformService) -> None:
    payload = single_request(batch_of([row(1)]))
    payload["owned"]["source"] = "text"  # type: ignore[index]
    payload["records"][0]["fields"]["source"] = "demo"  # type: ignore[index]
    reply = post(service, rekeyed(payload))
    assert (
        reply.status == 422 and "identity fields cannot be owned" in json.loads(reply.body)["error"]
    )


def test_a_record_twice_in_one_request_is_a_422(service: PlatformService) -> None:
    payload = single_request(batch_of([row(1)]))
    payload["records"] = [payload["records"][0], payload["records"][0]]  # type: ignore[index]
    reply = post(service, encode_json_body(payload))
    assert reply.status == 422 and "only once" in json.loads(reply.body)["error"]


def test_a_body_that_is_not_json_or_not_an_object_is_a_400(service: PlatformService) -> None:
    assert post(service, b"not json").status == 400
    assert post(service, b"[1, 2]").status == 400


def test_no_idempotency_key_is_a_400_and_a_key_that_is_not_the_body_hash_is_a_422(
    service: PlatformService,
) -> None:
    body = encode_json_body(single_request(batch_of([row(1)])))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    missing = service.handle(Request("POST", INGEST_PATH, headers, "platform.test", body))
    assert missing.status == 400 and "Idempotency-Key header is required" in missing.body.decode()
    wrong = post(service, body, key="f" * 64)
    assert wrong.status == 422 and "sha256 of the request body" in wrong.body.decode()
    assert table(service) == {}


def test_the_key_is_checked_against_the_bytes_received_not_a_re_encoding(
    service: PlatformService,
) -> None:
    """A sender that writes the same JSON with spaces has different bytes, so a different key."""
    payload = single_request(batch_of([row(1)]))
    spaced = json.dumps(payload, separators=(", ", ": ")).encode()
    assert post(service, spaced).status == 200, "the key of those very bytes is accepted"
    assert post(service, spaced, key=sha256_hex(encode_json_body(payload))).status == 422


# --- what it stores -------------------------------------------------------------------------------


def test_it_stores_the_owned_fields_and_counts_what_it_did(service: PlatformService) -> None:
    [answer] = send(service, batch_of([row(1), row(2)]))
    assert (answer["inserted"], answer["updated"], answer["unchanged"]) == (2, 0, 0)
    assert answer["replayed"] is False and answer["absent"] == []
    stored = table(service)
    assert stored[("demo", "s1")]["name"] == "Supplier 1"
    assert stored[("demo", "s1")]["tags"] == ["a"]

    [again] = send(service, batch_of([row(1, rating=5.0), row(2)], run_id="run-2"))
    assert (again["inserted"], again["updated"], again["unchanged"]) == (0, 1, 1)
    assert table(service)[("demo", "s1")]["rating"] == 5.0


def test_a_column_the_batch_does_not_own_is_never_touched(service: PlatformService) -> None:
    send(service, batch_of([row(1)]))
    with sqlite3.connect(service.store.path) as conn:
        conn.execute('ALTER TABLE "supplier" ADD COLUMN "stock" REAL')
        conn.execute("UPDATE supplier SET stock = 12.5")
    send(service, batch_of([row(1, name="Renamed")], owned=("name",), run_id="run-2"))
    stored = table(service)[("demo", "s1")]
    assert stored["name"] == "Renamed" and stored["stock"] == 12.5
    assert stored["rating"] == 4.5, "an owned field of an earlier batch, not owned now, is kept"


def test_null_is_written_unless_the_field_keeps_its_value(service: PlatformService) -> None:
    send(service, batch_of([row(1)]))
    send(
        service,
        batch_of([row(1, rating=None, tags=[])], keep=("rating",), run_id="run-2"),
    )
    stored = table(service)[("demo", "s1")]
    assert stored["rating"] == 4.5, "kept: null did not overwrite"
    assert stored["tags"] == [], "not kept: an empty list was written"


def test_rows_are_changed_in_place_so_children_that_cascade_survive(
    service: PlatformService,
) -> None:
    send(service, batch_of([row(1)]))
    with sqlite3.connect(service.store.path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "CREATE TABLE note (id INTEGER PRIMARY KEY, source TEXT, source_id TEXT, text TEXT, "
            "FOREIGN KEY (source, source_id) REFERENCES supplier (source, source_id) "
            "ON DELETE CASCADE)"
        )
        conn.execute("INSERT INTO note (source, source_id, text) VALUES ('demo', 's1', 'keep me')")
    send(service, batch_of([row(1, name="Renamed")], run_id="run-2"))
    with sqlite3.connect(service.store.path) as conn:
        assert conn.execute("SELECT text FROM note").fetchall() == [("keep me",)]


def test_the_database_file_is_private(tmp_path: Path) -> None:
    PlatformService(tmp_path / "private.db", token=TOKEN)
    assert stat.S_IMODE(os.stat(tmp_path / "private.db").st_mode) == 0o600


# --- idempotency ----------------------------------------------------------------------------------


def test_the_same_request_twice_is_applied_once_and_answered_from_the_store(
    service: PlatformService,
) -> None:
    body = encode_json_body(single_request(batch_of([row(1)])))
    first = json.loads(post(service, body).body)
    assert (first["inserted"], first["replayed"]) == (1, False)

    with sqlite3.connect(service.store.path) as conn:  # someone edits the row meanwhile
        conn.execute("UPDATE supplier SET name = 'edited by hand'")
    second = json.loads(post(service, body).body)
    assert (second["inserted"], second["replayed"]) == (1, True), "the stored answer, flagged"
    assert table(service)[("demo", "s1")]["name"] == "edited by hand", "nothing was applied again"


def test_a_replay_is_logged_and_the_log_carries_no_content(service: PlatformService) -> None:
    seen: list[PlatformEvent] = []
    logged = PlatformService(service.store.path, token=TOKEN, on_event=seen.append)
    body = encode_json_body(single_request(batch_of([row(1, name="Secret Name")])))
    post(logged, body)
    post(logged, body)
    post(logged, body, token="wrong")
    assert [(e.kind, e.status) for e in seen] == [
        ("ingested", 200),
        ("replayed", 200),
        ("rejected", 401),
    ]
    lines = [event.line() for event in seen]
    assert "answered from the stored result, nothing applied" in lines[1]
    assert all("Secret Name" not in line and TOKEN not in line for line in lines)
    assert logged.events == seen


def test_the_stored_answers_survive_a_restart(tmp_path: Path) -> None:
    first = PlatformService(tmp_path / "p.db", token=TOKEN)
    body = encode_json_body(single_request(batch_of([row(1)])))
    post(first, body)
    restarted = PlatformService(tmp_path / "p.db", token=TOKEN)
    assert json.loads(post(restarted, body).body)["replayed"] is True


# --- rows the run did not contain -----------------------------------------------------------------


def test_the_last_request_of_a_full_snapshot_reports_the_stored_rows_it_did_not_contain(
    service: PlatformService,
) -> None:
    send(service, batch_of([row(n) for n in range(1, 6)]))
    answers = send(
        service,
        batch_of([row(1), row(2), row(4)], is_full_snapshot=True, run_id="run-2"),
        size=2,
    )
    assert [a["absent"] for a in answers] == [
        [],
        [{"source": "demo", "source_id": "s3"}, {"source": "demo", "source_id": "s5"}],
    ], "only the last request knows what the whole run held"
    assert len(table(service)) == 5, "reported, never deleted"


def test_absent_is_not_guessed_when_a_request_of_the_run_never_arrived(
    service: PlatformService,
) -> None:
    send(service, batch_of([row(n) for n in range(1, 4)]))
    requests = list(
        build_requests(batch_of([row(1), row(2)], is_full_snapshot=True, run_id="run-2"), ORIGIN, 1)
    )
    last = requests[-1]
    reply = post(service, encode_json_body(last.payload), key=last.key)  # request 2 of 2, not 1
    assert json.loads(reply.body)["absent"] == []


def test_an_incremental_load_never_reports_absent_rows(service: PlatformService) -> None:
    send(service, batch_of([row(1), row(2)]))
    [answer] = send(service, batch_of([row(1)], run_id="run-2"))
    assert answer["absent"] == []


def test_only_the_sources_in_the_run_are_compared(service: PlatformService) -> None:
    send(service, batch_of([row(1), {**row(2), "source": "other"}]))
    [answer] = send(service, batch_of([row(1)], is_full_snapshot=True, run_id="run-2"))
    assert answer["absent"] == [], "the 'other' source was not part of this run"


# --- over a real socket ---------------------------------------------------------------------------


@pytest.mark.mock
def test_a_batch_posted_over_loopback_is_stored(tmp_path: Path) -> None:
    import requests

    with PlatformServer(tmp_path / "wire.db", token=TOKEN) as server:
        body = encode_json_body(single_request(batch_of([row(1)])))
        headers = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": sha256_hex(body)}
        reply = requests.post(server.ingest_url, data=body, headers=headers, timeout=5)
        assert reply.status_code == 200 and reply.json()["inserted"] == 1
        assert requests.get(f"{server.base_url}/health", timeout=5).json() == {"status": "ok"}
    assert ("demo", "s1") in read_table(tmp_path / "wire.db", "supplier")


@pytest.mark.mock
def test_a_body_larger_than_the_limit_is_refused_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import requests

    monkeypatch.setattr("catalog_sync.mock.platform_server.MAX_BODY_BYTES", 1000)
    with PlatformServer(tmp_path / "big.db", token=TOKEN) as server:
        reply = requests.post(server.ingest_url, data=b"x" * 5000, timeout=5)
        assert reply.status_code == 413
        assert requests.get(f"{server.base_url}/health", timeout=5).status_code == 200


def test_the_public_demo_token_is_only_accepted_on_loopback(tmp_path: Path) -> None:
    PlatformServer(tmp_path / "a.db", token=DEMO_TOKEN, host="127.0.0.1")
    with pytest.raises(ConfigError, match="the demo token is public"):
        PlatformServer(tmp_path / "b.db", token=DEMO_TOKEN, host="0.0.0.0")  # noqa: S104
    PlatformServer(tmp_path / "c.db", token=TOKEN, host="0.0.0.0")  # noqa: S104
