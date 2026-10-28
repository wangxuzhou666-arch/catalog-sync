"""The committed replay snapshots are what their generators produce, and are well formed.

``examples/synthetic`` is the Mock-shaped snapshot the offline demo and the generic tests replay
(tools/make_synthetic.py).
"""

from __future__ import annotations

from pathlib import Path

import make_synthetic
from catalog_sync.snapshot import RunSnapshot
from tests.conftest import MOCK_SNAPSHOT_DIR
from tests.fixtures import mock_snapshot


def tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_the_committed_example_is_exactly_what_the_generator_produces(tmp_path: Path) -> None:
    regenerated = make_synthetic.build(tmp_path)
    assert tree(regenerated) == tree(MOCK_SNAPSHOT_DIR), (
        "examples/synthetic is stale: run `uv run python tools/make_synthetic.py`"
    )


def test_the_example_snapshot_is_intact_and_holds_the_mock_materials() -> None:
    snapshot = RunSnapshot.open(MOCK_SNAPSHOT_DIR)
    assert snapshot.verify() == []
    assert snapshot.source == mock_snapshot.SOURCE
    assert make_synthetic.ITEM_COUNT == mock_snapshot.ITEM_COUNT
    assert sorted(snapshot.item_ids()) == [m.id for m in mock_snapshot.MATERIALS]
    bundle = snapshot.bundle(mock_snapshot.CLEAN.id)
    assert bundle.detail is not None and bundle.detail["code"] == mock_snapshot.CLEAN.code
    assert [stored.ref.role for stored in bundle.assets] == ["thumb", "swatch"]


def test_the_example_keeps_the_traps_of_the_mock_data() -> None:
    """Every case the generic tests lean on has to be in the snapshot: a weight of 0, a fibre code
    no table lists, a root-level item, items in nested folders, and the two payloads that are not
    supersets of each other (the listing has the picture and a price of 0, the detail the price)."""
    assert len(mock_snapshot.ZERO_WEIGHT) == 2 and len(mock_snapshot.UNKNOWN_FIBRE) == 2
    assert {m.folder_id for m in mock_snapshot.MATERIALS} >= {"0", "f-woven", "f-twill"}
    snapshot = RunSnapshot.open(MOCK_SNAPSHOT_DIR)
    bundle = snapshot.bundle(mock_snapshot.CLEAN.id)
    assert bundle.item.payload["price"] == 0 and bundle.item.payload["thumbnail_url"]
    assert bundle.detail is not None
    assert (
        bundle.detail["price"] == str(mock_snapshot.CLEAN.price) and bundle.detail["thumbs"] == []
    )


def test_the_example_pictures_are_decodable_8_by_8_pngs() -> None:
    from PIL import Image

    pictures = sorted((MOCK_SNAPSHOT_DIR / "assets").glob("*.bin"))
    assert len(pictures) == 2 * mock_snapshot.ITEM_COUNT
    for path in pictures:
        with Image.open(path) as image:
            image.load()
            assert image.format == "PNG"
            assert image.size == (mock_snapshot.PICTURE_EDGE, mock_snapshot.PICTURE_EDGE)
