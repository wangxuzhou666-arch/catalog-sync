"""docs/platform-contract.md shows a request and its answer; the reference receiver must agree.

A contract that nothing runs rots: the example in the document is fed, as written, to the receiver
that implements it. The `Idempotency-Key` is not in the document (it is a header); it is computed
the way the document says, as the sha256 of the compact body.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from catalog_sync.http import encode_json_body
from catalog_sync.mock.platform import INGEST_PATH, PlatformService
from catalog_sync.mock.service import Request
from catalog_sync.sinks.http_wire import record_key
from catalog_sync.snapshot import sha256_hex
from tests.conftest import REPO_ROOT

CONTRACT = REPO_ROOT / "docs" / "platform-contract.md"
TOKEN = "doc-example-canary-" + "8e41"


def documented(marker: str) -> dict[str, object]:
    text = CONTRACT.read_text(encoding="utf-8")
    block = re.search(rf"<!-- contract:{marker} -->\n```json\n(.*?)```", text, re.S)
    assert block is not None, f"the contract has no {marker} example"
    parsed = json.loads(block.group(1))
    assert isinstance(parsed, dict)
    return parsed


def post(service: PlatformService, body: bytes) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": sha256_hex(body)}
    reply = service.handle(Request("POST", INGEST_PATH, headers, "platform.test", body))
    assert reply.status == 200, reply.body
    parsed = json.loads(reply.body)
    assert isinstance(parsed, dict)
    return parsed


def test_the_documented_request_is_accepted_and_answered_as_documented(tmp_path: Path) -> None:
    service = PlatformService(tmp_path / "doc.db", token=TOKEN)
    body = encode_json_body(documented("request"))
    assert post(service, body) == documented("response")


def test_the_same_bytes_again_are_a_replay_that_changes_nothing(tmp_path: Path) -> None:
    service = PlatformService(tmp_path / "doc.db", token=TOKEN)
    body = encode_json_body(documented("request"))
    post(service, body)
    assert post(service, body) == {**documented("response"), "replayed": True}


def test_the_record_keys_in_the_document_are_the_ones_the_code_computes() -> None:
    request = documented("request")
    for record in request["records"]:  # type: ignore[attr-defined]
        assert record["idempotency_key"] == record_key(
            record["source"], record["source_id"], record["fields"]
        )
