from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from catalog_sync.errors import RecordFormatError
from catalog_sync.models import (
    ALL_FIELDS,
    OWNABLE_FIELDS,
    PROVENANCE_FIELDS,
    Composition,
    ImageRef,
    MaterialRecord,
    Money,
    is_empty_json,
)


def full_record() -> MaterialRecord:
    return MaterialRecord(
        source="demo",
        source_id="A1",
        code="DEMO-0001",
        name="Demo Twill 01",
        composition=(Composition("cotton", 60.0, "60% cotton"), Composition(None, 40.0, "40% ??")),
        composition_raw="60% cotton 40% ??",
        weight_gsm=180.5,
        width_cm=150.0,
        price=Money(Decimal("12.50"), "USD"),
        category="twill",
        use_cases=("shirt",),
        tags=("a", "b"),
        supplier="Demo Supplier Ltd",
        folder_paths=("Demo Library/Twills",),
        images=(
            ImageRef(
                "thumb",
                "https://assets.example.invalid/x.png",
                "assets/x.bin",
                "ab" * 32,
                8,
                8,
                100,
            ),
        ),
        extra={"mock": {"raw_flag": 1}},
        issues=("composition:unknown_code:V",),
        source_version="7",
        raw_ref="run1/list/A1.json",
        fetched_at=datetime(2026, 1, 2, tzinfo=UTC),
        normalized_at=datetime(2026, 1, 3, tzinfo=UTC),
    )


def test_json_round_trip_is_lossless() -> None:
    record = full_record()
    text = json.dumps(record.to_dict(), allow_nan=False)
    assert MaterialRecord.from_dict(json.loads(text)) == record


def test_to_dict_covers_exactly_the_declared_fields() -> None:
    assert set(MaterialRecord("s", "i").to_dict()) == ALL_FIELDS
    assert {"source", "source_id"} & OWNABLE_FIELDS == set()
    assert PROVENANCE_FIELDS <= OWNABLE_FIELDS


def test_money_travels_as_a_string_and_keeps_its_exponent() -> None:
    encoded = Money(Decimal("0.10"), "USD").to_dict()
    assert encoded == {"amount": "0.10", "currency": "USD"}
    assert Money.from_dict(encoded).amount == Decimal("0.10")
    assert str(Money.from_dict(encoded).amount) == "0.10"


def test_money_survives_what_floats_cannot() -> None:
    amount = Decimal("1234567890123456.78")
    text = json.dumps(Money(amount, "USD").to_dict())
    assert Money.from_dict(json.loads(text)).amount == amount


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity"])
def test_money_rejects_non_finite_amounts(amount: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        Money(Decimal(amount), "USD")


@pytest.mark.parametrize("currency", ["usd", "US", "USDX", ""])
def test_money_rejects_malformed_currency(currency: str) -> None:
    with pytest.raises(ValueError, match="currency"):
        Money(Decimal(1), currency)


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        MaterialRecord("s", "i", fetched_at=datetime(2026, 1, 1))


def test_unknown_missing_or_mistyped_fields_are_record_format_errors() -> None:
    good = MaterialRecord("s", "i").to_dict()
    with pytest.raises(RecordFormatError, match="unknown fields"):
        MaterialRecord.from_dict({**good, "bonus": 1})
    missing = {k: v for k, v in good.items() if k != "tags"}
    with pytest.raises(RecordFormatError, match="KeyError"):
        MaterialRecord.from_dict(missing)
    with pytest.raises(RecordFormatError, match="expected str"):
        MaterialRecord.from_dict({**good, "name": 5})
    with pytest.raises(RecordFormatError, match="expected number"):
        MaterialRecord.from_dict({**good, "weight_gsm": True})
    with pytest.raises(RecordFormatError, match="schema_version"):
        MaterialRecord.from_dict({**good, "schema_version": 99})
    with pytest.raises(RecordFormatError):
        MaterialRecord.from_dict({**good, "price": {"amount": "not a number", "currency": "USD"}})


def test_empty_means_null_or_empty_container_only() -> None:
    assert all(is_empty_json(v) for v in (None, [], {}))
    assert not any(is_empty_json(v) for v in (0, 0.0, "", "x", [0], {"a": 1}, False))
