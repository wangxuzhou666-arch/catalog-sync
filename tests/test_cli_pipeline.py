"""normalize, load, run and audit through the real CLI, over the synthetic replay snapshot.

Only the source is a replay of stored data; the config, state database, snapshots, mapping engine,
sinks and audit log are the real ones, and the suite runs with sockets disabled.
"""

from __future__ import annotations

import csv
import json
import re
import socket
import sqlite3
import textwrap
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pytest_socket import SocketBlockedError

from catalog_sync import cli, cli_pipeline, cli_sources
from catalog_sync.audit import AuditLog
from catalog_sync.compliance import Compliance
from catalog_sync.config import Workspace
from catalog_sync.errors import EXIT_COMPLETED_WITH_FAILURES, ItemFailed
from catalog_sync.puller import PullReport
from catalog_sync.sinks.csv import CsvSink
from catalog_sync.sinks.material import MATERIAL_KINDS
from catalog_sync.sinks.sqlite import read_table
from catalog_sync.state import FailureRow, RunStatus, Stage, StageStatus, StateStore
from tests.conftest import MOCK_SNAPSHOT_DIR
from tests.doubles import Spy, make_scripted_mock
from tests.fixtures import mock_snapshot
from tests.fixtures.mock_saas import picked

DEFAULT_MAPPING = mock_snapshot.MAPPING
SOURCE = mock_snapshot.SOURCE
ITEMS = mock_snapshot.ITEM_COUNT
MATERIALS = mock_snapshot.MATERIALS
SUPPLIER_OF = {m.id: m.supplier_name for m in MATERIALS}  # synthetic names, sensitive in the tests
SUPPLIERS = mock_snapshot.SUPPLIERS

SENSITIVE_MAPPING = mock_snapshot.sensitive_supplier_mapping()


class Bench:
    """A workspace, a config with one replay source and the three file sinks, and a CLI runner."""

    def __init__(self, tmp_path: Path, *, mapping: Path = DEFAULT_MAPPING, sink_extra: str = ""):
        self.dir = tmp_path
        self.home = tmp_path / "ws"
        self.config = tmp_path / "catalog-sync.toml"
        self.db = tmp_path / "out" / "catalog.db"
        self.config.write_text(
            textwrap.dedent(
                f"""
                [sources.demo]
                plugin = "replay"
                path = "{MOCK_SNAPSHOT_DIR}"

                [sinks.db]
                kind = "sqlite"
                path = "out/catalog.db"
                {sink_extra}

                [sinks.lines]
                kind = "jsonl"
                path = "out/catalog.jsonl"

                [sinks.sheet]
                kind = "csv"
                path = "out/catalog.csv"

                [profiles.demo]
                source = "demo"
                mapping = "{mapping}"
                sinks = ["db", "lines", "sheet"]
                """
            ),
            encoding="utf-8",
        )

    def run(self, *args: str) -> int:
        return cli.main(["--config", str(self.config), "--workspace", str(self.home), *args])

    def normalize(self, out: Path | None = None, mapping: Path = DEFAULT_MAPPING) -> int:
        out = out or self.dir / "records.jsonl"
        return self.run(
            "normalize", SOURCE, "--run", "latest", "--mapping", str(mapping), "--out", str(out)
        )

    @property
    def audit(self) -> list[dict[str, object]]:
        return AuditLog(self.home / "audit.jsonl").read()


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    return Bench(tmp_path)


def rows(bench: Bench) -> dict[tuple[str, str], dict[str, object]]:
    return read_table(bench.db)


# --- normalize ------------------------------------------------------------------------------------


