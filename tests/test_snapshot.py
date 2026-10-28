from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from catalog_sync.errors import SnapshotError
from catalog_sync.fsutil import atomic_write_bytes
from catalog_sync.snapshot import (
    BlobInfo,
    RunSnapshot,
    asset_blob_name,
    blob_stem,
    canonical_json,
    sha256_hex,
    source_id_from_item_name,
)
from catalog_sync.sources.base import AssetRef, ListedItem
from tests.doubles import SimulatedCrash
from tests.fixtures import sample_items

CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def new_snapshot(runs_root: Path) -> RunSnapshot:
    return RunSnapshot.create(runs_root, run_id="run1", source="demo", created_at=CREATED)


def test_every_blob_is_recorded_with_its_sha256(runs_root: Path) -> None:
    snapshot = new_snapshot(runs_root)
    info = snapshot.put("detail", "a.json", b'{"x":1}')
    assert info.sha256 == sha256_hex(b'{"x":1}')
    assert info.size_bytes == 7
    manifest = [
        json.loads(line) for line in (snapshot.run_dir / "manifest.jsonl").read_text().splitlines()
    ]
    assert manifest == [{"kind": "detail", "name": "a.json", "sha256": info.sha256, "size": 7}]
    assert snapshot.verify() == []


def test_reopening_restores_blobs_order_and_metadata(runs_root: Path) -> None:
    snapshot = new_snapshot(runs_root)
    for name in ("b.json", "a.json", "c.json"):
        snapshot.put("list", name, name.encode())
    reopened = RunSnapshot.open(runs_root / "run1")
    assert (reopened.run_id, reopened.source, reopened.created_at) == ("run1", "demo", CREATED)
    assert reopened.names("list") == ["b.json", "a.json", "c.json"], "first-written order is kept"
    assert reopened.get("list", "a.json") == b"a.json"


def test_tampering_with_a_blob_is_detected(runs_root: Path) -> None:
    snapshot = new_snapshot(runs_root)
    snapshot.put("detail", "a.json", b"original")
    (snapshot.run_dir / "detail" / "a.json").write_bytes(b"tampered")
    with pytest.raises(SnapshotError, match="sha256"):
        snapshot.get("detail", "a.json")
    assert len(snapshot.verify()) == 1


def test_a_blob_deleted_from_disk_is_reported(runs_root: Path) -> None:
    snapshot = new_snapshot(runs_root)
    snapshot.put("detail", "a.json", b"x")
    (snapshot.run_dir / "detail" / "a.json").unlink()
    with pytest.raises(SnapshotError, match="missing"):
        snapshot.get("detail", "a.json")


def test_unknown_blob_is_an_error_not_an_empty_answer(runs_root: Path) -> None:
    with pytest.raises(SnapshotError, match="not in the manifest"):
        new_snapshot(runs_root).get("detail", "nope.json")


def test_rewriting_a_blob_replaces_it_and_keeps_its_position(runs_root: Path) -> None:
    snapshot = new_snapshot(runs_root)
    snapshot.put("list", "a.json", b"one")
    snapshot.put("list", "b.json", b"two")
    snapshot.put("list", "a.json", b"three")
    assert snapshot.get("list", "a.json") == b"three"
    assert snapshot.names("list") == ["a.json", "b.json"]
    assert RunSnapshot.open(snapshot.run_dir).get("list", "a.json") == b"three"


