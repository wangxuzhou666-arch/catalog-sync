"""The mock connector, against the real Mock SaaS service through the fake transport.

No socket is opened: ``MockService`` answers the plugin's requests directly, and one clock serves
both sides, so Retry-After, backoff and token lifetimes are exercised without waiting.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PIL import Image

from catalog_sync.credentials.base import Credential, CredentialHandle
from catalog_sync.credentials.browser import grab_headers
from catalog_sync.credentials.curl_paste import credential_values, parse_curl
from catalog_sync.credentials.jwt import decode_claims
from catalog_sync.errors import (
    AuthExpired,
    ComplianceError,
    ConfigError,
    FuseTripped,
    HttpStatusError,
    ItemFailed,
    LoginFailed,
    SourceShapeError,
)
from catalog_sync.http import HttpSeams
from catalog_sync.mock import auth as server_auth
from catalog_sync.mock.service import MockOptions, RateLimit, Request
from catalog_sync.models import JsonObject
from catalog_sync.plugins.mock import login as plugin_login
from catalog_sync.plugins.mock.api import (
    MaterialPage,
    is_folder_row,
    parse_detail,
    parse_material_page,
    parse_supplier_page,
)
from catalog_sync.plugins.mock.login import (
    account_from_env,
    form_login,
    mock_credential_kind,
    verify_live,
)
from catalog_sync.plugins.mock.settings import MockSettings
from catalog_sync.plugins.mock.source import FRONTIER_KEY, MockMaterialsSource, MockSuppliersSource
from catalog_sync.sdk import FolderWalker, WalkSettings
from catalog_sync.sources.base import AssetRef, Inspectable, ListedItem
from catalog_sync.state import Checkpoint, StateStore
from catalog_sync.testing.fake_transport import FakeResponse, FakeTransport
from tests.doubles import SimulatedCrash
from tests.fakes_browser import FakeBrowser
from tests.fixtures.mock_saas import BASE_URL, World

HANDLE = CredentialHandle(
    "mock", lambda: Credential({"authorization": "Bearer unused"}, {}, None, None), "renew"
)


class Rig:
    """A world plus the two sources wired to it."""

    def __init__(self, **mock_options: object) -> None:
        self.world = World(MockOptions(**mock_options))  # type: ignore[arg-type]
        self.handle = self.world.handle()

    def options(self, **overrides: object) -> dict[str, object]:
        return {"credential": self.handle, "request_gap_s": 0, **overrides}

    def materials(self, **overrides: object) -> MockMaterialsSource:
        settings = MockSettings.from_options(self.options(**overrides), source="mock.materials")
        return MockMaterialsSource(settings, seams=self.world.seams)

    def suppliers(self, **overrides: object) -> MockSuppliersSource:
        settings = MockSettings.from_options(self.options(**overrides), source="mock.suppliers")
        return MockSuppliersSource(settings, seams=self.world.seams)


@pytest.fixture
def checkpoint(store: StateStore) -> Checkpoint:
    store.create_run("run-1", "mock.materials")
    return store.checkpoint("run-1")


def listed(source: MockMaterialsSource, checkpoint: Checkpoint) -> list[ListedItem]:
    return list(source.enumerate(checkpoint))


# --- options --------------------------------------------------------------------------------------


def settings(source: str = "mock.materials", **options: object) -> MockSettings:
    return MockSettings.from_options({"credential": HANDLE, **options}, source=source)


def test_the_defaults_are_for_a_mock_on_loopback() -> None:
    chosen = settings()
    assert (chosen.base_url, chosen.page_size, chosen.root_folder_id) == (BASE_URL, 10, "0")
    assert chosen.max_requests_per_minute is None


def test_option_values_may_be_strings_as_from_the_command_line() -> None:
    chosen = settings(page_size="4", max_depth="2", request_gap_s="0.5")
    assert (chosen.page_size, chosen.max_depth, chosen.request_gap_s) == (4, 2, 0.5)


@pytest.mark.parametrize(
    ("source", "options", "complaint"),
    [
        ("mock.materials", {"nope": 1}, r"unknown option\(s\) \['nope'\]"),
        ("mock.suppliers", {"root_folder_id": "1"}, "unknown option"),
        ("mock.suppliers", {"images": {}}, "unknown option"),
        ("mock.materials", {"page_size": 0}, "'page_size' must be an integer between 1 and 50"),
        ("mock.materials", {"page_size": 51}, "between 1 and 50"),
        ("mock.materials", {"max_depth": -1}, "'max_depth' must be an integer"),
        ("mock.materials", {"request_gap_s": -1}, "'request_gap_s' must be a number >= 0"),
        ("mock.materials", {"request_gap_s": "fast"}, "must be a number"),
        ("mock.materials", {"base_url": "ftp://x"}, "'base_url' must look like"),
        ("mock.materials", {"base_url": "http://x/api"}, "'base_url' must look like"),
        ("mock.materials", {"base_url": "localhost:8765"}, "'base_url' must look like"),
        ("mock.materials", {"base_url": "http://[::1:8765"}, r"is not a valid http\(s\) address"),
        ("mock.materials", {"base_url": "http://ops:hunter2@x:8765"}, "must not carry a user name"),
        ("mock.materials", {"root_folder_id": ""}, "'root_folder_id' must be a non-empty"),
        ("mock.materials", {"images": [1]}, "'images' must be a table"),
    ],
)
def test_a_bad_option_is_a_config_error_naming_it(
    source: str, options: dict[str, object], complaint: str
) -> None:
    with pytest.raises(ConfigError, match=complaint):
        settings(source, **options)


def test_a_source_needs_a_credential() -> None:
    with pytest.raises(ConfigError, match="needs `credential"):
        MockSettings.from_options({}, source="mock.suppliers")
    with pytest.raises(ConfigError, match="needs `credential"):
        MockSettings.from_options({"credential": "mock"}, source="mock.suppliers")


def test_a_trailing_slash_on_the_base_url_is_dropped() -> None:
    assert settings(base_url="http://127.0.0.1:9/").base_url == "http://127.0.0.1:9"


def test_the_request_ceiling_raises_an_unset_gap_and_refuses_a_shorter_one() -> None:
    assert settings(max_requests_per_minute=60).request_gap_s == 1.0
    assert settings(max_requests_per_minute=600, request_gap_s=0.5).request_gap_s == 0.5
    with pytest.raises(ComplianceError, match="shorter than 60 / max_requests_per_minute"):
        settings(max_requests_per_minute=60, request_gap_s=0.5)


# --- reading the service's answers ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "complaint"),
    [
        ({"state": False, "msg": "no"}, "state is false"),
        ({"data": {}}, "no boolean 'state'"),
        ({"state": True, "data": {"rows": []}}, "data.total is not a non-negative integer"),
        ({"state": True, "data": {"total": -1, "rows": []}}, "data.total"),
        ({"state": True, "data": {"total": True, "rows": []}}, "data.total"),
        ({"state": True, "data": {"total": 1}}, "data.rows is not a list of objects"),
        ({"state": True, "data": {"total": 1, "rows": [1]}}, "data.rows is not a list"),
        ({"state": True, "data": [1]}, "expected an object under 'data'"),
    ],
)
def test_a_material_page_of_the_wrong_shape_is_a_source_shape_error(
    payload: dict[str, object], complaint: str
) -> None:
    with pytest.raises(SourceShapeError, match=complaint):
        parse_material_page(payload)


def test_a_detail_is_unwrapped_exactly_twice() -> None:
    assert parse_detail({"state": True, "data": {"data": {"id": "a", "data": {"x": 1}}}}) == {
        "id": "a",
        "data": {"x": 1},
    }, "a record's own field called data survives"
    with pytest.raises(SourceShapeError, match="level 2 of 2"):
        parse_detail({"state": True, "data": {"id": "a"}})
    with pytest.raises(SourceShapeError, match="empty"):
        parse_detail({"state": True, "data": {"data": {}}})


@pytest.mark.parametrize(
    "payload",
    [
        {"state": True, "data": {"items": "x", "next_cursor": None}},
        {"state": True, "data": {"items": [], "next_cursor": ""}},
        {"state": True, "data": {"items": [], "next_cursor": 5}},
    ],
)
def test_a_supplier_page_of_the_wrong_shape_is_refused(payload: dict[str, object]) -> None:
    with pytest.raises(SourceShapeError):
        parse_supplier_page(payload)


# --- the folder walk ------------------------------------------------------------------------------


def row(item_id: str, name: str = "n") -> JsonObject:
    return {"id": item_id, "type": "material", "name": name}


def folder(folder_id: str, name: str) -> JsonObject:
    return {"id": folder_id, "type": "folder", "name": name}


@dataclass
class Folders:
    """Serves a folder tree page by page and remembers every request."""

    tree: dict[str, list[JsonObject]]
    per_page: int = 2
    totals: dict[str, int] = field(default_factory=dict)
    crash_on_call: int | None = None
    calls: list[tuple[str, int]] = field(default_factory=list)

    def __call__(self, folder_id: str, page: int) -> MaterialPage:
        self.calls.append((folder_id, page))
        if self.crash_on_call == len(self.calls):
            raise SimulatedCrash
        rows = self.tree.get(folder_id, [])
        window = rows[(page - 1) * self.per_page : page * self.per_page]
        return MaterialPage(self.totals.get(folder_id, len(rows)), tuple(window))


TREE = {
    "0": [row("r1"), folder("A", "Alpha"), folder("B", "Beta")],
    "A": [row("a1"), row("a2"), row("shared"), folder("A2", "Alpha Sub")],
    "A2": [row("a3")],
    "B": [row("b1"), row("shared")],
}


def walker(
    fetch: Folders, *, max_depth: int = 6, max_pages: int = 100, page_size: int = 10
) -> FolderWalker:
    return FolderWalker(
        fetch,
        WalkSettings("0", max_depth, max_pages, page_size),
        frontier_key=FRONTIER_KEY,
        is_folder=is_folder_row,
        resume="folder",
    )


def found(sightings: object) -> list[tuple[str, str]]:
    return [(s.source_id, s.folder_path) for s in sightings]  # type: ignore[attr-defined]


def test_the_tree_is_walked_breadth_first_and_every_item_reports_its_folder(
    checkpoint: Checkpoint,
) -> None:
    sightings = list(walker(Folders(TREE)).walk(checkpoint))
    assert found(sightings) == [
        ("r1", ""),
        ("a1", "Alpha"),
        ("a2", "Alpha"),
        ("shared", "Alpha"),
        ("b1", "Beta"),
        ("shared", "Beta"),
        ("a3", "Alpha/Alpha Sub"),
    ]


def test_every_page_of_a_folder_is_read_until_the_total_is_reached(checkpoint: Checkpoint) -> None:
    fetch = Folders(TREE, per_page=2)
    list(walker(fetch).walk(checkpoint))
    assert [call for call in fetch.calls if call[0] == "A"] == [("A", 1), ("A", 2)]


def test_an_empty_page_ends_a_folder_even_when_the_total_promises_more(
    checkpoint: Checkpoint, caplog: pytest.LogCaptureFixture
) -> None:
    fetch = Folders({"0": [row("x")]}, totals={"0": 5})
    with caplog.at_level(logging.WARNING):
        assert found(walker(fetch).walk(checkpoint)) == [("x", "")]
    assert "reports 5 rows but stopped after 1" in caplog.text


def test_a_total_smaller_than_what_was_delivered_does_not_loop(checkpoint: Checkpoint) -> None:
    fetch = Folders({"0": [row("x"), row("y"), row("z")]}, totals={"0": 1}, per_page=2)
    assert [s.source_id for s in walker(fetch).walk(checkpoint)] == ["x", "y"]


def test_a_folder_that_contains_its_parent_is_visited_once(checkpoint: Checkpoint) -> None:
    cyclic = {"0": [folder("A", "A")], "A": [row("a"), folder("0", "Root again"), folder("A", "A")]}
    fetch = Folders(cyclic)
    assert found(walker(fetch).walk(checkpoint)) == [("a", "A")]
    assert fetch.calls == [("0", 1), ("A", 1), ("A", 2)], "no folder is read twice"


def test_a_tree_deeper_than_max_depth_trips_the_fuse(checkpoint: Checkpoint) -> None:
    chain = {"0": [folder("1", "one")], "1": [folder("2", "two")], "2": [folder("3", "three")]}
    with pytest.raises(FuseTripped, match="deeper than max_depth=2"):
        list(walker(Folders(chain), max_depth=2).walk(checkpoint))


def test_a_folder_with_too_many_pages_trips_the_fuse(checkpoint: Checkpoint) -> None:
    fetch = Folders({"0": [row(str(i)) for i in range(9)]}, per_page=2)
    with pytest.raises(FuseTripped, match="more than 3 pages"):
        list(walker(fetch, max_pages=3).walk(checkpoint))


def test_a_row_without_an_id_is_a_source_shape_error(checkpoint: Checkpoint) -> None:
    with pytest.raises(SourceShapeError, match="no usable 'id'"):
        list(walker(Folders({"0": [{"type": "material"}]})).walk(checkpoint))


def test_a_killed_walk_resumes_without_losing_an_item(checkpoint: Checkpoint) -> None:
    delivered: list[str] = []
    crashing = Folders(TREE, crash_on_call=4)  # dies while reading the third folder
    with pytest.raises(SimulatedCrash):
        for sighting in walker(crashing).walk(checkpoint):
            delivered.append(sighting.source_id)
    resumed = Folders(TREE)
    delivered += [s.source_id for s in walker(resumed).walk(checkpoint)]
    assert {"r1", "a1", "a2", "shared", "b1", "a3"} <= set(delivered)
    assert resumed.calls[0][0] != "0", "the root, already finished, is not read again"


def test_a_finished_walk_resumed_has_nothing_left_to_do(checkpoint: Checkpoint) -> None:
    list(walker(Folders(TREE)).walk(checkpoint))
    again = Folders(TREE)
    assert list(walker(again).walk(checkpoint)) == [] and again.calls == []


def test_an_unreadable_saved_position_asks_for_a_new_run(checkpoint: Checkpoint) -> None:
    checkpoint.set_frontier(FRONTIER_KEY, {"format": 99})
    with pytest.raises(ConfigError, match="unreadable"):
        list(walker(Folders(TREE)).walk(checkpoint))


# --- the materials source -------------------------------------------------------------------------


def test_enumerate_lists_every_material_with_its_folder_and_change_marker(
    checkpoint: Checkpoint,
) -> None:
    rig = Rig()
    items = listed(rig.materials(), checkpoint)
    dataset = rig.world.dataset
    assert sorted(i.source_id for i in items) == sorted(m.id for m in dataset.materials)
    by_id = {i.source_id: i for i in items}
    twill = dataset.materials[2]
    assert by_id[twill.id].context == {"folder_paths": [dataset.folder_path(twill.folder_id)]}
    assert by_id[dataset.materials[0].id].context == {"folder_paths": []}, "root items: no folder"
    assert by_id[twill.id].version == twill.updated_at
    assert by_id[twill.id].payload["price"] == 0, "the listing never has the price"


def test_the_listing_is_read_in_pages(checkpoint: Checkpoint) -> None:
    rig = Rig()
    listed(rig.materials(page_size=5), checkpoint)
    pages = [r for r in rig.world.transport.requests if r.path == "/api/v1/materials"]
    assert len(pages) > 6 and {r.query["per_page"] for r in pages} == {"5"}


def test_the_detail_is_the_record_without_its_envelope() -> None:
    rig = Rig()
    material = rig.world.dataset.materials[3]
    item = ListedItem(material.id, material.updated_at, {}, {})
    detail = rig.materials().fetch_detail(item)
    assert detail["price"] == str(material.price) and detail["thumbs"] == []
    assert "state" not in detail and "data" not in detail


def test_a_missing_detail_fails_that_item_only() -> None:
    rig = Rig()
    with pytest.raises(ItemFailed, match=r"mat-9999.*HTTP 404"):
        rig.materials().fetch_detail(ListedItem("mat-9999", None, {}, {}))


def make_item(rig: Rig, material_id: str) -> tuple[ListedItem, JsonObject]:
    """The listing row and the detail record of one material, as a pull would hold them."""
    material = rig.world.dataset.material(material_id)
    assert material is not None
    path = rig.world.dataset.folder_path(material.folder_id)
    item = ListedItem(
        material_id,
        material.updated_at,
        rig.world.listing_row(material_id),
        {"folder_paths": [path] if path else []},
    )
    return item, rig.materials().fetch_detail(item)


def test_pictures_come_from_the_listing_and_the_detail_without_their_signatures() -> None:
    rig = Rig(signed_url_ttl_s=300)
    source = rig.materials()
    item, detail = make_item(rig, "mat-0003")
    thumb, swatch = source.asset_refs(item, detail)
    assert (thumb.role, swatch.role) == ("thumb", "swatch")
    assert thumb.url == f"{BASE_URL}/media/thumb/mat-0003.png"
    assert swatch.url == f"{BASE_URL}/media/swatch/mat-0003.jpg"
    assert "sig=" not in repr([thumb, swatch])


def test_without_a_detail_only_the_listing_picture_is_known() -> None:
    rig = Rig()
    item, _ = make_item(rig, "mat-0003")
    refs = rig.materials().asset_refs(item, None)
    assert [ref.role for ref in refs] == ["thumb"]


def test_an_empty_thumbs_list_in_the_detail_does_not_hide_the_listing_picture() -> None:
    rig = Rig()
    item, detail = make_item(rig, "mat-0003")
    assert detail["thumbs"] == []
    assert rig.materials().asset_refs(item, detail)[0].url.endswith("/media/thumb/mat-0003.png")


def test_a_picture_on_another_host_is_refused_not_fetched() -> None:
    rig = Rig()
    item, detail = make_item(rig, "mat-0003")
    foreign = ListedItem(
        item.source_id,
        None,
        {**item.payload, "thumbnail_url": "http://elsewhere.invalid/a.png"},
        {},
    )
    with pytest.raises(SourceShapeError, match="not on the service's own host"):
        rig.materials().asset_refs(foreign, detail)
    with pytest.raises(SourceShapeError, match="is not a string"):
        rig.materials().asset_refs(
            ListedItem(item.source_id, None, {**item.payload, "thumbnail_url": 5}, {}), detail
        )


def test_a_picture_is_fetched_without_the_token_and_fitted_into_a_jpeg() -> None:
    rig = Rig()
    source = rig.materials()
    item, detail = make_item(rig, "mat-0003")
    thumb, swatch = source.asset_refs(item, detail)
    pictures = [Image.open(io.BytesIO(source.fetch_asset(ref))) for ref in (thumb, swatch)]
    assert [(p.format, p.size) for p in pictures] == [("JPEG", (96, 96)), ("JPEG", (192, 192))]
    media = [r for r in rig.world.transport.requests if r.path.startswith("/media/")]
    assert len(media) == 2 and all(r.header("authorization") is None for r in media)
    api = [r for r in rig.world.transport.requests if r.path.startswith("/api/")]
    assert api and all(r.header("authorization") for r in api)


def test_a_signed_link_that_has_expired_fails_that_picture_with_the_reason() -> None:
    rig = Rig(signed_url_ttl_s=60)
    source = rig.materials()
    item, detail = make_item(rig, "mat-0003")
    (thumb, _) = source.asset_refs(item, detail)
    rig.world.clock.now += 61
    with pytest.raises(
        ItemFailed, match=r"picture 'thumb' answered HTTP 403.*signed link has expired"
    ):
        source.fetch_asset(thumb)


def test_a_resumed_pull_has_only_the_stripped_url_and_a_signed_link_then_fails_loudly() -> None:
    """The signature is kept in memory only: another process cannot download a signed picture."""
    rig = Rig(signed_url_ttl_s=300)
    item, detail = make_item(rig, "mat-0003")
    refs = rig.materials().asset_refs(item, detail)
    fresh_process = rig.materials()
    with pytest.raises(ItemFailed, match="HTTP 403"):
        fresh_process.fetch_asset(refs[0])


def test_inspect_reports_the_total_the_folders_and_the_field_names() -> None:
    rig = Rig()
    source = rig.materials()
    assert isinstance(source, Inspectable)
    report = source.inspect(None)
    assert report.total == len(rig.world.dataset.materials_in("0")) + 2
    assert report.folders == ("Demo Woven [f-woven]", "Demo Knit [f-knit]")
    assert any(n.startswith("listing row fields: ") and "thumbnail_url" in n for n in report.notes)
    assert any(n.startswith("detail record fields: ") and "swatch_url" in n for n in report.notes)
    assert source.inspect("f-twill").total == len(rig.world.dataset.materials_in("f-twill"))


# --- the suppliers source -------------------------------------------------------------------------


def test_suppliers_are_followed_cursor_by_cursor_to_the_end(checkpoint: Checkpoint) -> None:
    rig = Rig()
    items = list(rig.suppliers(page_size=4).enumerate(checkpoint))
    assert [i.source_id for i in items] == [s.id for s in rig.world.dataset.suppliers]
    pages = [r for r in rig.world.transport.requests if r.path == "/api/v1/suppliers"]
    assert [r.query.get("cursor") for r in pages] == [None, "cur-4", "cur-8"]
    assert all(i.version is None and i.context == {} for i in items)


def test_suppliers_have_no_detail_and_no_pictures() -> None:
    rig = Rig()
    source = rig.suppliers()
    item = ListedItem("sup-001", None, {}, {})
    assert source.fetch_detail(item) is None
    assert source.asset_refs(item, None) == []
    with pytest.raises(ItemFailed, match="no pictures"):
        source.fetch_asset(AssetRef("sup-001", "thumb", BASE_URL))


def test_a_killed_supplier_walk_resumes_at_the_cursor_it_had_reached(
    checkpoint: Checkpoint,
) -> None:
    rig = Rig()
    walk = rig.suppliers(page_size=4).enumerate(checkpoint)
    first_page = [next(walk) for _ in range(4)]
    assert checkpoint.get_frontier("mock.suppliers.cursor") is None, "still inside the first page"
    next(walk)  # the consumer is done with page one: the position moves
    assert checkpoint.get_frontier("mock.suppliers.cursor") == "cur-4"
    walk.close()  # killed
    resumed = [i.source_id for i in rig.suppliers(page_size=4).enumerate(checkpoint)]
    assert resumed == [s.id for s in rig.world.dataset.suppliers[4:]]
    assert len(first_page) == 4


def test_a_cursor_that_does_not_advance_stops_the_walk(
    checkpoint: Checkpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig()
    stuck = {"state": True, "data": {"items": [], "next_cursor": "cur-1"}}
    monkeypatch.setattr(
        "catalog_sync.plugins.mock.api.MockApi.list_suppliers",
        lambda self, cursor: parse_supplier_page(stuck),
    )
    with pytest.raises(SourceShapeError, match="cursor did not advance"):
        list(rig.suppliers().enumerate(checkpoint))


def test_a_supplier_list_longer_than_max_pages_stops(checkpoint: Checkpoint) -> None:
    rig = Rig()
    with pytest.raises(FuseTripped, match="more than 2 pages of suppliers"):
        list(rig.suppliers(page_size=2, max_pages=2).enumerate(checkpoint))


def test_inspect_of_suppliers_admits_there_is_no_total() -> None:
    report = Rig().suppliers(page_size=3).inspect(None)
    assert report.total is None and report.folders == ()
    assert any("does not report a total" in note for note in report.notes)
    with pytest.raises(SourceShapeError, match="flat list"):
        Rig().suppliers().inspect("f-woven")


# --- traps survived -------------------------------------------------------------------------------


def test_a_rate_limit_is_waited_out_and_the_walk_completes(checkpoint: Checkpoint) -> None:
    rig = Rig(rate_limit=RateLimit(4, 10))
    source = rig.materials(page_size=3)
    items = listed(source, checkpoint)
    assert len(items) == len(rig.world.dataset.materials)
    statuses = rig.world.log_of("/api/v1/materials")
    assert statuses.count(429) >= 2 and statuses.count(200) > 6
    assert rig.world.clock.sleeps and all(s <= 10 for s in rig.world.clock.sleeps)
    assert max(rig.world.clock.sleeps) == 10.0, "the wait is the service's Retry-After"


def test_an_occasional_503_is_retried_with_backoff_and_nothing_is_lost(
    checkpoint: Checkpoint,
) -> None:
    rig = Rig(error_every=4)
    items = listed(rig.materials(), checkpoint)
    assert len(items) == len(rig.world.dataset.materials)
    assert 503 in rig.world.log_of("/api/v1/materials")
    assert 1.0 in rig.world.clock.sleeps, "the first retry waits the base backoff"


def test_an_expired_token_is_one_request_then_auth_expired() -> None:
    rig = Rig(token_ttl_s=60)
    source = rig.materials()
    rig.world.clock.now += 61
    with pytest.raises(AuthExpired, match="catalog-sync login mock"):
        source.fetch_detail(ListedItem("mat-0001", None, {}, {}))
    assert rig.world.log_of("/api/v1/materials/")[-1] == 401
    assert len(rig.world.log_of("/api/v1/materials/")) == 1, "a 401 is never retried"


def test_the_gap_a_ceiling_implies_is_kept_between_requests(checkpoint: Checkpoint) -> None:
    rig = Rig()
    source = rig.materials(max_requests_per_minute=60, request_gap_s=1)
    list(source.enumerate(checkpoint))
    assert rig.world.clock.sleeps, "at 60 a minute, requests wait for each other"
    assert min(rig.world.clock.sleeps) >= 0.99


def test_a_listing_that_changes_shape_is_a_source_shape_error(checkpoint: Checkpoint) -> None:
    changed = FakeTransport(
        lambda _request: FakeResponse.json({"state": True, "data": {"list": []}})
    )
    source = MockMaterialsSource(
        MockSettings.from_options(
            {"credential": HANDLE, "request_gap_s": 0}, source="mock.materials"
        ),
        seams=HttpSeams(adapter=changed),
    )
    with pytest.raises(SourceShapeError, match="materials folder 0 page 1"):
        listed(source, checkpoint)


# --- the credential kind --------------------------------------------------------------------------


def test_the_demo_account_is_the_same_on_both_sides() -> None:
    assert (plugin_login.DEMO_USER, plugin_login.DEMO_PASSWORD) == (
        server_auth.DEMO_USER,
        server_auth.DEMO_PASSWORD,
    )
    assert (plugin_login.USER_ENV, plugin_login.PASSWORD_ENV) == (
        server_auth.USER_ENV,
        server_auth.PASSWORD_ENV,
    )


def test_the_account_comes_from_the_environment_or_is_the_public_demo_one() -> None:
    demo = account_from_env({})
    assert (demo.user, demo.password) == ("demo", "demo-password")
    custom = account_from_env({"MOCK_SAAS_USER": "ops", "MOCK_SAAS_PASSWORD": "pw"})
    assert (custom.user, custom.password) == ("ops", "pw")
    assert "pw" not in repr(custom)


@pytest.mark.parametrize("env", [{"MOCK_SAAS_USER": "ops"}, {"MOCK_SAAS_PASSWORD": "pw"}])
def test_half_an_account_is_an_error(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="both be set, or neither"):
        account_from_env(env)
    with pytest.raises(ConfigError, match="both be set, or neither"):
        server_auth.account_from_env(env)


def test_the_kind_is_wired_from_the_base_url(tmp_path: Path) -> None:
    kind = mock_credential_kind({"base_url": "http://127.0.0.1:9000/"}, base_dir=tmp_path)
    assert kind.name == "mock" and kind.form_login is not None and kind.verify is not None
    assert kind.layout.host_suffixes == ("127.0.0.1",)
    assert kind.browser is not None
    assert kind.browser.landing_url == "http://127.0.0.1:9000/app"
    assert kind.browser.is_site_host("http://127.0.0.1:9000/api/v1/suppliers")
    assert not kind.browser.is_site_host("http://example.com/")
    assert kind.browser.is_login_url("http://127.0.0.1:9000/login")


@pytest.mark.parametrize(
    ("options", "complaint"),
    [({"nope": 1}, "unknown option"), ({"base_url": 5}, "'base_url' must be a URL")],
)
def test_bad_credential_options_are_config_errors(
    tmp_path: Path, options: dict[str, object], complaint: str
) -> None:
    with pytest.raises(ConfigError, match=complaint):
        mock_credential_kind(options, base_dir=tmp_path)


def test_the_selectors_the_browser_login_uses_exist_on_the_login_page() -> None:
    rig = Rig()
    kind = mock_credential_kind({}, base_dir=Path())
    assert kind.browser is not None
    page = rig.world.service.handle(Request("GET", "/login", {}, "x")).body.decode()
    for selector in (
        kind.browser.user_selector,
        kind.browser.password_selector,
        kind.browser.submit_selector,
    ):
        assert f'id="{selector.removeprefix("#")}"' in page


def test_a_form_login_returns_a_bearer_token_the_tool_can_read() -> None:
    world = World()
    values = form_login({}, base_url=BASE_URL, seams=world.seams)
    assert set(values) == {"AUTHORIZATION"} and values["AUTHORIZATION"].startswith("Bearer ")
    claims = decode_claims(values["AUTHORIZATION"])
    assert claims.expires_at is not None and claims.token_id
    sent = world.transport.requests[0]
    assert (
        sent.header("authorization") is None and sent.header("content-type") == "application/json"
    )
    assert sent.json_body() == {"username": "demo", "password": "demo-password"}


def test_each_form_login_gets_a_new_token_id() -> None:
    world = World()
    ids = {
        decode_claims(
            form_login({}, base_url=BASE_URL, seams=world.seams)["AUTHORIZATION"]
        ).token_id
        for _ in range(3)
    }
    assert len(ids) == 3


def test_a_rejected_form_login_says_so_without_echoing_the_password() -> None:
    world = World(MockOptions(account=server_auth.Account("ops", "the-real-password")))
    wrong = {"MOCK_SAAS_USER": "ops", "MOCK_SAAS_PASSWORD": "guess-9999"}
    with pytest.raises(LoginFailed, match="rejected the user name or password") as caught:
        form_login(wrong, base_url=BASE_URL, seams=world.seams)
    assert "guess-9999" not in str(caught.value) and "the-real-password" not in str(caught.value)
    assert len(world.transport.requests) == 1, "a refused password is not retried"


def test_a_login_answer_without_a_token_is_a_login_failure() -> None:
    def answer(payload: object) -> HttpSeams:
        return HttpSeams(adapter=FakeTransport(lambda _request: FakeResponse.json(payload)))

    with pytest.raises(LoginFailed, match="carries no token"):
        form_login({}, base_url=BASE_URL, seams=answer({"state": True, "data": {}}))
    with pytest.raises(LoginFailed, match="not what was expected"):
        form_login({}, base_url=BASE_URL, seams=answer({"state": False, "msg": "x"}))


def test_a_stored_token_is_proven_by_asking_for_one_supplier() -> None:
    world = World()
    credential = world.handle().load()
    verify_live(credential, base_url=BASE_URL, seams=world.seams)
    assert world.log_of("/api/v1/suppliers") == [200]


def test_a_token_the_service_rejects_fails_the_login_after_one_request() -> None:
    world = World(MockOptions(token_ttl_s=30))
    credential = world.handle().load()
    world.clock.now += 31
    with pytest.raises(LoginFailed, match="service rejected it"):
        verify_live(credential, base_url=BASE_URL, seams=world.seams)
    assert world.log_of("/api/v1/suppliers") == [401]


def test_only_the_mock_host_is_trusted_with_a_pasted_curl_command(tmp_path: Path) -> None:
    kind = mock_credential_kind({}, base_dir=tmp_path)
    token = "Bearer " + "x" * 40
    good = f"curl 'http://127.0.0.1:8765/api/v1/suppliers' -H 'authorization: {token}'"
    assert credential_values(parse_curl(good), kind.layout) == {"AUTHORIZATION": token}
    with pytest.raises(LoginFailed, match=r"not for 127\.0\.0\.1"):
        credential_values(parse_curl(good.replace("127.0.0.1:8765", "example.com")), kind.layout)


def test_a_service_that_keeps_failing_is_reported_not_hidden() -> None:
    world = World(MockOptions(error_every=1))
    credential = world.handle().load()
    with pytest.raises(HttpStatusError) as caught:
        verify_live(credential, base_url=BASE_URL, seams=world.seams)
    assert caught.value.status == 503
    assert world.log_of("/api/v1/suppliers") == [503, 503, 503], "three attempts, then it gives up"


# --- the browser path, driven by a fake browser against the mock's own pages ----------------------


def test_a_browser_login_takes_the_token_the_apps_own_calls_carry(tmp_path: Path) -> None:
    """The mock's page redirects to /login, takes the account, then calls the API with the bearer
    token: the capture is passive, and what it stores passes the same check `--form` does."""
    world = World()
    kind = mock_credential_kind({}, base_dir=tmp_path)
    site = kind.browser
    assert site is not None and kind.verify is not None
    token = form_login({}, base_url=BASE_URL, seams=world.seams)["AUTHORIZATION"]
    browser = FakeBrowser(site)

    def redirect_to_login(b: FakeBrowser) -> None:
        b.url = f"{BASE_URL}/login"

    def after_login(b: FakeBrowser) -> None:
        b.url = f"{BASE_URL}/app"
        b.send(f"{BASE_URL}/api/v1/suppliers?limit=10", {"authorization": token})

    browser.script = [redirect_to_login, lambda b: None, after_login]
    headers = grab_headers(
        site,
        profile_dir=tmp_path / "profile",
        account=kind.account({}),
        headless=True,
        timeout_s=30,
        force_login=False,
        launch=browser.launcher(),
        monotonic=browser.clock.monotonic,
    )
    assert headers["authorization"] == token
    assert [c for c in browser.calls if c[0] in ("fill", "click")] == [
        ("fill", "#username", "demo"),
        ("fill", "#password", "demo-password"),
        ("click", "#sign-in"),
    ]
    verify_live(
        Credential({"authorization": headers["authorization"]}, {}, None, None),
        base_url=BASE_URL,
        seams=world.seams,
    )
