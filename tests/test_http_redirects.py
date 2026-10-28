"""Redirects are followed by hand or not at all: the picture rule, the pacing and the credentials
are applied to every hop, and a POST (a login form) is never sent twice.

The reviewers' probes found three ways out: a picture host that redirected to another port or to
the cloud metadata address, and a login form whose 307 re-sent the password to a second host.
Each is a test below, on the fake transport (exact bytes) and once on real loopback servers.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import SplitResult

import pytest

from catalog_sync.errors import HttpStatusError, NetworkError, SourceShapeError
from catalog_sync.http import (
    HttpClient,
    HttpSeams,
    HttpSettings,
    RateLimiter,
    is_private_host,
)
from catalog_sync.sdk import https_only, same_host
from catalog_sync.testing.fake_transport import (
    FakeClock,
    FakeResponse,
    FakeTransport,
    RecordedRequest,
)
from tests.test_sdk_http_side import API, CDN, listed, make_pictures

ORIGIN = "https://assets.example.invalid"
START = f"{ORIGIN}/pic.jpg"


def ANYWHERE(_parts: SplitResult) -> bool:
    return True


class CountingLimiter(RateLimiter):
    """A ceiling that is never reached, but counts how often it was asked."""

    def __init__(self) -> None:
        super().__init__(10_000)
        self.acquired = 0

    def acquire(self) -> None:
        self.acquired += 1
        super().acquire()


def redirect(location: str, status: int = 302) -> FakeResponse:
    return FakeResponse(status, headers={"Location": location})


def client_for(
    transport: FakeTransport,
    clock: FakeClock,
    *,
    headers: dict[str, str] | None = None,
    credentialed: bool = False,
    limiter: RateLimiter | None = None,
    gap_s: float = 0.0,
) -> HttpClient:
    return HttpClient(
        headers or {"accept": "image/*"},
        credentialed=credentialed,
        settings=HttpSettings(gap_s=gap_s, max_attempts=2),
        seams=HttpSeams(adapter=transport, sleep=clock.sleep, monotonic=clock.monotonic),
        limiter=limiter,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# --- never by default -----------------------------------------------------------------------------


def test_a_download_that_is_redirected_fails_unless_the_caller_gave_a_rule(
    clock: FakeClock,
) -> None:
    transport = FakeTransport()
    transport.queue(redirect(f"{ORIGIN}/real.jpg"))
    with pytest.raises(HttpStatusError) as failure:
        client_for(transport, clock).get_bytes(START, max_bytes=100)
    assert failure.value.status == 302
    assert [r.url for r in transport.requests] == [START]


def test_a_client_with_a_credential_ignores_the_rule_and_does_not_follow(
    clock: FakeClock,
) -> None:
    transport = FakeTransport()
    transport.queue(redirect(f"{ORIGIN}/real.jpg"))
    client = client_for(transport, clock, credentialed=True)
    with pytest.raises(HttpStatusError):
        client.get_bytes(START, max_bytes=100, redirect_rule=ANYWHERE)
    assert len(transport.requests) == 1


# --- the rule, on every hop -----------------------------------------------------------------------


def test_a_redirect_the_rule_accepts_is_followed(clock: FakeClock) -> None:
    transport = FakeTransport()
    transport.queue(redirect(f"{ORIGIN}/real.jpg"), FakeResponse(200, b"bytes"))
    body = client_for(transport, clock).get_bytes(START, max_bytes=100, redirect_rule=ANYWHERE)
    assert body == b"bytes"
    assert [r.url for r in transport.requests] == [START, f"{ORIGIN}/real.jpg"]


def test_a_relative_location_is_resolved_against_the_current_url(clock: FakeClock) -> None:
    transport = FakeTransport()
    transport.queue(redirect("/moved/pic.jpg"), FakeResponse(200, b"bytes"))
    client_for(transport, clock).get_bytes(START, max_bytes=100, redirect_rule=ANYWHERE)
    assert transport.requests[1].url == f"{ORIGIN}/moved/pic.jpg"


def test_a_hop_to_another_host_is_refused_and_never_requested(clock: FakeClock) -> None:
    transport = FakeTransport()
    transport.queue(redirect("https://elsewhere.example.invalid/x.jpg"))
    rule = same_host(ORIGIN).accepts
    with pytest.raises(HttpStatusError, match=r"elsewhere\.example\.invalid, which the connector"):
        client_for(transport, clock).get_bytes(START, max_bytes=100, redirect_rule=rule)
    assert len(transport.requests) == 1


def test_the_rule_is_asked_about_the_second_hop_too(clock: FakeClock) -> None:
    transport = FakeTransport()
    transport.queue(
        redirect(f"{ORIGIN}/step2.jpg"), redirect("https://elsewhere.example.invalid/x.jpg")
    )
    rule = same_host(ORIGIN).accepts
    with pytest.raises(HttpStatusError):
        client_for(transport, clock).get_bytes(START, max_bytes=100, redirect_rule=rule)
    assert [r.url for r in transport.requests] == [START, f"{ORIGIN}/step2.jpg"]


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/latest/meta-data/x.png",
        "https://169.254.169.254/latest/meta-data/x.png",
        "https://localhost/internal.png",
        "https://127.0.0.1:8443/internal.png",
        "https://[::1]/internal.png",
        "https://10.1.2.3/internal.png",
        "https://2130706433/internal.png",
    ],
)
def test_a_picture_host_cannot_redirect_a_fetch_to_the_metadata_service_or_the_local_network(
    target: str,
) -> None:
    transport = FakeTransport()
    pictures = make_pictures(transport, https_only())
    (ref,) = pictures.refs(listed({"image": {"url": f"{CDN}/p.png"}}), None)
    transport.queue(redirect(target))
    with pytest.raises(HttpStatusError, match="was redirected"):
        pictures.fetch(ref)
    assert [r.url for r in transport.requests] == [f"{CDN}/p.png"]


def test_a_redirect_from_the_apis_own_host_to_another_one_is_refused() -> None:
    """The reviewers' probe: same_host() held for the first URL only."""
    transport = FakeTransport(lambda _request: redirect("http://169.254.169.254/latest/x.png"))
    pictures = make_pictures(transport, same_host(API))
    (ref,) = pictures.refs(listed({"image": {"url": f"{API}/p/1.png"}}), None)
    with pytest.raises(HttpStatusError):
        pictures.fetch(ref)
    assert [r.url for r in transport.requests] == [f"{API}/p/1.png"]


