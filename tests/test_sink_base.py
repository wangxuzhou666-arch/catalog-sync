"""Batch validation and the MaterialRecord side of the sink interface."""

from __future__ import annotations

from decimal import Decimal

import pytest

from catalog_sync.models import OWNABLE_FIELDS, Composition, ImageRef, MaterialRecord, Money
from catalog_sync.sinks.base import Batch, FieldKind, check_value, row_key
from catalog_sync.sinks.material import MATERIAL_KINDS, material_batch
from catalog_sync.testing.sink_contract import CONTRACT_KINDS, contract_row

ROW = contract_row("A", name="a")


def test_batch_rejects_ownership_of_identity_fields() -> None:
    with pytest.raises(ValueError, match="identity"):
        Batch([ROW], owned=frozenset({"source_id"}), kinds=CONTRACT_KINDS)


def test_batch_needs_a_kind_for_every_owned_field() -> None:
    with pytest.raises(ValueError, match="without a kind"):
        Batch([ROW], owned=frozenset({"name", "colour"}), kinds=CONTRACT_KINDS)


def test_batch_keep_set_must_be_owned() -> None:
    with pytest.raises(ValueError, match="must be owned"):
        Batch(
            [ROW],
            owned=frozenset({"name"}),
            kinds=CONTRACT_KINDS,
            keep_existing_if_null=frozenset({"price"}),
        )


def test_batch_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError, match="only once"):
        Batch([ROW, ROW], owned=frozenset({"name"}), kinds=CONTRACT_KINDS)


def test_batch_rejects_a_row_that_lacks_an_owned_field() -> None:
    sparse = {"source": "contract", "source_id": "A", "name": "a"}
    with pytest.raises(ValueError, match="lacks the owned field 'price'"):
        Batch([sparse], owned=frozenset({"name", "price"}), kinds=CONTRACT_KINDS)


def test_a_row_without_a_string_key_is_refused() -> None:
    with pytest.raises(TypeError, match="source_id"):
        row_key({"source": "s", "source_id": 7})


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (FieldKind.TEXT, 3),
        (FieldKind.INTEGER, 1.5),
        (FieldKind.INTEGER, True),
        (FieldKind.REAL, "1.5"),
        (FieldKind.REAL, float("nan")),
        (FieldKind.REAL, float("inf")),
        (FieldKind.TIMESTAMP, 1_700_000_000),
        (FieldKind.TIMESTAMP, "yesterday"),
        (FieldKind.TIMESTAMP, "2026-01-02T03:04:05"),  # no offset: a different instant per machine
        (FieldKind.TIMESTAMP, "2026-01-02"),
    ],
)
def test_a_value_that_does_not_fit_its_kind_is_refused(kind: FieldKind, value: object) -> None:
    with pytest.raises(ValueError, match="declared"):
        check_value("x", kind, value)


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (FieldKind.TEXT, "t"),
        (FieldKind.INTEGER, 3),
        (FieldKind.REAL, 3),
        (FieldKind.REAL, 3.5),
        (FieldKind.JSON, {"a": [1]}),
        (FieldKind.JSON, []),
        (FieldKind.TEXT, None),
        (FieldKind.REAL, None),
        (FieldKind.TIMESTAMP, "2026-01-02T03:04:05+00:00"),
        (FieldKind.TIMESTAMP, "2026-01-02T03:04:05.123456+08:00"),
        (FieldKind.TIMESTAMP, None),
    ],
)
def test_values_that_fit_their_kind_pass(kind: FieldKind, value: object) -> None:
    check_value("x", kind, value)


def test_every_material_field_has_a_storage_kind() -> None:
    assert set(MATERIAL_KINDS) == OWNABLE_FIELDS, (
        "a MaterialRecord field was added or removed: decide how sinks store it"
    )


def test_a_fully_populated_record_fits_the_declared_kinds() -> None:
    full = MaterialRecord(
        source="s",
        source_id="1",
        code="c",
        name="n",
        composition=(Composition("Cotton", 60.0, "60%C"),),
        composition_raw="60%C",
        weight_gsm=180.0,
        width_cm=150.0,
        price=Money(Decimal("1.50"), "USD"),
        category="cat",
        use_cases=("u",),
        tags=("t",),
        supplier="sup",
        folder_paths=("a/b",),
        images=(ImageRef("thumb"),),
        extra={"ns": {"k": 1}},
        issues=("i",),
        source_version="v",
        raw_ref="r",
    )
    # Batch checks every owned value against its kind, so building it is the assertion.
    batch = material_batch([full], owned=frozenset(OWNABLE_FIELDS))
    assert batch.kinds is MATERIAL_KINDS
