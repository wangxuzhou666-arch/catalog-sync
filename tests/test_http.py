from __future__ import annotations

import logging
from dataclasses import replace

import pytest
import requests

from catalog_sync.errors import (
    AuthExpired,
    HttpStatusError,
    NetworkError,
    ResponseTooLarge,
    SourceShapeError,
)
from catalog_sync.http import HttpClient, HttpSeams, HttpSettings, RateLimiter, strip_query
from catalog_sync.testing.fake_transport import FakeClock, FakeResponse, FakeTransport

URL = "https://api.example.invalid/v1/things"
OK = FakeResponse.json({"state": True})


def make_client(
    transport: FakeTransport,
    clock: FakeClock,
    *,
    credentialed: bool = True,
    headers: dict[str, str] | None = None,
    limiter: RateLimiter | None = None,
    **settings: float,
) -> HttpClient:
    base = HttpSettings(gap_s=0.0, max_attempts=4)
    return HttpClient(
        headers or {"x-test": "1"},
        credentialed=credentialed,
        settings=replace(base, **settings),
        renewal_hint="run: catalog-sync login demo",
        seams=HttpSeams(adapter=transport, sleep=clock.sleep, monotonic=clock.monotonic),
        limiter=limiter,
    )


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# --- what goes on the wire ----------------------------------------------------------------------


def test_a_get_sends_exactly_the_configured_headers_and_nothing_that_identifies_the_library(
    transport: FakeTransport, clock: FakeClock
) -> None:
    transport.queue(OK)
    client = make_client(transport, clock, headers={"user-agent": "Generic/1.0", "x-a": "b"})
    client.get_json(URL, params={"id": 7, "type": "t"})

    (sent,) = transport.requests
    assert sent.method == "GET"
    assert sent.url == f"{URL}?id=7&type=t"
    assert {k.lower(): v for k, v in sent.headers.items()} == {
        "user-agent": "Generic/1.0",
        "x-a": "b",
        "accept-encoding": "gzip, deflate",
    }
    assert sent.body is None


def test_a_post_sends_compact_json_in_insertion_order(
    transport: FakeTransport, clock: FakeClock
) -> None:
    transport.queue(OK)
    client = make_client(transport, clock, headers={"content-type": "application/json"})
    client.post_json(URL, body={"mode": "m", "page": 2, "name": "café"})

    (sent,) = transport.requests
    assert sent.body == '{"mode":"m","page":2,"name":"café"}'.encode()
    assert sent.header("content-type") == "application/json"
    assert sent.header("content-length") == str(len(sent.body))


def test_per_request_headers_are_added_to_the_session_headers(
    transport: FakeTransport, clock: FakeClock
) -> None:
    transport.queue(OK)
    make_client(transport, clock).get_json(URL, headers={"region": "r1"})
    assert transport.requests[0].header("region") == "r1"
    assert transport.requests[0].header("x-test") == "1"


# --- retry policy ---------------------------------------------------------------------------------


def test_network_errors_are_retried_with_exponential_backoff(
    transport: FakeTransport, clock: FakeClock
) -> None:
    transport.queue(
        requests.exceptions.ConnectionError("reset"), requests.exceptions.ReadTimeout(), OK
    )
    make_client(transport, clock).get_json(URL)
    assert len(transport.requests) == 3
    assert clock.sleeps == [1.0, 2.0]


def test_a_network_error_that_never_clears_is_raised_after_the_last_attempt(
    transport: FakeTransport, clock: FakeClock
) -> None:
    transport.queue(*[requests.exceptions.ConnectionError("x")] * 4)
    with pytest.raises(NetworkError, match="ConnectionError after 4 attempt"):
        make_client(transport, clock).get_json(URL)
    assert len(transport.requests) == 4
    assert clock.sleeps == [1.0, 2.0, 4.0], "no sleep after the final attempt"


