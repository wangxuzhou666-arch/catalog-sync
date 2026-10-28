"""What the Mock SaaS answers, as a pure function from a request to a response.

No sockets here: ``MockService.handle`` takes a ``Request`` and returns a ``Response``, so every
behaviour (and every trap) can be tested without a network; ``server.py`` is the thin HTTP skin.

The API, in one place:

* ``POST /api/login`` (JSON or form): ``{"state": true, "data": {"token": ...}}`` plus a session
  cookie. Everything under ``/api/v1`` accepts either the bearer token or the cookie.
* ``GET /api/v1/materials?folder_id=&page=&per_page=``: a folder's sub-folders and materials in one
  listing, page-number pagination with ``total``.
* ``GET /api/v1/materials/{id}``: the detail, wrapped twice (``data.data``).
* ``GET /api/v1/suppliers?cursor=&limit=``: cursor pagination, no folders, no total.
* ``GET /media/{thumb|swatch}/{id}.{png|jpg}``: pictures, served without any login (like a
  storage host), optionally behind a signed link that expires.

Traps, each switched on by an option: a rate limit (HTTP 429 with ``Retry-After``), a short token
lifetime (HTTP 401), a login that works for N data requests only (HTTP 401 in the middle of a run,
whatever the clock says), an error every Nth request (HTTP 503, which a client retries), signed
picture links that expire (HTTP 403), and every Nth material whose detail is gone (HTTP 404, which
a client cannot retry away: the item fails, the run goes on). The shape traps (a price of 0 in the
listing, an empty ``thumbs`` in the detail, the double envelope) are always on: they are how the
data looks.
"""

from __future__ import annotations

import hmac
import json
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, urlsplit

from catalog_sync.mock.auth import Account, Authority, Unauthorized, account_from_env
from catalog_sync.mock.data import (
    CURRENCY,
    DEFAULT_MATERIALS,
    DEFAULT_SUPPLIERS,
    ROOT_FOLDER_ID,
    MockDataset,
    MockMaterial,
    MockSupplier,
    generate,
)
from catalog_sync.mock.images import picture
from catalog_sync.mock.pages import app_page, login_page, terms_page

SESSION_COOKIE = "mock_session"
# A week, like the tokens of the services this tool is for: a login lasts a whole demo and the
# tool's "renew soon" warning stays quiet. Set it to seconds to see an expired token (401).
DEFAULT_TOKEN_TTL_S = 7 * 24 * 3600.0
DEFAULT_PER_PAGE = 10
MAX_PER_PAGE = 100
DEFAULT_SUPPLIER_LIMIT = 5
MAX_SUPPLIER_LIMIT = 50
_CURSOR = re.compile(r"cur-(\d+)")
_MEDIA = re.compile(r"/media/(?P<role>thumb|swatch)/(?P<id>[A-Za-z0-9-]+)\.(?P<ext>png|jpg)")
_MATERIAL_DETAIL = re.compile(r"/api/v1/materials/(?P<id>[A-Za-z0-9-]+)")
_HTML = "text/html; charset=utf-8"
_JSON = "application/json"
_HTTP_OK = 200
_HTTP_FOUND = 302
_HTTP_BAD_REQUEST = 400
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404
_HTTP_METHOD_NOT_ALLOWED = 405
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_UNAVAILABLE = 503


@dataclass(frozen=True)
class RateLimit:
    requests: int
    per_seconds: float

    def __post_init__(self) -> None:
        if self.requests < 1 or self.per_seconds <= 0:
            raise ValueError("a rate limit needs at least one request and a positive window")

    @classmethod
    def parse(cls, text: str) -> RateLimit:
        """``"30/10"``: at most 30 requests in any 10 seconds."""
        count, separator, seconds = text.partition("/")
        message = f"expected COUNT/SECONDS such as 30/10, got {text!r}"
        if not separator:
            raise ValueError(message)
        try:
            return cls(int(count), float(seconds))
        except ValueError as exc:
            raise ValueError(message) from exc


@dataclass(frozen=True)
class MockOptions:
    seed: int = 1
    materials: int = DEFAULT_MATERIALS
    suppliers: int = DEFAULT_SUPPLIERS
    token_ttl_s: float = DEFAULT_TOKEN_TTL_S
    rate_limit: RateLimit | None = None
    error_every: int = 0  # every Nth data request answers 503; 0 never does
    gone_every: int = 0  # the detail of every Nth material answers 404; 0 never does
    expire_after: int = 0  # a login works for N data requests, then 401; 0 never expires so
    signed_url_ttl_s: float | None = None  # None: picture links are plain
    account: Account = field(default_factory=lambda: account_from_env({}))


