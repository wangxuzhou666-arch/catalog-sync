"""Run snapshots: the raw responses of one pull, kept so later steps can run offline.

Layout of one run directory (``<workspace>/cache/runs/<run_id>/``)::

    run.json         run id, source name, creation time
    manifest.jsonl   one line per blob write: kind, name, sha256, size (the last line per name wins)
    list/            one ListedItem per file, in the order the source listed them
    detail/          one detail record per file
    assets/          ``<item>+<role>.bin`` files and one ``<item>.refs.json`` per item

Integrity: every blob has a sha256 in the manifest and every read verifies it. A blob is written
write-ahead: the content goes to a ``.pending`` file, the manifest line is appended, and only then
is the file renamed into place. A reader that finds the rename missing finishes it, so a process
killed between the steps loses nothing and never exposes a half-written file.

A power cut can leave half an appended manifest line. Every opener ignores that fragment in memory;
only the writer removes it from the file (atomically, right before its next append), so opening a
run to read it never changes it and cannot race a writer that is still appending.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeAlias
from urllib.parse import quote, unquote

from catalog_sync.errors import SnapshotError
from catalog_sync.fsutil import atomic_write_bytes
from catalog_sync.images import probe_dimensions
from catalog_sync.models import JsonObject
from catalog_sync.sources.base import AssetRef, ListedItem, RawBundle, StoredAsset

BlobKind: TypeAlias = Literal["list", "detail", "assets"]
BLOB_KINDS: tuple[BlobKind, ...] = ("list", "detail", "assets")

_RUN_META = "run.json"
_MANIFEST = "manifest.jsonl"
_FORMAT = 1
_MAX_NAME = 200
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_BLOB_NAME = re.compile(r"[A-Za-z0-9%][A-Za-z0-9%._+-]*")


@dataclass(frozen=True)
class BlobInfo:
    kind: BlobKind
    name: str
    sha256: str
    size_bytes: int


def is_valid_run_id(run_id: str) -> bool:
    return _RUN_ID.fullmatch(run_id) is not None


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_json(value: object) -> bytes:
    """Deterministic JSON bytes, so equal data always hashes equally."""
    text = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return text.encode("utf-8")


def blob_stem(source_id: str) -> str:
    """File-name-safe, reversible form of an id that comes from a remote service."""
    stem = quote(source_id, safe="")
    if stem.startswith("."):
        stem = "%2E" + stem[1:]  # quote() leaves dots alone; a leading one would make "." or ".."
    if not stem or len(stem) > _MAX_NAME:
        raise SnapshotError(
            f"cannot store an item with id {source_id[:60]!r}: unusable as a file name"
        )
    return stem


def source_id_from_item_name(name: str) -> str:
    return unquote(name.removesuffix(".json"))


def asset_blob_name(ref: AssetRef) -> str:
    # "+" is always percent-encoded inside a stem or a role, so it separates them unambiguously.
    if not ref.role:
        raise SnapshotError(f"asset of {ref.source_id!r} has an empty role")
    return f"{blob_stem(ref.source_id)}+{quote(ref.role, safe='')}.bin"


class RunSnapshot:
    def __init__(
        self,
        run_dir: Path,
        run_id: str,
        source: str,
        created_at: datetime,
        entries: dict[tuple[BlobKind, str], BlobInfo],
        *,
        has_torn_manifest_tail: bool = False,
    ) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.source = source
        self.created_at = created_at
        self._entries = entries
        self._has_torn_manifest_tail = has_torn_manifest_tail

    @classmethod
    def create(
        cls, runs_root: Path, *, run_id: str, source: str, created_at: datetime
    ) -> RunSnapshot:
        if not is_valid_run_id(run_id):
            raise SnapshotError(f"invalid run id {run_id!r}")
        run_dir = runs_root / run_id
        if run_dir.exists():
            raise SnapshotError(f"run directory already exists: {run_id}")
        for kind in BLOB_KINDS:
            (run_dir / kind).mkdir(parents=True)
        meta = {
            "format": _FORMAT,
            "run_id": run_id,
            "source": source,
            "created_at": created_at.isoformat(),
        }
        atomic_write_bytes(run_dir / _RUN_META, canonical_json(meta) + b"\n")
        return cls(run_dir, run_id, source, created_at, {})

    @classmethod
    def open(cls, run_dir: Path) -> RunSnapshot:
        meta_path = run_dir / _RUN_META
        if not meta_path.is_file():
            raise SnapshotError(f"not a run snapshot (no {_RUN_META}): {run_dir.name}")
        meta = _as_object(json.loads(meta_path.read_text(encoding="utf-8")), _RUN_META)
        if meta.get("format") != _FORMAT:
            raise SnapshotError(f"unsupported snapshot format {meta.get('format')!r}")
        try:
            run_id, source, created = meta["run_id"], meta["source"], meta["created_at"]
        except KeyError as exc:
            raise SnapshotError(f"{_RUN_META} is missing {exc}") from exc
        if not (isinstance(run_id, str) and isinstance(source, str) and isinstance(created, str)):
            raise SnapshotError(f"{_RUN_META} has non-string identity fields")
        entries, has_torn_tail = _load_manifest(run_dir / _MANIFEST)
        return cls(
            run_dir,
            run_id,
            source,
            datetime.fromisoformat(created),
            entries,
            has_torn_manifest_tail=has_torn_tail,
        )

    # --- blobs ------------------------------------------------------------------------------

    def put(self, kind: BlobKind, name: str, content: bytes) -> BlobInfo:
        final = self._path(kind, name)
        blob = BlobInfo(kind, name, sha256_hex(content), len(content))
        if self._entries.get((kind, name)) == blob and final.exists():
            return blob
        pending = _pending_path(final, blob.sha256)
        atomic_write_bytes(pending, content)
        self._append_manifest(blob)
        pending.replace(final)
        self._entries[(kind, name)] = blob
        return blob

    def get(self, kind: BlobKind, name: str) -> bytes:
        blob = self._entries.get((kind, name))
        if blob is None:
            raise SnapshotError(f"{kind}/{name} is not in the manifest of run {self.run_id}")
        return self._read_verified(blob)

    def has(self, kind: BlobKind, name: str) -> bool:
        return (kind, name) in self._entries

    def blob_info(self, kind: BlobKind, name: str) -> BlobInfo:
        blob = self._entries.get((kind, name))
        if blob is None:
            raise SnapshotError(f"{kind}/{name} is not in the manifest of run {self.run_id}")
        return blob

    def names(self, kind: BlobKind) -> list[str]:
        """Blob names of one kind in the order they were first written."""
        return [name for (entry_kind, name) in self._entries if entry_kind == kind]

    def verify(self) -> list[str]:
        """Re-hash every blob; return one message per problem (empty means intact)."""
        problems: list[str] = []
        for blob in self._entries.values():
            try:
                self._read_verified(blob)
            except SnapshotError as exc:
                problems.append(str(exc))
        return problems

    def put_json(self, kind: BlobKind, name: str, value: object) -> BlobInfo:
        return self.put(kind, name, canonical_json(value))

    def get_json(self, kind: BlobKind, name: str) -> object:
        return json.loads(self.get(kind, name))

    # --- typed access used by the puller, the replay source and offline normalisation --------

    def put_item(self, item: ListedItem) -> BlobInfo:
        body = {
            "source_id": item.source_id,
            "version": item.version,
            "payload": item.payload,
            "context": item.context,
        }
        return self.put_json("list", _item_name(item.source_id), body)

    def get_item(self, source_id: str) -> ListedItem:
        label = f"list/{_item_name(source_id)}"
        body = _as_object(self.get_json("list", _item_name(source_id)), label)
        version = body.get("version")
        if version is not None and not isinstance(version, str):
            raise SnapshotError(f"{label}: version must be a string or null")
        return ListedItem(
            source_id=source_id,
            version=version,
            payload=_as_object(body.get("payload"), f"{label} payload"),
            context=_as_object(body.get("context"), f"{label} context"),
        )

    def item_ids(self) -> list[str]:
        return [source_id_from_item_name(name) for name in self.names("list")]

    def put_detail(self, source_id: str, detail: JsonObject) -> BlobInfo:
        return self.put_json("detail", _item_name(source_id), detail)

    def get_detail(self, source_id: str) -> JsonObject | None:
        name = _item_name(source_id)
        if not self.has("detail", name):
            return None
        return _as_object(self.get_json("detail", name), f"detail/{name}")

    def put_asset_refs(self, source_id: str, refs: Sequence[AssetRef]) -> BlobInfo:
        body = [{"role": ref.role, "url": ref.url} for ref in refs]
        return self.put_json("assets", _refs_name(source_id), body)

    def get_asset_refs(self, source_id: str) -> list[AssetRef] | None:
        """The recorded refs, or None when the assets stage has not run for this item."""
        name = _refs_name(source_id)
        if not self.has("assets", name):
            return None
        body = self.get_json("assets", name)
        if not isinstance(body, list):
            raise SnapshotError(f"assets/{name}: expected a list")
        refs: list[AssetRef] = []
        for entry in body:
            fields = _as_object(entry, f"assets/{name} entry")
            role, url = fields.get("role"), fields.get("url")
            if not (isinstance(role, str) and isinstance(url, str)):
                raise SnapshotError(f"assets/{name}: entries need string role and url")
            refs.append(AssetRef(source_id=source_id, role=role, url=url))
        return refs

    def put_asset(self, ref: AssetRef, content: bytes) -> BlobInfo:
        return self.put("assets", asset_blob_name(ref), content)

    def has_asset(self, ref: AssetRef) -> bool:
        return self.has("assets", asset_blob_name(ref))

    def get_asset(self, ref: AssetRef) -> bytes:
        return self.get("assets", asset_blob_name(ref))

    def bundle(self, source_id: str) -> RawBundle:
        """Everything stored for one item. Assets that were never downloaded are left out."""
        stored: list[StoredAsset] = []
        for ref in self.get_asset_refs(source_id) or []:
            if self.has_asset(ref):
                blob = self.blob_info("assets", asset_blob_name(ref))
                size = self._picture_size(blob)
                stored.append(
                    StoredAsset(
                        ref,
                        blob.name,
                        blob.sha256,
                        blob.size_bytes,
                        width=size[0] if size else None,
                        height=size[1] if size else None,
                        is_unreadable=size is None,
                    )
                )
        return RawBundle(self.get_item(source_id), self.get_detail(source_id), tuple(stored))

    # --- internals --------------------------------------------------------------------------

    def _picture_size(self, blob: BlobInfo) -> tuple[int, int] | None:
        final = self._path(blob.kind, blob.name)
        if not final.exists():
            self._read_verified(blob)  # finishes an interrupted write, or says the blob is missing
        return probe_dimensions(final)

    def _path(self, kind: BlobKind, name: str) -> Path:
        if kind not in BLOB_KINDS or not _BLOB_NAME.fullmatch(name) or len(name) > _MAX_NAME:
            raise SnapshotError(f"unsafe blob name {kind}/{name[:60]!r}")
        return self.run_dir / kind / name

    def _read_verified(self, blob: BlobInfo) -> bytes:
        final = self._path(blob.kind, blob.name)
        content = _read_or_none(final)
        if content is None or sha256_hex(content) != blob.sha256:
            pending = _pending_path(final, blob.sha256)
            if pending.exists():
                # The writer died after logging the blob but before the rename: finish it.
                pending.replace(final)
                content = _read_or_none(final)
        if content is None:
            raise SnapshotError(f"{blob.kind}/{blob.name} is listed in the manifest but missing")
        if sha256_hex(content) != blob.sha256:
            raise SnapshotError(f"{blob.kind}/{blob.name} does not match its recorded sha256")
        return content

    def _drop_torn_manifest_tail(self) -> None:
        """Cut a half-written last line off the file, before the next append glues onto it."""
        path = self.run_dir / _MANIFEST
        complete, _, _ = path.read_bytes().rpartition(b"\n")
        atomic_write_bytes(path, complete + b"\n" if complete else b"")
        self._has_torn_manifest_tail = False

    def _append_manifest(self, blob: BlobInfo) -> None:
        if self._has_torn_manifest_tail:
            self._drop_torn_manifest_tail()
        line = canonical_json(
            {"kind": blob.kind, "name": blob.name, "sha256": blob.sha256, "size": blob.size_bytes}
        )
        fd = os.open(self.run_dir / _MANIFEST, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "ab") as handle:
            handle.write(line + b"\n")
            handle.flush()
            os.fsync(handle.fileno())


def _item_name(source_id: str) -> str:
    return f"{blob_stem(source_id)}.json"


def _refs_name(source_id: str) -> str:
    return f"{blob_stem(source_id)}.refs.json"


def _pending_path(final: Path, digest: str) -> Path:
    return final.with_name(f".{final.name}.{digest[:16]}.pending")


def _read_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _as_object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise SnapshotError(f"{label}: expected a JSON object, got {type(value).__name__}")
    return value


def _load_manifest(path: Path) -> tuple[dict[tuple[BlobKind, str], BlobInfo], bool]:
    """The manifest's entries, and whether its last line is a torn fragment that was skipped.

    Read-only on purpose: the writer may be appending right now, and a half line seen here can be
    a whole one a moment later. The fragment's blob was never renamed into place, so skipping it
    loses nothing.
    """
    entries: dict[tuple[BlobKind, str], BlobInfo] = {}
    if not path.exists():
        return entries, False
    text = path.read_bytes().decode("utf-8")
    complete, _, torn_tail = text.rpartition("\n")
    for number, line in enumerate(complete.splitlines(), start=1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SnapshotError(f"{_MANIFEST} line {number} is not valid JSON") from exc
        fields = _as_object(raw, f"{_MANIFEST} line {number}")
        kind, name, digest, size = (fields.get(k) for k in ("kind", "name", "sha256", "size"))
        if kind not in BLOB_KINDS or not isinstance(name, str) or not isinstance(digest, str):
            raise SnapshotError(f"{_MANIFEST} line {number} is malformed")
        if isinstance(size, bool) or not isinstance(size, int):
            raise SnapshotError(f"{_MANIFEST} line {number} has a non-integer size")
        entries[(kind, name)] = BlobInfo(kind, name, digest, size)
    return entries, bool(torn_tail)
