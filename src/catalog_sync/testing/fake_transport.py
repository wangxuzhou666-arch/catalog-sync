"""A scripted stand-in for the network, for tests of HTTP-speaking code.

It plugs into ``requests`` at the adapter level, i.e. below everything this project's HTTP
client does: the recorded requests are the real prepared requests (header casing, body bytes and
all), which is what "what exactly do we send?" tests need.

Responses come from a queue (consumed in order) and, once the queue is empty, from an optional
responder function that can model a whole service. Anything unscripted raises, so a test never
silently talks to a real host.
"""

from __future__ import annotations

import io
import json
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from http.client import HTTPMessage
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit

import requests
from requests.adapters import BaseAdapter, HTTPAdapter
from requests.structures import CaseInsensitiveDict

if TYPE_CHECKING:
    import pytest


class UnscriptedRequest(AssertionError):
    """A request arrived and nothing was scripted for it."""


class SimulatedCrash(BaseException):
    """Stands in for kill -9. It is a BaseException so that nothing in the pipeline can catch it.

    Hand it to ``FakeTransport.crash_on_request`` to test that a pull resumes after a crash.
    """


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    @property
    def query(self) -> dict[str, str]:
        return dict(parse_qsl(urlsplit(self.url).query, keep_blank_values=True))

    def json_body(self) -> object:
        assert self.body is not None, "request has no body"
        return json.loads(self.body)

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == wanted), None)


@dataclass(frozen=True)
class FakeResponse:
    status: int = 200
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def json(
        cls, payload: object, *, status: int = 200, headers: Mapping[str, str] | None = None
    ) -> FakeResponse:
        return cls(status, json.dumps(payload).encode("utf-8"), headers or {})


Scripted = FakeResponse | BaseException
Responder = Callable[[RecordedRequest], Scripted]


class FakeTransport(BaseAdapter):
    def __init__(self, responder: Responder | None = None) -> None:
        super().__init__()
        self.requests: list[RecordedRequest] = []
        self._queue: deque[Scripted] = deque()
        self._responder = responder
        self._crash_at: dict[int, BaseException] = {}

    def queue(self, *items: Scripted) -> None:
        self._queue.extend(items)

    def crash_on_request(self, number: int, error: BaseException) -> None:
        """Raise `error` instead of answering the `number`-th request (1-based).

        Pass a BaseException that nothing in the code under test catches to simulate a kill.
        """
        self._crash_at[number] = error

    def requests_to(self, path_suffix: str) -> list[RecordedRequest]:
        return [r for r in self.requests if r.path.endswith(path_suffix)]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Route every ``requests`` session in the process through this transport."""

        def send(
            _adapter: HTTPAdapter,
            request: requests.PreparedRequest,
            stream: bool = False,
            timeout: float | tuple[float, float] | tuple[float, None] | None = None,
            verify: bool | str = True,
            cert: bytes | str | tuple[bytes | str, bytes | str] | None = None,
            proxies: Mapping[str, str] | None = None,
        ) -> requests.Response:
            return self.send(request, stream, timeout, verify, cert, proxies)

        monkeypatch.setattr(HTTPAdapter, "send", send)

    # --- BaseAdapter ------------------------------------------------------------------------

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float, float] | tuple[float, None] | None = None,
        verify: bool | str = True,
        cert: bytes | str | tuple[bytes | str, bytes | str] | None = None,
        proxies: Mapping[str, str] | None = None,
    ) -> requests.Response:
        recorded = RecordedRequest(
            method=request.method or "",
            url=request.url or "",
            headers=dict(request.headers),
            body=_body_bytes(request.body),
        )
        self.requests.append(recorded)
        crash = self._crash_at.get(len(self.requests))
        if crash is not None:
            raise crash
        scripted = self._next(recorded)
        if isinstance(scripted, BaseException):
            raise scripted
        return _to_response(scripted, request)

    def close(self) -> None:
        return None

    def _next(self, request: RecordedRequest) -> Scripted:
        if self._queue:
            return self._queue.popleft()
        if self._responder is not None:
            return self._responder(request)
        raise UnscriptedRequest(f"{request.method} {request.url}")


def _body_bytes(body: object) -> bytes | None:
    if body is None or isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    raise TypeError(f"the fake transport records bytes or str bodies, got {type(body).__name__}")


def _to_response(scripted: FakeResponse, request: requests.PreparedRequest) -> requests.Response:
    response = requests.Response()
    response.status_code = scripted.status
    response.reason = HTTPStatus(scripted.status).phrase
    response.headers = CaseInsensitiveDict(scripted.headers)
    response.raw = _with_cookie_headers(io.BytesIO(scripted.body), scripted.headers)
    response.url = request.url or ""
    response.request = request
    return response


def _with_cookie_headers(raw: io.BytesIO, headers: Mapping[str, str]) -> io.BytesIO:
    """Let ``requests`` find a ``Set-Cookie`` header the way it does on a real connection: it reads
    the cookies from ``raw._original_response.msg``, which a plain byte stream does not have."""
    message = HTTPMessage()
    for name, value in headers.items():
        if name.lower() == "set-cookie":
            message.add_header(name, value)
    if message.keys():
        raw._original_response = type("_Original", (), {"msg": message})()  # type: ignore[attr-defined]
    return raw


class FakeClock:
    """A clock whose sleep advances it instantly: pacing tests run in zero time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