@dataclass(frozen=True)
class Request:
    method: str
    target: str  # path and query, as on the request line
    headers: Mapping[str, str]
    host: str
    body: bytes = b""

    @property
    def path(self) -> str:
        return urlsplit(self.target).path

    @property
    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.target).query)

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((value for key, value in self.headers.items() if key.lower() == wanted), None)


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class LoggedRequest:
    """One request the service saw. Paths only: a query can hold a signature."""

    method: str
    path: str
    status: int
    sent_authorization: bool
    sent_cookie: bool


def _json(status: int, payload: object, headers: tuple[tuple[str, str], ...] = ()) -> Response:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return Response(status, body, _JSON, headers)


def _ok(data: object, headers: tuple[tuple[str, str], ...] = ()) -> Response:
    return _json(_HTTP_OK, {"state": True, "data": data}, headers)


def _fail(status: int, message: str, headers: tuple[tuple[str, str], ...] = ()) -> Response:
    return _json(status, {"state": False, "msg": message}, headers)


def _page(content: str) -> Response:
    return Response(_HTTP_OK, content.encode("utf-8"), _HTML)


class MockService:
    def __init__(
        self,
        options: MockOptions | None = None,
        *,
        clock: Callable[[], float] = time.time,
        on_request: Callable[[LoggedRequest], None] | None = None,
    ) -> None:
        self.options = options or MockOptions()
        self._on_request = on_request
        self.dataset: MockDataset = generate(
            self.options.seed,
            materials=self.options.materials,
            suppliers=self.options.suppliers,
        )
        self._clock = clock
        self._authority = Authority(token_ttl_s=self.options.token_ttl_s, clock=clock)
        self._lock = threading.Lock()
        self._window: deque[float] = deque()
        self._data_requests = 0
        self._served_since_login = 0
        self._log: list[LoggedRequest] = []
        every = self.options.gone_every
        # By position in the catalogue, so the same items are gone in every run of the same seed.
        self._gone = {
            material.id
            for number, material in enumerate(self.dataset.materials, start=1)
            if every and number % every == 0
        }

    @property
    def request_log(self) -> list[LoggedRequest]:
        with self._lock:
            return list(self._log)

    def handle(self, request: Request) -> Response:
        # One request at a time: counters, the rate window and the log stay consistent, and a
        # demo server has no use for parallelism.
        with self._lock:
            response = self._dispatch(request)
            entry = LoggedRequest(
                request.method,
                request.path,
                response.status,
                sent_authorization=request.header("authorization") is not None,
                sent_cookie=request.header("cookie") is not None,
            )
            self._log.append(entry)
            if self._on_request is not None:
                self._on_request(entry)
        return response

    # --- routing ----------------------------------------------------------------------------

    def _dispatch(self, request: Request) -> Response:
        path, method = request.path, request.method
        pages = {"/login": login_page, "/app": app_page, "/terms": terms_page}
        if path == "/" and method == "GET":
            return Response(_HTTP_FOUND, b"", _HTML, (("Location", "/login"),))
        if path in pages and method == "GET":
            return _page(pages[path]())
        if path == "/api/login":
            return (
                self._login(request)
                if method == "POST"
                else _fail(_HTTP_METHOD_NOT_ALLOWED, "POST only")
            )
        media = _MEDIA.fullmatch(path)
        if media is not None and method == "GET":
            return self._media(request, media)
        data_route = self._data_route(request)
        if data_route is None:
            return _fail(_HTTP_NOT_FOUND, "not found")
        return self._guarded(request, data_route)

    def _data_route(self, request: Request) -> Callable[[Request], Response] | None:
        if request.method != "GET":
            return None
        path = request.path
        if path == "/api/v1/materials":
            return self._list_materials
        if path == "/api/v1/suppliers":
            return self._list_suppliers
        detail = _MATERIAL_DETAIL.fullmatch(path)
        if detail is not None:
            return lambda req: self._material_detail(req, detail["id"])
        return None

    def _guarded(self, request: Request, route: Callable[[Request], Response]) -> Response:
        """Login, then the rate limit, then the injected failure: the order a real service has."""
        try:
            self._authenticate(request)
        except Unauthorized as exc:
            return _fail(
                _HTTP_UNAUTHORIZED, exc.reason, (("WWW-Authenticate", 'Bearer realm="mock"'),)
            )
        if self._is_login_used_up():
            return _fail(
                _HTTP_UNAUTHORIZED, "token expired", (("WWW-Authenticate", 'Bearer realm="mock"'),)
            )
        return self._rate_limited() or self._injected_failure() or route(request)

    # --- login ------------------------------------------------------------------------------

    def _login(self, request: Request) -> Response:
        credentials = _credentials(request)
        account = self.options.account
        is_user = _same(credentials.get("username", ""), account.user)
        is_password = _same(credentials.get("password", ""), account.password)
        if not (is_user and is_password):
            return _fail(_HTTP_UNAUTHORIZED, "invalid username or password")
        self._served_since_login = 0
        token, expires = self._authority.issue_token(account.user)
        session, _ = self._authority.open_session(account.user)
        cookie = f"{SESSION_COOKIE}={session}; Path=/; HttpOnly; SameSite=Lax"
        data = {"token": token, "token_type": "Bearer", "expires_at": int(expires)}
        return _ok(data, (("Set-Cookie", cookie),))

    def _authenticate(self, request: Request) -> None:
        header = request.header("authorization")
        if header is not None:
            scheme, _, token = header.partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                raise Unauthorized("a bearer token is expected")
            self._authority.verify_token(token.strip())
            return
        session = _session_cookie(request.header("cookie"))
        if session is None:
            raise Unauthorized("login required")
        self._authority.session_user(session)

    # --- traps ------------------------------------------------------------------------------

    def _rate_limited(self) -> Response | None:
        limit = self.options.rate_limit
        if limit is None:
            return None
        now = self._clock()
        while self._window and now - self._window[0] >= limit.per_seconds:
            self._window.popleft()
        if len(self._window) >= limit.requests:
            wait = max(1, math.ceil(limit.per_seconds - (now - self._window[0])))
            return _fail(
                _HTTP_TOO_MANY_REQUESTS, "rate limit exceeded", (("Retry-After", str(wait)),)
            )
        self._window.append(now)
        return None

    def _is_login_used_up(self) -> bool:
        """With `expire_after`, the (N+1)th data request since the last login is refused."""
        limit = self.options.expire_after
        if not limit:
            return False
        self._served_since_login += 1
        return self._served_since_login > limit

    def _injected_failure(self) -> Response | None:
        every = self.options.error_every
        if not every:
            return None
        self._data_requests += 1
        if self._data_requests % every == 0:
            return _fail(_HTTP_UNAVAILABLE, "temporarily unavailable")
        return None

    # --- materials --------------------------------------------------------------------------

    def _list_materials(self, request: Request) -> Response:
        try:
            folder_id = _text(request, "folder_id", ROOT_FOLDER_ID)
            page = _whole(request, "page", 1, minimum=1)
            per_page = _whole(
                request, "per_page", DEFAULT_PER_PAGE, minimum=1, maximum=MAX_PER_PAGE
            )
        except ValueError as exc:
            return _fail(_HTTP_BAD_REQUEST, str(exc))
        if not self.dataset.has_folder(folder_id):
            return _fail(_HTTP_NOT_FOUND, "no such folder")
        rows: list[dict[str, object]] = [
            {"id": folder.id, "type": "folder", "name": folder.name}
            for folder in self.dataset.subfolders(folder_id)
        ]
        rows += [self._material_row(request, m) for m in self.dataset.materials_in(folder_id)]
        start = (page - 1) * per_page
        data = {
            "total": len(rows),
            "page": page,
            "per_page": per_page,
            "rows": rows[start : start + per_page],
        }
        return _ok(data)

    def _material_row(self, request: Request, material: MockMaterial) -> dict[str, object]:
        """The listing row: a picture, a name, and a price of 0 (the real one is in the detail)."""
        return {
            "id": material.id,
            "type": "material",
            "code": material.code,
            "name": material.name,
            "price": 0,
            "currency": CURRENCY,
            "thumbnail_url": self._media_url(request, "thumb", material.id, "png"),
            "updated_at": material.updated_at,
        }

    def _material_detail(self, request: Request, material_id: str) -> Response:
        material = self.dataset.material(material_id)
        if material is None or material.id in self._gone:
            return _fail(_HTTP_NOT_FOUND, "no such material")
        record = {
            "id": material.id,
            "code": material.code,
            "name": material.name,
            "price": str(material.price),
            "currency": CURRENCY,
            "weight": f"{material.weight_gsm} gsm",
            "width": material.width,
            "composition": material.composition,
            "supplier_id": material.supplier_id,
            "supplier_name": material.supplier_name,
            # Empty here and filled in the listing: neither payload contains the other.
            "thumbs": [],
            "swatch_url": self._media_url(request, "swatch", material.id, "jpg"),
        }
        return _ok({"data": record})  # wrapped twice, like the services this tool is for

    # --- suppliers --------------------------------------------------------------------------

    def _list_suppliers(self, request: Request) -> Response:
        try:
            limit = _whole(
                request, "limit", DEFAULT_SUPPLIER_LIMIT, minimum=1, maximum=MAX_SUPPLIER_LIMIT
            )
            start = _cursor_position(request)
        except ValueError as exc:
            return _fail(_HTTP_BAD_REQUEST, str(exc))
        suppliers = self.dataset.suppliers
        end = start + limit
        next_cursor = f"cur-{end}" if end < len(suppliers) else None
        items = [_supplier_row(s) for s in suppliers[start:end]]
        return _ok({"items": items, "next_cursor": next_cursor})

    # --- pictures ---------------------------------------------------------------------------

    def _media(self, request: Request, match: re.Match[str]) -> Response:
        if self.options.signed_url_ttl_s is not None:
            denied = self._check_link(request)
            if denied is not None:
                return denied
        if self.dataset.material(match["id"]) is None:
            return _fail(_HTTP_NOT_FOUND, "no such picture")
        content, content_type = picture(match["role"], match["id"])
        return Response(_HTTP_OK, content, content_type)

    def _check_link(self, request: Request) -> Response | None:
        query = request.query
        try:
            expires, signature = int(query["exp"][0]), query["sig"][0]
        except (KeyError, IndexError, ValueError):
            return _fail(_HTTP_FORBIDDEN, "signed link required")
        if not self._authority.is_link_valid(request.path, expires, signature):
            return _fail(_HTTP_FORBIDDEN, "signed link invalid")
        if self._authority.is_link_expired(expires):
            return _fail(_HTTP_FORBIDDEN, "signed link expired")
        return None

    def _media_url(self, request: Request, role: str, item_id: str, extension: str) -> str:
        path = f"/media/{role}/{item_id}.{extension}"
        url = f"http://{request.host}{path}"
        ttl = self.options.signed_url_ttl_s
        if ttl is None:
            return url
        expires = int(self._clock() + ttl)
        return f"{url}?exp={expires}&sig={self._authority.sign_link(path, expires)}"


