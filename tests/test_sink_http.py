"""The HTTP sink: what it sends, how it retries, what it refuses, and what it never reveals.

The platform on the other end is scripted (the shared fake transport, one clock for client and
service), so every byte the sink puts on the wire is compared. The sink against a platform that
really implements the contract is in test_sink_http_contract.py.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

from catalog_sync.errors import AuthExpired, ConfigError, SinkError
from catalog_sync.schema import FieldKind
from catalog_sync.sinks.base import Batch, BatchOrigin, Row
from catalog_sync.sinks.http import HttpSink, HttpSinkSettings
from catalog_sync.sinks.http_wire import record_key, request_key
from catalog_sync.sinks.sensitivity import SensitivePolicy, apply_policy
from catalog_sync.snapshot import canonical_json, sha256_hex
from catalog_sync.testing.fake_transport import FakeResponse, RecordedRequest
from catalog_sync.testing.world import ServiceWorld

URL = "https://platform.example/api/ingest"
AT = "@"
TOKEN = "tok-canary-" + "7f3a9c1e"  # split so that scanners do not take it for a secret
ORIGIN = BatchOrigin("supplier", 3, "run-20261026")
KINDS = {"name": FieldKind.TEXT, "rating": FieldKind.REAL, "tags": FieldKind.JSON}
ENV = {"PLATFORM_TOKEN": TOKEN}


def answer(inserted: int = 0, updated: int = 0, unchanged: int = 0, **more: object) -> FakeResponse:
    return FakeResponse.json(
        {"inserted": inserted, "updated": updated, "unchanged": unchanged, **more}
    )


def row(number: int, **fields: object) -> Row:
    defaults: dict[str, object] = {"name": f"Supplier {number}", "rating": 4.5, "tags": ["a"]}
    return {"source": "demo", "source_id": f"s{number:03d}", **defaults, **fields}


def batch_of(
    rows: Iterable[Row],
    *,
    owned: Iterable[str] = ("name", "rating", "tags"),
    keep: Iterable[str] = (),
    is_full_snapshot: bool = False,
) -> Batch:
    return Batch(
        list(rows),
        owned=frozenset(owned),
        kinds=KINDS,
        keep_existing_if_null=frozenset(keep),
        is_full_snapshot=is_full_snapshot,
        origin=ORIGIN,
    )


def make_sink(
    world: ServiceWorld, environ: Mapping[str, str] | None = None, **options: object
) -> HttpSink:
    settings = HttpSinkSettings.from_options(
        {"url": URL, "auth_header_env": "PLATFORM_TOKEN", **options}
    )
    return HttpSink(settings, seams=world.seams, environ=ENV if environ is None else environ)


def loaded(sink: HttpSink, batch: Batch) -> object:
    sink.open()
    sink.backup()
    try:
        return sink.upsert(batch)
    finally:
        sink.close()


def body_of(request: RecordedRequest) -> dict[str, object]:
    payload = request.json_body()
    assert isinstance(payload, dict)
    return payload


# --- the options ----------------------------------------------------------------------------------


def test_the_only_required_option_is_the_url_and_the_defaults_are_conservative() -> None:
    settings = HttpSinkSettings.from_options({"url": URL})
    assert settings == HttpSinkSettings(url=URL)
    assert (settings.batch_size, settings.auth_header, settings.auth_scheme) == (
        100,
        "Authorization",
        "Bearer",
    )
    assert settings.auth_header_env is None and settings.allow_plain_http is False
    with pytest.raises(ConfigError, match="'url' is required"):
        HttpSinkSettings.from_options({})


def test_a_misspelt_option_is_an_error_not_a_default() -> None:
    with pytest.raises(ConfigError, match=r"unknown option\(s\) \['batchsize'\]"):
        HttpSinkSettings.from_options({"url": URL, "batchsize": 5})


@pytest.mark.parametrize(
    "name",
    ["token", "api_key", "apikey", "secret", "password", "authorization", "bearer", "headers"],
)
def test_a_token_inline_in_the_config_is_refused_like_a_postgres_connection_string(
    name: str,
) -> None:
    with pytest.raises(
        ConfigError, match=f"there is no '{name}' option.*auth_header_env"
    ) as caught:
        HttpSinkSettings.from_options({"url": URL, name: "inline-" + "value-9z"})
    assert "inline-" + "value-9z" not in str(caught.value)


@pytest.mark.parametrize(
    "secret", [TOKEN, "Bearer abc.def", "has space", "dash-ed", "9starts_with_digit", ""]
)
def test_auth_header_env_takes_a_variable_name_and_never_echoes_what_looks_like_a_secret(
    secret: str,
) -> None:
    with pytest.raises(ConfigError, match="must be the NAME of an environment variable") as caught:
        HttpSinkSettings.from_options({"url": URL, "auth_header_env": secret})
    assert secret not in str(caught.value) or secret == ""


@pytest.mark.parametrize(
    "url",
    [
        f"https://user:hunter2{AT}platform.example/ingest",
        f"https://:hunter2{AT}platform.example/ingest",
    ],
)
def test_credentials_in_the_url_are_refused_without_being_echoed(url: str) -> None:
    with pytest.raises(ConfigError, match="must not carry a user name or password") as caught:
        HttpSinkSettings.from_options({"url": url})
    assert "hunter2" not in str(caught.value)


@pytest.mark.parametrize("name", ["token", "api_key", "access_key", "secret", "sig", "X-Auth"])
def test_a_secret_looking_query_parameter_is_refused(name: str) -> None:
    with pytest.raises(ConfigError, match="query parameter that looks like a secret") as caught:
        HttpSinkSettings.from_options({"url": f"{URL}?{name}=abc123"})
    assert "abc123" not in str(caught.value)


def test_an_innocent_query_parameter_is_fine() -> None:
    assert HttpSinkSettings.from_options({"url": f"{URL}?tenant=acme&dry=0"}).url.endswith("dry=0")


@pytest.mark.parametrize("url", ["ftp://platform.example/x", "platform.example/x", "https:///x"])
def test_only_an_http_or_https_address_is_a_url(url: str) -> None:
    with pytest.raises(ConfigError, match="must be an http"):
        HttpSinkSettings.from_options({"url": url})


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8080/i", "http://localhost/i", "http://[::1]:9/i", "https://p.example/i"],
)
def test_the_token_travels_over_https_or_loopback_only(url: str) -> None:
    assert HttpSinkSettings.from_options({"url": url}).url == url


def test_plain_http_to_another_machine_needs_to_be_asked_for_by_name() -> None:
    plain = "http://platform.internal:8080/ingest"
    with pytest.raises(ConfigError, match=r"plain http.*allow_plain_http = true"):
        HttpSinkSettings.from_options({"url": plain})
    allowed = HttpSinkSettings.from_options({"url": plain, "allow_plain_http": True})
    assert allowed.allow_plain_http is True


@pytest.mark.parametrize(("size", "ok"), [(1, True), (1000, True), (0, False), (1001, False)])
def test_the_batch_size_has_bounds(size: int, ok: bool) -> None:
    if ok:
        assert HttpSinkSettings.from_options({"url": URL, "batch_size": size}).batch_size == size
    else:
        with pytest.raises(ConfigError, match="'batch_size' must be an integer between 1 and 1000"):
            HttpSinkSettings.from_options({"url": URL, "batch_size": size})


def test_the_registry_builds_the_sink_from_its_entry_point(tmp_path: Path) -> None:
    from catalog_sync import registry

    sink = registry.sink_factory("http")({"url": URL}, base_dir=tmp_path)
    assert isinstance(sink, HttpSink) and sink.settings.url == URL


# --- what goes on the wire ------------------------------------------------------------------------


def test_one_request_carries_the_owned_fields_the_keep_list_the_schema_and_the_run() -> None:
    world = ServiceWorld(lambda request: answer(inserted=2))
    sink = make_sink(world)
    extra = {"internal_note": "never sent"}  # a field of the row that the batch does not own
    batch = batch_of(
        [row(1, **extra), row(2)], owned=("name", "rating"), keep=("rating",), is_full_snapshot=True
    )
    report = loaded(sink, batch)

    [request] = world.transport.requests
    assert (request.method, request.url) == ("POST", URL)
    payload = body_of(request)
    assert payload == {
        "protocol": 1,
        "run_id": "run-20261026",
        "schema": {"entity": "supplier", "version": 3},
        "batch": {"index": 0, "count": 1},
        "is_full_snapshot": True,
        "owned": {"name": "text", "rating": "real"},
        "keep_existing_if_null": ["rating"],
        "records": [
            {
                "source": "demo",
                "source_id": "s001",
                "idempotency_key": record_key(
                    "demo", "s001", {"name": "Supplier 1", "rating": 4.5}
                ),
                "fields": {"name": "Supplier 1", "rating": 4.5},
            },
            {
                "source": "demo",
                "source_id": "s002",
                "idempotency_key": record_key(
                    "demo", "s002", {"name": "Supplier 2", "rating": 4.5}
                ),
                "fields": {"name": "Supplier 2", "rating": 4.5},
            },
        ],
    }
    assert "internal_note" not in (request.body or b"").decode(), "only owned fields leave"
    assert (report.inserted, report.updated, report.unchanged) == (2, 0, 0)  # type: ignore[attr-defined]


def test_the_bytes_are_compact_and_the_header_is_the_sha256_of_exactly_those_bytes() -> None:
    world = ServiceWorld(lambda request: answer(inserted=1))
    loaded(make_sink(world), batch_of([row(1)]))
    [request] = world.transport.requests
    assert request.body is not None and b", " not in request.body and b'": ' not in request.body
    assert request.header("Idempotency-Key") == sha256_hex(request.body)
    assert request.header("Idempotency-Key") == request_key(body_of(request))
    assert request.header("content-type") == "application/json"
    assert (request.header("user-agent") or "").startswith("catalog-sync/")


def test_a_record_key_follows_its_identity_and_what_it_owns_and_nothing_else() -> None:
    same = record_key("demo", "s1", {"name": "A", "rating": 1.0})
    assert same == sha256_hex(
        canonical_json(["demo", "s1", sha256_hex(canonical_json({"name": "A", "rating": 1.0}))])
    )
    assert record_key("demo", "s1", {"rating": 1.0, "name": "A"}) == same, (
        "field order is not content"
    )
    assert record_key("demo", "s1", {"name": "A", "rating": 1.1}) != same
    assert record_key("demo", "s2", {"name": "A", "rating": 1.0}) != same
    assert record_key("other", "s1", {"name": "A", "rating": 1.0}) != same, "the source is identity"
    assert record_key("demo", "s1", {"name": "A"}) != same, "a different owned set is different"


def test_the_same_batch_twice_is_the_same_request_and_a_changed_value_is_not() -> None:
    world = ServiceWorld(lambda request: answer(unchanged=1))
    sink = make_sink(world)
    loaded(sink, batch_of([row(1)]))
    loaded(sink, batch_of([row(1)]))
    loaded(sink, batch_of([row(1, name="Renamed")]))
    first, again, changed = (r.header("Idempotency-Key") for r in world.transport.requests)
    assert first == again != changed


def test_records_go_out_in_batches_in_order_and_each_says_where_it_stands() -> None:
    world = ServiceWorld(lambda request: answer(inserted=len(body_of(request)["records"])))  # type: ignore[arg-type]
    sink = make_sink(world, batch_size=100)
    report = loaded(sink, batch_of([row(n) for n in range(250)], is_full_snapshot=True))

    bodies = [body_of(r) for r in world.transport.requests]
    assert [(b["batch"], len(b["records"])) for b in bodies] == [  # type: ignore[arg-type]
        ({"index": 0, "count": 3}, 100),
        ({"index": 1, "count": 3}, 100),
        ({"index": 2, "count": 3}, 50),
    ]
    sent = [r["source_id"] for b in bodies for r in b["records"]]  # type: ignore[attr-defined]
    assert sent == [f"s{n:03d}" for n in range(250)]
    assert all(b["is_full_snapshot"] is True for b in bodies)
    assert len({r.header("Idempotency-Key") for r in world.transport.requests}) == 3
    assert report.inserted == 250  # type: ignore[attr-defined]


def test_an_empty_batch_sends_nothing() -> None:
    world = ServiceWorld(lambda request: answer())
    report = loaded(make_sink(world), batch_of([]))
    assert world.transport.requests == []
    assert (report.inserted, report.updated, report.unchanged) == (0, 0, 0)  # type: ignore[attr-defined]


def test_a_batch_that_does_not_say_where_it_came_from_is_refused() -> None:
    world = ServiceWorld(lambda request: answer())
    sink = make_sink(world)
    bare = Batch([row(1)], owned=frozenset({"name"}), kinds=KINDS)
    sink.open()
    sink.backup()
    with pytest.raises(SinkError, match="where a batch came from"):
        sink.upsert(bare)
    assert world.transport.requests == []


# --- the sensitive-field policy applies before anything is sent -----------------------------------


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (SensitivePolicy.DROP, None),
        (SensitivePolicy.HASH, "sha256:"),
        (SensitivePolicy.KEEP, "owner@example.com"),
    ],
)
def test_a_sensitive_field_is_dropped_hashed_or_kept_as_the_policy_says(
    policy: SensitivePolicy, expected: str | None
) -> None:
    world = ServiceWorld(lambda request: answer(inserted=1))
    contact = {"contact": "owner@example.com"}
    kinds = {**KINDS, "contact": FieldKind.TEXT}
    batch = Batch(
        [row(1, **contact)],
        owned=frozenset({"name", "contact"}),
        kinds=kinds,
        origin=ORIGIN,
    )
    safe = apply_policy(batch, frozenset({"contact"}), policy, lambda: b"workspace-salt")
    assert safe.origin == ORIGIN, "the policy keeps where the batch came from"
    loaded(make_sink(world), safe)

    [request] = world.transport.requests
    payload = body_of(request)
    [record] = payload["records"]  # type: ignore[misc]
    if expected is None:
        assert "contact" not in payload["owned"] and "contact" not in record["fields"]  # type: ignore[operator]
        assert "owner@example.com" not in (request.body or b"").decode()
    elif expected == "sha256:":
        assert record["fields"]["contact"].startswith("sha256:")
        assert "owner@example.com" not in (request.body or b"").decode()
    else:
        assert record["fields"]["contact"] == expected


# --- the token ------------------------------------------------------------------------------------


def test_the_token_goes_in_the_authorization_header_from_the_named_variable() -> None:
    world = ServiceWorld(lambda request: answer(inserted=1))
    loaded(make_sink(world), batch_of([row(1)]))
    [request] = world.transport.requests
    assert request.header("authorization") == f"Bearer {TOKEN}"
    assert TOKEN not in (request.body or b"").decode() and TOKEN not in request.url


def test_another_header_and_no_scheme_for_an_api_key_style_platform() -> None:
    world = ServiceWorld(lambda request: answer(inserted=1))
    sink = make_sink(world, auth_header="X-Api-Key", auth_scheme="none")
    loaded(sink, batch_of([row(1)]))
    [request] = world.transport.requests
    assert request.header("x-api-key") == TOKEN and request.header("authorization") is None


def test_a_platform_that_wants_no_token_gets_none() -> None:
    world = ServiceWorld(lambda request: answer(inserted=1))
    sink = HttpSink(HttpSinkSettings(url=URL), seams=world.seams, environ={})
    loaded(sink, batch_of([row(1)]))
    assert world.transport.requests[0].header("authorization") is None


@pytest.mark.parametrize("environ", [{}, {"PLATFORM_TOKEN": ""}])
def test_a_named_variable_that_is_not_set_stops_the_load_before_a_request(
    environ: Mapping[str, str],
) -> None:
    world = ServiceWorld(lambda request: answer(inserted=1))
    sink = make_sink(world, environ=environ)
    sink.open()
    sink.backup()
    with pytest.raises(ConfigError, match=r"environment variable PLATFORM_TOKEN is not set"):
        sink.upsert(batch_of([row(1)]))
    assert world.transport.requests == []


# --- reading the answer ---------------------------------------------------------------------------


def test_the_counts_of_every_request_are_added_up_and_absent_rows_are_collected() -> None:
    script = iter(
        [
            answer(inserted=1, updated=1),
            answer(unchanged=2, absent=[{"source": "demo", "source_id": "gone"}]),
        ]
    )
    world = ServiceWorld(lambda request: next(script))
    report = loaded(make_sink(world, batch_size=2), batch_of([row(n) for n in range(4)]))
    assert (report.inserted, report.updated, report.unchanged) == (1, 1, 2)  # type: ignore[attr-defined]
    assert report.absent == (("demo", "gone"),)  # type: ignore[attr-defined]


def test_a_replayed_request_counts_as_unchanged_because_this_load_changed_nothing_by_it() -> None:
    world = ServiceWorld(lambda request: answer(inserted=2, replayed=True))
    report = loaded(make_sink(world), batch_of([row(1), row(2)]))
    assert (report.inserted, report.updated, report.unchanged) == (0, 0, 2)  # type: ignore[attr-defined]
    assert "replay" in report.notes[0]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("reply", "complaint"),
    [
        ({"inserted": 1, "updated": 0}, "'unchanged' must be a non-negative integer, got None"),
        ({"inserted": True, "updated": 0, "unchanged": 0}, "'inserted' must be a non-negative"),
        ({"inserted": -1, "updated": 1, "unchanged": 1}, "'inserted' must be a non-negative"),
        ({"inserted": "1", "updated": 0, "unchanged": 0}, "'inserted' must be a non-negative"),
        ({"inserted": 1, "updated": 0, "unchanged": 0}, "accounts for 1 record.*but 2 were sent"),
        ({"inserted": 2, "updated": 1, "unchanged": 0}, "accounts for 3 record.*but 2 were sent"),
        (
            {"inserted": 2, "updated": 0, "unchanged": 0, "replayed": "yes"},
            "'replayed' that is not true/false",
        ),
        (
            {"inserted": 2, "updated": 0, "unchanged": 0, "absent": {"a": 1}},
            "'absent' that is not a list",
        ),
        (
            {"inserted": 2, "updated": 0, "unchanged": 0, "absent": [{"source": "x"}]},
            "absent record without a string",
        ),
    ],
)
def test_an_answer_that_does_not_account_for_the_records_sent_is_an_error(
    reply: dict[str, object], complaint: str
) -> None:
    world = ServiceWorld(lambda request: FakeResponse.json(reply))
    with pytest.raises(SinkError, match=complaint):
        loaded(make_sink(world), batch_of([row(1), row(2)]))


def test_a_success_status_with_a_body_that_is_not_the_contracts_json_is_an_error() -> None:
    world = ServiceWorld(lambda request: FakeResponse(200, b"<html>ok</html>"))
    with pytest.raises(SinkError, match="not with the JSON object the contract defines"):
        loaded(make_sink(world), batch_of([row(1)]))
    assert len(world.transport.requests) == 1, "a malformed answer is not retried"


# --- retries and refusals -------------------------------------------------------------------------


def test_a_5xx_is_retried_with_the_same_bytes_and_the_same_key_until_it_goes_through() -> None:
    script = iter([FakeResponse(503), FakeResponse(502), answer(inserted=1)])
    world = ServiceWorld(lambda request: next(script))
    report = loaded(make_sink(world), batch_of([row(1)]))
    attempts = world.transport.requests
    assert len(attempts) == 3
    assert (
        len({r.body for r in attempts}) == 1
        and len({r.header("Idempotency-Key") for r in attempts}) == 1
    )
    assert world.clock.sleeps == [1.0, 2.0], "exponential backoff, on the shared fake clock"
    assert report.inserted == 1  # type: ignore[attr-defined]


def test_a_429_waits_as_told() -> None:
    script = iter([FakeResponse(429, headers={"Retry-After": "7"}), answer(inserted=1)])
    world = ServiceWorld(lambda request: next(script))
    loaded(make_sink(world), batch_of([row(1)]))
    assert world.clock.sleeps == [7.0]


def test_a_platform_that_keeps_failing_ends_the_load_and_says_nothing_was_applied() -> None:
    world = ServiceWorld(lambda request: FakeResponse(500))
    with pytest.raises(
        SinkError, match=r"HTTP 500.*request 1 of 1.*none applied.*Loading again is safe"
    ):
        loaded(make_sink(world), batch_of([row(1)]))
    assert len(world.transport.requests) == 6, "six attempts, then it gives up"


def test_a_refusal_shows_the_start_of_the_platforms_explanation_with_secrets_masked() -> None:
    explanation = f'{{"error": "field rating must be a number", "echo": "Bearer {TOKEN}"}}'
    world = ServiceWorld(lambda request: FakeResponse(422, explanation.encode()))
    with pytest.raises(SinkError, match=r"refused request 1 of 1 \(HTTP 422\)") as caught:
        loaded(make_sink(world), batch_of([row(1)]))
    message = str(caught.value)
    assert "field rating must be a number" in message
    assert TOKEN not in message, "a token the platform echoed back is masked"
    assert len(world.transport.requests) == 1, "a 4xx is final: retrying repeats the refusal"


def test_a_secret_the_platform_echoes_as_json_or_a_dict_is_masked_too() -> None:
    """The explanation is the platform's text, so it may repeat what we sent in any rendering."""
    explanation = (
        f'{{"error": "bad row", "token": "{TOKEN}", "request": {{\'password\': \'hunter2\'}}, '
        f'"api_key": "{TOKEN}"}}'
    )
    world = ServiceWorld(lambda request: FakeResponse(422, explanation.encode()))
    with pytest.raises(SinkError, match=r"refused request 1 of 1") as caught:
        loaded(make_sink(world), batch_of([row(1)]))
    message = str(caught.value)
    assert "bad row" in message
    assert TOKEN not in message
    assert "hunter2" not in message


