"""The Mock SaaS as a pure function from request to response: its API, its data and its traps.

No sockets: ``MockService.handle`` is called directly, with a clock the test moves by hand, so the
token expiry, the rate window and the signed links are exercised without waiting for any of them.
The same service behind a real HTTP server is in test_mock_server.py.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import pytest

from catalog_sync.credentials.jwt import decode_claims
from catalog_sync.mock.auth import DEMO_PASSWORD, DEMO_USER, Account
from catalog_sync.mock.data import ROOT_FOLDER_ID, generate
from catalog_sync.mock.service import (
    SESSION_COOKIE,
    MockOptions,
    MockService,
    RateLimit,
    Request,
    Response,
)

HOST = "mock.invalid"


@dataclass
class Clock:
    now: float = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def body_of(response: Response) -> dict[str, object]:
    loaded = json.loads(response.body)
    assert isinstance(loaded, dict)
    return loaded


def data_of(response: Response) -> dict[str, object]:
    data = body_of(response)["data"]
    assert isinstance(data, dict)
    return data


def get(
    service: MockService,
    target: str,
    *,
    token: str | None = None,
    cookie: str | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    sent = dict(headers or {})
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    if cookie is not None:
        sent["Cookie"] = f"{SESSION_COOKIE}={cookie}"
    return service.handle(Request("GET", target, sent, HOST))


def login(
    service: MockService,
    user: str = DEMO_USER,
    password: str = DEMO_PASSWORD,
    *,
    form: bool = False,
) -> Response:
    if form:
        content = f"username={user}&password={password}".encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    else:
        content = json.dumps({"username": user, "password": password}).encode()
        headers = {"Content-Type": "application/json"}
    return service.handle(Request("POST", "/api/login", headers, HOST, content))


def token_of(response: Response) -> str:
    return str(data_of(response)["token"])


def cookie_of(response: Response) -> str:
    set_cookie = dict(response.headers)["Set-Cookie"]
    return set_cookie.split(";")[0].split("=", 1)[1]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(clock: Clock) -> MockService:
    return MockService(clock=clock)


@pytest.fixture
def token(service: MockService) -> str:
    return token_of(login(service))


def build(clock: Clock, **options: object) -> MockService:
    return MockService(MockOptions(**options), clock=clock)  # type: ignore[arg-type]


# --- login ----------------------------------------------------------------------------------------


def test_login_issues_a_jwt_shaped_hs256_token_and_a_cookie(service: MockService) -> None:
    response = login(service)
    assert response.status == 200
    token = token_of(response)
    header = json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "=="))
    assert header == {"alg": "HS256", "typ": "JWT"}
    claims = decode_claims(f"Bearer {token}")  # the tool's own reader must understand it
    assert claims.expires_at is not None and claims.token_id
    assert data_of(response)["token_type"] == "Bearer"
    assert "HttpOnly" in dict(response.headers)["Set-Cookie"]


def test_every_login_gets_its_own_token_id(service: MockService) -> None:
    first, second = (decode_claims(token_of(login(service))) for _ in range(2))
    assert first.token_id != second.token_id


def test_login_accepts_a_form_post_too(service: MockService) -> None:
    assert login(service, form=True).status == 200


@pytest.mark.parametrize(
    ("user", "password"), [("demo", "wrong"), ("someone", DEMO_PASSWORD), ("", ""), ("demo", "")]
)
def test_wrong_credentials_get_401_and_no_cookie(
    service: MockService, user: str, password: str
) -> None:
    response = login(service, user, password)
    assert response.status == 401
    assert "Set-Cookie" not in dict(response.headers)
    assert "token" not in json.dumps(body_of(response))


def test_login_honours_a_custom_account(clock: Clock) -> None:
    service = build(clock, account=Account("ops", "a-long-secret"))
    assert login(service).status == 401, "the demo default no longer works"
    assert login(service, "ops", "a-long-secret").status == 200


def test_login_is_post_only_and_survives_garbage_bodies(service: MockService) -> None:
    assert get(service, "/api/login").status == 405
    garbage = service.handle(
        Request("POST", "/api/login", {"Content-Type": "application/json"}, HOST, b"{not json")
    )
    assert garbage.status == 401


# --- either kind of proof -------------------------------------------------------------------------


def test_the_api_accepts_the_bearer_token(service: MockService, token: str) -> None:
    assert get(service, "/api/v1/materials", token=token).status == 200


def test_the_api_accepts_the_cookie_session(service: MockService) -> None:
    cookie = cookie_of(login(service))
    assert get(service, "/api/v1/materials", cookie=cookie).status == 200


@pytest.mark.parametrize(
    "target", ["/api/v1/materials", "/api/v1/materials/mat-0001", "/api/v1/suppliers"]
)
def test_the_api_demands_a_login(service: MockService, target: str) -> None:
    response = get(service, target)
    assert response.status == 401
    assert dict(response.headers)["WWW-Authenticate"].startswith("Bearer")


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({"Authorization": "Basic abc"}, "a bearer token is expected"),
        ({"Authorization": "Bearer "}, "a bearer token is expected"),
        ({"Authorization": "Bearer not.a.token"}, "token invalid"),
        ({"Authorization": "Bearer nonsense"}, "token invalid"),
        ({"Cookie": f"{SESSION_COOKIE}=forged"}, "session invalid"),
        ({"Cookie": "other=1"}, "login required"),
    ],
)
def test_bad_proof_is_refused_with_a_reason(
    service: MockService, headers: dict[str, str], reason: str
) -> None:
    response = get(service, "/api/v1/materials", headers=headers)
    assert (response.status, body_of(response)["msg"]) == (401, reason)


def test_a_token_with_a_tampered_payload_is_refused(service: MockService, token: str) -> None:
    head, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["exp"] += 10**6
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    response = get(service, "/api/v1/materials", token=f"{head}.{forged}.{signature}")
    assert body_of(response)["msg"] == "token invalid"


def test_a_token_from_another_server_process_is_refused(clock: Clock) -> None:
    """The signing secret is random per process, so tokens never outlive the server."""
    first, second = MockService(clock=clock), MockService(clock=clock)
    foreign = token_of(login(first))
    assert get(second, "/api/v1/materials", token=foreign).status == 401


# --- trap: token expiry ---------------------------------------------------------------------------


def test_an_expired_token_gets_401_and_a_fresh_login_works_again(clock: Clock) -> None:
    service = build(clock, token_ttl_s=30)
    token = token_of(login(service))
    assert get(service, "/api/v1/materials", token=token).status == 200
    clock.now += 29
    assert get(service, "/api/v1/materials", token=token).status == 200
    clock.now += 2
    expired = get(service, "/api/v1/materials", token=token)
    assert (expired.status, body_of(expired)["msg"]) == (401, "token expired")
    assert get(service, "/api/v1/materials", token=token_of(login(service))).status == 200


def test_an_expired_cookie_session_gets_401_too(clock: Clock) -> None:
    service = build(clock, token_ttl_s=30)
    cookie = cookie_of(login(service))
    clock.now += 31
    response = get(service, "/api/v1/materials", cookie=cookie)
    assert (response.status, body_of(response)["msg"]) == (401, "session expired")


def test_the_token_carries_its_own_expiry_for_the_tools_freshness_check(clock: Clock) -> None:
    service = build(clock, token_ttl_s=120)
    claims = decode_claims(f"Bearer {token_of(login(service))}")
    assert claims.expires_at is not None
    assert claims.expires_at.timestamp() == int(clock.now + 120)


# --- materials: tree, pages, the list/detail trap -------------------------------------------------


def walk(service: MockService, token: str, per_page: int = 7) -> dict[str, list[dict[str, object]]]:
    """Every material row by folder, found the way a source would: folders breadth first."""
    found: dict[str, list[dict[str, object]]] = {}
    queue = [ROOT_FOLDER_ID]
    while queue:
        folder = queue.pop(0)
        page = 1
        while True:
            data = data_of(
                get(
                    service,
                    f"/api/v1/materials?folder_id={folder}&page={page}&per_page={per_page}",
                    token=token,
                )
            )
            rows = data["rows"]
            assert isinstance(rows, list)
            queue += [str(row["id"]) for row in rows if row["type"] == "folder"]
            found.setdefault(folder, []).extend(row for row in rows if row["type"] == "material")
            if not rows or page * per_page >= int(str(data["total"])):
                break
            page += 1
    return found


def test_the_folder_walk_finds_every_material_exactly_once(
    service: MockService, token: str
) -> None:
    found = walk(service, token)
    ids = [str(row["id"]) for rows in found.values() for row in rows]
    assert sorted(ids) == sorted(m.id for m in service.dataset.materials)
    assert len(ids) == len(set(ids))
    assert set(found) == {ROOT_FOLDER_ID, "f-woven", "f-knit", "f-twill", "f-denim", "f-jersey"}
    assert found[ROOT_FOLDER_ID], "some materials sit in the root folder"


def test_a_listing_reports_a_total_and_pages_by_number(service: MockService, token: str) -> None:
    first = data_of(get(service, "/api/v1/materials?page=1&per_page=3", token=token))
    second = data_of(get(service, "/api/v1/materials?page=2&per_page=3", token=token))
    assert first["total"] == second["total"]
    assert (first["page"], second["page"], first["per_page"]) == (1, 2, 3)
    rows = [*first["rows"], *second["rows"]]  # type: ignore[misc]
    assert len(rows) == 6 and len({row["id"] for row in rows}) == 6
    beyond = data_of(get(service, "/api/v1/materials?page=99&per_page=3", token=token))
    assert beyond["rows"] == [] and beyond["total"] == first["total"]


@pytest.mark.parametrize(
    "query",
    ["page=0", "page=x", "per_page=0", "per_page=101", "per_page=-1"],
)
def test_bad_paging_is_a_400(service: MockService, token: str, query: str) -> None:
    assert get(service, f"/api/v1/materials?{query}", token=token).status == 400


def test_an_unknown_folder_is_a_404(service: MockService, token: str) -> None:
    assert get(service, "/api/v1/materials?folder_id=nope", token=token).status == 404


def test_listing_and_detail_do_not_contain_each_other(service: MockService, token: str) -> None:
    """The trap: the row has the picture and a price of 0; the detail has the price, no picture."""
    material = service.dataset.materials[2]
    rows = walk(service, token)[material.folder_id]
    row = next(r for r in rows if r["id"] == material.id)
    assert row["price"] == 0
    assert str(row["thumbnail_url"]).endswith(f"/media/thumb/{material.id}.png")

    detail = get(service, f"/api/v1/materials/{material.id}", token=token)
    outer = data_of(detail)
    assert set(outer) == {"data"}, "the record is wrapped twice: data.data"
    record = outer["data"]
    assert isinstance(record, dict)
    assert record["price"] == str(material.price) and material.price > 0
    assert record["thumbs"] == [], "no picture where the listing has one"
    assert "thumbnail_url" not in record
    assert str(record["swatch_url"]).endswith(f"/media/swatch/{material.id}.jpg")
    assert record["weight"] == f"{material.weight_gsm} gsm"
    assert record["composition"] == material.composition


def test_an_unknown_material_is_a_404(service: MockService, token: str) -> None:
    response = get(service, "/api/v1/materials/mat-9999", token=token)
    assert (response.status, body_of(response)["state"]) == (404, False)


def test_the_data_has_the_documented_blemishes() -> None:
    dataset = generate(1, materials=24)
    assert any(m.weight_gsm == 0 for m in dataset.materials), "a weight of 0 means 'not filled in'"
    assert any("ZZ" in m.composition for m in dataset.materials), "a fibre code no table lists"
    assert {m.width[-1] for m in dataset.materials} >= {"m", '"'}, "centimetres and inches both"


# --- suppliers: cursors ---------------------------------------------------------------------------


def test_the_cursor_walk_ends_with_a_null_cursor_and_loses_nothing(
    service: MockService, token: str
) -> None:
    seen: list[str] = []
    cursor: str | None = ""
    pages = 0
    while cursor is not None:
        target = "/api/v1/suppliers?limit=4" + (f"&cursor={cursor}" if cursor else "")
        data = data_of(get(service, target, token=token))
        seen += [str(item["id"]) for item in data["items"]]  # type: ignore[attr-defined]
        cursor = data["next_cursor"]  # type: ignore[assignment]
        pages += 1
        assert pages < 20, "the walk must terminate"
    assert seen == [s.id for s in service.dataset.suppliers]
    assert pages == 3
    assert "total" not in data, "cursor pagination does not say how many there are"


def test_a_cursor_the_service_never_issued_is_a_400(service: MockService, token: str) -> None:
    assert get(service, "/api/v1/suppliers?cursor=garbage", token=token).status == 400
    assert get(service, "/api/v1/suppliers?limit=0", token=token).status == 400


def test_suppliers_have_no_folders_and_carry_their_whole_record(
    service: MockService, token: str
) -> None:
    items = data_of(get(service, "/api/v1/suppliers?limit=1", token=token))["items"]
    assert isinstance(items, list) and set(items[0]) == {
        "id", "name", "country", "rating", "lead_time_days", "certifications",
        "contact_email", "address", "listed_at",
    }  # fmt: skip
    assert items[0]["contact_email"].endswith("@example.com")


def test_a_seed_fixes_the_data_and_another_seed_changes_it() -> None:
    assert generate(5) == generate(5)
    assert generate(5).materials != generate(6).materials
    assert [s.name for s in generate(1, suppliers=12).suppliers][-1].endswith(" 2")


# --- trap: rate limit -----------------------------------------------------------------------------


def test_beyond_the_limit_the_answer_is_429_with_retry_after(clock: Clock) -> None:
    service = build(clock, rate_limit=RateLimit(3, 10))
    token = token_of(login(service))
    statuses = [get(service, "/api/v1/materials", token=token).status for _ in range(5)]
    assert statuses == [200, 200, 200, 429, 429]
    limited = get(service, "/api/v1/materials", token=token)
    assert body_of(limited)["state"] is False
    assert dict(limited.headers)["Retry-After"] == "10"


def test_retry_after_counts_down_and_the_window_reopens(clock: Clock) -> None:
    service = build(clock, rate_limit=RateLimit(2, 10))
    token = token_of(login(service))
    for _ in range(2):
        get(service, "/api/v1/materials", token=token)
    clock.now += 4
    assert dict(get(service, "/api/v1/materials", token=token).headers)["Retry-After"] == "6"
    clock.now += 6
    assert get(service, "/api/v1/materials", token=token).status == 200


def test_the_limit_applies_to_data_requests_only(clock: Clock) -> None:
    service = build(clock, rate_limit=RateLimit(1, 60))
    token = token_of(login(service))
    assert get(service, "/api/v1/materials", token=token).status == 200
    assert get(service, "/api/v1/materials", token=token).status == 429
    assert login(service).status == 200, "logging in is never limited"
    assert get(service, "/media/thumb/mat-0001.png").status == 200, "pictures are not either"
    assert get(service, "/terms").status == 200
    assert get(service, "/api/v1/materials").status == 401, "login is checked before the limit"


def test_rate_limit_text_is_parsed_and_checked() -> None:
    assert RateLimit.parse("30/10") == RateLimit(30, 10.0)
    for bad in ("30", "a/b", "0/10", "5/0", "/"):
        with pytest.raises(ValueError, match=r"COUNT/SECONDS|at least one"):
            RateLimit.parse(bad)


# --- trap: a login that runs out after N requests -------------------------------------------------


def test_a_login_works_for_n_data_requests_and_then_answers_401(clock: Clock) -> None:
    service = build(clock, expire_after=3)
    token = token_of(login(service))
    statuses = [get(service, "/api/v1/suppliers", token=token).status for _ in range(5)]
    assert statuses == [200, 200, 200, 401, 401]
    refused = get(service, "/api/v1/suppliers", token=token)
    assert dict(refused.headers)["WWW-Authenticate"].startswith("Bearer")


def test_a_new_login_gives_another_n_requests_and_the_clock_plays_no_part(clock: Clock) -> None:
    service = build(clock, expire_after=2)
    first = token_of(login(service))
    assert [get(service, "/api/v1/materials", token=first).status for _ in range(3)] == [
        200,
        200,
        401,
    ]
    clock.now += 1  # the token itself is still young: only the count ran out
    second = token_of(login(service))
    assert [get(service, "/api/v1/materials", token=second).status for _ in range(3)] == [
        200,
        200,
        401,
    ]


def test_only_authenticated_data_requests_use_up_the_login(clock: Clock) -> None:
    service = build(clock, expire_after=2)
    token = token_of(login(service))
    for _ in range(4):
        get(service, "/api/v1/materials")  # no credential: 401 without using anything up
        get(service, "/media/thumb/mat-0001.png")  # pictures are not data requests
        get(service, "/terms")
    assert [get(service, "/api/v1/materials", token=token).status for _ in range(3)] == [
        200,
        200,
        401,
    ]


def test_expire_after_zero_never_expires(clock: Clock) -> None:
    service = build(clock)
    token = token_of(login(service))
    assert {get(service, "/api/v1/suppliers", token=token).status for _ in range(30)} == {200}


# --- trap: occasional 5xx -------------------------------------------------------------------------


def test_every_nth_data_request_fails_with_503(clock: Clock) -> None:
    service = build(clock, error_every=3)
    token = token_of(login(service))
    statuses = [get(service, "/api/v1/suppliers", token=token).status for _ in range(7)]
    assert statuses == [200, 200, 503, 200, 200, 503, 200]


def test_unauthenticated_requests_do_not_advance_the_failure_count(clock: Clock) -> None:
    service = build(clock, error_every=2)
    token = token_of(login(service))
    for _ in range(5):
        get(service, "/api/v1/materials")
    assert [get(service, "/api/v1/materials", token=token).status for _ in range(2)] == [200, 503]


# --- trap: details that are gone ------------------------------------------------------------------


def test_the_detail_of_every_nth_material_is_gone_and_the_listing_still_shows_it(
    clock: Clock,
) -> None:
    service = build(clock, materials=12, gone_every=4)
    token = token_of(login(service))
    statuses = {
        f"mat-{number:04d}": get(service, f"/api/v1/materials/mat-{number:04d}", token=token).status
        for number in range(1, 13)
    }
    assert [i for i, status in statuses.items() if status == 404] == [
        "mat-0004",
        "mat-0008",
        "mat-0012",
    ]
    gone = service.dataset.material("mat-0004")
    assert gone is not None
    listed = data_of(get(service, f"/api/v1/materials?folder_id={gone.folder_id}", token=token))
    rows = listed["rows"]
    assert isinstance(rows, list)
    assert "mat-0004" in {row["id"] for row in rows}


def test_a_gone_detail_stays_gone_on_every_request(clock: Clock) -> None:
    """Unlike --error-every this is a property of the item, not of the request count: a retry and
    a later run meet the same 404."""
    service = build(clock, materials=8, gone_every=4)
    token = token_of(login(service))
    answers = [get(service, "/api/v1/materials/mat-0004", token=token).status for _ in range(3)]
    assert answers == [404, 404, 404]
    assert get(service, "/api/v1/materials/mat-0003", token=token).status == 200


def test_gone_every_zero_leaves_every_detail_in_place(clock: Clock) -> None:
    service = build(clock, materials=8)
    token = token_of(login(service))
    assert {
        get(service, f"/api/v1/materials/mat-{n:04d}", token=token).status for n in range(1, 9)
    } == {200}


def test_a_gone_detail_does_not_advance_the_503_count(clock: Clock) -> None:
    service = build(clock, materials=8, gone_every=4, error_every=2)
    token = token_of(login(service))
    first = get(service, "/api/v1/materials/mat-0001", token=token).status
    second = get(service, "/api/v1/materials/mat-0004", token=token).status
    assert (first, second) == (200, 503), "the 2nd data request fails with 503 before the 404"


# --- pictures, plain and signed -------------------------------------------------------------------


def test_pictures_are_real_png_and_jpeg_and_always_the_same(service: MockService) -> None:
    thumb = get(service, "/media/thumb/mat-0001.png")
    swatch = get(service, "/media/swatch/mat-0001.jpg")
    assert (thumb.status, thumb.content_type) == (200, "image/png")
    assert thumb.body.startswith(b"\x89PNG\r\n\x1a\n")
    assert (swatch.status, swatch.content_type) == (200, "image/jpeg")
    assert swatch.body.startswith(b"\xff\xd8\xff")
    assert get(service, "/media/thumb/mat-0001.png").body == thumb.body
    assert get(service, "/media/thumb/mat-0002.png").body != thumb.body
    assert get(service, "/media/thumb/mat-9999.png").status == 404


def test_pictures_need_no_login_and_ignore_a_bearer_token(service: MockService, token: str) -> None:
    """Like a storage host: the token is neither needed nor looked at."""
    assert get(service, "/media/thumb/mat-0001.png").status == 200
    assert get(service, "/media/thumb/mat-0001.png", token="garbage").status == 200


def test_plain_links_carry_no_signature(service: MockService, token: str) -> None:
    rows = data_of(get(service, "/api/v1/materials", token=token))["rows"]
    url = next(str(r["thumbnail_url"]) for r in rows if r["type"] == "material")  # type: ignore[attr-defined]
    assert urlsplit(url).query == ""


def signed_url(service: MockService, token: str) -> str:
    rows = data_of(get(service, "/api/v1/materials", token=token))["rows"]
    return next(str(r["thumbnail_url"]) for r in rows if r["type"] == "material")  # type: ignore[attr-defined]


def target_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


def test_a_signed_link_works_then_expires(clock: Clock) -> None:
    service = build(clock, signed_url_ttl_s=60)
    token = token_of(login(service))
    url = signed_url(service, token)
    query = parse_qs(urlsplit(url).query)
    assert set(query) == {"exp", "sig"} and int(query["exp"][0]) == int(clock.now + 60)
    assert get(service, target_of(url)).status == 200
    clock.now += 59
    assert get(service, target_of(url)).status == 200
    clock.now += 2
    expired = get(service, target_of(url))
    assert (expired.status, body_of(expired)["msg"]) == (403, "signed link expired")


def test_a_signed_link_cannot_be_forged_or_reused_for_another_picture(clock: Clock) -> None:
    service = build(clock, signed_url_ttl_s=60)
    url = signed_url(service, token_of(login(service)))
    target = target_of(url)
    assert get(service, target.replace("sig=", "sig=0")).status == 403
    assert get(service, target.replace("mat-0001", "mat-0002")).status == 403
    assert get(service, target.split("?")[0]).status == 403, "no signature at all"
    longer = target.replace("exp=", "exp=9")
    assert get(service, longer).status == 403, "a later expiry does not match its signature"


def test_swatch_links_are_signed_too(clock: Clock) -> None:
    service = build(clock, signed_url_ttl_s=60)
    token = token_of(login(service))
    record = data_of(get(service, "/api/v1/materials/mat-0001", token=token))["data"]
    assert isinstance(record, dict)
    assert get(service, target_of(str(record["swatch_url"]))).status == 200


# --- the request log, the pages -------------------------------------------------------------------


def test_the_log_records_path_status_and_what_proof_was_sent_but_never_a_query(
    clock: Clock,
) -> None:
    service = build(clock, signed_url_ttl_s=60)
    token = token_of(login(service))
    url = signed_url(service, token)
    get(service, target_of(url), token="would-be-leaked")
    log = service.request_log
    assert [(e.method, e.path, e.status) for e in log] == [
        ("POST", "/api/login", 200),
        ("GET", "/api/v1/materials", 200),
        ("GET", "/media/thumb/mat-0001.png", 200),
    ]
    assert [e.sent_authorization for e in log] == [False, True, True]
    assert "sig=" not in repr(log) and "exp=" not in repr(log)
    assert service.request_log is not service.request_log, "callers get a copy"


def test_the_pages_have_what_a_browser_login_looks_for(service: MockService) -> None:
    login_page = get(service, "/login").body.decode()
    for selector in ('id="username"', 'id="password"', 'id="sign-in"', "/api/login"):
        assert selector in login_page
    app = get(service, "/app").body.decode()
    assert "Bearer" in app and "/api/v1/materials" in app and "/api/v1/suppliers" in app
    terms = get(service, "/terms")
    assert terms.status == 200 and "fake" in terms.body.decode()


def test_no_page_loads_anything_from_elsewhere(service: MockService) -> None:
    for page in ("/login", "/app", "/terms"):
        html = get(service, page).body.decode()
        assert "http://" not in html and "https://" not in html
        assert "innerHTML" not in html, "API values go on the page as text, never as markup"


def test_unknown_paths_and_methods(service: MockService, token: str) -> None:
    assert get(service, "/nope").status == 404
    assert get(service, "/api/v1/nope", token=token).status == 404
    post = service.handle(Request("POST", "/api/v1/materials", {}, HOST))
    assert post.status == 404
    assert get(service, "/").status == 302
    assert dict(get(service, "/").headers)["Location"] == "/login"


def test_the_app_page_asks_the_api_for_nothing_without_a_token(service: MockService) -> None:
    """A browser login watches for the first request that carries a bearer token. A page that sent
    `Bearer null` before redirecting would put a worthless one in front of it."""
    script = get(service, "/app").body.decode().split("<script>")[1]
    guard = script.index("if (!token)")
    assert script.index('call("/api/v1/materials') > guard
    assert script.index('call("/api/v1/suppliers') > guard
    assert "else" in script[guard : script.index('call("/api/v1/materials')]