def test_5xx_is_retried_and_the_last_status_is_raised(
    transport: FakeTransport, clock: FakeClock
) -> None:
    transport.queue(*[FakeResponse(503)] * 4)
    with pytest.raises(HttpStatusError) as failure:
        make_client(transport, clock).get_json(URL)
    assert failure.value.status == 503
    assert len(transport.requests) == 4


def test_a_5xx_followed_by_success_returns_the_success(
    transport: FakeTransport, clock: FakeClock
) -> None:
    transport.queue(FakeResponse(500), FakeResponse(502), OK)
    assert make_client(transport, clock).get_json(URL) == {"state": True}
    assert clock.sleeps == [1.0, 2.0]


def test_backoff_is_capped(transport: FakeTransport, clock: FakeClock) -> None:
    transport.queue(*[FakeResponse(500)] * 6, OK)
    client = make_client(transport, clock, max_attempts=7, backoff_cap_s=5.0)
    client.get_json(URL)
    assert clock.sleeps == [1.0, 2.0, 4.0, 5.0, 5.0, 5.0]


def test_429_honours_retry_after_instead_of_the_computed_backoff(
    transport: FakeTransport, clock: FakeClock
) -> None:
    transport.queue(FakeResponse(429, headers={"Retry-After": "7"}), OK)
    make_client(transport, clock).get_json(URL)
    assert clock.sleeps == [7.0]


def test_a_huge_retry_after_is_capped(transport: FakeTransport, clock: FakeClock) -> None:
    transport.queue(FakeResponse(429, headers={"Retry-After": "86400"}), OK)
    make_client(transport, clock, retry_after_cap_s=60.0).get_json(URL)
    assert clock.sleeps == [60.0]


