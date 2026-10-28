# Platform contract: what the HTTP sink sends and what your platform must do

The **HTTP sink** (`kind = "http"`) loads normalized records into a platform you run by `POST`ing
JSON to one endpoint. This page is the contract between the two: the exact request and answer, the
upsert semantics the receiving side has to implement, the status codes, and how to check that
yours does. Version 1 of the protocol.

What the records hold (fields, empty values, issues, provenance): [data-contract.md](data-contract.md).

1. [The short version](#the-short-version)
2. [The request](#the-request)
3. [What the platform must do](#what-the-platform-must-do)
4. [The answer](#the-answer)
5. [Status codes](#status-codes)
6. [Backups and recovery](#backups-and-recovery)
7. [Verify your platform](#verify-your-platform)
8. [The reference receiver](#the-reference-receiver)
9. [Security notes](#security-notes)

## The short version

- One endpoint, `POST`, JSON in and JSON out, authenticated with a token the platform issued.
- A request carries a batch of records and says which fields **the mapping owns**. The platform
  writes those fields and **only** those: every other column or field of the record is left
  alone.
- A null in an owned field is written as null, unless the field is listed in
  `keep_existing_if_null`: then a stored non-null value is kept.
- **Nothing is ever deleted.** Records the source stopped returning may be reported back; they are
  not removed.
- Every request has an `Idempotency-Key`. Seeing a key again, the platform answers with the stored
  result and applies nothing.
- A request is all or nothing.

## The request

```
POST <url>
Authorization: Bearer <token>
Idempotency-Key: <sha256 of the body, in hex>
Content-Type: application/json
Accept: application/json
User-Agent: catalog-sync/<version>
```

`<url>` and the token are the sink's `url` and the environment variable it names (`auth_header_env`;
`auth_header` and `auth_scheme` change the header and the word before the token). The body is one
JSON object. This example is a real request, shown indented for reading; the bytes on the wire are
compact (no spaces after `,` or `:`, UTF-8), and the `Idempotency-Key` is the sha256 of those bytes.
A test feeds this block to the reference receiver, so it cannot drift from the code:

<!-- contract:request -->
```json
{
  "protocol": 1,
  "run_id": "20261026T093000Z-a1b2c3",
  "schema": {"entity": "supplier", "version": 1},
  "batch": {"index": 0, "count": 1},
  "is_full_snapshot": true,
  "owned": {"certifications": "json", "name": "text", "rating": "real"},
  "keep_existing_if_null": ["rating"],
  "records": [
    {
      "source": "demo",
      "source_id": "sup-001",
      "idempotency_key": "13b152606304b7763a166612a5285c2d7b33bda84a1159c90dfbcba9db6fdacf",
      "fields": {"certifications": ["ISO 9001", "OEKO-TEX"], "name": "Northwind Textiles", "rating": 4.5}
    },
    {
      "source": "demo",
      "source_id": "sup-002",
      "idempotency_key": "0121cb27f976d015c25cb965fe6a7bdcf355b6581b833d2a321d0ec930992a27",
      "fields": {"certifications": [], "name": "Harbor Mills", "rating": null}
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `protocol` | `1`. A platform that does not know the number refuses with 422. |
| `run_id` | The pull the records come from. The same for every request of one load. |
| `schema.entity`, `schema.version` | The record type (`material`, `supplier`, your own) and its `schema_version`, which is bumped when a field changes meaning. A platform may refuse a version it has not been taught (422). |
| `batch.index`, `batch.count` | This is request `index + 1` of `count`. Requests of one load are sent in order, one at a time. |
| `is_full_snapshot` | `true` when the records of the whole load are everything the source currently returns. Only then can the platform tell which stored records the source has stopped returning. |
| `owned` | The fields this load writes, each with the kind it is stored as: `text`, `integer`, `real`, `timestamp` (ISO 8601 with an offset), `json` (any JSON value: lists, objects, money, compositions). A platform with its own schema ignores the kinds. |
| `keep_existing_if_null` | A subset of `owned`. See below. |
| `records[].source`, `.source_id` | The record's key: the pair, never `source_id` alone (two sources may reuse an id). |
| `records[].fields` | **Exactly** the owned fields, every one present. A field that is absent from a record never means null; a null is `null`. |
| `records[].idempotency_key` | sha256 of the record's identity and of a hash of its owned values (see [Idempotency](#idempotency)). Optional for a platform to use. |

Fields the mapping does not own, and fields a sensitive-field policy dropped, are not in the request
at all. A field the policy *hashed* arrives as a `text` value `sha256:<hex>`.

## What the platform must do

These rules are the sink contract of every sink in catalog-sync (the SQL and file sinks implement
them locally); an HTTP platform implements them on its side. The shared contract suite checks each
one (see [Verify your platform](#verify-your-platform)).

1. **Owned fields only.** For a record, write the fields named in `owned` and nothing else. A
   column the platform added itself (a stock level, a note, a status) is never touched, whatever
   the request holds.
2. **Absent is not null.** Every owned field is present in every record, so there is nothing to
   guess. (A platform may refuse a record that lacks one: 422.)
3. **Null is written, unless kept.** An owned field whose value is `null` is stored as null.
   Except fields listed in `keep_existing_if_null`: there, `null` (and for a `json` field, an empty
   list or object) means "the source gave nothing", and a stored value that is not null is kept. A
   non-null incoming value always replaces the stored one.
4. **Upsert in place, never delete.** If the key exists, update that record where it is, do not
   delete and re-insert it (that would cascade-delete dependent rows in a relational store). If it
   does not exist, create it with the owned fields. No request, whatever it holds, removes a record.
5. **Report, do not remove.** When `is_full_snapshot` is true and the request completes the load
   (see below), the platform *may* list the stored records of the load's sources that the load did
   not contain, in `absent`. They stay.
6. **All or nothing.** A request is applied completely or not at all. Validate everything first;
   write in one transaction.
7. **Idempotency.** See below.
8. **Types.** A value that does not fit its declared kind (a string in a `real` field) is a 422
   for the whole request, not a silent coercion.

### Idempotency

`Idempotency-Key` is the sha256 (hex) of the exact bytes of the body. The platform stores, for each
key it has applied, the answer it gave. A request whose key it already knows is **not applied
again**: it answers with the stored result and `"replayed": true`.

This is what makes retries safe: the sink retries a request that timed out, and a load can be run
again after a crash, with the same bytes and so the same key. Because the key is a hash of the
body, the same key never arrives with different content, and a platform can treat the key as
opaque or verify it (the reference receiver does, and answers 422 when they differ). Keep the
stored answers for at least a day; after they expire, a repeated request is simply applied again,
which the rules above make harmless.

A replay is how the sink knows nothing changed: it reports the records of a replayed request as
`unchanged`.

`records[].idempotency_key` identifies a record *at a given content*. A platform that stores it per
record can skip a record whose key it has already applied. The contract does not require that:
comparing values is always correct.

### Which request completes a load

To report `absent`, the platform has to know that it has seen the whole load. It keeps, per
`run_id`, the keys and the `batch.index` values it has received; the request that makes
`index` 0 to `count - 1` complete (normally the last one, because they arrive in order) gets the
`absent` list in its answer. Before that, `absent` is empty: "not in the load" would be a guess.
This is optional. A platform that does not track runs answers `"absent": []` always, and the sink
reports nothing missing.

## The answer

Status `200`, a JSON object:

<!-- contract:response -->
```json
{"inserted": 2, "updated": 0, "unchanged": 0, "replayed": false, "absent": []}
```

That is the reference receiver's answer to the request above on an empty database (a test checks
it). Sent again, the same bytes get the same answer with `"replayed": true`. When a full snapshot
is complete and the platform tracks runs, `absent` lists what the load did not contain:
`"absent": [{"source": "demo", "source_id": "sup-009"}]`.

| Field | Meaning |
|---|---|
| `inserted`, `updated`, `unchanged` | Non-negative integers that **add up to the number of records in the request**. A record is *updated* when applying it changed something, *unchanged* when it was already as sent. The sink refuses an answer whose counts do not add up: records dropped without a word would otherwise go unnoticed. |
| `replayed` | `true` when the key was already applied and this is the stored answer. Optional, false by default. |
| `absent` | `[{"source": ..., "source_id": ...}]`, as described above. Optional. |

## Status codes

| Status | When | What the sink does |
|---|---|---|
| `200` | Applied, or replayed. | Reads the counts. |
| `400` | The body is not JSON, or there is no `Idempotency-Key`. | Stops: `SinkError` with the start of the answer's body. |
| `401`, `403` | The token is missing, wrong or not allowed. | Stops at once, exit code 2, and says which variable to check. Never retried. |
| `404`, `405`, `409`, `410`, ... | The endpoint is wrong, or a conflict. | Stops: `SinkError` with the start of the body. |
| `413` | The body is too large. | Stops. Lower `batch_size`. |
| `422` | The request breaks the contract (wrong type, unknown protocol or schema version, key mismatch, ...). | Stops: `SinkError` with the start of the body, secrets masked. Put the broken rule in the body: it is what the operator reads. |
| `429` | Too many requests. | Waits for `Retry-After` (seconds, capped at two minutes) and retries. |
| `5xx` | The platform failed. | Retries with exponential backoff (six attempts), then stops. |
| `3xx` | A redirect. | Not followed (the HTTP client never lets the library follow one, and a POST is never re-sent): the token would travel to the new address. Stops, and says to use the final address as `url`. |

Every other 4xx is final: the sink never retries a request the platform understood and refused.
When a request fails, the ones before it *were* applied; the sink says so, and running the load
again is safe.

## Backups and recovery

Other sinks copy what they are about to change before a load (`backup()`). The HTTP sink cannot:
the records are not on its side. Its `backup()` returns a documented no-op receipt, and the load
report says `backup: none taken: the rows live on the receiving platform`. Taking a snapshot
before an ingest, keeping history, or being able to restore, is the **platform's responsibility**.
A sink declares this with `backup_supported = False`; the contract suite then checks the
declaration instead of backed-up rows.

Likewise a `--dry-run` cannot ask the platform what would change. It builds every request, counts
records and requests, prints that, and sends nothing (not even a "validate only" call).

## Verify your platform

Two checks, in order of effort.

**By hand, with curl.** Sending the example above to the reference receiver
(`catalog-sync mock platform serve`, [below](#the-reference-receiver)), or to yours:

```console
$ KEY=$(shasum -a 256 request.json | cut -d' ' -f1)      # request.json: the compact body
$ curl -sS -X POST "$URL" -H "Authorization: Bearer $PLATFORM_TOKEN" \
      -H "Idempotency-Key: $KEY" -H "Content-Type: application/json" --data-binary @request.json
```

Send it twice: the second answer must say `"replayed": true` and change nothing.

**With the contract suite.** The same checks that run against every built-in sink run against
yours through the HTTP sink, with a harness that tells the suite how to read your platform's
records back:

```python
import pytest

from catalog_sync.sinks.http import HttpSink, HttpSinkSettings
from catalog_sync.testing.sink_contract import CONTRACT_CHECKS, SinkHarness


def make_harness(workdir):
    sink = HttpSink(
        HttpSinkSettings(
            url="https://staging.platform.example/api/ingest", auth_header_env="PLATFORM_TOKEN"
        )
    )
    return SinkHarness(sink, read_rows=read_rows_from_my_platform, read_backup=no_backup)


@pytest.mark.parametrize("check", CONTRACT_CHECKS, ids=lambda check: check.__name__)
def test_my_platform_honours_the_contract(check, tmp_path):
    check(make_harness, tmp_path)
```

`read_rows_from_my_platform()` returns `{(source, source_id): {field: value}}` for the contract's
entity (the checks write a made-up schema and the `material` and `supplier` ones; use a staging
environment, never production). `no_backup` may raise: a sink with `backup_supported = False` is never
asked for one.

## The reference receiver

`catalog-sync mock platform serve` is a platform that implements this contract and stores into
SQLite. It is for trying the HTTP sink with nothing of your own to point it at, for the contract
suite to run against, and for reading when you build the real thing. It is not production: no TLS,
one writer at a time, and it keeps every idempotency record forever.

```console
$ PLATFORM_TOKEN=pick-a-token catalog-sync mock platform serve --db out/platform.db
mock platform listening on http://127.0.0.1:8780 (pid 4242)
  send batches to: http://127.0.0.1:8780/api/v1/ingest
  token: from $PLATFORM_TOKEN
  stores into: out/platform.db
```

| Option | Meaning |
|---|---|
| `--db PATH` | The SQLite file it stores into (default `./platform.db`, created readable by you only). One table per entity, named after `schema.entity`, keyed by `(source, source_id)`, with a column per owned field. Three `_cs_*` tables hold the idempotency answers and the per-run bookkeeping. |
| `--port N`, `--host H` | Default `127.0.0.1:8780`. `--port 0` picks a free port. |
| `--token-env NAME` | The environment variable that holds the token (default `PLATFORM_TOKEN`). When it is not set, the token is the public demo token `demo-platform-token`, announced on start, and the server then refuses to listen beyond loopback. |
| `--detach --pid-file F` | Run in the background; `catalog-sync mock stop --pid-file F` ends it. |
| `--verbose` | Log every request. Replays and refusals are always logged. |

It answers `GET /health` (no token) for a container's health check, and `POST /api/v1/ingest`.
It checks every rule above on the way in, so a bug in a sender is a 422 with the rule named, not a
quiet corruption, and it logs what it did without content or tokens:

```
replayed key 011d6706aa17 run 20261026T093000Z-a1b2c3 request 1/1, 2 record(s), HTTP 200: answered from the stored result, nothing applied
```

Its rows are written by the SQLite sink's own upsert, which is how it honours the partial-upsert
contract by construction. Point the sink at it:

```toml
[sinks.platform]
kind = "http"
url = "http://127.0.0.1:8780/api/v1/ingest"
auth_header_env = "PLATFORM_TOKEN"
```

## Security notes

- **Use https.** The token is a bearer credential. The sink refuses plain `http://` except for the
  machine's own loopback address; `allow_plain_http = true` is for a private network you control.
- **The token lives in the environment, not in the config.** `auth_header_env` names the variable.
  A config file that holds the token, a user name and password in the URL, or a secret-looking query
  parameter is refused, and the refusal does not repeat the value.
- **Give the token the least it needs**: write access to this ingest endpoint. Rotate it like any
  credential. A `401` or `403` stops the run with exit code 2.
- **Do not log request bodies**, and treat the stored idempotency answers as data of the same
  sensitivity as the records.
- **Bound what you accept**: reject bodies beyond a size (`413`), and rate-limit (`429` with
  `Retry-After`); the sink honours both.
- **Sensitive fields** are dropped (the default), hashed or kept before the request is built, by
  the sink's `sensitive_fields` option. Check what your mapping marks `sensitive: true` before you
  switch it to `keep`.