@pytest.mark.parametrize(
    "location",
    ["file:///etc/passwd", "gopher://host/x", "https://host:notaport/x"],
)
def test_a_redirect_that_is_not_a_plain_web_address_is_refused(
    clock: FakeClock, location: str
) -> None:
    transport = FakeTransport()
    transport.queue(redirect(location))
    with pytest.raises(HttpStatusError, match="was redirected to something that is not"):
        client_for(transport, clock).get_bytes(START, max_bytes=100, redirect_rule=ANYWHERE)
    assert len(transport.requests) == 1


def test_a_location_that_cannot_even_be_parsed_is_a_network_error_not_a_crash(
    clock: FakeClock,
) -> None:
    transport = FakeTransport()
    transport.queue(redirect("http://[::1/x"))
    with pytest.raises(NetworkError, match="ValueError"):
        client_for(transport, clock).get_bytes(START, max_bytes=100, redirect_rule=ANYWHERE)
    assert len(transport.requests) == 1


def test_a_redirect_from_https_to_plain_http_is_refused_even_if_the_rule_would_allow_it(
    clock: FakeClock,
) -> None:
    transport = FakeTransport()
    transport.queue(redirect("http://assets.example.invalid/pic.jpg"))
    with pytest.raises(HttpStatusError, match="from https to plain http"):
        client_for(transport, clock).get_bytes(START, max_bytes=100, redirect_rule=ANYWHERE)
    assert len(transport.requests) == 1


def test_the_error_names_no_query_string(clock: FakeClock) -> None:
    transport = FakeTransport()
    transport.queue(redirect("https://elsewhere.example.invalid/x.jpg?sig=SECRET"))
    with pytest.raises(HttpStatusError) as failure:
        client_for(transport, clock).get_bytes(
            START, max_bytes=100, redirect_rule=same_host(ORIGIN).accepts
        )
    assert "SECRET" not in str(failure.value)


# --- how many hops --------------------------------------------------------------------------------


def chain(length: int) -> Callable[[RecordedRequest], FakeResponse]:
    """/h0 redirects to /h1 ... and /h<length> answers."""

    def respond(request: RecordedRequest) -> FakeResponse:
        step = int(request.path.removeprefix("/h"))
        if step < length:
            return redirect(f"/h{step + 1}")
        return FakeResponse(200, b"end")

    return respond


