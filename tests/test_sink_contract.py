"""Every built-in sink against the shared contract suite, and the suite against broken sinks."""

from __future__ import annotations

from pathlib import Path

import pytest

from catalog_sync.testing.sink_contract import CONTRACT_CHECKS, ContractCheck, ContractShape
from tests.sink_harnesses import (
    MUTANTS,
    SHAPES,
    SINK_NAMES,
    MutantBuilder,
    SinkName,
    failing_checks,
    harness_for,
    honest,
)


@pytest.mark.parametrize("name", SINK_NAMES)
@pytest.mark.parametrize("shape", SHAPES.values(), ids=SHAPES.keys())
@pytest.mark.parametrize("check", CONTRACT_CHECKS, ids=lambda check: check.__name__)
def test_sink_honours_the_contract(
    name: SinkName, shape: ContractShape, check: ContractCheck, tmp_path: Path
) -> None:
    check(harness_for(name, shape=shape), tmp_path)


def test_the_shapes_play_their_roles_with_real_columns() -> None:
    material, supplier = SHAPES["material"], SHAPES["supplier"]
    assert material.fields == {
        "name": "code",
        "weight": "weight_gsm",
        "rank": "schema_version",
        "price": "price",
        "tags": "use_cases",
        "seen_at": "fetched_at",
    }
    assert supplier.fields == {
        "name": "name",
        "weight": "rating",
        "rank": "lead_time_days",
        "price": "address",
        "tags": "certifications",
        "seen_at": "listed_at",
    }
    assert set(material.samples) == set(material.kinds) and len(material.kinds) == 20
    assert set(supplier.samples) == set(supplier.kinds) and len(supplier.kinds) == 15


# --- the contract suite must be able to fail (CODING-STYLE R.7) ----------------------------


@pytest.mark.parametrize("name", SINK_NAMES)
def test_the_real_sink_fails_no_check(name: SinkName, tmp_path: Path) -> None:
    assert failing_checks(name, tmp_path, honest) == set()


@pytest.mark.parametrize("name", SINK_NAMES)
@pytest.mark.parametrize(
    ("build", "caught_by"),
    [(build, caught_by) for _, build, caught_by in MUTANTS],
    ids=[description for description, _, _ in MUTANTS],
)
def test_each_contract_violation_turns_the_intended_check_red(
    name: SinkName, build: MutantBuilder, caught_by: str, tmp_path: Path
) -> None:
    assert caught_by in failing_checks(name, tmp_path, build)