def test_killed_after_logging_but_before_rename_rolls_forward(
    runs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = new_snapshot(runs_root)
    snapshot.put("list", "a.json", b"old")
    real_append = RunSnapshot._append_manifest

    def append_then_die(self: RunSnapshot, info: BlobInfo) -> None:
        real_append(self, info)
        raise SimulatedCrash

    monkeypatch.setattr(RunSnapshot, "_append_manifest", append_then_die)
    with pytest.raises(SimulatedCrash):
        snapshot.put("list", "a.json", b"new")
    monkeypatch.undo()

    on_disk = (snapshot.run_dir / "list" / "a.json").read_bytes()
    assert on_disk == b"old", "the crash happened before the rename"
    survivor = RunSnapshot.open(snapshot.run_dir)
    assert survivor.get("list", "a.json") == b"new", "a reader finishes the interrupted write"
    assert survivor.verify() == []


def test_killed_before_logging_leaves_the_previous_content_valid(
    runs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = new_snapshot(runs_root)
    snapshot.put("list", "a.json", b"old")

    def die(self: RunSnapshot, info: BlobInfo) -> None:
        raise SimulatedCrash

    monkeypatch.setattr(RunSnapshot, "_append_manifest", die)
    with pytest.raises(SimulatedCrash):
        snapshot.put("list", "a.json", b"new")
    monkeypatch.undo()

    survivor = RunSnapshot.open(snapshot.run_dir)
    assert survivor.get("list", "a.json") == b"old"
    assert survivor.verify() == []


def test_a_torn_manifest_tail_is_dropped_so_the_next_append_is_clean(runs_root: Path) -> None:
    snapshot = new_snapshot(runs_root)
    snapshot.put("list", "a.json", b"a")
    with (snapshot.run_dir / "manifest.jsonl").open("ab") as handle:
        handle.write(b'{"kind":"list","name":"b.js')  # power cut mid-append
    survivor = RunSnapshot.open(snapshot.run_dir)
    assert survivor.names("list") == ["a.json"]
    survivor.put("list", "c.json", b"c")
    assert RunSnapshot.open(snapshot.run_dir).names("list") == ["a.json", "c.json"]


def torn_snapshot(runs_root: Path) -> tuple[RunSnapshot, bytes]:
    snapshot = new_snapshot(runs_root)
    snapshot.put("list", "a.json", b"a")
    with (snapshot.run_dir / "manifest.jsonl").open("ab") as handle:
        handle.write(b'{"kind":"list","name":"b.js')
    return snapshot, (snapshot.run_dir / "manifest.jsonl").read_bytes()


def test_opening_a_run_to_read_it_never_rewrites_its_manifest(runs_root: Path) -> None:
    """The writer may be mid-append: what looks torn to a reader can be whole a moment later."""
    snapshot, before = torn_snapshot(runs_root)
    reader = RunSnapshot.open(snapshot.run_dir)
    assert reader.names("list") == ["a.json"], "the fragment is ignored in memory"
    assert reader.verify() == []
    assert (snapshot.run_dir / "manifest.jsonl").read_bytes() == before


def test_a_reader_opening_mid_append_does_not_destroy_the_writers_line(runs_root: Path) -> None:
    snapshot = new_snapshot(runs_root)
    snapshot.put("list", "a.json", b"a")
    manifest = snapshot.run_dir / "manifest.jsonl"
    whole_line = canonical_json(
        {"kind": "list", "name": "b.json", "sha256": sha256_hex(b"b"), "size": 1}
    )
    with manifest.open("ab") as handle:
        handle.write(whole_line[:20])  # the writer is part way through its append
        handle.flush()
        RunSnapshot.open(snapshot.run_dir)  # a concurrent `normalize --run <id>`
        handle.write(whole_line[20:] + b"\n")
    assert RunSnapshot.open(snapshot.run_dir).names("list") == ["a.json", "b.json"]


def test_the_writer_removes_the_fragment_atomically_before_its_next_append(
    runs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, before = torn_snapshot(runs_root)
    writer = RunSnapshot.open(snapshot.run_dir)

    real_replace = Path.replace

    def fail_for_the_manifest(self: Path, other: Path) -> Path:
        if other.name == "manifest.jsonl":
            raise OSError("disk went away")
        return real_replace(self, other)

    monkeypatch.setattr(Path, "replace", fail_for_the_manifest)
    with pytest.raises(OSError, match="disk went away"):
        writer.put("list", "c.json", b"c")
    monkeypatch.undo()
    assert (snapshot.run_dir / "manifest.jsonl").read_bytes() == before, "never half-repaired"

    writer.put("list", "c.json", b"c")
    assert RunSnapshot.open(snapshot.run_dir).names("list") == ["a.json", "c.json"]
    assert (snapshot.run_dir / "manifest.jsonl").read_bytes().count(b"\n") == 2


def test_atomic_write_leaves_the_old_file_when_the_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.bin"
    atomic_write_bytes(target, b"old")

    def fail_replace(self: Path, other: Path) -> Path:
        raise OSError("disk went away")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="disk went away"):
        atomic_write_bytes(target, b"new")
    monkeypatch.undo()

    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["file.bin"], "no temp file may be left behind"


def test_files_are_private(runs_root: Path) -> None:
    snapshot = new_snapshot(runs_root)
    snapshot.put("detail", "a.json", b"x")
    assert (snapshot.run_dir / "detail" / "a.json").stat().st_mode & 0o077 == 0
    assert (snapshot.run_dir / "manifest.jsonl").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("name", ["../escape", "a/b", "", ".hidden", "x" * 300, "sp ace"])
def test_unsafe_blob_names_are_refused(runs_root: Path, name: str) -> None:
    with pytest.raises(SnapshotError, match="unsafe blob name"):
        new_snapshot(runs_root).put("detail", name, b"x")


@pytest.mark.parametrize("run_id", ["", ".", "..", "a/b", "-lead", "x" * 80])
def test_unsafe_run_ids_are_refused(runs_root: Path, run_id: str) -> None:
    with pytest.raises(SnapshotError, match="invalid run id"):
        RunSnapshot.create(runs_root, run_id=run_id, source="demo", created_at=CREATED)


def test_a_run_directory_is_never_reused(runs_root: Path) -> None:
    new_snapshot(runs_root)
    with pytest.raises(SnapshotError, match="already exists"):
        new_snapshot(runs_root)


@pytest.mark.parametrize(
    "source_id",
    ["plain", "with space", "a/b", "../up", ".", "..", ".hidden", "ünï", "100%", "a+b", "x@y"],
)
def test_ids_from_the_service_become_safe_reversible_file_names(source_id: str) -> None:
    stem = blob_stem(source_id)
    assert "/" not in stem
    assert not stem.startswith(".")
    assert source_id_from_item_name(f"{stem}.json") == source_id


@pytest.mark.parametrize("source_id", ["", "x" * 300])
def test_ids_that_cannot_be_file_names_are_refused(source_id: str) -> None:
    with pytest.raises(SnapshotError, match="unusable as a file name"):
        blob_stem(source_id)


def test_asset_names_cannot_collide_across_items_and_roles() -> None:
    first = asset_blob_name(AssetRef("a__b", "c", "u"))
    second = asset_blob_name(AssetRef("a", "b__c", "u"))
    third = asset_blob_name(AssetRef("a+b", "c", "u"))
    fourth = asset_blob_name(AssetRef("a", "b+c", "u"))
    assert len({first, second, third, fourth}) == 4


def test_typed_item_detail_asset_round_trip_and_bundle(runs_root: Path) -> None:
    snapshot = new_snapshot(runs_root)
    item = ListedItem("a/1", "v1", {"name": "x"}, {"folder_paths": ["F"]})
    ref = AssetRef("a/1", "thumb", "https://assets.example.invalid/t.png")
    other = AssetRef("a/1", "swatch", "https://assets.example.invalid/s.png")
    snapshot.put_item(item)
    snapshot.put_detail("a/1", {"price": "1"})
    snapshot.put_asset_refs("a/1", [ref, other])
    snapshot.put_asset(ref, b"PNG")  # `other` was never downloaded

    reopened = RunSnapshot.open(snapshot.run_dir)
    bundle = reopened.bundle("a/1")
    assert bundle.item == item
    assert bundle.detail == {"price": "1"}
    assert [stored.ref for stored in bundle.assets] == [ref]
    assert bundle.assets[0].sha256 == sha256_hex(b"PNG")
    assert reopened.item_ids() == ["a/1"]
    assert reopened.get_asset(ref) == b"PNG"


def test_missing_detail_and_refs_read_as_none(runs_root: Path) -> None:
    snapshot = new_snapshot(runs_root)
    snapshot.put_item(ListedItem("a", None, {}, {}))
    assert snapshot.get_detail("a") is None
    assert snapshot.get_asset_refs("a") is None
    assert snapshot.bundle("a").assets == ()


def test_canonical_json_is_key_order_independent_and_rejects_nan() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    with pytest.raises(ValueError, match="Out of range float"):
        canonical_json({"x": float("nan")})


def test_a_bundle_carries_the_size_of_each_stored_picture(runs_root: Path) -> None:
    snapshot = new_snapshot(runs_root)
    snapshot.put_item(ListedItem("a", None, {}, {}))
    picture = AssetRef("a", "thumb", "https://assets.example.invalid/t.png")
    junk = AssetRef("a", "other", "https://assets.example.invalid/o.bin")
    snapshot.put_asset_refs("a", [picture, junk])
    buffer = io.BytesIO()
    Image.new("RGB", (5, 3)).save(buffer, "PNG")  # not square, so a swapped pair would show
    snapshot.put_asset(picture, buffer.getvalue())
    snapshot.put_asset(junk, b"not a picture")

    stored = {asset.ref.role: asset for asset in snapshot.bundle("a").assets}
    assert (stored["thumb"].width, stored["thumb"].height, stored["thumb"].is_unreadable) == (
        5,
        3,
        False,
    )
    assert (stored["other"].width, stored["other"].height, stored["other"].is_unreadable) == (
        None,
        None,
        True,
    )


def test_a_picture_whose_write_was_cut_off_before_the_rename_is_still_measured(
    runs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = new_snapshot(runs_root)
    snapshot.put_item(ListedItem("a", None, {}, {}))
    ref = AssetRef("a", "thumb", "https://assets.example.invalid/t.png")
    snapshot.put_asset_refs("a", [ref])
    real_append = RunSnapshot._append_manifest

    def append_then_die(self: RunSnapshot, info: BlobInfo) -> None:
        real_append(self, info)
        raise SimulatedCrash

    monkeypatch.setattr(RunSnapshot, "_append_manifest", append_then_die)
    with pytest.raises(SimulatedCrash):
        snapshot.put_asset(
            ref, sample_items.png_bytes(1, "thumb")
        )  # logged, never renamed into place
    monkeypatch.undo()

    (asset,) = RunSnapshot.open(snapshot.run_dir).bundle("a").assets
    assert (asset.width, asset.height) == (8, 8)
