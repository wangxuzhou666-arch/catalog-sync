"""HTTP client for source plugins: polite pacing, bounded retries, no secrets in logs.

Policy, in one place so every plugin behaves the same way towards the services it calls:

* A minimum gap between requests (``HttpSettings.gap_s``). It is a floor, not a target.
* Optionally a ceiling on requests per minute (``RateLimiter``), the rate the operator declared
  in the compliance block. It is a sliding window, so a burst after a pause cannot exceed it, and
  one limiter can be shared by every client of a source. Retries count: they are requests too.
* Network errors, HTTP 5xx and HTTP 429 are retried with exponential backoff; a ``Retry-After``
  header (in seconds) replaces the computed delay, capped so a hostile or broken server cannot park
  the run for hours. After the last attempt the failure is raised, never swallowed.
* Every other 4xx is final and is never retried: retrying a request the service has understood and
  refused only repeats the refusal. For a credentialed client 401/403 become ``AuthExpired``.
* Redirects are never followed by the library. Only a download (``get_bytes``) on a client without
  a credential may follow them, and only when the caller passes the rule every target must satisfy:
  at most 5 hops, each one checked against the rule, never from https to http, each one paced like
  any other request (gap and ceiling), and ``Authorization`` / ``Cookie`` dropped as soon as the
  target is another origin. A POST is never redirected: a login form must not be re-sent, with the
  password in it, to wherever a response points.
* Nothing that could be a secret is logged: no headers, no query strings (signed download URLs
  carry credentials in them), no response bodies.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import re
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import SplitResult, urlencode, urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import BaseAdapter

from catalog_sync.errors import (
    AuthExpired,
    HttpStatusError,
    NetworkError,
    ResponseTooLarge,
    SourceShapeError,
)
from catalog_sync.models import JsonObject

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_LEGACY_NUMERIC_HOST = re.compile(r"(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+)){0,3}")

# What a download may be redirected to: the rule the caller states for every target URL.
RedirectRule = Callable[[SplitResult], bool]

_CHUNK_BYTES = 64 * 1024
_BODY_EXCERPT_BYTES = 512
_HTTP_REDIRECT_MIN = 300
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5
# Headers that identify the caller to one origin: dropped when a redirect leaves it.
_ORIGIN_BOUND_HEADERS = ("authorization", "cookie", "proxy-authorization")
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR_MIN = 500
_AUTH_STATUSES = frozenset({401, 403})
# Failures that say nothing about the request itself: worth another try.
_TRANSIENT_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def strip_query(url: str) -> str:
    """The URL without query, fragment and userinfo: safe to log or to store in a record.

    Signed download links keep their signature in the query string, so anything that is stored
    or printed goes through here.
    """
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def is_loopback_host(host: str) -> bool:
    """Whether `host` (a name or an address, no port) can only be reached from this machine."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_private_host(host: str) -> bool:
    """Whether `host` (a name or an address, no port) is this machine or a network that is not the
    public internet: loopback, private, link-local (cloud metadata lives there), carrier-grade NAT.

    Also the numeric spellings an operating system reads as an address but ``ipaddress`` does not
    (``2130706433``, ``0x7f.1``): a URL must not get around the check by writing 127.0.0.1 oddly.
    A name that merely *resolves* to a private address is beyond what a string check can see.
    """
    name = host.rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return _LEGACY_NUMERIC_HOST.fullmatch(name) is not None
    return not address.is_global


def encode_json_body(body: Mapping[str, object]) -> bytes:
    """The bytes ``post_json`` puts on the wire for `body`: compact, insertion order, UTF-8.

    Serialised by hand, like a browser would, so that the bytes are exactly what the code says and
    a caller that needs them (to hash a request, to compare it in a test) can have the same ones.
    """
    text = json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return text.encode("utf-8")


@dataclass(frozen=True)
class HttpSettings:
    gap_s: float = 0.3
    timeout_s: float = 30.0
    max_attempts: int = 6
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 30.0
    retry_after_cap_s: float = 120.0

    def __post_init__(self) -> None:
        if self.gap_s < 0 or self.backoff_cap_s < 0 or self.retry_after_cap_s < 0:
            raise ValueError("gap and delay caps must not be negative")
        if self.timeout_s <= 0 or self.backoff_base_s <= 0:
            raise ValueError("timeout and backoff base must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")


