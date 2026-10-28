"""examples/contract is exactly what the generator produces, and it shows what the contract says.

The first test regenerates the example (a real run against a Mock SaaS and the reference platform
on loopback) into a temp directory and compares every file with the committed copy. The pictures
the Mock SaaS draws are encoded by Pillow, so their digests and sizes follow the Pillow version
pinned in uv.lock; regenerate with `uv run python tools/make_contract_example.py` after bumping it.

The other tests read the committed files and check the cases docs/data-contract.md talks about, so
a regenerated example that stopped showing one of them cannot pass for a clean diff.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from make_contract_example import build
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.mock

EXAMPLE = REPO_ROOT / "examples" / "contract"
HOST_PATH = re.compile(r"/(?:Users|home|private)/")


def tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def case(entity: str, item_id: str) -> dict[str, dict[str, object]]:
    loaded = json.loads(
        (EXAMPLE / entity / "cases" / f"{item_id}.json").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def issues_of(shown: dict[str, object]) -> list[str]:
    record = shown["normalized_record"]
    assert isinstance(record, dict)
    return list(record["issues"])


def test_the_committed_example_is_exactly_what_the_generator_produces(tmp_path: Path) -> None:
    regenerated = build(tmp_path / "contract")
    assert tree(regenerated) == tree(EXAMPLE), (
        "examples/contract is stale: run `uv run python tools/make_contract_example.py`"
    )


def test_the_example_holds_no_host_path_and_no_secret_shaped_value() -> None:
    for name, text in tree(EXAMPLE).items():
        assert not HOST_PATH.search(text), name
        assert "Bearer " not in text and "PLATFORM_TOKEN=" not in text, name


@pytest.mark.parametrize(
    ("item_id", "expected_issues"),
    [
        ("mat-0002", []),  # the key is missing
        ("mat-0003", []),  # the key is null
        ("mat-0004", []),  # the key is ""
        ("mat-0005", ["width_cm:unparseable"]),
    ],
)
def test_missing_null_empty_and_unparseable_all_end_up_as_null_and_only_the_last_says_so(
    item_id: str, expected_issues: list[str]
) -> None:
    run_1 = case("materials", item_id)["run_1"]
    record = run_1["normalized_record"]
    assert isinstance(record, dict)
    assert record["width_cm"] is None
    assert issues_of(run_1) == expected_issues
    detail = run_1["raw_detail_response"]
    assert isinstance(detail, dict)
    raw_detail = detail["body"]["data"]["data"]  # type: ignore[index]
    assert ("width" in raw_detail) == (item_id != "mat-0002")


def test_a_failed_item_is_isolated_listed_and_loaded_nowhere_until_it_succeeds() -> None:
    run_1, run_2 = case("materials", "mat-0007")["run_1"], case("materials", "mat-0007")["run_2"]
    assert run_1["raw_detail_response"] == {
        "http_status": 404,
        "body": {"state": False, "msg": "no such material"},
    }
    assert run_1["normalized_record"] is None
    assert run_1["sqlite_row"] is None and run_1["platform_row"] is None
    assert run_2["sqlite_row"] is not None and run_2["platform_row"] is not None
    transcript = (EXAMPLE / "materials" / "run-1" / "transcript.txt").read_text(encoding="utf-8")
    assert "failed detail mat-0007: " in transcript.split("$ catalog-sync runs show")[1]
    assert "skipped mat-0007: not fully pulled" in transcript
    assert "[exit 5]" in transcript


def test_keep_existing_and_a_platform_owned_column_survive_the_second_load() -> None:
    run_2 = case("materials", "mat-0001")["run_2"]
    record = run_2["normalized_record"]
    assert isinstance(record, dict) and record["images"] == []
    for stored in ("sqlite_row", "platform_row"):
        row = run_2[stored]
        assert isinstance(row, dict)
        assert len(row["images"]) == 2, "on_null: keep_existing kept the stored pictures"
        assert row["ops_note"] == "checked by hand, keep", "a column no mapping owns is untouched"


def test_a_vanished_record_is_reported_and_its_row_is_kept() -> None:
    run_2 = case("materials", "mat-0008")["run_2"]
    assert run_2["normalized_record"] is None and run_2["raw_list_row"] is None
    assert run_2["sqlite_row"] is not None and run_2["platform_row"] is not None
    transcript = (EXAMPLE / "materials" / "run-2" / "transcript.txt").read_text(encoding="utf-8")
    assert transcript.count("1 stored row(s) no longer in the source (reported, kept)") == 2


def test_the_sensitive_value_is_plain_in_the_file_and_dropped_or_hashed_in_the_sinks() -> None:
    shown = case("suppliers", "sup-001")
    record = shown["normalized_record"]
    assert isinstance(record, dict) and record["contact_email"] == "supplier-001@example.com"
    for dropped in ("sqlite_row_default_policy_drop", "platform_row"):
        row = shown[dropped]
        assert isinstance(row, dict) and "contact_email" not in row
    hashed = shown["sqlite_row_policy_hash"]
    assert isinstance(hashed, dict) and hashed["contact_email"] == "sha256:<salted-hash>"
    audit = (EXAMPLE / "audit.jsonl").read_text(encoding="utf-8")
    assert "supplier-001@example.com" not in audit


def test_the_second_entity_shows_a_blank_and_an_unparseable_value() -> None:
    assert issues_of(case("suppliers", "sup-003")) == []
    assert issues_of(case("suppliers", "sup-004")) == ["listed_at:unparseable"]
