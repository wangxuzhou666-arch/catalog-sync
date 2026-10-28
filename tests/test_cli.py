from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from catalog_sync import cli
from catalog_sync.errors import EXIT_COMPLETED_WITH_FAILURES, AuthExpired, ItemFailed
from catalog_sync.sources.base import Source
from tests.conftest import MOCK_SNAPSHOT_DIR
from tests.doubles import ScriptedSource, Spy, make_scripted_mock
from tests.fixtures import compliance, mock_snapshot

ITEMS = mock_snapshot.ITEM_COUNT


def run_cli(workspace: Path, *args: str, config: Path | None = None) -> int:
    prefix = ["--config", str(config)] if config else []
    return cli.main([*prefix, "--workspace", str(workspace), *args])


def pull_replay(workspace: Path) -> int:
    return run_cli(workspace, "pull", "replay", "--option", f"path={MOCK_SNAPSHOT_DIR}")


def run_id_from(capsys: pytest.CaptureFixture[str]) -> str:
    first_line = capsys.readouterr().out.splitlines()[0]
    return first_line.split()[1]


def test_pull_help_works() -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["pull", "replay", "--help"])
    assert exit_info.value.code == 0


def test_a_replay_pull_produces_a_run_that_runs_list_shows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert pull_replay(tmp_path) == 0
    out = capsys.readouterr().out
    assert "complete" in out
    assert f"items: {ITEMS}" in out
    assert f"{ITEMS} done" in out
    run_id = out.splitlines()[0].split()[1]

    assert run_cli(tmp_path, "runs", "list") == 0
    listing = capsys.readouterr().out
    assert run_id in listing
    assert mock_snapshot.SOURCE in listing

    assert run_cli(tmp_path, "runs", "show", run_id) == 0
    shown = capsys.readouterr().out
    assert f"run:      {run_id}" in shown
    assert "listing:  complete" in shown
    assert f"items:    {ITEMS}" in shown


def test_runs_list_on_a_fresh_workspace_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(tmp_path, "runs", "list") == 0
    assert "no runs yet" in capsys.readouterr().out


def test_a_second_pull_reports_that_nothing_was_fetched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pull_replay(tmp_path)
    capsys.readouterr()
    pull_replay(tmp_path)
    assert "fetched this time: 0 detail, 0 assets" in capsys.readouterr().out


def test_pull_resumes_a_named_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_cli(tmp_path, "pull", "replay", "--option", f"path={MOCK_SNAPSHOT_DIR}", "--stage", "list")
    run_id = run_id_from(capsys)
    code = run_cli(
        tmp_path,
        "pull",
        "replay",
        "--option",
        f"path={MOCK_SNAPSHOT_DIR}",
        "--stage",
        "detail",
        "--resume",
        run_id,
    )
    assert code == 0
    assert f"detail : {ITEMS} done" in capsys.readouterr().out


def test_a_config_file_names_the_source_and_its_relative_paths_start_at_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copytree(MOCK_SNAPSHOT_DIR, tmp_path / "snapshot")
    config = tmp_path / "conf" / "catalog-sync.toml"
    config.parent.mkdir()
    config.write_text('[sources.demo]\nplugin = "replay"\npath = "../snapshot"\n', encoding="utf-8")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    argv = ["--config", str(config), "--workspace", str(tmp_path / "ws"), "pull", "demo"]
    monkeypatch.chdir(elsewhere)  # proves the path is not resolved against the working directory
    assert cli.main(argv) == 0
    assert "complete" in capsys.readouterr().out


def test_unknown_source_is_a_clear_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(tmp_path, "pull", "nosuch") == 1
    assert "unknown source 'nosuch'" in capsys.readouterr().err


def test_unknown_run_is_a_clear_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "runs", "show", "ghost") == 1
    assert "no such run: ghost" in capsys.readouterr().err


def test_a_new_run_cannot_skip_the_list_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_cli(
        tmp_path, "pull", "replay", "--option", f"path={MOCK_SNAPSHOT_DIR}", "--stage", "detail"
    )
    assert code == 1
    assert "include the list stage" in capsys.readouterr().err


def test_a_malformed_option_is_rejected_by_the_parser(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run_cli(tmp_path, "pull", "replay", "--option", "nokeyvalue")
    assert exit_info.value.code == 2


def stub_source(monkeypatch: pytest.MonkeyPatch, source: Source) -> None:
    monkeypatch.setattr(cli, "_build_source", lambda name, overrides, context: source)


def pull_stub(tmp_path: Path) -> int:
    return run_cli(tmp_path, "pull", "x", config=compliance.write_stub_config(tmp_path))


def test_items_that_failed_make_the_exit_code_five(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_one(source_id: str) -> None:
        if source_id == mock_snapshot.MATERIALS[1].id:
            raise ItemFailed(source_id, "HTTP 404")

    stub_source(monkeypatch, Spy(make_scripted_mock(3), before_detail=fail_one))
    assert pull_stub(tmp_path) == EXIT_COMPLETED_WITH_FAILURES == 5
    captured = capsys.readouterr()
    assert "complete with failures" in captured.out
    assert "HTTP 404" in captured.err


def test_an_expired_credential_exits_with_code_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def expire(source_id: str) -> None:
        raise AuthExpired("the token was rejected; run 'catalog-sync login' again")

    stub_source(monkeypatch, Spy(make_scripted_mock(3), before_detail=expire))
    assert pull_stub(tmp_path) == 2
    assert "run 'catalog-sync login' again" in capsys.readouterr().err


def test_a_tripped_fuse_exits_with_code_three_and_names_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def always_fail(source_id: str) -> None:
        raise ItemFailed(source_id, "HTTP 500")

    source: ScriptedSource = make_scripted_mock(10)
    stub_source(monkeypatch, Spy(source, before_detail=always_fail))
    assert pull_stub(tmp_path) == 3
    err = capsys.readouterr().err
    assert "5 consecutive detail failures" in err
    assert "--resume" in err
