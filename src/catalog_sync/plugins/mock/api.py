"""The Mock SaaS calls this connector makes, each response checked before anything uses it.

* ``GET /api/v1/materials``: a folder's rows (sub-folders and materials mixed), page-number
  pagination with a ``total``.
* ``GET /api/v1/materials/{id}``: one material's detail, wrapped twice (``data.data``).
* ``GET /api/v1/suppliers``: cursor pagination; no total, no folders.

Every response is an envelope ``{"state": true, "data": ...}``; ``state`` is checked on each, and
the detail is unwrapped exactly twice (see envelope.py).
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

from catalog_sync.envelope import peel_data, require_state_ok
from catalog_sync.http import HttpClient
from catalog_sync.models import JsonObject
from catalog_sync.sdk import RowPage, TokenPage, parse_detail, rows_page, shape_context, token_page

# One page of a folder. ``total`` counts every row of the folder, folders and items both.
MaterialPage = RowPage
# One page of suppliers: the rows and the token of the next page (None on the last).
SupplierPage = TokenPage


def is_folder_row(row: Mapping[str, object]) -> bool:
    return row.get("type") == "folder"


class MockApi:
    def __init__(self, client: HttpClient, *, base_url: str, page_size: int) -> None:
        self._client = client
        self._base_url = base_url
        self._page_size = page_size

    def list_materials(self, folder_id: str, page: int) -> MaterialPage:
        payload = self._client.get_json(
            f"{self._base_url}/api/v1/materials",
            params={"folder_id": folder_id, "page": page, "per_page": self._page_size},
        )
        with shape_context(f"materials folder {folder_id} page {page}"):
            return parse_material_page(payload)

    def material_detail(self, material_id: str) -> JsonObject:
        payload = self._client.get_json(
            f"{self._base_url}/api/v1/materials/{quote(material_id, safe='')}"
        )
        with shape_context(f"detail of material {material_id}"):
            return parse_detail(payload)

    def list_suppliers(self, cursor: str | None) -> SupplierPage:
        params: dict[str, str | int] = {"limit": self._page_size}
        if cursor is not None:
            params["cursor"] = cursor
        payload = self._client.get_json(f"{self._base_url}/api/v1/suppliers", params=params)
        with shape_context(f"suppliers after cursor {cursor!r}"):
            return parse_supplier_page(payload)


def parse_material_page(payload: Mapping[str, object]) -> MaterialPage:
    require_state_ok(payload)
    return rows_page(peel_data(payload, 1), where="data")


def parse_supplier_page(payload: Mapping[str, object]) -> SupplierPage:
    require_state_ok(payload)
    return token_page(peel_data(payload, 1), where="data")
