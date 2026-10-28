"""The two Mock SaaS sources: ``mock.materials`` and ``mock.suppliers``.

They are deliberately unlike each other, so that one pipeline is shown handling both shapes:

* materials come in a folder tree, with page-number pagination; the listing row has the picture
  and no price, the detail record the price and no picture (they are kept apart, never merged);
  pictures are downloaded and fitted into a byte budget.
* suppliers come as one flat list with cursor pagination; there is no detail and no picture.

Pictures are fetched anonymously (no credential headers: a storage host must never see the bearer
token) and only from the host the API itself lives on: a listing that points elsewhere is a
reason to stop, not to fetch.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

from catalog_sync.errors import ItemFailed, SourceShapeError
from catalog_sync.http import HttpSeams
from catalog_sync.models import JsonObject
from catalog_sync.plugins.mock.api import MockApi, is_folder_row
from catalog_sync.plugins.mock.headers import api_headers
from catalog_sync.plugins.mock.settings import MockSettings
from catalog_sync.sdk import (
    USER_AGENT,
    Connection,
    FolderWalker,
    PictureAssets,
    Sighting,
    WalkSettings,
    folder_label,
    item_scoped,
    same_host,
    walk_token_pages,
)
from catalog_sync.sources.base import AssetRef, InspectReport, ListedItem
from catalog_sync.state import Checkpoint

FRONTIER_KEY = "mock.walk"
SUPPLIER_FRONTIER_KEY = "mock.suppliers.cursor"
_MAX_LISTED_NAMES = 40
# Where each picture role comes from; the first expression that yields a URL wins.
_PICTURES: Mapping[str, tuple[str, ...]] = {
    "thumb": ("list.thumbnail_url", "detail.thumbs[0].url"),
    "swatch": ("detail.swatch_url",),
}


def _connection(settings: MockSettings, seams: HttpSeams | None) -> Connection:
    return Connection(
        credential=settings.credential,
        api_headers=api_headers,
        asset_headers={"user-agent": USER_AGENT, "accept": "image/*"},
        gap_s=settings.request_gap_s,
        max_requests_per_minute=settings.max_requests_per_minute,
        seams=seams,
    )


def _make_api(connection: Connection, settings: MockSettings) -> MockApi:
    return MockApi(connection.api(), base_url=settings.base_url, page_size=settings.page_size)


class MockMaterialsSource:
    name = "mock.materials"

    def __init__(self, settings: MockSettings, *, seams: HttpSeams | None = None) -> None:
        self._settings = settings
        self._connection = _connection(settings, seams)
        self._pictures = PictureAssets(
            self._connection,
            roles=_PICTURES,
            url_rule=same_host(settings.base_url),
            policy=settings.images,
        )
        self._api_client: MockApi | None = None

    @classmethod
    def from_options(cls, options: Mapping[str, object], *, base_dir: Path) -> MockMaterialsSource:
        del base_dir  # no option of this source is a path
        return cls(MockSettings.from_options(options, source=cls.name))

    # --- Source -----------------------------------------------------------------------------

    def enumerate(self, state: Checkpoint) -> Iterator[ListedItem]:
        settings = self._settings
        # Resumes at folder granularity: a kill repeats the folder it was in. The coarser setting is
        # enough for the demo's small folders, and shows the option (a connector that keeps
        # the default saves after every page).
        walker = FolderWalker(
            self._api().list_materials,
            WalkSettings(
                settings.root_folder_id,
                settings.max_depth,
                settings.max_pages,
                settings.page_size,
            ),
            frontier_key=FRONTIER_KEY,
            is_folder=is_folder_row,
            resume="folder",
        )
        for sighting in walker.walk(state):
            yield _listed_item(sighting)

    def fetch_detail(self, item: ListedItem) -> JsonObject:
        with item_scoped(item.source_id, "detail request"):
            return self._api().material_detail(item.source_id)

    def asset_refs(self, item: ListedItem, detail: JsonObject | None) -> list[AssetRef]:
        return self._pictures.refs(item, detail)

    def fetch_asset(self, ref: AssetRef) -> bytes:
        return self._pictures.fetch(ref)

    # --- Inspectable ------------------------------------------------------------------------

    def inspect(self, folder: str | None) -> InspectReport:
        """Read one page of the folder (no descent into sub-folders) and one detail record."""
        api = self._api()
        folder_id = folder or self._settings.root_folder_id
        page = api.list_materials(folder_id, 1)
        items = [row for row in page.rows if not is_folder_row(row)]
        folders = [row for row in page.rows if is_folder_row(row)]
        notes = [
            f"folder {folder_id}: the service reports {page.total} row(s) in total; the first "
            f"page has {len(items)} item(s) and {len(folders)} folder(s)"
        ]
        if items:
            notes.append(f"listing row fields: {', '.join(sorted(items[0]))}")
            detail = api.material_detail(str(items[0]["id"]))
            notes.append(f"detail record fields: {', '.join(sorted(detail))}")
        labels = tuple(folder_label(row) for row in folders[:_MAX_LISTED_NAMES])
        return InspectReport(total=page.total, folders=labels, notes=tuple(notes))

    def _api(self) -> MockApi:
        if self._api_client is None:
            self._api_client = _make_api(self._connection, self._settings)
        return self._api_client


class MockSuppliersSource:
    name = "mock.suppliers"

    def __init__(self, settings: MockSettings, *, seams: HttpSeams | None = None) -> None:
        self._settings = settings
        self._connection = _connection(settings, seams)

    @classmethod
    def from_options(cls, options: Mapping[str, object], *, base_dir: Path) -> MockSuppliersSource:
        del base_dir  # no option of this source is a path
        return cls(MockSettings.from_options(options, source=cls.name))

    # --- Source -----------------------------------------------------------------------------

    def enumerate(self, state: Checkpoint) -> Iterator[ListedItem]:
        """Follow the cursors. The position saved is the cursor of the page *about to be read*, and
        it moves only once every item of the page before it has been handled."""
        pages = walk_token_pages(
            _make_api(self._connection, self._settings).list_suppliers,
            state,
            key=SUPPLIER_FRONTIER_KEY,
            max_pages=self._settings.max_pages,
            what="suppliers",
        )
        for page in pages:
            for row in page.rows:
                yield _supplier_item(row)

    def fetch_detail(self, item: ListedItem) -> JsonObject | None:
        """The listing row already holds the whole record."""
        del item
        return None

    def asset_refs(self, item: ListedItem, detail: JsonObject | None) -> list[AssetRef]:
        return []

    def fetch_asset(self, ref: AssetRef) -> bytes:
        raise ItemFailed(ref.source_id, "suppliers have no pictures")

    # --- Inspectable ------------------------------------------------------------------------

    def inspect(self, folder: str | None) -> InspectReport:
        """One page: cursor pagination says nothing about a total, so there is none to report."""
        if folder is not None:
            raise SourceShapeError("suppliers are a flat list; there are no folders to look into")
        page = _make_api(self._connection, self._settings).list_suppliers(None)
        notes = [
            f"cursor pagination: the first page has {len(page.rows)} supplier(s); "
            "the service does not report a total",
        ]
        if page.rows:
            notes.append(f"listing row fields: {', '.join(sorted(page.rows[0]))}")
        return InspectReport(total=None, folders=(), notes=tuple(notes))


def _listed_item(sighting: Sighting) -> ListedItem:
    version = sighting.row.get("updated_at")
    return sighting.listed_item(version if isinstance(version, str) else None)


def _supplier_item(row: JsonObject) -> ListedItem:
    source_id = row.get("id")
    if not isinstance(source_id, str) or not source_id:
        raise SourceShapeError("a supplier row has no usable 'id'")
    # No change marker in the row: the puller falls back to a hash of the whole row.
    return ListedItem(source_id, None, row, {})
