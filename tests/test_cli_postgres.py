"""The whole demo pipeline into Postgres, through the real CLI (needs the throw-away server)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from catalog_sync import cli
from catalog_sync.sinks.postgres import read_table
from tests.conftest import MOCK_SNAPSHOT_DIR
from tests.fixtures import mock_snapshot

pytestmark = pytest.mark.postgres

DEFAULT_MAPPING = mock_snapshot.MAPPING
ITEMS = mock_snapshot.ITEM_COUNT
DSN_VARIABLE = "CATALOG_SYNC_TEST_CLI_DSN"


def write_config(tmp_path: Path, schema: str, table: str) -> Path:
    config = tmp_path / "catalog-sync.toml"
    config.write_text(
        textwrap.dedent(
            f"""
            [sources.demo]
            plugin = "replay"
            path = "{MOCK_SNAPSHOT_DIR}"

            [sinks.warehouse]
            kind = "postgres"
            dsn_env = "{DSN_VARIABLE}"
            schema = "{schema}"
            table = "{table}"

            [profiles.demo]
            source = "demo"
            mapping = "{DEFAULT_MAPPING}"
            sinks = ["warehouse"]
            """
        ),
        encoding="utf-8",
    )
    return config


def test_run_loads_the_demo_into_postgres_and_a_second_run_backs_up_first(
    tmp_path: Path,
    postgres_dsn: str,
    postgres_schema: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(DSN_VARIABLE, postgres_dsn)
    config = write_config(tmp_path, postgres_schema, "cli_demo")
    base = ["--config", str(config), "--workspace", str(tmp_path / "ws")]

    assert cli.main([*base, "run", "demo"]) == 0
    first = capsys.readouterr().out
    assert f"inserted {ITEMS}" in first and "nothing stored yet" in first
    rows = read_table(postgres_dsn, "cli_demo", postgres_schema)
    assert len(rows) == ITEMS
    sample = rows[(mock_snapshot.SOURCE, mock_snapshot.MATERIALS[0].id)]
    assert sample["images"], "pictures are stored as jsonb"
    assert str(sample["fetched_at"]).endswith("+00:00")

    assert cli.main([*base, "run", "demo"]) == 0
    second = capsys.readouterr().out
    assert f"unchanged {ITEMS}" in second
    assert "cli_demo__backup_" in second and f"({ITEMS} row(s) before)" in second

    audit = (tmp_path / "ws" / "audit.jsonl").read_text(encoding="utf-8")
    assert postgres_dsn not in audit and DSN_VARIABLE not in audit
    assert [json.loads(line)["command"] for line in audit.splitlines()].count("load") == 2


def test_a_dry_run_into_postgres_reports_and_writes_nothing(
    tmp_path: Path,
    postgres_dsn: str,
    postgres_schema: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(DSN_VARIABLE, postgres_dsn)
    config = write_config(tmp_path, postgres_schema, "cli_dry")
    base = ["--config", str(config), "--workspace", str(tmp_path / "ws")]
    records = tmp_path / "records.jsonl"
    assert cli.main([*base, "pull", "demo"]) == 0
    assert (
        cli.main(
            [
                *base,
                "normalize",
                mock_snapshot.SOURCE,
                "--mapping",
                str(DEFAULT_MAPPING),
                "--out",
                str(records),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        cli.main([*base, "load", "--sink", "warehouse", "--input", str(records), "--dry-run"]) == 0
    )
    assert f"inserted {ITEMS}" in capsys.readouterr().out
    assert read_table(postgres_dsn, "cli_dry", postgres_schema) == {}


def test_a_missing_connection_variable_is_a_config_error_with_exit_code_1(
    tmp_path: Path,
    postgres_schema: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(DSN_VARIABLE, raising=False)
    config = write_config(tmp_path, postgres_schema, "cli_missing")
    base = ["--config", str(config), "--workspace", str(tmp_path / "ws")]
    records = tmp_path / "records.jsonl"
    assert cli.main([*base, "pull", "demo"]) == 0
    assert (
        cli.main(
            [
                *base,
                "normalize",
                mock_snapshot.SOURCE,
                "--mapping",
                str(DEFAULT_MAPPING),
                "--out",
                str(records),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main([*base, "load", "--sink", "warehouse", "--input", str(records)]) == 1
    assert f"{DSN_VARIABLE} is not set" in capsys.readouterr().err
