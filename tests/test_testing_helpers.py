"""The helpers shipped in ``catalog_sync.testing`` for connectors' own tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from catalog_sync.errors import ConfigError
from catalog_sync.http import HttpClient, HttpSeams, HttpSettings
from catalog_sync.models import JsonObject
from catalog_sync.plugins.mock.settings import MockSettings
from catalog_sync.plugins.mock.source import MockMaterialsSource, MockSuppliersSource
from catalog_sync.testing.boundaries import other_plugins_reached, python_files, reaches
from catalog_sync.testing.fake_transport import (
    FakeResponse,
    FakeTransport,
    RecordedRequest,
    SimulatedCrash,
)
from catalog_sync.testing.oracle import picked, sorted_issues
from catalog_sync.testing.pipeline import pull_and_normalize
from catalog_sync.testing.user_agent import policy_section
from catalog_sync.testing.world import ServiceWorld, WorldClock
from tests.conftest import REPO_ROOT
from tests.fixtures.mock_saas import BASE_URL, World, expected_material

MATERIALS_MAPPING = REPO_ROOT / "src/catalog_sync/plugins/mock/mappings/materials.yaml"


# --- the world ------------------------------------------------------------------------------------


def test_the_clients_waiting_moves_the_services_clock_by_the_same_amount() -> None:
    clock = WorldClock()
    started = clock.now
    answers = iter([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse.json({})])
    seen_at: list[float] = []

    def service(_request: RecordedRequest) -> FakeResponse:
        seen_at.append(clock())  # the service reads the same clock the client sleeps on
        return next(answers)

    world = ServiceWorld(service, clock=clock)
    client = HttpClient({}, credentialed=False, settings=HttpSettings(gap_s=0.0), seams=world.seams)
    client.get_json("https://svc.example.invalid/x")
    assert world.clock.sleeps == [7.0], "the client honoured Retry-After"
    assert seen_at == [started, started + 7.0], "and the service saw those seven seconds pass"


def test_a_world_makes_its_own_clock_when_none_is_given() -> None:
    first, second = (
        ServiceWorld(lambda _r: FakeResponse(200)),
        ServiceWorld(lambda _r: FakeResponse(200)),
    )
    assert first.clock is not second.clock
    first.clock.sleep(5)
    assert second.clock.sleeps == []


# --- the oracle -----------------------------------------------------------------------------------


def test_issues_are_compared_in_a_stable_order() -> None:
    assert sorted_issues({"issues": ["b:2", "a:1"]}) == ["a:1", "b:2"]


def test_a_record_is_reduced_to_the_keys_the_expectation_names() -> None:
    row: JsonObject = {
        "name": "x",
        "extra": 1,
        "issues": ["z", "a"],
        "images": [{"role": "thumb", "sha256": "ab", "width": 8}, {"role": "swatch", "width": 9}],
    }
    expected: JsonObject = {
        "name": "x",
        "issues": ["a", "z"],
        "images": [{"role": "thumb", "width": 8}, {"role": "swatch", "width": 9}],
    }
    assert picked(row, expected) == expected
    assert picked(row, {"name": "y"}) == {"name": "x"}, "a difference shows up in the comparison"
    wrong = {**expected, "images": [{"role": "thumb", "width": 7}, {"role": "swatch", "width": 9}]}
    assert picked(row, wrong) != wrong


# --- pull and normalize ---------------------------------------------------------------------------


def test_pull_and_normalize_matches_the_dataset_oracle(tmp_path: Path) -> None:
    world = World()
    settings = MockSettings.from_options(
        {"credential": world.handle(), "request_gap_s": 0}, source="mock.materials"
    )
    source = MockMaterialsSource(settings, seams=world.seams)
    result = pull_and_normalize(source, mapping=MATERIALS_MAPPING, workdir=tmp_path)
    assert result.report.failures == () and result.normalized.skipped == ()
    rows = result.rows()
    assert set(rows) == {m.id for m in world.dataset.materials}
    for material in world.dataset.materials:
        expected = expected_material(BASE_URL, world.dataset, material)
        assert picked(rows[material.id], expected) == expected, material.id  # type: ignore[arg-type]


def test_a_mapping_for_another_source_is_refused(tmp_path: Path) -> None:
    world = World()
    settings = MockSettings.from_options(
        {"credential": world.handle(), "request_gap_s": 0}, source="mock.suppliers"
    )
    source = MockSuppliersSource(settings, seams=world.seams)
    with pytest.raises(ConfigError, match=r"written for source 'mock\.materials'"):
        pull_and_normalize(source, mapping=MATERIALS_MAPPING, workdir=tmp_path)


# --- a crash that nothing catches -----------------------------------------------------------------


def test_a_simulated_crash_is_raised_by_the_scripted_request_and_is_not_an_exception() -> None:
    clock = WorldClock()
    transport = FakeTransport()
    transport.crash_on_request(1, SimulatedCrash())
    seams = HttpSeams(adapter=transport, sleep=clock.sleep, monotonic=clock.monotonic)
    client = HttpClient({}, credentialed=False, settings=HttpSettings(gap_s=0.0), seams=seams)
    with pytest.raises(SimulatedCrash):
        client.get_json("https://svc.example.invalid/x")
    assert not issubclass(SimulatedCrash, Exception), "no `except Exception` may swallow a kill"


# --- the scanners for a connector package's own tests ---------------------------------------------


def test_a_connector_package_outside_the_repository_is_scanned_like_a_built_in_one(
    tmp_path: Path,
) -> None:
    package = tmp_path / "catalog_sync_acme"
    package.mkdir()
    (package / "source.py").write_text("from catalog_sync.sdk import Connection\n", "utf-8")
    (package / "bad.py").write_text("from catalog_sync.plugins.mock import api\n", "utf-8")
    files = python_files(package)
    assert [path.name for path in files] == ["bad.py", "source.py"]
    reached = {path.name: other_plugins_reached(path.read_text("utf-8")) for path in files}
    assert reached == {"bad.py": ["mock"], "source.py": []}
    assert reaches((package / "source.py").read_text("utf-8"), "catalog_sync.sdk")


def test_the_policy_section_runs_to_the_next_heading_and_is_empty_without_the_heading() -> None:
    document = "# Title\n### User-Agent policy\nthe text\nmore\n### Next\nelsewhere\n"
    assert policy_section(document) == "\nthe text\nmore"
    assert policy_section("# Title\nno such heading\n") == ""
