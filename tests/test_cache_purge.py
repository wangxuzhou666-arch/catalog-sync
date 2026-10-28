"""`runs list|show` and `cache purge`: retention is enforced, and nothing still needed is deleted.

Runs are made by the real CLI over the synthetic replay snapshot. Their age is set by rewriting
``runs.started_at`` in the state database, since the CLI has no clock of its own to turn.
"""

from __future__ import annotations

import sqlite3
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from catalog_sync import cli, cli_pipeline
from catalog_sync.audit import AuditLog
from catalog_sync.cache import parse_age_days, plan_purge, purge_runs, snapshot_size
from catalog_sync.compliance import parse_compliance
from catalog_sync.config import Workspace as PathsOnly
from catalog_sync.errors import CatalogSyncError, ComplianceError, ConfigError, SnapshotError
from catalog_sync.puller import Puller
from catalog_sync.snapshot import RunSnapshot
from catalog_sync.sources.replay import ReplaySource
from catalog_sync.state import RunStatus, StageStatus, StateStore
from tests.conftest import MOCK_SNAPSHOT_DIR
from tests.fixtures import compliance, mock_snapshot

SOURCE = mock_snapshot.SOURCE
ITEM_COUNT = mock_snapshot.ITEM_COUNT


class Workspace:
    """A workspace, a config with one replay source and one source with declared retention."""

    def __init__(self, tmp_path: Path, *, retention_days: int | None = 30) -> None:
        self.home = tmp_path / "ws"
        self.config = tmp_path / "catalog-sync.toml"
        text = f'[sources.demo]\nplugin = "replay"\npath = "{MOCK_SNAPSHOT_DIR}"\n'
        if retention_days is not None:
            text += '[sources.mock-materials]\nplugin = "mock.materials"\n'
            text += textwrap.dedent(compliance.block("mock-materials")).replace(
                "retention_days = 90", f"retention_days = {retention_days}"
            )
        self.config.write_text(text, encoding="utf-8")

    def run(self, *args: str) -> int:
        return cli.main(["--config", str(self.config), "--workspace", str(self.home), *args])

    def pull(self, capsys: pytest.CaptureFixture[str], *extra: str) -> str:
        assert self.run("pull", "demo", *extra) == 0
        return capsys.readouterr().out.splitlines()[0].split()[1]

    def age(self, run_id: str, days: float) -> None:
        started = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        conn = sqlite3.connect(self.home / "state.sqlite")
        try:
            conn.execute("UPDATE runs SET started_at = ? WHERE run_id = ?", (started, run_id))
            conn.commit()
        finally:
            conn.close()

    def snapshot(self, run_id: str) -> Path:
        return self.home / "cache" / "runs" / run_id

    def run_ids(self) -> set[str]:
        with StateStore(self.home / "state.sqlite") as store:
            return {run.run_id for run in store.list_runs()}

    @property
    def audit(self) -> list[dict[str, object]]:
        return AuditLog(self.home / "audit.jsonl").read()


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


# --- age ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "days"), [("30d", 30), ("1d", 1), (" 7d ", 7)])
def test_an_age_is_whole_days(text: str, days: int) -> None:
    assert parse_age_days(text) == days


@pytest.mark.parametrize("text", ["30", "0d", "-3d", "d", "1.5d", "2w", "36h", "thirty days", ""])
def test_anything_else_is_not_an_age(text: str) -> None:
    with pytest.raises(ConfigError, match="number of days"):
        parse_age_days(text)


# --- what purge deletes ---------------------------------------------------------------------------


