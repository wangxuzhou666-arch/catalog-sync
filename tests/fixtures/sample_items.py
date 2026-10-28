"""Synthetic items shaped like the Mock SaaS's materials, for tests that need a few in memory.

Everything is invented. The shapes follow the Mock connector, which is also the shape most
services have: a listing row has the picture and a price of 0, the detail record has the real
price and an empty ``thumbs`` that must not erase pictures, and an item can sit in several
folders. Tests that need the real Mock service or its committed snapshot use mock_saas.py and
mock_snapshot.py instead.
"""

from __future__ import annotations

from catalog_sync.models import JsonObject
from tests.fixtures.pictures import png_bytes as png_bytes  # re-exported: callers use items.*

SOURCE_NAME = "mock.materials"
ASSET_HOST = "https://assets.example.invalid"
BASE_TIMESTAMP = 1_700_000_000


def item_id(index: int) -> str:
    return f"item-{index:04d}"


def code(index: int) -> str:
    return f"SAMPLE-{index:04d}"


def name(index: int) -> str:
    return f"Sample Twill {index:02d}"


def folder_paths(index: int) -> list[str]:
    return ["Demo Library/Woven"] if index % 2 == 0 else ["Demo Library/Knit"]


def asset_roles(index: int) -> tuple[str, ...]:
    """Every item has a thumbnail; every third one also has a swatch (so some items have two)."""
    return ("thumb", "swatch") if index % 3 == 0 else ("thumb",)


def thumb_url(index: int, query: str = "") -> str:
    return f"{ASSET_HOST}/thumbs/{code(index)}.png{query}"


def swatch_url(index: int) -> str:
    return f"{ASSET_HOST}/swatches/{code(index)}.png"


def list_row(index: int, *, update_timestamp: int | None = None, query: str = "") -> JsonObject:
    """A listing row: has the picture (its URL may carry a `query`), has no price."""
    return {
        "id": item_id(index),
        "code": code(index),
        "name": name(index),
        "price": 0,
        "thumbnail_url": thumb_url(index, query),
        "updated_at": BASE_TIMESTAMP + index if update_timestamp is None else update_timestamp,
    }


def detail_record(index: int) -> JsonObject:
    """The detail record: a price, and an empty ``thumbs`` that must not erase pictures."""
    record: JsonObject = {
        "id": item_id(index),
        "code": code(index),
        "name": name(index),
        "thumbs": [],
        "weight": "0 gsm" if index % 5 == 4 else f"{150 + index} gsm",
        "width": "150",
        "composition": "60%CO/40%PES",
        "price": f"{10 + index}.50",
        "supplier_name": "Demo Supplier Ltd",
    }
    if "swatch" in asset_roles(index):
        record["swatch_url"] = swatch_url(index)
    return record


def detail_envelope(index: int) -> JsonObject:
    """A detail response that wraps its record twice: ``{"state": true, "data": {"data": ...}}``."""
    return {"state": True, "data": {"data": detail_record(index)}}
