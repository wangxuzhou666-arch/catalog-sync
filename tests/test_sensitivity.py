"""Fields a mapping marks sensitive: dropped by default, hashed or kept only when asked."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from catalog_sync.errors import ConfigError
from catalog_sync.sinks.base import Batch, FieldKind
from catalog_sync.sinks.jsonl import JsonlSink
from catalog_sync.sinks.sensitivity import (
    SensitivePolicy,
    apply_policy,
    hash_value,
    split_policy,
    workspace_salt,
)

SECRET = "Canary Supplier Ltd"
KINDS = {"name": FieldKind.TEXT, "supplier": FieldKind.TEXT, "tags": FieldKind.JSON}
SALT = b"\x01" * 32


def batch(**fields: object) -> Batch:
    row = {"source": "s", "source_id": "1", "name": "Twill", "supplier": SECRET, "tags": ["a"]}
    return Batch(
        [{**row, **fields}],
        owned=frozenset(KINDS),
        kinds=KINDS,
        keep_existing_if_null=frozenset({"supplier", "tags"}),
        is_full_snapshot=True,
    )


def refuse_salt() -> bytes:
    raise AssertionError("the salt must not be touched when nothing is hashed")


# --- the option ---------------------------------------------------------------------------------


def test_the_default_policy_is_drop_and_the_sink_never_sees_the_option() -> None:
    policy, remaining = split_policy({"path": "x.db"})
    assert policy is SensitivePolicy.DROP and remaining == {"path": "x.db"}
    policy, remaining = split_policy({"path": "x.db", "sensitive_fields": "hash"})
    assert policy is SensitivePolicy.HASH and remaining == {"path": "x.db"}


@pytest.mark.parametrize("bad", ["", "redact", "KEEP", True, 3])
def test_an_unknown_policy_is_a_config_error_listing_the_choices(bad: object) -> None:
    with pytest.raises(ConfigError, match="drop, hash, keep"):
        split_policy({"sensitive_fields": bad})


# --- drop ---------------------------------------------------------------------------------------


def test_drop_removes_the_field_from_the_batch_entirely() -> None:
    result = apply_policy(batch(), frozenset({"supplier"}), SensitivePolicy.DROP, refuse_salt)
    assert "supplier" not in result.owned and "supplier" not in result.kinds
    assert "supplier" not in result.keep_existing_if_null
    assert all("supplier" not in row for row in result.rows)
    assert result.is_full_snapshot and result.owned == {"name", "tags"}
    assert SECRET not in repr(result)


def test_a_dropped_field_is_not_touched_in_a_store_that_already_holds_it(tmp_path: Path) -> None:
    sink = JsonlSink(tmp_path / "out.jsonl")
    sink.open()
    sink.backup()
    sink.upsert(batch(supplier="Held Earlier"))
    sink.upsert(apply_policy(batch(), frozenset({"supplier"}), SensitivePolicy.DROP, refuse_salt))
    stored = (tmp_path / "out.jsonl").read_text(encoding="utf-8")
    assert "Held Earlier" in stored, "dropping means not writing, not erasing"
    assert SECRET not in stored


def test_drop_only_affects_owned_sensitive_fields() -> None:
    narrow = Batch(
        [{"source": "s", "source_id": "1", "name": "Twill", "supplier": SECRET}],
        owned=frozenset({"name"}),
        kinds=KINDS,
    )
    assert (
        apply_policy(narrow, frozenset({"supplier"}), SensitivePolicy.DROP, refuse_salt) is narrow
    )


# --- hash ---------------------------------------------------------------------------------------


def test_hash_replaces_the_value_with_a_stable_salted_digest() -> None:
    result = apply_policy(batch(), frozenset({"supplier"}), SensitivePolicy.HASH, lambda: SALT)
    (row,) = result.rows
    assert row["supplier"] == hash_value(SECRET, SALT)
    assert str(row["supplier"]).startswith("sha256:") and SECRET not in repr(result)
    assert row["name"] == "Twill", "other fields are untouched"
    assert result.kinds["supplier"] is FieldKind.TEXT
    assert "supplier" in result.owned and "supplier" in result.keep_existing_if_null


def test_the_same_value_hashes_the_same_and_a_different_salt_differs() -> None:
    assert hash_value(SECRET, SALT) == hash_value(SECRET, SALT)
    assert hash_value(SECRET, SALT) != hash_value(SECRET, b"\x02" * 32)
    assert hash_value("other", SALT) != hash_value(SECRET, SALT)
    assert hash_value({"a": 1, "b": 2}, SALT) == hash_value({"b": 2, "a": 1}, SALT)


def test_nothing_stays_nothing_when_hashing() -> None:
    for empty in (None, [], {}):
        kinds = {**KINDS, "supplier": FieldKind.JSON}
        source = Batch(
            [{"source": "s", "source_id": "1", "name": "n", "supplier": empty, "tags": []}],
            owned=frozenset(kinds),
            kinds=kinds,
        )
        result = apply_policy(source, frozenset({"supplier"}), SensitivePolicy.HASH, lambda: SALT)
        assert result.rows[0]["supplier"] is None


def test_hashing_a_structured_value_gives_one_digest_string() -> None:
    kinds = {**KINDS, "supplier": FieldKind.JSON}
    source = Batch(
        [{"source": "s", "source_id": "1", "name": "n", "supplier": {"id": 7}, "tags": []}],
        owned=frozenset(kinds),
        kinds=kinds,
    )
    result = apply_policy(source, frozenset({"supplier"}), SensitivePolicy.HASH, lambda: SALT)
    assert result.rows[0]["supplier"] == hash_value({"id": 7}, SALT)
    assert result.kinds["supplier"] is FieldKind.TEXT


# --- keep ---------------------------------------------------------------------------------------


def test_keep_leaves_the_batch_alone_and_never_asks_for_the_salt() -> None:
    original = batch()
    result = apply_policy(original, frozenset({"supplier"}), SensitivePolicy.KEEP, refuse_salt)
    assert result is original


def test_a_batch_with_no_sensitive_fields_does_not_create_a_salt() -> None:
    original = batch()
    assert apply_policy(original, frozenset(), SensitivePolicy.HASH, refuse_salt) is original


# --- the workspace salt -------------------------------------------------------------------------


def test_the_salt_is_created_once_private_and_then_reused(tmp_path: Path) -> None:
    path = tmp_path / "ws" / "secrets" / "hash.salt"
    first = workspace_salt(path)
    assert len(first) == 32 and first != b"\x00" * 32
    assert workspace_salt(path) == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["hash.salt"], "no temp file left behind"


def test_two_workspaces_have_different_salts(tmp_path: Path) -> None:
    assert workspace_salt(tmp_path / "a" / "s.salt") != workspace_salt(tmp_path / "b" / "s.salt")


def test_an_existing_salt_is_never_replaced(tmp_path: Path) -> None:
    path = tmp_path / "s.salt"
    path.write_text("ab" * 32 + "\n", encoding="ascii")
    os.chmod(path, 0o600)
    assert workspace_salt(path) == bytes.fromhex("ab" * 32)


def test_a_damaged_salt_file_is_an_error_not_a_silent_new_salt(tmp_path: Path) -> None:
    path = tmp_path / "s.salt"
    path.write_text("not hex\n", encoding="ascii")
    with pytest.raises(ConfigError, match="corrupt"):
        workspace_salt(path)