def test_old_runs_nothing_depends_on_are_deleted_with_their_snapshot_and_their_rows(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    old = ws.pull(capsys)
    ws.pull(capsys, "--full")  # moves every item into a newer run, so `old` is no longer needed
    new = ws.pull(capsys, "--full")
    ws.age(old, 100)
    assert ws.snapshot(old).is_dir()

    assert ws.run("cache", "purge") == 0
    out = capsys.readouterr().out
    assert "purged 1 run(s)" in out and old in out

    assert not ws.snapshot(old).exists()
    assert old not in ws.run_ids() and new in ws.run_ids()
    assert (ws.snapshot(new) / "run.json").is_file(), "a run inside its retention is untouched"


def test_a_run_that_unchanged_items_depend_on_is_kept_and_the_output_says_why(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    first = ws.pull(capsys)
    second = ws.pull(capsys)  # an incremental pull: every item unchanged, so nothing new is stored
    ws.age(first, 100)

    assert ws.run("cache", "purge") == 0
    out = capsys.readouterr().out
    assert "no run may be purged" in out
    assert "kept 1 run(s) past their retention that items depend on" in out
    assert f"{ITEM_COUNT} item(s)" in out and "--full" in out

    assert ws.snapshot(first).is_dir() and {first, second} <= ws.run_ids()
    assert ws.run("normalize", SOURCE, "--mapping", str(_mapping()), "--out", str(_out(ws))) == 0, (
        "the kept run is what lets `normalize --run latest` see the whole library"
    )
    capsys.readouterr()

    assert ws.run("pull", "demo", "--full") == 0  # now the items live in a newer run...
    capsys.readouterr()
    assert ws.run("cache", "purge") == 0  # ...and the old one can go
    assert "purged 1 run(s)" in capsys.readouterr().out
    assert not ws.snapshot(first).exists()


def _mapping() -> Path:
    return mock_snapshot.MAPPING


def _out(ws: Workspace) -> Path:
    return ws.home.parent / "records.jsonl"


def test_a_dry_run_deletes_nothing_and_says_what_would_go(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    old = ws.pull(capsys)
    ws.pull(capsys, "--full")
    ws.age(old, 100)

    assert ws.run("cache", "purge", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "would purge 1 run(s)" in out and old in out
    assert ws.snapshot(old).is_dir() and old in ws.run_ids()
    assert ws.audit[-1]["outcome"] == "dry_run"
    assert ws.audit[-1]["counts"]["runs_purged"] == 1  # type: ignore[index]


def test_older_than_replaces_the_declared_retention(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    middle = ws.pull(capsys)
    ws.pull(capsys, "--full")
    ws.age(middle, 20)  # inside the declared 30 days
    assert ws.run("cache", "purge") == 0
    assert "nothing is past its retention" in capsys.readouterr().out
    assert ws.snapshot(middle).is_dir()

    assert ws.run("cache", "purge", "--older-than", "10d") == 0
    assert middle in capsys.readouterr().out
    assert not ws.snapshot(middle).exists()


def test_the_declared_retention_is_per_source_and_a_source_without_one_is_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = Workspace(tmp_path, retention_days=None)  # no source declares anything
    old = ws.pull(capsys)
    ws.pull(capsys, "--full")
    ws.age(old, 400)
    assert ws.run("cache", "purge") == 0
    out = capsys.readouterr().out
    assert "no retention is declared" in out and "--older-than" in out and SOURCE in out
    assert ws.snapshot(old).is_dir()

    assert ws.run("cache", "purge", "--older-than", "30d") == 0
    assert not ws.snapshot(old).exists()


def test_the_shorter_declaration_wins_when_two_entries_run_one_plugin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = Workspace(tmp_path, retention_days=60)
    ws.config.write_text(
        ws.config.read_text(encoding="utf-8")
        + '[sources.other]\nplugin = "mock.materials"\n'
        + textwrap.dedent(compliance.block("other")).replace(
            "retention_days = 90", "retention_days = 10"
        ),
        encoding="utf-8",
    )
    middle = ws.pull(capsys)
    ws.pull(capsys, "--full")
    ws.age(middle, 20)  # inside 60 days, outside 10
    assert ws.run("cache", "purge") == 0
    assert not ws.snapshot(middle).exists()


def test_an_invalid_declaration_stops_the_default_purge_with_exit_four(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = Workspace(tmp_path, retention_days=60)
    old = ws.pull(capsys)
    ws.pull(capsys, "--full")
    ws.age(old, 400)
    text = ws.config.read_text(encoding="utf-8").replace(
        "retention_days = 60", "retention_days = 0"
    )
    ws.config.write_text(text, encoding="utf-8")
    assert ws.run("cache", "purge") == ComplianceError.exit_code
    assert ws.snapshot(old).is_dir(), "nothing is deleted on a guess"
    assert ws.run("cache", "purge", "--older-than", "30d") == 0, (
        "an explicit age needs no declaration"
    )


def test_a_run_marked_running_is_kept_unless_asked(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    killed = ws.pull(capsys)
    ws.pull(capsys, "--full")
    ws.age(killed, 100)
    conn = sqlite3.connect(ws.home / "state.sqlite")
    conn.execute("UPDATE runs SET status = ? WHERE run_id = ?", (RunStatus.RUNNING, killed))
    conn.commit()
    conn.close()

    assert ws.run("cache", "purge") == 0
    out = capsys.readouterr().out
    assert "marked running" in out and "--include-running" in out
    assert ws.snapshot(killed).is_dir()

    assert ws.run("cache", "purge", "--include-running") == 0
    assert not ws.snapshot(killed).exists()


def test_only_the_named_source_is_touched(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    old = ws.pull(capsys)
    ws.pull(capsys, "--full")
    ws.age(old, 100)
    assert ws.run("cache", "purge", "--source", "some.other.source") == 0
    assert "nothing is past its retention" in capsys.readouterr().out
    assert ws.snapshot(old).is_dir()
    assert ws.run("cache", "purge", "--source", "mock-materials") == 0, "a config name works too"
    assert not ws.snapshot(old).exists()


def test_a_snapshot_that_is_already_gone_does_not_stop_the_rows_from_being_cleaned(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil

    old = ws.pull(capsys)
    ws.pull(capsys, "--full")
    ws.age(old, 100)
    shutil.rmtree(ws.snapshot(old))
    assert ws.run("cache", "purge") == 0
    out = capsys.readouterr().out
    assert "already gone" in out
    assert old not in ws.run_ids()


def test_purge_is_audited_without_names_or_content(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    old = ws.pull(capsys)
    ws.pull(capsys, "--full")
    ws.age(old, 100)
    ws.run("cache", "purge")
    entry = ws.audit[-1]
    assert entry["command"] == "cache_purge" and entry["outcome"] == "ok"
    counts = entry["counts"]
    assert isinstance(counts, dict)
    assert counts["runs_purged"] == 1 and counts["runs_kept_in_use"] == 0
    assert counts["bytes"] > 0
    last_line = (ws.home / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    assert old not in last_line, "the line says how many runs, not which"


def test_a_purge_that_fails_part_way_is_audited_and_can_be_finished_later(
    ws: Workspace, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import catalog_sync.cache as cache

    old = ws.pull(capsys)
    ws.pull(capsys, "--full")
    ws.age(old, 100)

    def refuse(_directory: Path) -> None:
        raise SnapshotError("cannot delete: permission denied")

    monkeypatch.setattr(cache, "_delete_directory", refuse)
    assert ws.run("cache", "purge") == 1
    assert ws.audit[-1]["outcome"] == "error:SnapshotError"
    assert old in ws.run_ids(), "the rows stay while the directory is there"
    monkeypatch.undo()
    assert ws.run("cache", "purge") == 0
    assert old not in ws.run_ids()


# --- the safeguards, below the CLI ----------------------------------------------------------------


def test_the_state_store_itself_refuses_to_forget_a_run_items_depend_on(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        store.create_run("r1", SOURCE)
        store.checkpoint("r1").add_item(
            "a", "v1", detail=StageStatus.PENDING, assets=StageStatus.PENDING
        )
        store.record_version(SOURCE, "a", "v1", "r1")
        with pytest.raises(CatalogSyncError, match=r"1 item.*still depend"):
            store.delete_run("r1")
        assert store.get_run("r1").run_id == "r1" and store.checkpoint("r1").count_items() == 1
        store.record_version(SOURCE, "a", "v1", "r2")  # the item moved on to another run
        store.delete_run("r1")
        assert store.list_runs() == []
        with pytest.raises(CatalogSyncError, match="no such run"):
            store.delete_run("r1")


def test_a_run_id_that_would_leave_the_runs_directory_is_never_deleted(tmp_path: Path) -> None:
    runs_root = tmp_path / "cache" / "runs"
    victim = tmp_path / "cache" / "precious"
    victim.mkdir(parents=True)
    (victim / "keep.txt").write_text("data", encoding="utf-8")
    with StateStore(tmp_path / "state.sqlite") as store:
        store.create_run("../precious", SOURCE)
        (run,) = store.list_runs()
        with pytest.raises(SnapshotError, match="invalid run id"):
            purge_runs(store, runs_root, [run])
        with pytest.raises(SnapshotError, match="invalid run id"):
            snapshot_size(runs_root, "../precious")
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "data"


def test_a_symbolic_link_in_place_of_a_run_directory_is_not_followed(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_text("data", encoding="utf-8")
    runs_root.mkdir()
    (runs_root / "r1").symlink_to(target)
    with StateStore(tmp_path / "state.sqlite") as store:
        store.create_run("r1", SOURCE)
        (run,) = store.list_runs()
        with pytest.raises(SnapshotError, match="symbolic link"):
            purge_runs(store, runs_root, [run])
    assert (target / "keep.txt").exists()


def test_the_plan_sorts_dependent_running_and_free_runs(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        for run_id in ("free", "needed", "running"):
            store.create_run(run_id, SOURCE)
        store.finish_run("free", RunStatus.COMPLETE)
        store.finish_run("needed", RunStatus.COMPLETE)
        store.record_version(SOURCE, "a", "v1", "needed")
        plan = plan_purge(store, store.list_runs())
        assert [r.run_id for r in plan.purgeable] == ["free"]
        assert [(u.run.run_id, u.dependents) for u in plan.still_needed] == [("needed", 1)]
        assert [r.run_id for r in plan.still_running] == ["running"]
        everything = plan_purge(store, store.list_runs(), include_running=True)
        assert {r.run_id for r in everything.purgeable} == {"free", "running"}


# --- runs list / show -----------------------------------------------------------------------------


def test_runs_list_shows_failures_and_how_many_items_still_use_each_run(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    first = ws.pull(capsys)
    second = ws.pull(capsys)
    assert ws.run("runs", "list") == 0
    out = capsys.readouterr().out
    header, *lines = out.splitlines()
    assert "USED BY" in header and "FAILED" in header
    by_run = {line.split()[0]: line.split() for line in lines if line.startswith(("2",))}
    assert by_run[first][-2] == str(ITEM_COUNT), "the first run still holds every item's data"
    assert by_run[second][-2] == "0"


def test_runs_list_can_be_limited_to_one_source(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = ws.pull(capsys)
    assert ws.run("runs", "list", "--source", "mock-materials") == 0
    assert run_id in capsys.readouterr().out, "a config name selects the plugin's runs"
    assert ws.run("runs", "list", "--source", "another.source") == 0
    assert "no runs of another.source" in capsys.readouterr().out


def test_runs_show_reports_the_snapshot_and_who_uses_it(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = ws.pull(capsys)
    assert ws.run("runs", "show", run_id) == 0
    out = capsys.readouterr().out
    assert f"used by:  {ITEM_COUNT} item(s)" in out
    assert "cache:    " in out and "file(s)" in out and "kB" in out
    ws.age(run_id, 100)
    ws.pull(capsys, "--full")
    assert ws.run("cache", "purge") == 0
    capsys.readouterr()
    assert ws.run("runs", "show", run_id) == 1, "a purged run is no longer known at all"
    assert "no such run" in capsys.readouterr().err


# --- the warning that points at purge -------------------------------------------------------------


def test_the_stale_cache_warning_points_at_cache_purge(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    old = ws.pull(capsys)
    ws.pull(capsys, "--full")
    ws.age(old, 100)
    declared = parse_compliance(
        {
            "data_owner": "Example Co",
            "authorization": "own_account",
            "authorization_ref": "T-1",
            "tos_url": "https://vendor.example.invalid/terms",
            "tos_reviewed_on": "2026-10-01",
            "max_requests_per_minute": 60,
            "retention_days": 30,
            "tos_review_max_age_days": 36500,
        },
        label="x",
        today=datetime.now(UTC).date(),
    )
    cli_pipeline._warn_about_old_cache(cli.Context(None, PathsOnly(ws.home)), SOURCE, declared)
    err = capsys.readouterr().err
    assert "1 run(s)" in err and "cache purge --dry-run" in err and old in err


def test_a_run_that_gained_a_dependent_is_refused_before_its_directory_is_touched(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    (runs_root / "r1").mkdir(parents=True)
    (runs_root / "r1" / "run.json").write_text("{}", encoding="utf-8")
    with StateStore(tmp_path / "state.sqlite") as store:
        store.create_run("r1", SOURCE)
        store.finish_run("r1", RunStatus.COMPLETE)
        (run,) = store.list_runs()
        store.record_version(SOURCE, "a", "v1", "r1")  # an item came to depend on it after planning
        with pytest.raises(CatalogSyncError, match="still depend"):
            purge_runs(store, runs_root, [run])
    assert (runs_root / "r1" / "run.json").exists(), "a refusal must leave the data in place"


# --- purge versus a concurrent pull ---------------------------------------------------------------


def two_full_pulls(db: Path, runs_root: Path) -> tuple[str, str]:
    """Two runs of the replay source; the second holds every item, freeing the first."""
    with StateStore(db) as store:
        puller = Puller(ReplaySource(RunSnapshot.open(MOCK_SNAPSHOT_DIR)), store, runs_root)
        first = puller.pull().run_id
        second = puller.pull(incremental=False).run_id
    return first, second


def test_a_pull_cannot_resume_a_run_while_a_purge_is_deleting_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two connections to one sqlite file, as two processes would have: the purge is frozen in the
    middle of removing the directory while a `pull --resume` of the same run arrives."""
    import catalog_sync.cache as cache

    db, runs_root = tmp_path / "state.sqlite", tmp_path / "runs"
    victim, _ = two_full_pulls(db, runs_root)
    with StateStore(db) as store:
        planned = store.get_run(victim)
    deleting, proceed = threading.Event(), threading.Event()
    real_delete = cache._delete_directory

    def frozen_delete(directory: Path) -> None:
        deleting.set()
        assert proceed.wait(10)
        real_delete(directory)

    monkeypatch.setattr(cache, "_delete_directory", frozen_delete)

    def purge() -> None:
        with StateStore(db) as purger_store:  # a connection belongs to the thread that made it
            purge_runs(purger_store, runs_root, [planned])

    with ThreadPoolExecutor(max_workers=1) as pool:
        purging = pool.submit(purge)
        try:
            assert deleting.wait(10), "the purge never reached the directory delete"
            with StateStore(db) as store:
                assert store.get_run(victim).status is RunStatus.PURGING
                resumer = Puller(
                    ReplaySource(RunSnapshot.open(MOCK_SNAPSHOT_DIR)), store, runs_root
                )
                with pytest.raises(ConfigError, match="being deleted by a cache purge"):
                    resumer.pull(resume=victim)
                with pytest.raises(CatalogSyncError, match="being deleted by a cache purge"):
                    store.reopen_run(victim)  # the atomic guard, for a resume past the early check
        finally:
            proceed.set()
        purging.result(timeout=10)  # re-raises anything the purge thread died of
    assert not (runs_root / victim).exists()
    with StateStore(db) as store:
        assert victim not in {run.run_id for run in store.list_runs()}


def test_a_run_a_pull_reopened_after_the_plan_is_not_purged_under_it(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    (runs_root / "r1").mkdir(parents=True)
    (runs_root / "r1" / "run.json").write_text("{}", encoding="utf-8")
    with StateStore(tmp_path / "state.sqlite") as store:
        store.create_run("r1", SOURCE)
        store.finish_run("r1", RunStatus.COMPLETE)
        (planned,) = store.list_runs()
        store.reopen_run("r1")  # `pull --resume r1` started after the plan was made
        with pytest.raises(CatalogSyncError, match=r"now running, not complete"):
            purge_runs(store, runs_root, [planned])
        assert store.get_run("r1").status is RunStatus.RUNNING
    assert (runs_root / "r1" / "run.json").exists(), "a refusal must leave the data in place"


def test_a_purge_that_died_after_claiming_the_run_is_finished_by_the_next_one(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    (runs_root / "r1").mkdir(parents=True)
    (runs_root / "r1" / "run.json").write_text("{}", encoding="utf-8")
    with StateStore(tmp_path / "state.sqlite") as store:
        store.create_run("r1", SOURCE)
        store.finish_run("r1", RunStatus.COMPLETE)
        store.mark_purging("r1", was=RunStatus.COMPLETE)  # ...and the process died here
        (leftover,) = store.list_runs()
        assert leftover.status is RunStatus.PURGING
        assert [r.run_id for r in plan_purge(store, [leftover]).purgeable] == ["r1"]
        purge_runs(store, runs_root, [leftover])
        assert store.list_runs() == []
    assert not (runs_root / "r1").exists()
