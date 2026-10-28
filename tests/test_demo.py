"""The one-command demo must reject incomplete or inconsistent imported data."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

import demo


def sample() -> tuple[list[dict[str, Any]], dict[Any, Any], dict[Any, Any], dict[Any, Any]]:
    records = [
        {"source": "mock.materials", "source_id": str(n), "weight_gsm": 250.0} for n in range(24)
    ]
    rows = {(row["source"], row["source_id"]): dict(row) for row in records}
    return records, rows, deepcopy(rows), {str(n): {} for n in range(9)}


def test_demo_checks_values_and_identities_in_both_destinations() -> None:
    demo.verify_records(*sample())
    records, local, receiver, suppliers = sample()
    receiver[("mock.materials", "0")]["weight_gsm"] = 999
    with pytest.raises(ValueError, match="Stored fields"):
        demo.verify_records(records, local, receiver, suppliers)
    receiver[("mock.materials", "0")]["weight_gsm"] = 250.0
    local[("different-source", "0")] = local.pop(("mock.materials", "0"))
    with pytest.raises(ValueError, match="identities"):
        demo.verify_records(records, local, receiver, suppliers)


def test_demo_rejects_missing_and_duplicated_records() -> None:
    records, local, receiver, suppliers = sample()
    with pytest.raises(ValueError, match="Expected 24"):
        demo.verify_records(records[:-1], local, receiver, suppliers)
    records[-1] = dict(records[0])
    with pytest.raises(ValueError, match="identities"):
        demo.verify_records(records, local, receiver, suppliers)


def test_failed_verification_returns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> None:
        raise ValueError("mismatch")

    monkeypatch.setattr(demo, "run_demo", fail)
    assert demo.main() == 1