def test_a_long_explanation_is_cut_short() -> None:
    world = ServiceWorld(lambda request: FakeResponse(400, b"x" * 5000))
    with pytest.raises(SinkError) as caught:
        loaded(make_sink(world), batch_of([row(1)]))
    assert len(str(caught.value)) < 700


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_stops_the_run_at_once_with_exit_code_2_and_says_what_to_check(
    status: int,
) -> None:
    world = ServiceWorld(lambda request: FakeResponse(status, b'{"error": "nope"}'))
    with pytest.raises(
        AuthExpired, match=r"PLATFORM_TOKEN holds a token the platform accepts"
    ) as e:
        loaded(make_sink(world), batch_of([row(1)]))
    assert e.value.exit_code == 2 and TOKEN not in str(e.value)
    assert len(world.transport.requests) == 1


def test_a_redirect_is_not_followed_because_the_token_would_go_with_it() -> None:
    world = ServiceWorld(
        lambda request: FakeResponse(302, headers={"Location": "https://evil.example/"})
    )
    with pytest.raises(SinkError, match=r"redirect.*not followed.*token would travel"):
        loaded(make_sink(world), batch_of([row(1)]))
    assert [r.url for r in world.transport.requests] == [URL]


def test_the_second_request_failing_says_the_first_was_applied() -> None:
    script = iter([answer(inserted=1), FakeResponse(400, b"bad record")])
    world = ServiceWorld(lambda request: next(script))
    with pytest.raises(
        SinkError, match=r"request 2 of 2.*the 1 request\(s\) before it were applied"
    ):
        loaded(make_sink(world, batch_size=1), batch_of([row(1), row(2)]))