@dataclass(frozen=True)
class HttpSeams:
    """The things a test replaces: the transport, the clock and sleeping."""

    adapter: BaseAdapter | None = None
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


class RateLimiter:
    """At most `limit` requests in any 60-second window, enforced by waiting."""

    WINDOW_S = 60.0

    def __init__(
        self,
        limit_per_minute: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if limit_per_minute < 1:
            raise ValueError("the request limit must be at least 1 per minute")
        self.limit_per_minute = limit_per_minute
        self._monotonic = monotonic
        self._sleep = sleep
        self._started: deque[float] = deque()

    def acquire(self) -> None:
        """Return when one more request may be sent, and count it."""
        while True:
            now = self._monotonic()
            while self._started and now - self._started[0] >= self.WINDOW_S:
                self._started.popleft()
            if len(self._started) < self.limit_per_minute:
                self._started.append(now)
                return
            self._sleep(self._started[0] + self.WINDOW_S - now)


_SCOPED_LIMITER: ContextVar[RateLimiter | None] = ContextVar("scoped_limiter", default=None)


@contextmanager
def shared_limiter(limiter: RateLimiter) -> Iterator[None]:
    """Make every client built inside the block, without a limiter of its own, use `limiter`.

    For a command that acts on behalf of one source but reaches the service through code it does
    not own (a connector's login and credential check build their own clients): the source's
    declared ceiling must hold for those requests too.
    """
    token = _SCOPED_LIMITER.set(limiter)
    try:
        yield
    finally:
        _SCOPED_LIMITER.reset(token)


class _Transient(Exception):
    """One failed attempt that is worth repeating (internal; never leaves this module)."""

    def __init__(
        self,
        reason: str,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.retry_after_s = retry_after_s
        self.cause = cause


class HttpClient:
    """A requests session with the policy above.

    ``headers`` are the only headers sent (the library's own ``python-requests`` User-Agent is
    removed, so the caller decides what identifies the client, normally the SDK's honest
    ``catalog-sync/<version>`` string). A credentialed client never follows a redirect: its headers
    must never be replayed to another host. Nor does any client unless ``get_bytes`` is given a
    ``redirect_rule`` (see the module docstring).
    """

    def __init__(
        self,
        headers: Mapping[str, str],
        *,
        credentialed: bool,
        settings: HttpSettings | None = None,
        renewal_hint: str = "",
        seams: HttpSeams | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._settings = settings or HttpSettings()
        self._limiter = limiter if limiter is not None else _SCOPED_LIMITER.get()
        self._seams = seams or HttpSeams()
        self._credentialed = credentialed
        self._renewal_hint = renewal_hint
        self._last_done: float | None = None
        self._session = requests.Session()
        self._session.headers.clear()
        self._session.headers.update({"accept-encoding": "gzip, deflate", **headers})
        if self._seams.adapter is not None:
            self._session.mount("https://", self._seams.adapter)
            self._session.mount("http://", self._seams.adapter)

    def close(self) -> None:
        self._session.close()

    # --- public calls -----------------------------------------------------------------------

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> JsonObject:
        return self._call("GET", url, params=params, body=None, headers=headers, read=_read_json)

    def post_json(
        self, url: str, *, body: Mapping[str, object], headers: Mapping[str, str] | None = None
    ) -> JsonObject:
        return self._call(
            "POST", url, params=None, body=encode_json_body(body), headers=headers, read=_read_json
        )

    def post_form(
        self, url: str, *, fields: Mapping[str, str], headers: Mapping[str, str] | None = None
    ) -> JsonObject:
        """POST ``application/x-www-form-urlencoded`` fields; the answer is read as JSON."""
        return self._call(
            "POST",
            url,
            params=None,
            body=urlencode(fields).encode("utf-8"),
            headers={"content-type": "application/x-www-form-urlencoded", **(headers or {})},
            read=_read_json,
        )

    def cookie(self, name: str) -> str | None:
        """The value of a cookie the service has set during this client's session, if any."""
        return self._session.cookies.get(name)

    def get_bytes(
        self, url: str, *, max_bytes: int, redirect_rule: RedirectRule | None = None
    ) -> bytes:
        """Download a body, giving up as soon as it grows beyond `max_bytes`.

        A redirect is an error unless `redirect_rule` is given and accepts its target (and the
        client carries no credential): the rule is asked about every hop, not only the first.
        """

        def read_limited(response: requests.Response) -> bytes:
            return _read_limited(response, max_bytes)

        return self._call(
            "GET",
            url,
            params=None,
            body=None,
            headers=None,
            read=read_limited,
            stream=True,
            follow=None if self._credentialed else redirect_rule,
        )

    # --- machinery --------------------------------------------------------------------------

    def _call(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int] | None,
        body: bytes | None,
        headers: Mapping[str, str] | None,
        read: Callable[[requests.Response], _T],
        stream: bool = False,
        follow: RedirectRule | None = None,
    ) -> _T:
        label = f"{method} {strip_query(url)}"
        last_attempt = self._settings.max_attempts
        for attempt in range(1, last_attempt + 1):
            self._pace()
            try:
                return self._attempt(
                    method,
                    url,
                    label,
                    params=params,
                    body=body,
                    headers=headers,
                    stream=stream,
                    follow=follow,
                    read=read,
                )
            except _Transient as failure:
                if attempt == last_attempt:
                    raise _final_error(label, failure, attempt) from failure.cause
                delay = self._retry_delay(attempt, failure.retry_after_s)
                logger.warning(
                    "%s failed (%s); attempt %d of %d, retrying in %.1fs",
                    label,
                    failure.reason,
                    attempt,
                    last_attempt,
                    delay,
                )
                self._seams.sleep(delay)
            finally:
                self._last_done = self._seams.monotonic()
        raise AssertionError("the loop returns or raises on its last attempt")

    def _attempt(
        self,
        method: str,
        url: str,
        label: str,
        *,
        params: Mapping[str, str | int] | None,
        body: bytes | None,
        headers: Mapping[str, str] | None,
        stream: bool,
        follow: RedirectRule | None,
        read: Callable[[requests.Response], _T],
    ) -> _T:
        response = self._send(
            method, url, label, params=params, body=body, headers=headers, stream=stream
        )
        if follow is not None:
            response = self._follow_redirects(
                response, url, label, follow, headers=headers, stream=stream
            )
        with response:
            self._check_status(response, label)
            logger.debug("%s -> %d", label, response.status_code)
            try:
                return read(response)
            except _TRANSIENT_ERRORS as exc:
                raise _Transient(type(exc).__name__, cause=exc) from exc

    def _send(
        self,
        method: str,
        url: str,
        label: str,
        *,
        params: Mapping[str, str | int] | None,
        body: bytes | None,
        headers: Mapping[str, str | None] | None,
        stream: bool,
    ) -> requests.Response:
        """One request on the wire. Never follows a redirect: that is `_follow_redirects`."""
        try:
            return self._session.request(
                method,
                url,
                params=params,
                data=body,
                headers=headers,
                timeout=self._settings.timeout_s,
                stream=stream,
                allow_redirects=False,
            )
        except requests.exceptions.SSLError as exc:
            raise NetworkError(f"{label}: TLS failure ({type(exc).__name__})") from exc
        except _TRANSIENT_ERRORS as exc:
            raise _Transient(type(exc).__name__, cause=exc) from exc
        except (requests.RequestException, ValueError) as exc:
            # Translation at the library boundary: the exception type survives in the message.
            # A bare ValueError is how ``requests`` reports a redirect target it cannot parse:
            # it reads the Location header of any 3xx answer, followed or not.
            raise NetworkError(f"{label}: {type(exc).__name__}") from exc

    def _follow_redirects(
        self,
        response: requests.Response,
        url: str,
        label: str,
        rule: RedirectRule,
        *,
        headers: Mapping[str, str | None] | None,
        stream: bool,
    ) -> requests.Response:
        """Walk a chain of redirects by hand, GET only: each hop is vetted, paced, and sent
        without the caller's credentials once it is on another origin."""
        for _ in range(_MAX_REDIRECTS):
            self._last_done = self._seams.monotonic()
            location = response.headers.get("Location")
            status = response.status_code
            if status not in _REDIRECT_STATUSES or not location:
                return response
            response.close()
            target, problem = _resolve_redirect(url, location, rule)
            if problem is not None:
                raise HttpStatusError(status, f"{label} was redirected {problem}")
            if _origin(target) != _origin(url):
                headers = _without_credentials(headers)
            self._pace()
            response = self._send(
                "GET", target, label, params=None, body=None, headers=headers, stream=stream
            )
            url = target
        if response.status_code in _REDIRECT_STATUSES and response.headers.get("Location"):
            status = response.status_code
            response.close()
            message = f"{label} was redirected more than {_MAX_REDIRECTS} times"
            raise HttpStatusError(status, message)
        return response

    def _pace(self) -> None:
        """Wait until one more request may go out: the minimum gap, then the declared ceiling."""
        self._respect_gap()
        if self._limiter is not None:
            self._limiter.acquire()

    def _check_status(self, response: requests.Response, label: str) -> None:
        status = response.status_code
        if status < _HTTP_REDIRECT_MIN:
            return
        if self._credentialed and status in _AUTH_STATUSES:
            hint = f" {self._renewal_hint}" if self._renewal_hint else ""
            raise AuthExpired(
                f"{label} was rejected with HTTP {status}: the credential is expired or "
                f"not accepted.{hint}"
            )
        if status == _HTTP_TOO_MANY_REQUESTS or status >= _HTTP_SERVER_ERROR_MIN:
            raise _Transient(
                f"HTTP {status}",
                status=status,
                retry_after_s=_parse_retry_after(response.headers.get("Retry-After")),
            )
        raise HttpStatusError(
            status, f"{label} answered HTTP {status}", body_excerpt=_body_excerpt(response)
        )

    def _respect_gap(self) -> None:
        if self._last_done is None:
            return
        waited = self._seams.monotonic() - self._last_done
        if waited < self._settings.gap_s:
            self._seams.sleep(self._settings.gap_s - waited)

    def _retry_delay(self, attempt: int, retry_after_s: float | None) -> float:
        settings = self._settings
        if retry_after_s is not None:
            return min(retry_after_s, settings.retry_after_cap_s)
        return min(settings.backoff_base_s * (1 << (attempt - 1)), settings.backoff_cap_s)


def _body_excerpt(response: requests.Response) -> str:
    """The first bytes of the body as text, whitespace collapsed. Reads one chunk only, so a huge
    error page costs nothing more than a small one."""
    chunk = next(response.iter_content(chunk_size=_BODY_EXCERPT_BYTES), b"")
    return " ".join(chunk.decode("utf-8", errors="replace").split())


def _final_error(label: str, failure: _Transient, attempts: int) -> HttpStatusError | NetworkError:
    message = f"{label}: {failure.reason} after {attempts} attempt(s)"
    if failure.status is not None:
        return HttpStatusError(failure.status, message)
    return NetworkError(message)


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header. The HTTP-date form is ignored (backoff applies)."""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _resolve_redirect(current: str, location: str, rule: RedirectRule) -> tuple[str, str | None]:
    """The absolute target of a ``Location`` header, and why it must not be followed (None: it may).

    The reason completes "was redirected ...". The target itself is never put in it, beyond its
    host: a signed link carries its secret in the rest of the URL.
    """
    try:
        target = urljoin(current, location)
        parts = urlsplit(target)
        host = parts.hostname
        _checked_port = parts.port  # raises ValueError for a port that is not a number
    except ValueError:
        return "", "to something that is not a valid URL"
    if parts.scheme not in {"http", "https"} or not host:
        return "", "to something that is not an http(s) URL"
    if urlsplit(current).scheme == "https" and parts.scheme == "http":
        return "", f"from https to plain http ({host})"
    if not rule(parts):
        return "", f"to {host}, which the connector does not allow"
    return target, None


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port


def _without_credentials(headers: Mapping[str, str | None] | None) -> dict[str, str | None]:
    """`headers` minus the ones that identify the caller; a None value also removes the same
    header from the session's own, which is how ``requests`` takes one away for a single request."""
    kept = {k: v for k, v in (headers or {}).items() if k.lower() not in _ORIGIN_BOUND_HEADERS}
    return {**kept, **dict.fromkeys(_ORIGIN_BOUND_HEADERS)}


def _read_json(response: requests.Response) -> JsonObject:
    try:
        payload = response.json()
    except ValueError as exc:
        raise SourceShapeError("response body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise SourceShapeError(f"expected a JSON object, got {type(payload).__name__}")
    return payload


def _read_limited(response: requests.Response, max_bytes: int) -> bytes:
    declared = response.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise ResponseTooLarge(f"response announces {declared} bytes, limit is {max_bytes}")
    body = bytearray()
    for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
        body += chunk
        if len(body) > max_bytes:
            raise ResponseTooLarge(f"response is larger than the limit of {max_bytes} bytes")
    return bytes(body)