def test_five_hops_are_followed(clock: FakeClock) -> None:
    transport = FakeTransport(chain(5))
    body = client_for(transport, clock).get_bytes(
        f"{ORIGIN}/h0", max_bytes=100, redirect_rule=ANYWHERE
    )
    assert body == b"end"
    assert len(transport.requests) == 6


def test_a_sixth_hop_is_an_error(clock: FakeClock) -> None:
    transport = FakeTransport(chain(6))
    with pytest.raises(HttpStatusError, match="more than 5 times"):
        client_for(transport, clock).get_bytes(
            f"{ORIGIN}/h0", max_bytes=100, redirect_rule=ANYWHERE
        )
    assert len(transport.requests) == 6, "the loop is bounded, the sixth redirect is not followed"


# --- pacing ---------------------------------------------------------------------------------------


def test_every_hop_takes_a_place_in_the_rate_window(clock: FakeClock) -> None:
    transport = FakeTransport(chain(3))
    limiter = CountingLimiter()
    client_for(transport, clock, limiter=limiter).get_bytes(
        f"{ORIGIN}/h0", max_bytes=100, redirect_rule=ANYWHERE
    )
    assert limiter.acquired == len(transport.requests) == 4


def test_the_ceiling_holds_across_a_redirect_chain(clock: FakeClock) -> None:
    transport = FakeTransport(chain(2))
    limiter = RateLimiter(2, monotonic=clock.monotonic, sleep=clock.sleep)
    client_for(transport, clock, limiter=limiter).get_bytes(
        f"{ORIGIN}/h0", max_bytes=100, redirect_rule=ANYWHERE
    )
    assert clock.sleeps == [RateLimiter.WINDOW_S], "the third request waited for the window"


def test_the_gap_is_kept_between_hops(clock: FakeClock) -> None:
    transport = FakeTransport(chain(2))
    client_for(transport, clock, gap_s=1.0).get_bytes(
        f"{ORIGIN}/h0", max_bytes=100, redirect_rule=ANYWHERE
    )
    assert clock.sleeps == [1.0, 1.0]


# --- credentials stay with their origin -----------------------------------------------------------

SECRET_HEADERS = {"authorization": "Bearer SECRET", "cookie": "sid=SECRET", "x-keep": "1"}


def test_authorization_and_cookie_do_not_leave_the_origin(clock: FakeClock) -> None:
    transport = FakeTransport()
    transport.queue(redirect("https://cdn.example.invalid/x.jpg"), FakeResponse(200, b"b"))
    client_for(transport, clock, headers=SECRET_HEADERS).get_bytes(
        START, max_bytes=100, redirect_rule=ANYWHERE
    )
    first, second = transport.requests
    assert first.header("authorization") == "Bearer SECRET"
    assert first.header("cookie") == "sid=SECRET"
    assert second.header("authorization") is None
    assert second.header("cookie") is None
    assert second.header("x-keep") == "1", "only what identifies the caller is dropped"


def test_they_do_not_come_back_when_the_chain_returns_to_the_first_origin(
    clock: FakeClock,
) -> None:
    transport = FakeTransport()
    transport.queue(
        redirect("https://cdn.example.invalid/x.jpg"),
        redirect(f"{ORIGIN}/back.jpg"),
        FakeResponse(200, b"b"),
    )
    client_for(transport, clock, headers=SECRET_HEADERS).get_bytes(
        START, max_bytes=100, redirect_rule=ANYWHERE
    )
    assert transport.requests[2].header("authorization") is None


def test_a_redirect_inside_the_origin_keeps_them(clock: FakeClock) -> None:
    transport = FakeTransport()
    transport.queue(redirect(f"{ORIGIN}/other.jpg"), FakeResponse(200, b"b"))
    client_for(transport, clock, headers=SECRET_HEADERS).get_bytes(
        START, max_bytes=100, redirect_rule=ANYWHERE
    )
    assert transport.requests[1].header("authorization") == "Bearer SECRET"


# --- a POST is sent once --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_login_form_is_not_re_sent_wherever_the_answer_points(
    clock: FakeClock, status: int
) -> None:
    transport = FakeTransport()
    transport.queue(redirect("https://collector.example.invalid/collect", status))
    client = client_for(transport, clock)
    with pytest.raises(HttpStatusError) as failure:
        client.post_form(f"{ORIGIN}/login", fields={"username": "u", "password": "hunter2"})
    assert failure.value.status == status
    assert [(r.method, r.url) for r in transport.requests] == [("POST", f"{ORIGIN}/login")]