def test_a_network_failure_is_retried_then_reported_with_where_it_stopped() -> None:
    import requests

    world = ServiceWorld(lambda request: requests.exceptions.ConnectionError("refused"))  # type: ignore[arg-type,return-value]
    with pytest.raises(SinkError, match=r"request 1 of 1 did not get through.*ConnectionError"):
        loaded(make_sink(world), batch_of([row(1)]))


def test_the_rate_ceiling_holds_across_requests() -> None:
    world = ServiceWorld(lambda request: answer(inserted=1))
    sink = make_sink(world, batch_size=1, max_requests_per_minute=2)
    loaded(sink, batch_of([row(n) for n in range(3)]))
    assert sum(world.clock.sleeps) > 0, "the third request in a minute had to wait for the window"


# --- the lifecycle: backup and dry run ------------------------------------------------------------


def test_the_sink_says_it_takes_no_backup_and_whose_job_recovery_is() -> None:
    world = ServiceWorld(lambda request: answer())
    sink = make_sink(world)
    assert sink.backup_supported is False and sink.dry_run_predicts_counts is False
    sink.open()
    receipt = sink.backup()
    assert receipt.path is None and receipt.table is None and receipt.rows_before == 0
    assert receipt.note is not None and "receiving platform" in receipt.note
    assert receipt.describe() == receipt.note