def _supplier_row(supplier: MockSupplier) -> dict[str, object]:
    return {
        "id": supplier.id,
        "name": supplier.name,
        "country": supplier.country,
        "rating": supplier.rating,
        "lead_time_days": supplier.lead_time_days,
        "certifications": list(supplier.certifications),
        "contact_email": supplier.contact_email,
        "address": dict(supplier.address),
        "listed_at": supplier.listed_at,
    }


def _credentials(request: Request) -> dict[str, str]:
    """User name and password from a JSON or a form body; anything else yields nothing."""
    if "json" in (request.header("content-type") or ""):
        try:
            loaded = json.loads(request.body)
        except ValueError:
            return {}
        pairs = loaded.items() if isinstance(loaded, dict) else ()
        return {str(key): value for key, value in pairs if isinstance(value, str)}
    form = parse_qs(request.body.decode("utf-8", errors="replace"))
    return {key: values[0] for key, values in form.items() if values}


def _same(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _session_cookie(header: str | None) -> str | None:
    if header is None:
        return None
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(header)
    except CookieError:
        return None
    morsel = jar.get(SESSION_COOKIE)
    return morsel.value if morsel is not None else None


def _text(request: Request, name: str, default: str) -> str:
    values = request.query.get(name)
    return values[0] if values else default


def _whole(
    request: Request, name: str, default: int, *, minimum: int, maximum: int | None = None
) -> int:
    values = request.query.get(name)
    if not values:
        return default
    try:
        number = int(values[0])
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number") from exc
    if number < minimum or (maximum is not None and number > maximum):
        bound = f"at least {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise ValueError(f"{name} must be {bound}")
    return number


def _cursor_position(request: Request) -> int:
    values = request.query.get("cursor")
    if not values:
        return 0
    match = _CURSOR.fullmatch(values[0])
    if match is None:
        raise ValueError("cursor is not one this service issued")
    return int(match[1])
