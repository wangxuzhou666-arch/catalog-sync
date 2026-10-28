"""Pull and normalize the Mock SaaS in process: what a correct run stores, and how it survives.

The seeded dataset is the oracle (tests/fixtures/mock_saas.py). Nothing here opens a socket: the
sources talk to the real service through the fake transport.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalog_sync.errors import ItemFailed
from catalog_sync.mock.service import RateLimit
from catalog_sync.normalize.mapping import load_mapping
from catalog_sync.normalize.run import NormalizedRun, find_run, normalize_run
from catalog_sync.puller import Puller, PullReport
from catalog_sync.snapshot import RunSnapshot
from catalog_sync.sources.base import Source
from catalog_sync.state import RunStatus, Stage, StateStore
from tests.conftest import REPO_ROOT
from tests.doubles import SimulatedCrash, Spy
from tests.fixtures.mock_saas import (
    BASE_URL,
    expected_material,
    expected_supplier,
    picked,
)
from tests.test_mock_plugin import Rig

MAPPINGS = REPO_ROOT / "src/catalog_sync/plugins/mock/mappings"
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def pull(source: Source, store: StateStore, runs_root: Path, **kwargs: object) -> PullReport:
    return Puller(source, store, runs_root).pull(**kwargs)  # type: ignore[arg-type]


def normalized(store: StateStore, runs_root: Path, source_name: str, mapping: str) -> NormalizedRun:
    run = find_run(store, [source_name], "latest")
    return normalize_run(store, runs_root, run, load_mapping(MAPPINGS / mapping), clock=lambda: NOW)


# --- what a correct run stores --------------------------------------------------------------------


def test_every_material_is_stored_as_the_seeded_data_says(
    store: StateStore, runs_root: Path
) -> None:
    rig = Rig()
    report = pull(rig.materials(), store, runs_root)
    assert report.status is RunStatus.COMPLETE and report.failures == ()
    result = normalized(store, runs_root, "mock.materials", "materials.yaml")
    assert result.is_full_snapshot and result.skipped == ()
    records = {r.source_id: r.to_dict() for r in result.records}
    dataset = rig.world.dataset
    assert set(records) == {m.id for m in dataset.materials}
    for material in dataset.materials:
        expected = expected_material(BASE_URL, dataset, material)
        assert picked(records[material.id], expected) == expected, material.id


def test_the_price_comes_from_the_detail_and_the_picture_from_the_listing(
    store: StateStore, runs_root: Path
) -> None:
    """The trap in one record: the listing says price 0 and has the picture, the detail has the
    price and an empty `thumbs`. Both facts must survive."""
    rig = Rig()
    pull(rig.materials(), store, runs_root)
    material = rig.world.dataset.materials[2]
    snapshot = RunSnapshot.open(runs_root / store.latest_run("mock.materials").run_id)  # type: ignore[union-attr]
    bundle = snapshot.bundle(material.id)
    assert bundle.item.payload["price"] == 0 and bundle.detail is not None
    assert bundle.detail["thumbs"] == []
    record = {
        r.source_id: r
        for r in normalized(store, runs_root, "mock.materials", "materials.yaml").records
    }[material.id].to_dict()
    assert record["price"] == {"amount": str(material.price), "currency": "USD"}
    assert [image["role"] for image in record["images"]] == ["thumb", "swatch"]  # type: ignore[index,union-attr]
    assert all(image["local_path"] for image in record["images"])  # type: ignore[union-attr]


def test_the_documented_blemishes_become_issues_not_values(
    store: StateStore, runs_root: Path
) -> None:
    rig = Rig()
    pull(rig.materials(), store, runs_root)
    records = {
        r.source_id: r.to_dict()
        for r in normalized(store, runs_root, "mock.materials", "materials.yaml").records
    }
    zero_weight = next(m for m in rig.world.dataset.materials if m.weight_gsm == 0)
    assert records[zero_weight.id]["weight_gsm"] is None
    assert "weight_gsm:source_zero" in records[zero_weight.id]["issues"]  # type: ignore[operator]
    unknown_code = next(m for m in rig.world.dataset.materials if "ZZ" in m.composition)
    assert "composition:unknown_code:ZZ" in records[unknown_code.id]["issues"]  # type: ignore[operator]
    shares = records[unknown_code.id]["composition"]
    assert any(share["fiber"] is None for share in shares)  # type: ignore[union-attr]


def test_every_supplier_is_stored_as_the_seeded_data_says(
    store: StateStore, runs_root: Path
) -> None:
    rig = Rig()
    report = pull(rig.suppliers(page_size=4), store, runs_root)
    assert report.status is RunStatus.COMPLETE and report.item_count == 9
    result = normalized(store, runs_root, "mock.suppliers", "suppliers.yaml")
    records = {r.source_id: r.to_dict() for r in result.records}
    for supplier in rig.world.dataset.suppliers:
        expected = expected_supplier(supplier)
        assert picked(records[supplier.id], expected) == expected, supplier.id
        assert records[supplier.id]["contact_email"] == supplier.contact_email
    assert result.records[0].schema.entity == "supplier"
    mapping = load_mapping(MAPPINGS / "suppliers.yaml")
    assert mapping.sensitive_fields == {"contact_email"}


def test_suppliers_cost_one_request_per_page_and_none_for_detail_or_pictures(
    store: StateStore, runs_root: Path
) -> None:
    rig = Rig()
    pull(rig.suppliers(page_size=4), store, runs_root)
    assert rig.world.log_of("/api/v1/suppliers") == [200, 200, 200]
    assert [e.path for e in rig.world.service.request_log if e.path.startswith("/media")] == []


# --- a second pull, and a pull that is interrupted ------------------------------------------------


def test_a_second_pull_fetches_no_detail_and_no_picture_again(
    store: StateStore, runs_root: Path
) -> None:
    rig = Rig()
    pull(rig.materials(), store, runs_root)
    details = len(rig.world.log_of("/api/v1/materials/"))
    media = len(rig.world.log_of("/media/"))
    assert details == 24 and media == 48
    second = pull(rig.materials(), store, runs_root)
    assert (second.detail_fetches, second.asset_fetches) == (0, 0)
    assert len(rig.world.log_of("/api/v1/materials/")) == details
    assert len(rig.world.log_of("/media/")) == media
    result = normalized(store, runs_root, "mock.materials", "materials.yaml")
    assert len(result.records) == 24, (
        "unchanged items are resolved through the run that fetched them"
    )


def test_suppliers_without_a_change_marker_are_detected_unchanged_by_their_hash(
    store: StateStore, runs_root: Path
) -> None:
    rig = Rig()
    pull(rig.suppliers(), store, runs_root)
    second = pull(rig.suppliers(), store, runs_root)
    assert second.stage_counts and second.item_count == 9
    assert second.detail_fetches == 0, "the listing row hashed the same, so nothing is refetched"


def test_one_item_that_cannot_be_fetched_does_not_stop_the_run(
    store: StateStore, runs_root: Path
) -> None:
    rig = Rig()
    victim = rig.world.dataset.materials[4].id

    def refuse(source_id: str) -> None:
        if source_id == victim:
            raise ItemFailed(source_id, "detail request answered HTTP 404")

    report = pull(Spy(rig.materials(), before_detail=refuse), store, runs_root)
    assert report.status is RunStatus.COMPLETE_WITH_FAILURES
    assert [f.source_id for f in report.failures] == [victim]
    assert [f.error for f in report.failures] == ["detail request answered HTTP 404"], "no id in it"
    result = normalized(store, runs_root, "mock.materials", "materials.yaml")
    assert {s.source_id for s in result.skipped} == {victim}, "never normalised from the row alone"
    assert len(result.records) == 23


def test_a_killed_pull_resumes_and_fetches_each_item_exactly_once(
    store: StateStore, runs_root: Path
) -> None:
    rig = Rig()
    calls = 0

    def die_on_tenth(_source_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 10:
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        pull(Spy(rig.materials(), before_detail=die_on_tenth), store, runs_root)
    run_id = store.latest_run("mock.materials").run_id  # type: ignore[union-attr]
    assert store.get_run(run_id).status is RunStatus.INTERRUPTED
    report = pull(rig.materials(), store, runs_root, resume=run_id)
    assert report.status is RunStatus.COMPLETE and report.item_count == 24
    assert len(rig.world.log_of("/api/v1/materials/")) == 24, "one detail request per item"
    assert len(rig.world.log_of("/media/")) == 48
    assert len(normalized(store, runs_root, "mock.materials", "materials.yaml").records) == 24


# --- surviving the traps through a whole pull -----------------------------------------------------


def test_a_whole_pull_survives_rate_limit_and_flaky_errors_together(
    store: StateStore, runs_root: Path
) -> None:
    rig = Rig(rate_limit=RateLimit(10, 5), error_every=7)
    report = pull(rig.materials(), store, runs_root)
    assert report.status is RunStatus.COMPLETE and report.item_count == 24
    statuses = rig.world.log_of("/api/")
    assert 429 in statuses and 503 in statuses, "both traps actually fired"
    result = normalized(store, runs_root, "mock.materials", "materials.yaml")
    assert len(result.records) == 24 and result.skipped == ()


def test_signed_links_are_kept_out_of_the_records_and_the_asset_refs(
    store: StateStore, runs_root: Path
) -> None:
    """The raw cache keeps the service's answers as they came, signatures included; what the tool
    derives from them (the asset list, the normalised records) carries the link without its
    query, so a signature does not travel into a database or an export."""
    rig = Rig(signed_url_ttl_s=300)
    report = pull(rig.materials(), store, runs_root)
    assert report.status is RunStatus.COMPLETE
    result = normalized(store, runs_root, "mock.materials", "materials.yaml")
    assert "sig=" not in repr([r.to_dict() for r in result.records])
    run_dir = runs_root / store.latest_run("mock.materials").run_id  # type: ignore[union-attr]
    refs = list((run_dir / "assets").glob("*.refs.json"))
    assert len(refs) == 24
    for path in refs:
        assert "sig=" not in path.read_text(encoding="utf-8"), path.name
    raw_rows = [p.read_text(encoding="utf-8") for p in (run_dir / "list").glob("*.json")]
    assert any("sig=" in row for row in raw_rows), "the premise: the raw answers did carry them"


def test_pictures_that_expire_before_they_are_downloaded_fail_those_items_not_the_run(
    store: StateStore, runs_root: Path
) -> None:
    """Listing first, pictures later: a signed link outliving its ttl in between is a 403 for that
    item. The run records it and goes on, the failure says why, and a new pull repairs it."""
    rig = Rig(signed_url_ttl_s=60, materials=3, suppliers=1)
    source = rig.materials()
    listed_only = Puller(source, store, runs_root).pull(stages=(Stage.LIST, Stage.DETAIL))
    rig.world.clock.now += 61
    late = Puller(source, store, runs_root).pull(stages=(Stage.ASSETS,), resume=listed_only.run_id)
    assert late.status is RunStatus.COMPLETE_WITH_FAILURES and len(late.failures) == 3
    assert "signed link has expired" in late.failures[0].error
    repaired = pull(rig.materials(), store, runs_root)
    assert repaired.status is RunStatus.COMPLETE and repaired.failures == ()


def test_a_declared_ceiling_keeps_a_pull_under_the_services_own_limit(
    store: StateStore, runs_root: Path
) -> None:
    """The operator declares five requests a minute; the service refuses more than five. A source
    that holds to its declaration is never refused, however many requests the pull needs."""
    rig = Rig(rate_limit=RateLimit(5, 60), materials=3, suppliers=1)
    source = rig.materials(max_requests_per_minute=5, request_gap_s=12)
    report = pull(source, store, runs_root)
    assert report.status is RunStatus.COMPLETE and report.item_count == 3
    assert 429 not in rig.world.log_of("/api/")
    assert len(rig.world.service.request_log) > 5, "more requests than the limit, spread out"
