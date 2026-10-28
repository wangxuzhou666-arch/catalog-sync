"""Small builders shared by the normalisation tests."""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime
from pathlib import Path

from catalog_sync.models import JsonObject
from catalog_sync.normalize.engine import Provenance
from catalog_sync.normalize.mapping import MappingSpec, load_mapping
from catalog_sync.sources.base import AssetRef, ListedItem, RawBundle, StoredAsset
from tests.fixtures import sample_items as items

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
PROVENANCE = Provenance(
    source_version="1700000000", raw_ref="run-1:1000", fetched_at=NOW, normalized_at=NOW
)

PRICE_MAPPING = """
    version: 1
    source: mock.materials
    fields:
      code:
        from: [list.code]
      price:
        from: [detail.price, list.price]
        parse: money
        currency: USD
        zero_is_missing: true
      images:
        from:
          - list.thumbnail_url
          - detail.thumbs[0].url
"""


def write_mapping(tmp_path: Path, text: str, name: str = "mapping.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def spec_from(tmp_path: Path, text: str) -> MappingSpec:
    return load_mapping(write_mapping(tmp_path, text))


def bundle_for(
    n: int = 1,
    *,
    detail: JsonObject | str | None = "default",
    row: JsonObject | None = None,
    folder_paths: list[str] | None = None,
    assets: tuple[StoredAsset, ...] = (),
) -> RawBundle:
    """The synthetic item n: a listing row with its picture and no price, plus its detail."""
    payload = row if row is not None else items.list_row(n)
    record = items.detail_record(n) if detail == "default" else detail
    assert record is None or isinstance(record, dict)
    context: JsonObject = {"folder_paths": folder_paths if folder_paths is not None else ["Alpha"]}
    item = ListedItem(str(payload["id"]), str(payload["updated_at"]), payload, context)
    return RawBundle(item, record, assets)


def stored_thumb(n: int = 1) -> StoredAsset:
    """The downloaded thumbnail of item n, as the cache holds it."""
    source_id = items.item_id(n)
    ref = AssetRef(source_id, "thumb", items.thumb_url(n))
    return StoredAsset(ref, f"{source_id}+thumb.bin", "ab" * 32, 321)