@pytest.mark.parametrize("value", ["soon", "-5", "nan", "inf", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_an_unusable_retry_after_falls_back_to_backoff(
    transport: FakeTransport, clock: FakeClock, value: str
) -> None:
    transport.queue(FakeResponse(503, headers={"Retry-After": value}), OK)
    make_client(transport, clock).get_json(URL)
    assert clock.sleeps == [1.0]


@pytest.mark.parametrize("status", [400, 404, 410, 422])
def test_other_4xx_are_final_and_never_retried(
    transport: FakeTransport, clock: FakeClock, status: int
) -> None:
    transport.queue(FakeResponse(status), OK)
    with pytest.raises(HttpStatusError) as failure:
        make_client(transport, clock).get_json(URL)
    assert failure.value.status == status
    assert failure.value.is_item_scoped
    assert len(transport.requests) == 1
    assert clock.sleeps == []


def test_429_is_not_item_scoped_and_5xx_is_not_either() -> None:
    assert not HttpStatusError(429, "x").is_item_scoped
    assert not HttpStatusError(503, "x").is_item_scoped


@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_status_is_one_call_then_auth_expired_with_the_hint(
    transport: FakeTransport, clock: FakeClock, status: int
) -> None:
    transport.queue(FakeResponse(status), OK)
    with pytest.raises(AuthExpired, match=rf"HTTP {status}.*run: catalog-sync login demo"):
        make_client(transport, clock).get_json(URL)
    assert len(transport.requests) == 1


def test_an_uncredentialed_client_reports_403_as_a_plain_status_error(
    transport: FakeTransport, clock: FakeClock
) -> None:
    transport.queue(FakeResponse(403))
    with pytest.raises(HttpStatusError) as failure:
        make_client(transport, clock, credentialed=False).get_bytes(URL, max_bytes=10)
    assert failure.value.status == 403
    assert failure.value.is_item_scoped


def test_errors_that_are_not_transient_are_translated_without_retry(
    transport: FakeTransport, clock: FakeClock
) -> None:
    transport.queue(requests.exceptions.SSLError("bad cert"))
    with pytest.raises(NetworkError, match="TLS failure"):
        make_client(transport, clock).get_json(URL)
    transport.queue(requests.exceptions.InvalidURL("nope"))
    with pytest.raises(NetworkError, match="InvalidURL"):
        make_client(transport, clock).get_json(URL)
    assert len(transport.requests) == 2


# --- pacing ---------------------------------------------------------------------------------------


def test_consecutive_requests_are_spaced_by_the_gap(
    transport: FakeTransport, clock: FakeClock
) -> None:
    transport.queue(OK, OK, OK)
    client = make_client(transport, clock, gap_s=0.3)
    client.get_json(URL)
    assert clock.sleeps == [], "the first request does not wait"
    client.get_json(URL)
    client.get_json(URL)
    assert clock.sleeps == pytest.approx([0.3, 0.3])


def test_time_already_spent_counts_towards_the_gap(
    transport: FakeTransport, clock: FakeClock
) -> None:
    transport.queue(OK, OK)
    client = make_client(transport, clock, gap_s=0.3)
    client.get_json(URL)
    clock.now += 1.0
    client.get_json(URL)
    assert clock.sleeps == []


# --- redirects ------------------------------------------------------------------------------------


def test_a_credentialed_client_never_follows_a_redirect(
    transport: FakeTransport, clock: FakeClock
) -> None:
    transport.queue(FakeResponse(302, headers={"Location": "https://other.example.invalid/x"}))
    with pytest.raises(HttpStatusError) as failure:
        make_client(transport, clock).get_json(URL)
    assert failure.value.status == 302
    assert len(transport.requests) == 1


def test_an_uncredentialed_client_does_not_follow_one_either_unless_it_is_given_a_rule(
    transport: FakeTransport, clock: FakeClock
) -> None:
    """The rules for following (every hop vetted and paced) are in test_http_redirects.py."""
    transport.queue(FakeResponse(302, headers={"Location": "https://cdn.example.invalid/real"}))
    client = make_client(transport, clock, credentialed=False)
    with pytest.raises(HttpStatusError) as failure:
        client.get_bytes(URL, max_bytes=100)
    assert failure.value.status == 302
    assert len(transport.requests) == 1


# --- bodies ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("body", [b"<html>maintenance</html>", b"", b"[1, 2]", b'"text"'])
def test_a_body_that_is_not_a_json_object_is_a_shape_error(
    transport: FakeTransport, clock: FakeClock, body: bytes
) -> None:
    transport.queue(FakeResponse(200, body))
    with pytest.raises(SourceShapeError):
        make_client(transport, clock).get_json(URL)


def test_get_bytes_returns_the_body(transport: FakeTransport, clock: FakeClock) -> None:
    transport.queue(FakeResponse(200, b"x" * 100))
    assert make_client(transport, clock).get_bytes(URL, max_bytes=100) == b"x" * 100


def test_a_declared_length_over_the_limit_is_refused_before_reading(
    transport: FakeTransport, clock: FakeClock
) -> None:
    transport.queue(FakeResponse(200, b"x", headers={"Content-Length": "5000"}))
    with pytest.raises(ResponseTooLarge, match="announces 5000"):
        make_client(transport, clock).get_bytes(URL, max_bytes=100)


def test_a_body_that_outgrows_the_limit_is_cut_off(
    transport: FakeTransport, clock: FakeClock
) -> None:
    transport.queue(FakeResponse(200, b"x" * 200_000))
    with pytest.raises(ResponseTooLarge, match="larger than the limit"):
        make_client(transport, clock).get_bytes(URL, max_bytes=100_000)


# --- nothing secret in logs or messages ---------------------------------------------------------


def test_logs_and_errors_contain_neither_header_values_nor_query_strings(
    transport: FakeTransport, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    secret_header = "canary-header-" + "a1b2c3"
    secret_query = "canary-sig-" + "d4e5f6"
    signed = f"{URL}?sig={secret_query}"
    transport.queue(FakeResponse(503), FakeResponse(404))
    client = make_client(transport, clock, headers={"authorization": secret_header})
    caplog.set_level(logging.DEBUG)

    with pytest.raises(HttpStatusError) as failure:
        client.get_bytes(signed, max_bytes=10)

    everything = caplog.text + str(failure.value)
    assert secret_header not in everything
    assert secret_query not in everything
    assert "api.example.invalid/v1/things" in everything, "the log is still useful"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://h.example/p/q?sig=abc&x=1#frag", "https://h.example/p/q"),
        ("https://user:pw" + "@h.example:8443/p", "https://h.example:8443/p"),
        ("https://h.example/p", "https://h.example/p"),
        ("https://h.example", "https://h.example"),
    ],
)
def test_strip_query_removes_everything_that_can_carry_a_secret(url: str, expected: str) -> None:
    assert strip_query(url) == expected


@pytest.mark.parametrize(
    "settings",
    [
        {"gap_s": -1.0},
        {"timeout_s": 0.0},
        {"max_attempts": 0},
        {"backoff_base_s": 0.0},
        {"retry_after_cap_s": -1.0},
    ],
)
def test_nonsense_settings_are_rejected(settings: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must"):
        HttpSettings(**settings)  # type: ignore[arg-type]


# --- the declared request ceiling -----------------------------------------------------------------


def stamped_transport(clock: FakeClock, times: list[float]) -> FakeTransport:
    def respond(_request: object) -> FakeResponse:
        times.append(clock.now)
        return OK

    return FakeTransport(respond)


def busiest_window(times: list[float], window_s: float = 60.0) -> int:
    """The most requests that started within any `window_s` seconds."""
    return max(sum(1 for later in times if start <= later < start + window_s) for start in times)


def test_without_a_limiter_a_burst_is_not_slowed(clock: FakeClock) -> None:
    times: list[float] = []
    client = make_client(stamped_transport(clock, times), clock)
    for _ in range(10):
        client.get_json(URL)
    assert busiest_window(times) == 10, "the control: this is the behaviour the ceiling removes"


def test_the_ceiling_holds_in_every_sliding_window(clock: FakeClock) -> None:
    times: list[float] = []
    limiter = RateLimiter(3, monotonic=clock.monotonic, sleep=clock.sleep)
    client = make_client(stamped_transport(clock, times), clock, limiter=limiter)
    for _ in range(10):
        client.get_json(URL)
    assert busiest_window(times) == 3
    assert times[:7] == [1000.0] * 3 + [1060.0] * 3 + [1120.0]


def test_a_quiet_minute_gives_the_whole_allowance_back(clock: FakeClock) -> None:
    times: list[float] = []
    limiter = RateLimiter(2, monotonic=clock.monotonic, sleep=clock.sleep)
    client = make_client(stamped_transport(clock, times), clock, limiter=limiter)
    client.get_json(URL)
    client.get_json(URL)
    clock.now += 61
    client.get_json(URL)
    client.get_json(URL)
    assert clock.sleeps == [], "nothing had to wait: the earlier requests had left the window"


def test_retries_are_requests_too(transport: FakeTransport, clock: FakeClock) -> None:
    transport.queue(FakeResponse(503), FakeResponse(503), OK)
    limiter = RateLimiter(2, monotonic=clock.monotonic, sleep=clock.sleep)
    make_client(transport, clock, limiter=limiter).get_json(URL)
    assert len(transport.requests) == 3
    assert max(clock.sleeps) >= 55, "the third attempt had to wait for the window to move on"


def test_clients_that_share_a_limiter_share_one_ceiling(clock: FakeClock) -> None:
    times: list[float] = []
    transport = stamped_transport(clock, times)
    limiter = RateLimiter(4, monotonic=clock.monotonic, sleep=clock.sleep)
    api = make_client(transport, clock, limiter=limiter)
    pictures = make_client(transport, clock, credentialed=False, limiter=limiter)
    for _ in range(5):
        api.get_json(URL)
        pictures.get_json(URL)
    assert busiest_window(times) == 4


def test_a_limit_below_one_per_minute_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RateLimiter(0)