def test_a_json_post_is_not_redirected_either(clock: FakeClock) -> None:
    transport = FakeTransport()
    transport.queue(redirect("https://collector.example.invalid/collect", 307))
    with pytest.raises(HttpStatusError):
        client_for(transport, clock).post_json(f"{ORIGIN}/login", body={"password": "hunter2"})
    assert len(transport.requests) == 1


# --- the picture rule itself: which hosts are public ----------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "api.localhost",
        "localhost.",
        "127.0.0.1",
        "127.1",
        "0.0.0.0",  # noqa: S104 -- the address under test, not a bind
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "2130706433",
        "0x7f000001",
        "0x7f.1",
    ],
)
def test_a_private_or_local_host_is_recognised_in_every_spelling(host: str) -> None:
    assert is_private_host(host)


@pytest.mark.parametrize(
    "host", ["cdn.example.invalid", "8.8.8.8", "2001:4860:4860::8888", "localhost.example.com"]
)
def test_a_public_host_is_not_private(host: str) -> None:
    assert not is_private_host(host)


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/a.png",
        "https://127.0.0.1/a.png",
        "https://[::1]/a.png",
        "https://169.254.169.254/latest/a.png",
        "https://10.0.0.5:8443/a.png",
        "https://user@192.168.0.1/a.png",
        "https://2130706433/a.png",
    ],
)
def test_https_only_refuses_a_picture_on_this_machine_or_a_private_network(url: str) -> None:
    pictures = make_pictures(FakeTransport(), https_only())
    with pytest.raises(SourceShapeError, match="an absolute https URL on a public host") as failure:
        pictures.refs(listed({"image": {"url": url + "?sig=SECRET"}}), None)
    assert "SECRET" not in str(failure.value)


def test_https_only_can_be_told_the_service_lives_on_the_private_network() -> None:
    pictures = make_pictures(FakeTransport(), https_only(allow_private_hosts=True))
    (ref,) = pictures.refs(listed({"image": {"url": "https://10.0.0.5:8443/a.png"}}), None)
    assert ref.url == "https://10.0.0.5:8443/a.png"
    assert not https_only(allow_private_hosts=True).accepts(
        SplitResult("http", "10.0.0.5", "/a.png", "", ""),
    ), "plain http stays refused"


# --- the same, on real loopback servers (the reviewers' probe) ------------------------------------


class Hits:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str, bytes]] = []


def handler_for(
    hits: Hits, *, redirect_to: str | None, status: int
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def handle_any(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            hits.seen.append((self.command, self.path, self.rfile.read(length) if length else b""))
            self.send_response(status if redirect_to else 200)
            if redirect_to:
                self.send_header("Location", redirect_to)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        do_GET = handle_any
        do_POST = handle_any

    return Handler


@contextmanager
def serving(handler: type[BaseHTTPRequestHandler]) -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    # A short poll interval: shutdown() waits for one, and the default is half a second per server.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.mock
def test_on_real_sockets_a_redirect_to_another_port_is_not_followed() -> None:
    inner_hits, outer_hits = Hits(), Hits()
    with serving(handler_for(inner_hits, redirect_to=None, status=200)) as inner_port:
        target = f"http://127.0.0.1:{inner_port}/internal-secret"
        with serving(handler_for(outer_hits, redirect_to=target, status=302)) as outer_port:
            base = f"http://127.0.0.1:{outer_port}"
            client = HttpClient({"user-agent": "t"}, credentialed=False)
            with pytest.raises(HttpStatusError, match="which the connector does not allow"):
                client.get_bytes(
                    f"{base}/pic.jpg", max_bytes=100, redirect_rule=same_host(base).accepts
                )
            client.close()
    assert [path for _, path, _ in outer_hits.seen] == ["/pic.jpg"]
    assert inner_hits.seen == [], "the second server never heard from us"


@pytest.mark.mock
def test_on_real_sockets_a_login_307_does_not_re_post_the_password() -> None:
    collector = Hits()
    with serving(handler_for(collector, redirect_to=None, status=200)) as collector_port:
        target = f"http://127.0.0.1:{collector_port}/collect"
        with serving(handler_for(Hits(), redirect_to=target, status=307)) as login_port:
            client = HttpClient({"user-agent": "t"}, credentialed=False)
            with pytest.raises(HttpStatusError) as failure:
                client.post_form(
                    f"http://127.0.0.1:{login_port}/login",
                    fields={"username": "u", "password": "hunter2"},
                )
            client.close()
    assert failure.value.status == 307
    assert collector.seen == []