def test_a_load_without_the_backup_call_is_refused_as_for_every_other_sink() -> None:
    world = ServiceWorld(lambda request: answer())
    sink = make_sink(world)
    sink.open()
    with pytest.raises(SinkError, match="call backup\\(\\) before upsert"):
        sink.upsert(batch_of([row(1)]))


def test_a_dry_run_builds_the_requests_and_counts_them_but_sends_nothing() -> None:
    world = ServiceWorld(lambda request: answer())
    sink = make_sink(world, environ={}, batch_size=100)  # no token needed: nothing is sent
    sink.open()
    report = sink.upsert(batch_of([row(n) for n in range(250)]), dry_run=True)
    assert world.transport.requests == []
    assert (report.inserted, report.updated, report.unchanged) == (0, 0, 0)
    [note] = report.notes
    assert "250 record(s) in 3 request(s)" in note and URL in note and "Nothing was sent" in note


def test_a_dry_run_still_refuses_what_could_not_be_serialised() -> None:
    world = ServiceWorld(lambda request: answer())
    sink = make_sink(world)
    sink.open()
    bad = batch_of([row(1)])
    object.__setattr__(bad, "rows", [row(1, rating=float("nan"))])  # past Batch's own check
    with pytest.raises(ValueError, match="Out of range float"):
        sink.upsert(bad, dry_run=True)


def test_nothing_the_sink_prints_or_raises_carries_the_token() -> None:
    world = ServiceWorld(lambda request: FakeResponse(500, TOKEN.encode()))
    with pytest.raises(SinkError) as caught:
        loaded(make_sink(world), batch_of([row(1)]))
    assert TOKEN not in str(caught.value)
    assert json.dumps(str(caught.value)).count(TOKEN) == 0
