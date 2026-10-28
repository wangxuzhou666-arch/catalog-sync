"""The SDK's response checks, connection and picture handling, with a transport that records.

The shipped connectors' own tests cover these through a whole source; here each rule is shown on
its own so that a change to one can be told from a change to a connector.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from catalog_sync.credentials.base import Credential, CredentialHandle
from catalog_sync.errors import (
    AuthExpired,
    HttpStatusError,
    ItemFailed,
    ResponseTooLarge,
    SourceShapeError,
)
from catalog_sync.http import HttpSeams
from catalog_sync.images import ImagePolicy
from catalog_sync.sdk import (
    Connection,
    PictureAssets,
    https_only,
    item_scoped,
    parse_detail,
    same_host,
    shape_context,
)
from catalog_sync.sources.base import AssetRef, ListedItem
from catalog_sync.testing.fake_transport import FakeClock, FakeResponse, FakeTransport
from tests.fixtures import sample_items

# --- response checks ------------------------------------------------------------------------------


def test_a_shape_error_inside_the_block_is_prefixed_with_the_call() -> None:
    with (
        pytest.raises(SourceShapeError, match=r"^folder 7 page 2: data\.rows is not a list$"),
        shape_context("folder 7 page 2"),
    ):
        raise SourceShapeError("data.rows is not a list")


def test_other_errors_pass_through_the_shape_context_untouched() -> None:
    with pytest.raises(KeyError), shape_context("anything"):
        raise KeyError("x")


def test_a_detail_is_unwrapped_exactly_twice_by_default() -> None:
    record = {"id": "a", "data": {"x": 1}}
    assert parse_detail({"state": True, "data": {"data": record}}) == record
    with pytest.raises(SourceShapeError, match="level 2 of 2"):
        parse_detail({"state": True, "data": {"id": "a"}})
    with pytest.raises(SourceShapeError, match="the detail record is empty"):
        parse_detail({"state": True, "data": {"data": {}}})
    with pytest.raises(SourceShapeError, match="state is false"):
        parse_detail({"state": False, "data": {"data": record}})


def test_a_detail_may_sit_under_one_envelope_when_the_service_wraps_once() -> None:
    assert parse_detail({"state": True, "data": {"id": "a"}}, depth=1) == {"id": "a"}


@pytest.mark.parametrize("status", [400, 404, 410, 422])
def test_a_4xx_about_one_item_fails_that_item_with_the_status(status: int) -> None:
    with (
        pytest.raises(ItemFailed, match=rf"^i1: detail request answered HTTP {status}$"),
        item_scoped("i1", "detail request"),
    ):
        raise HttpStatusError(status, "x")


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_status_that_says_something_about_the_service_is_not_item_scoped(status: int) -> None:
    with pytest.raises(HttpStatusError) as caught, item_scoped("i1", "detail request"):
        raise HttpStatusError(status, "x")
    assert caught.value.status == status


def test_a_known_cause_is_added_to_the_message_for_that_status_only() -> None:
    hints = {403: "the link expired"}
    with (
        pytest.raises(ItemFailed, match=r"answered HTTP 403 \(the link expired\)$"),
        item_scoped("i1", "picture 'thumb'", hints=hints),
    ):
        raise HttpStatusError(403, "x")
    with (
        pytest.raises(ItemFailed, match=r"answered HTTP 404$"),
        item_scoped("i1", "picture 'thumb'", hints=hints),
    ):
        raise HttpStatusError(404, "x")


def test_a_download_over_its_limit_fails_the_item() -> None:
    with (
        pytest.raises(ItemFailed, match=r"^i1: picture 'thumb': response is larger than"),
        item_scoped("i1", "picture 'thumb'"),
    ):
        raise ResponseTooLarge("response is larger than the limit of 5 bytes")


# --- the connection -------------------------------------------------------------------------------


class Reads:
    """A credential handle that counts how often it is read."""

    def __init__(self) -> None:
        self.count = 0

    def handle(self) -> CredentialHandle:
        def load() -> Credential:
            self.count += 1
            return Credential({"authorization": "Bearer unused"}, {}, None, None)

        return CredentialHandle("c", load, "renew with: catalog-sync login c")


def make_connection(
    handle: CredentialHandle, transport: FakeTransport, clock: FakeClock, **options: object
) -> Connection:
    settings: dict[str, object] = {"gap_s": 0.0, "max_requests_per_minute": None, **options}
    return Connection(
        credential=handle,
        api_headers=lambda credential: {"x-api": "1", **credential.headers},
        asset_headers={"accept": "image/*"},
        seams=HttpSeams(adapter=transport, sleep=clock.sleep, monotonic=clock.monotonic),
        **settings,  # type: ignore[arg-type]
    )


def test_nothing_is_read_or_sent_until_it_is_used() -> None:
    reads = Reads()
    transport = FakeTransport()
    make_connection(reads.handle(), transport, FakeClock())
    assert reads.count == 0 and transport.requests == []


def test_the_credential_is_read_once_however_many_clients_ask() -> None:
    reads = Reads()
    connection = make_connection(reads.handle(), FakeTransport(), FakeClock())
    connection.credential()
    connection.api()
    connection.credential()
    assert reads.count == 1
    assert connection.api() is connection.api(), "one API client"
    assert connection.assets() is connection.assets()


def test_the_api_client_carries_the_credential_and_the_asset_client_never_does() -> None:
    transport = FakeTransport()
    transport.queue(FakeResponse.json({"ok": 1}), FakeResponse(200, b"x"))
    connection = make_connection(Reads().handle(), transport, FakeClock())
    connection.api().get_json("https://api.example.invalid/x")
    connection.assets().get_bytes("https://cdn.example.invalid/p.png", max_bytes=10)
    api, picture = transport.requests
    assert api.header("authorization") == "Bearer unused" and api.header("x-api") == "1"
    assert picture.header("authorization") is None and picture.header("x-api") is None
    assert picture.header("accept") == "image/*"


def test_a_credential_the_service_refuses_names_how_to_renew_it() -> None:
    transport = FakeTransport()
    transport.queue(FakeResponse(401))
    connection = make_connection(Reads().handle(), transport, FakeClock())
    with pytest.raises(AuthExpired, match="catalog-sync login c"):
        connection.api().get_json("https://api.example.invalid/x")


def test_both_clients_draw_on_one_rate_window() -> None:
    clock = FakeClock()
    times: list[float] = []

    def respond(_request: object) -> FakeResponse:
        times.append(clock.now)
        return FakeResponse(200, b"{}")

    transport = FakeTransport(respond)
    connection = make_connection(Reads().handle(), transport, clock, max_requests_per_minute=2)
    for _ in range(3):
        connection.api().get_json("https://api.example.invalid/x")
        connection.assets().get_bytes("https://cdn.example.invalid/p", max_bytes=10)
    in_first_minute = [t for t in times if t - times[0] < 60]
    assert len(in_first_minute) <= 2, "api and assets together never exceed the declared two"
    assert len(times) == 6


def test_without_a_ceiling_nothing_waits() -> None:
    clock = FakeClock()
    transport = FakeTransport(lambda _request: FakeResponse(200, b"{}"))
    connection = make_connection(Reads().handle(), transport, clock)
    for _ in range(5):
        connection.api().get_json("https://api.example.invalid/x")
    assert clock.sleeps == []


# --- pictures -------------------------------------------------------------------------------------

API = "https://api.example.invalid"
CDN = "https://cdn.example.invalid"
ROLES = {"thumb": ("list.image.url", "detail.image_url"), "swatch": ("detail.swatch",)}


def png_for(_request: object) -> FakeResponse:
    return FakeResponse(200, sample_items.png_bytes(1, "thumb"))


def make_pictures(
    transport: FakeTransport, rule: object, *, policy: ImagePolicy | None = None
) -> PictureAssets:
    connection = make_connection(Reads().handle(), transport, FakeClock())
    return PictureAssets(
        connection,
        roles=ROLES,
        url_rule=rule,  # type: ignore[arg-type]
        policy=policy or ImagePolicy(),
    )


def listed(payload: dict[str, object]) -> ListedItem:
    return ListedItem("i1", None, payload, {})


def test_the_first_expression_with_a_url_wins_and_the_row_is_read_before_the_detail() -> None:
    pictures = make_pictures(FakeTransport(), https_only())
    row = {"image": {"url": f"{CDN}/from-row.png"}}
    detail = {"image_url": f"{CDN}/from-detail.png", "swatch": f"{CDN}/s.png"}
    thumb, swatch = pictures.refs(listed(row), detail)
    assert (thumb.role, thumb.url) == ("thumb", f"{CDN}/from-row.png")
    assert (swatch.role, swatch.url) == ("swatch", f"{CDN}/s.png")


def test_an_empty_or_absent_value_falls_through_to_the_next_expression() -> None:
    pictures = make_pictures(FakeTransport(), https_only())
    detail = {"image_url": f"{CDN}/from-detail.png"}
    (thumb,) = pictures.refs(listed({"image": {"url": ""}}), detail)
    assert thumb.url.endswith("from-detail.png")
    assert pictures.refs(listed({}), None) == []
    assert pictures.refs(listed({}), {"image_url": None}) == []


def test_the_stored_url_loses_its_query_but_the_download_keeps_it() -> None:
    transport = FakeTransport(png_for)
    pictures = make_pictures(transport, https_only())
    signed = f"{CDN}/p.png?sig=SECRET&exp=9"
    (ref,) = pictures.refs(listed({"image": {"url": signed}}), None)
    assert ref == AssetRef("i1", "thumb", f"{CDN}/p.png") and "SECRET" not in repr(ref)
    pictures.fetch(ref)
    assert transport.requests[-1].url == signed


def test_a_ref_from_another_process_has_only_the_stripped_link() -> None:
    transport = FakeTransport(png_for)
    other_process = make_pictures(transport, https_only())
    other_process.fetch(AssetRef("i1", "thumb", f"{CDN}/p.png"))
    assert transport.requests[-1].url == f"{CDN}/p.png"


def test_a_picture_is_fetched_without_credentials_and_comes_back_a_jpeg() -> None:
    transport = FakeTransport(png_for)
    pictures = make_pictures(transport, https_only())
    (ref,) = pictures.refs(listed({"image": {"url": f"{CDN}/p.png"}}), None)
    image = Image.open(io.BytesIO(pictures.fetch(ref)))
    assert image.format == "JPEG"
    assert transport.requests[-1].header("authorization") is None


@pytest.mark.parametrize(
    "url", ["http://cdn.example.invalid/a.png", "//cdn.example.invalid/a.png", "a.png", "ftp://h/a"]
)
def test_https_only_refuses_everything_else_without_quoting_the_link(url: str) -> None:
    pictures = make_pictures(FakeTransport(), https_only())
    with pytest.raises(
        SourceShapeError, match="i1: picture 'thumb' is not an absolute https URL"
    ) as failure:
        pictures.refs(listed({"image": {"url": url + "?sig=SECRET"}}), None)
    assert "SECRET" not in str(failure.value)


def test_same_host_accepts_only_the_apis_own_host() -> None:
    pictures = make_pictures(FakeTransport(), same_host(API))
    pictures.refs(listed({"image": {"url": f"{API}/media/p.png"}}), None)
    for foreign in (f"{CDN}/p.png", "https://api.example.invalid.evil.test/p.png", "/media/p.png"):
        with pytest.raises(SourceShapeError, match="is not on the service's own host"):
            pictures.refs(listed({"image": {"url": foreign}}), None)


def test_a_picture_field_that_is_not_text_is_a_shape_error() -> None:
    pictures = make_pictures(FakeTransport(), https_only())
    with pytest.raises(SourceShapeError, match=r"i1: list\.image\.url is not a string"):
        pictures.refs(listed({"image": {"url": 12345}}), None)


def test_a_403_on_a_picture_says_the_signed_link_may_have_expired() -> None:
    transport = FakeTransport(lambda _request: FakeResponse(403))
    pictures = make_pictures(transport, https_only())
    with pytest.raises(ItemFailed, match=r"i1: picture 'thumb' answered HTTP 403 \(a signed link"):
        pictures.fetch(AssetRef("i1", "thumb", f"{CDN}/p.png"))


def test_a_404_on_a_picture_fails_that_item_but_a_500_stops_the_run() -> None:
    gone = make_pictures(FakeTransport(lambda _request: FakeResponse(404)), https_only())
    with pytest.raises(ItemFailed, match="answered HTTP 404"):
        gone.fetch(AssetRef("i1", "thumb", f"{CDN}/p.png"))
    broken = make_pictures(FakeTransport(lambda _request: FakeResponse(500)), https_only())
    with pytest.raises(HttpStatusError):
        broken.fetch(AssetRef("i1", "thumb", f"{CDN}/p.png"))


def test_a_picture_that_does_not_fit_the_budget_fails_that_item() -> None:
    noise = io.BytesIO()
    Image.effect_noise((300, 300), 80).convert("RGB").save(noise, "PNG")
    transport = FakeTransport(lambda _request: FakeResponse(200, noise.getvalue()))
    policy = ImagePolicy(max_bytes=1024, qualities=(90, 50))
    pictures = make_pictures(transport, https_only(), policy=policy)
    with pytest.raises(ItemFailed, match="does not fit 1024 bytes"):
        pictures.fetch(AssetRef("i1", "thumb", f"{CDN}/p.png"))


def test_a_download_larger_than_the_input_cap_is_refused_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("catalog_sync.sdk.assets.MAX_INPUT_BYTES", 8)
    transport = FakeTransport(lambda _request: FakeResponse(200, b"x" * 100))
    pictures = make_pictures(transport, https_only())
    with pytest.raises(ItemFailed, match="i1: picture 'thumb': response is larger"):
        pictures.fetch(AssetRef("i1", "thumb", f"{CDN}/p.png"))