def test_normalize_works_from_the_cache_alone_with_sockets_disabled_and_no_source_built(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bench.run("pull", "demo") == 0
    capsys.readouterr()

    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("normalize must not build a source")

    monkeypatch.setattr(cli_sources.registry, "source_factory", explode)
    with pytest.warns(UserWarning), pytest.raises(SocketBlockedError):
        socket.socket()  # the environment really does refuse sockets
    assert bench.normalize() == 0

    out = capsys.readouterr().out
    assert f"({SOURCE}): {ITEMS} record(s)" in out
    lines = (bench.dir / "records.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == ITEMS
    meta = json.loads((bench.dir / "records.jsonl.meta.json").read_text(encoding="utf-8"))
    assert meta["is_full_snapshot"] is True and meta["record_count"] == ITEMS


def test_normalize_reports_what_was_unusable_in_the_source(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    bench.run("pull", "demo")
    capsys.readouterr()
    bench.normalize()
    out = capsys.readouterr().out
    assert "weight_gsm:source_zero x2" in out
    assert "composition:unknown_code x2" in out


def test_a_changed_mapping_gives_a_different_file_with_no_pull_in_between(
    bench: Bench, tmp_path: Path
) -> None:
    bench.run("pull", "demo")
    bench.normalize(tmp_path / "a.jsonl")
    changed = tmp_path / "changed.yaml"
    changed.write_text(
        DEFAULT_MAPPING.read_text(encoding="utf-8").replace(
            "zero_is_missing: true", "zero_is_missing: false"
        ),
        encoding="utf-8",
    )
    bench.normalize(tmp_path / "b.jsonl", mapping=changed)

    def weights(name: str) -> list[object]:
        text = (tmp_path / name).read_text(encoding="utf-8")
        return [json.loads(line)["weight_gsm"] for line in text.splitlines()]

    assert weights("a.jsonl").count(None) == 2
    assert weights("b.jsonl").count(None) == 0 and weights("b.jsonl").count(0.0) == 2


def test_normalize_names_what_to_do_when_there_is_nothing_to_normalize(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bench.normalize() == 1
    assert "no run of mock.materials to normalise; pull first" in capsys.readouterr().err


def test_normalize_by_the_config_entry_name_finds_the_runs_recorded_under_the_plugin_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bench = Bench(tmp_path)
    bench.run("pull", "demo")
    # "demo" is a replay of a mock.materials snapshot: runs are recorded as mock.materials.
    assert (
        bench.run(
            "normalize",
            "demo",
            "--mapping",
            str(DEFAULT_MAPPING),
            "--out",
            str(tmp_path / "x.jsonl"),
        )
        == 1
    )
    assert "no run of demo / replay" in capsys.readouterr().err


def test_a_mapping_for_another_source_is_refused(
    bench: Bench, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bench.run("pull", "demo")
    other = tmp_path / "other.yaml"
    other.write_text(
        DEFAULT_MAPPING.read_text(encoding="utf-8").replace(
            "source: mock.materials", "source: other"
        ),
        encoding="utf-8",
    )
    assert bench.normalize(mapping=other) == 1
    assert "written for source 'other'" in capsys.readouterr().err


# --- load -----------------------------------------------------------------------------------------


def prepared(bench: Bench) -> Path:
    bench.run("pull", "demo")
    assert bench.normalize() == 0
    return bench.dir / "records.jsonl"


def test_a_dry_run_prints_the_report_and_writes_nothing_not_even_a_backup(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    records = prepared(bench)
    bench.run("load", "--sink", "db", "--input", str(records))
    capsys.readouterr()
    before = {p: p.read_bytes() for p in (bench.dir / "out").iterdir()}

    assert bench.run("load", "--sink", "db", "--input", str(records), "--dry-run") == 0
    out = capsys.readouterr().out
    assert "dry run, nothing written" in out and f"unchanged {ITEMS}" in out
    assert {p: p.read_bytes() for p in (bench.dir / "out").iterdir()} == before, "no backup either"
    assert bench.audit[-1]["outcome"] == "dry_run"


def test_a_dry_run_into_a_store_that_does_not_exist_yet_creates_nothing(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    records = prepared(bench)
    for sink in ("db", "lines", "sheet"):
        assert bench.run("load", "--sink", sink, "--input", str(records), "--dry-run") == 0
    assert f"inserted {ITEMS}" in capsys.readouterr().out
    assert not (bench.dir / "out").exists()


def test_a_load_takes_a_backup_holding_the_rows_from_before_it(
    bench: Bench, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    records = prepared(bench)
    assert bench.run("load", "--sink", "db", "--input", str(records)) == 0
    first = capsys.readouterr().out
    assert f"inserted {ITEMS}" in first and "backup: nothing stored yet" in first
    before = rows(bench)
    assert len(before) == ITEMS

    changed = tmp_path / "changed.yaml"
    changed.write_text(
        DEFAULT_MAPPING.read_text(encoding="utf-8").replace(
            "    from: [list.name, detail.name]", "    from: [detail.code]"
        ),
        encoding="utf-8",
    )
    bench.normalize(tmp_path / "changed.jsonl", mapping=changed)
    assert bench.run("load", "--sink", "db", "--input", str(tmp_path / "changed.jsonl")) == 0
    second = capsys.readouterr().out
    assert f"updated {ITEMS}" in second and f"({ITEMS} row(s) before)" in second

    (backup,) = bench.db.parent.glob("catalog.db.bak-*")
    assert read_table(backup) == before, "the backup holds exactly the pre-load rows"
    assert rows(bench) != before


def test_loading_the_same_file_twice_changes_nothing(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    records = prepared(bench)
    bench.run("load", "--sink", "db", "--input", str(records))
    bench.normalize()  # a new normalised_at must not look like a change
    capsys.readouterr()
    assert bench.run("load", "--sink", "db", "--input", str(records)) == 0
    assert f"inserted 0, updated 0, unchanged {ITEMS}" in capsys.readouterr().out


def test_load_refuses_input_without_its_metadata_and_unknown_sinks(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    records = prepared(bench)
    assert bench.run("load", "--sink", "nope", "--input", str(records)) == 1
    assert (
        "unknown sink 'nope' (defined in the config: db, lines, sheet)" in capsys.readouterr().err
    )
    (bench.dir / "records.jsonl.meta.json").unlink()
    assert bench.run("load", "--sink", "db", "--input", str(records)) == 1
    assert "metadata" in capsys.readouterr().err


def test_load_without_a_config_names_what_is_missing(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    records = prepared(bench)
    capsys.readouterr()
    code = cli.main(
        ["--workspace", str(bench.home), "load", "--sink", "db", "--input", str(records)]
    )
    assert code == 1
    assert "needs a config file defining [sinks.db]" in capsys.readouterr().err


# --- sensitive fields -----------------------------------------------------------------------------


def sensitive_bench(tmp_path: Path, policy: str | None) -> tuple[Bench, Path]:
    mapping = tmp_path / "sensitive.yaml"
    mapping.write_text(SENSITIVE_MAPPING, encoding="utf-8")
    extra = f'sensitive_fields = "{policy}"' if policy else ""
    bench = Bench(tmp_path, mapping=mapping, sink_extra=extra)
    bench.run("pull", "demo")
    assert bench.normalize(mapping=mapping) == 0
    return bench, bench.dir / "records.jsonl"


def test_a_sensitive_field_is_dropped_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bench, records = sensitive_bench(tmp_path, None)
    text = records.read_text(encoding="utf-8")
    assert all(name in text for name in SUPPLIERS), "the normalised file itself holds them"
    capsys.readouterr()
    assert bench.run("load", "--sink", "db", "--input", str(records)) == 0
    assert "sensitive fields (drop): supplier" in capsys.readouterr().out

    assert len(rows(bench)) == ITEMS
    assert all("supplier" not in row for row in rows(bench).values())
    stored = bench.db.read_bytes()
    assert not [name for name in SUPPLIERS if name.encode() in stored]
    for sink in ("lines", "sheet"):
        assert bench.run("load", "--sink", sink, "--input", str(records)) == 0
    for written in ("catalog.jsonl", "catalog.csv"):
        text = (bench.dir / "out" / written).read_text(encoding="utf-8")
        assert text and not [name for name in SUPPLIERS if name in text], written


def supplier_values(bench: Bench) -> dict[str, str]:
    """Each material's stored supplier value, by source id."""
    return {str(key[1]): str(row["supplier"]) for key, row in rows(bench).items()}


def test_hashing_is_stable_within_a_workspace_and_hides_the_value(tmp_path: Path) -> None:
    bench, records = sensitive_bench(tmp_path, "hash")
    assert bench.run("load", "--sink", "db", "--input", str(records)) == 0
    digests = supplier_values(bench)
    assert len(digests) == ITEMS
    assert all(value.startswith("sha256:") for value in digests.values())
    assert not [name for name in SUPPLIERS if name in "".join(digests.values())]
    assert not [name for name in SUPPLIERS if name.encode() in bench.db.read_bytes()]
    assert len(set(digests.values())) == len(SUPPLIERS), "one digest per supplier, no collisions"
    for material in MATERIALS:
        for other in MATERIALS:
            same_supplier = material.supplier_name == other.supplier_name
            assert (digests[material.id] == digests[other.id]) is same_supplier

    bench.run("pull", "demo")  # a whole new run, normalised and loaded again
    bench.normalize(mapping=tmp_path / "sensitive.yaml")
    assert bench.run("load", "--sink", "db", "--input", str(records)) == 0
    assert supplier_values(bench) == digests

    salt = bench.home / "secrets" / "hash.salt"
    assert salt.stat().st_mode & 0o777 == 0o600
    (tmp_path / "other").mkdir()
    other, other_records = sensitive_bench(tmp_path / "other", "hash")
    other.run("load", "--sink", "db", "--input", str(other_records))
    elsewhere = {str(key[1]): str(row["supplier"]) for key, row in read_table(other.db).items()}
    assert elsewhere.keys() == digests.keys()
    assert not [source_id for source_id in digests if elsewhere[source_id] == digests[source_id]]


def test_keep_writes_the_value_only_when_asked_to(tmp_path: Path) -> None:
    bench, records = sensitive_bench(tmp_path, "keep")
    assert bench.run("load", "--sink", "db", "--input", str(records)) == 0
    assert supplier_values(bench) == SUPPLIER_OF


def test_an_unknown_policy_is_refused_before_anything_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bench, records = sensitive_bench(tmp_path, "redact")
    assert bench.run("load", "--sink", "db", "--input", str(records)) == 1
    assert "drop, hash, keep" in capsys.readouterr().err
    assert not bench.db.exists()


def test_the_audit_log_never_holds_a_sensitive_value_or_a_record(tmp_path: Path) -> None:
    bench, records = sensitive_bench(tmp_path, "keep")  # even when the sink keeps it
    bench.run("load", "--sink", "db", "--input", str(records))
    logged = (bench.home / "audit.jsonl").read_text(encoding="utf-8")
    assert logged, "there is a log to look in"
    assert not [name for name in SUPPLIERS if name in logged]
    assert not [m.name for m in MATERIALS if m.name in logged]
    assert not [m.code for m in MATERIALS if m.code in logged]


# --- run: pull, normalize, load -------------------------------------------------------------------


def test_run_turns_the_synthetic_snapshot_into_the_expected_sqlite_rows(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bench.run("run", "demo") == 0
    out = capsys.readouterr().out
    assert f"complete, {ITEMS} item(s)" in out
    for sink in ("db (sqlite)", "lines (jsonl)", "sheet (csv)"):
        assert f"load -> {sink}, written" in out

    stored = rows(bench)
    assert len(stored) == ITEMS
    assert {key[0] for key in stored} == {SOURCE}
    for material in MATERIALS:  # the dataset says what every row must hold
        expected = mock_snapshot.expected_record(material)
        assert picked(stored[(SOURCE, material.id)], expected) == expected, material.id

    item = stored[(SOURCE, mock_snapshot.CLEAN.id)]
    assert item["issues"] == [] and item["schema_version"] == 1
    assert [image["role"] for image in item["images"]] == ["thumb", "swatch"]  # type: ignore[attr-defined,union-attr]
    for image in item["images"]:  # type: ignore[attr-defined,union-attr]
        assert len(image["sha256"]) == 64 and image["size_bytes"] > 0

    zero = stored[(SOURCE, mock_snapshot.ZERO_WEIGHT[0].id)]
    assert zero["weight_gsm"] is None and "weight_gsm:source_zero" in zero["issues"]  # type: ignore[operator]
    unknown = stored[(SOURCE, mock_snapshot.UNKNOWN_FIBRE[0].id)]
    assert [c["fiber"] is None for c in unknown["composition"]] == [False, True]  # type: ignore[attr-defined,union-attr]
    assert "composition:unknown_code:ZZ" in unknown["issues"]  # type: ignore[operator]


def test_run_fills_every_configured_sink_with_the_same_records(bench: Bench) -> None:
    assert bench.run("run", "demo") == 0
    jsonl = [
        json.loads(line)
        for line in (bench.dir / "out" / "catalog.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(jsonl) == ITEMS
    from_csv = CsvSink(bench.dir / "out" / "catalog.csv").read_rows(MATERIAL_KINDS)
    from_db = rows(bench)
    assert set(from_csv) == set(from_db) == {(r["source"], r["source_id"]) for r in jsonl}
    for row in jsonl:
        key = (row["source"], row["source_id"])
        for field in ("code", "name", "weight_gsm", "width_cm", "composition", "images", "issues"):
            assert from_db[key][field] == row[field] == from_csv[key][field], field
    with (bench.dir / "out" / "catalog.csv").open(encoding="utf-8", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == ITEMS


def test_a_second_run_pulls_nothing_resolves_through_the_first_and_changes_nothing(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bench.run("run", "demo") == 0
    capsys.readouterr()
    before = rows(bench)

    assert bench.run("run", "demo") == 0
    out = capsys.readouterr().out
    assert out.count(f"inserted 0, updated 0, unchanged {ITEMS}") == 3
    assert rows(bench) == before
    pulls = [e for e in bench.audit if e["command"] == "pull"]
    assert [p["counts"]["detail_fetches"] for p in pulls] == [ITEMS, 0]  # type: ignore[index]


def test_run_leaves_one_audit_line_per_step_and_a_summary(bench: Bench) -> None:
    assert bench.run("run", "demo") == 0
    trail = bench.audit
    assert [(e["command"], e["outcome"]) for e in trail] == [
        ("pull", "ok"),
        ("normalize", "ok"),
        ("load", "ok"),
        ("load", "ok"),
        ("load", "ok"),
        ("run", "ok"),
    ]
    assert [e["sinks"] for e in trail if e["command"] == "load"] == [["db"], ["lines"], ["sheet"]]
    summary = trail[-1]
    assert summary["profile"] == "demo" and summary["sinks"] == ["db", "lines", "sheet"]
    assert summary["counts"] == {"records": ITEMS, "failed_items": 0}
    assert len({e["run_id"] for e in trail}) == 1
    pull = trail[0]
    assert pull["compliance_sha256"] is None, "a replay source needs no declaration"
    assert trail[1]["counts"] == {
        "records": ITEMS,
        "skipped": 0,
        "records_with_issues": len(mock_snapshot.WITH_ISSUES),
    }


def one_item_of_three_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_one(source_id: str) -> None:
        if source_id == MATERIALS[1].id:
            raise ItemFailed(source_id, "HTTP 404")

    source = Spy(make_scripted_mock(3), before_detail=fail_one)
    monkeypatch.setattr(
        cli_sources.registry, "source_factory", lambda plugin: lambda options, *, base_dir: source
    )


def test_run_where_more_items_fail_than_the_limit_loads_the_rest_and_exits_one(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    one_item_of_three_fails(monkeypatch)  # a third of the items: over the default 10%
    assert bench.run("run", "demo") == 1
    captured = capsys.readouterr()
    assert "failed detail" in captured.err and "HTTP 404" in captured.err
    assert "1 of 3 fetched item(s) failed (33.3%), over the limit of 10.0%" in captured.err
    assert {key[1] for key in rows(bench)} == {MATERIALS[0].id, MATERIALS[2].id}
    assert bench.audit[-1]["outcome"] == "failed"


def test_a_failed_item_is_named_once_wherever_a_failure_is_printed(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    one_item_of_three_fails(monkeypatch)
    assert bench.run("run", "demo") == 1
    done = capsys.readouterr()
    run_id = re.search(r"(?m)^run (\S+) ", done.out)
    assert run_id is not None
    assert bench.run("runs", "show", run_id[1]) == 0
    shown = capsys.readouterr().out
    victim = MATERIALS[1].id
    for text in (done.err, shown):
        assert f"failed detail {victim}: HTTP 404\n" in text, "the id once, and the reason kept"
        assert f"{victim}: {victim}" not in text


def test_run_where_few_enough_items_fail_exits_five_and_says_it_completed(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    one_item_of_three_fails(monkeypatch)
    assert bench.run("run", "demo", "--max-failed-ratio", "0.5") == EXIT_COMPLETED_WITH_FAILURES
    err = capsys.readouterr().err
    assert "within the limit of 50.0%" in err and "completed with item failures (exit 5)" in err
    assert {key[1] for key in rows(bench)} == {MATERIALS[0].id, MATERIALS[2].id}
    assert bench.audit[-1]["outcome"] == "ok_with_failures"


def test_the_limit_can_be_set_on_the_profile_and_the_option_overrides_it(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_item_of_three_fails(monkeypatch)
    text = bench.config.read_text(encoding="utf-8")
    bench.config.write_text(
        text.replace(
            'sinks = ["db", "lines", "sheet"]',
            'sinks = ["db", "lines", "sheet"]\nmax_failed_ratio = 0.5',
        ),
        encoding="utf-8",
    )
    assert bench.run("run", "demo") == EXIT_COMPLETED_WITH_FAILURES
    assert bench.run("run", "demo", "--max-failed-ratio", "0.1") == 1


def test_a_limit_of_zero_tolerates_no_failed_item_and_one_tolerates_all(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_item_of_three_fails(monkeypatch)
    assert bench.run("run", "demo", "--max-failed-ratio", "0") == 1
    assert bench.run("run", "demo", "--max-failed-ratio", "1") == EXIT_COMPLETED_WITH_FAILURES


@pytest.mark.parametrize("text", ["1.5", "-0.1", "nan", "ten percent"])
def test_a_limit_that_is_not_a_share_from_zero_to_one_is_refused(bench: Bench, text: str) -> None:
    with pytest.raises(SystemExit) as refused:
        bench.run("run", "demo", "--max-failed-ratio", text)
    assert refused.value.code == 2


def report_with(item_count: int, skipped: int, failed_ids: list[str]) -> PullReport:
    failures = tuple(FailureRow(source_id, Stage.DETAIL, "HTTP 500") for source_id in failed_ids)
    return PullReport(
        run_id="r1",
        source="demo",
        status=RunStatus.COMPLETE_WITH_FAILURES,
        item_count=item_count,
        stage_counts={
            Stage.LIST: {StageStatus.DONE: item_count},
            Stage.DETAIL: {
                StageStatus.SKIPPED: skipped,
                StageStatus.FAILED: len(failed_ids),
                StageStatus.DONE: item_count - skipped - len(failed_ids),
            },
            Stage.ASSETS: {},
        },
        failures=failures,
        is_truncated=False,
        detail_fetches=item_count - skipped,
        asset_fetches=0,
    )


def test_items_an_incremental_pull_skipped_are_not_counted_as_attempts() -> None:
    """Two of the two items that changed failed: a total outage, not 2% of a hundred items."""
    report = report_with(item_count=100, skipped=98, failed_ids=["a", "b"])
    assert cli_pipeline.failed_item_ratio(report) == (2, 2)
    assert cli_pipeline._exit_code_for_failures(report, limit=0.10) == 1


def test_an_item_that_failed_twice_counts_once() -> None:
    report = report_with(item_count=10, skipped=0, failed_ids=["a", "a"])
    assert cli_pipeline.failed_item_ratio(report) == (1, 10)
    assert cli_pipeline._exit_code_for_failures(report, limit=0.10) == EXIT_COMPLETED_WITH_FAILURES


def test_run_names_the_profiles_that_exist_when_asked_for_one_that_does_not(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bench.run("run", "nope") == 1
    assert "unknown profile 'nope' (defined: demo)" in capsys.readouterr().err


def test_run_needs_a_config_and_a_mapping(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--workspace", str(tmp_path / "ws"), "run", "demo"]) == 1
    assert "needs a config file" in capsys.readouterr().err
    config = tmp_path / "c.toml"
    config.write_text(
        '[sources.demo]\nplugin="replay"\npath="x"\n[sinks.s]\nkind="jsonl"\npath="o"\n'
        '[profiles.p]\nsource="demo"\nsinks=["s"]\n',
        encoding="utf-8",
    )
    assert cli.main(["--config", str(config), "--workspace", str(tmp_path / "ws"), "run", "p"]) == 1
    assert "has no 'mapping'" in capsys.readouterr().err


def test_a_broken_mapping_is_found_before_anything_is_pulled(
    bench: Bench, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        "version: 1\nsource: mock.materials\nfields: {colour: {from: list.c}}\n", "utf-8"
    )
    config = bench.config.read_text(encoding="utf-8").replace(str(DEFAULT_MAPPING), str(broken))
    bench.config.write_text(config, encoding="utf-8")
    assert bench.run("run", "demo") == 1
    assert "is not a field a mapping can fill" in capsys.readouterr().err
    assert not (bench.home / "state.sqlite").exists() or bench.audit == []


def test_the_retention_warning_counts_runs_older_than_the_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    long_ago = datetime(2026, 1, 1, tzinfo=UTC)
    with StateStore(tmp_path / "ws" / "state.sqlite", clock=lambda: long_ago) as store:
        store.create_run("old-run", SOURCE)
    context = cli.Context(
        None, Workspace(tmp_path / "ws"), clock=lambda: long_ago + timedelta(days=100)
    )
    declared = Compliance(
        data_owner="Example Co",
        authorization="own_account",
        authorization_ref="T-1",
        tos_url="https://vendor.example.invalid/terms",
        tos_reviewed_on=date(2026, 1, 1),
        max_requests_per_minute=60,
        retention_days=90,
    )
    cli_pipeline._warn_about_old_cache(context, SOURCE, declared)
    err = capsys.readouterr().err
    assert "1 run(s)" in err and "retention of 90 day(s)" in err and "old-run" in err

    cli_pipeline._warn_about_old_cache(context, SOURCE, None)
    cli_pipeline._warn_about_old_cache(context, "another.source", declared)
    assert capsys.readouterr().err == "", "no declaration, or other sources' runs: no warning"


def test_the_sqlite_sink_keeps_foreign_key_children_across_a_whole_run(bench: Bench) -> None:
    assert bench.run("run", "demo") == 0
    conn = sqlite3.connect(bench.db)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE notes (id INTEGER PRIMARY KEY, source TEXT, source_id TEXT, body TEXT, "
        "FOREIGN KEY (source, source_id) REFERENCES materials (source, source_id) "
        "ON DELETE CASCADE)"
    )
    conn.execute(
        "INSERT INTO notes (source, source_id, body) VALUES (?, ?, 'mine')",
        (SOURCE, mock_snapshot.CLEAN.id),
    )
    conn.commit()
    conn.close()
    bench.run("pull", "demo", "--full")
    assert bench.normalize() == 0
    assert bench.run("load", "--sink", "db", "--input", str(bench.dir / "records.jsonl")) == 0
    check = sqlite3.connect(bench.db)
    assert check.execute("SELECT body FROM notes").fetchall() == [("mine",)]
    check.close()


def test_loaded_pictures_carry_the_width_and_height_read_from_the_cache(bench: Bench) -> None:
    assert bench.run("run", "demo") == 0
    (record,) = [row for key, row in rows(bench).items() if key[1] == MATERIALS[0].id]
    pictures = record["images"]
    assert isinstance(pictures, list) and pictures
    for picture in pictures:
        assert (picture["width"], picture["height"]) == (8, 8), "the synthetic pictures are 8 x 8"
        assert "images:" not in " ".join(str(issue) for issue in record["issues"])  # type: ignore[union-attr]
