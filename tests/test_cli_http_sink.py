"""``run`` and ``load`` into the HTTP sink, through the real CLI, against the reference platform.

The source is the synthetic replay snapshot; everything after it is real: the mapping engine, the
sensitive-field policy, the HTTP sink, a platform on loopback that checks the contract and stores
into SQLite, and the audit log.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from catalog_sync import cli
from catalog_sync.mock.platform_server import PlatformServer
from catalog_sync.sinks.sqlite import read_table
from tests.conftest import MOCK_SNAPSHOT_DIR
from tests.fixtures import mock_snapshot

pytestmark = pytest.mark.mock

TOKEN = "cli-platform-canary-" + "77d2e1"
DEFAULT_MAPPING = mock_snapshot.MAPPING
SOURCE = mock_snapshot.SOURCE
ITEMS = mock_snapshot.ITEM_COUNT
BATCH = 5
REQUESTS = [BATCH] * (ITEMS // BATCH) + ([ITEMS % BATCH] if ITEMS % BATCH else [])
SUPPLIER_OF = {m.id: m.supplier_name for m in mock_snapshot.MATERIALS}  # synthetic names


@pytest.fixture
def platform(tmp_path: Path) -> Iterator[PlatformServer]:
    with PlatformServer(tmp_path / "platform" / "platform.db", token=TOKEN) as server:
        yield server


class Bench:
    def __init__(self, tmp_path: Path, platform: PlatformServer, mapping: Path = DEFAULT_MAPPING):
        self.dir = tmp_path
        self.platform = platform
        self.config = tmp_path / "catalog-sync.toml"
        self.home = tmp_path / "ws"
        self.config.write_text(
            textwrap.dedent(
                f"""
                [sources.demo]
                plugin = "replay"
                path = "{MOCK_SNAPSHOT_DIR}"

                [sinks.platform]
                kind = "http"
                url = "{platform.ingest_url}"
                auth_header_env = "PLATFORM_TOKEN"
                batch_size = {BATCH}

                [profiles.demo]
                source = "demo"
                mapping = "{mapping}"
                sinks = ["platform"]
                """
            ),
            encoding="utf-8",
        )

    def run(self, *args: str) -> int:
        return cli.main(["--config", str(self.config), "--workspace", str(self.home), *args])

    def stored(self) -> dict[tuple[str, str], dict[str, object]]:
        return read_table(self.platform.service.store.path, "material")

    @property
    def audit(self) -> list[dict[str, object]]:
        lines = (self.home / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]


@pytest.fixture
def bench(tmp_path: Path, platform: PlatformServer, monkeypatch: pytest.MonkeyPatch) -> Bench:
    monkeypatch.setenv("PLATFORM_TOKEN", TOKEN)
    return Bench(tmp_path, platform)


def test_run_delivers_every_record_in_batches_and_says_what_the_platform_did(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bench.run("run", "demo") == 0
    out = capsys.readouterr().out
    assert "load -> platform (http), written:" in out
    assert f"inserted {ITEMS}, updated 0, unchanged 0" in out
    assert "backup: none taken" in out and "receiving platform" in out
    assert len(bench.stored()) == ITEMS
    assert [e.records for e in bench.platform.service.events] == REQUESTS, "one per batch"

    load_line = [e for e in bench.audit if e["command"] == "load"][-1]
    assert load_line["counts"] == {"absent": 0, "inserted": ITEMS, "unchanged": 0, "updated": 0}
    assert load_line["sinks"] == ["platform"]
    assert TOKEN not in (bench.home / "audit.jsonl").read_text(encoding="utf-8")


def test_a_second_run_changes_nothing_at_the_platform(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bench.run("run", "demo") == 0
    first = bench.stored()
    capsys.readouterr()
    assert bench.run("run", "demo") == 0
    assert f"inserted 0, updated 0, unchanged {ITEMS}" in capsys.readouterr().out
    assert bench.stored() == first


def test_loading_the_same_normalised_file_twice_is_a_replay_the_platform_recognises(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    records = bench.dir / "records.jsonl"
    assert bench.run("pull", "demo") == 0
    assert (
        bench.run("normalize", SOURCE, "--mapping", str(DEFAULT_MAPPING), "--out", str(records))
        == 0
    )
    assert bench.run("load", "--sink", "platform", "--input", str(records)) == 0
    capsys.readouterr()
    assert bench.run("load", "--sink", "platform", "--input", str(records)) == 0
    out = capsys.readouterr().out
    assert f"inserted 0, updated 0, unchanged {ITEMS}" in out
    assert f"{len(REQUESTS)} request(s) were replays the platform had already applied" in out
    assert [e.kind for e in bench.platform.service.events][-len(REQUESTS) :] == ["replayed"] * len(
        REQUESTS
    )


def test_a_dry_run_sends_nothing_needs_no_token_and_says_how_much_would_go(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PLATFORM_TOKEN")
    records = bench.dir / "records.jsonl"
    assert bench.run("pull", "demo") == 0
    assert (
        bench.run("normalize", SOURCE, "--mapping", str(DEFAULT_MAPPING), "--out", str(records))
        == 0
    )
    capsys.readouterr()
    assert bench.run("load", "--sink", "platform", "--input", str(records), "--dry-run") == 0
    out = capsys.readouterr().out
    assert "dry run, nothing written" in out
    assert f"{ITEMS} record(s) in {len(REQUESTS)} request(s) would be POSTed to" in out
    assert "backup:" not in out
    assert bench.platform.service.events == [] and bench.stored() == {}
    assert [e for e in bench.audit if e["command"] == "load"][-1]["outcome"] == "dry_run"


def test_a_wrong_token_ends_the_load_with_exit_code_2_and_never_prints_it(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wrong = "not-the-platforms-" + "canary-0000"
    monkeypatch.setenv("PLATFORM_TOKEN", wrong)
    assert bench.run("run", "demo") == 2
    err = capsys.readouterr().err
    assert "PLATFORM_TOKEN holds a token the platform accepts" in err
    assert wrong not in err and TOKEN not in err
    assert bench.stored() == {}


def test_a_missing_token_variable_is_a_config_error_before_any_request(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PLATFORM_TOKEN")
    assert bench.run("run", "demo") == 1
    assert "environment variable PLATFORM_TOKEN is not set" in capsys.readouterr().err
    assert bench.platform.service.events == []


def test_a_sensitive_field_is_dropped_before_it_leaves_and_hashed_or_kept_on_request(
    tmp_path: Path, platform: PlatformServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLATFORM_TOKEN", TOKEN)
    mapping = tmp_path / "sensitive.yaml"
    mapping.write_text(mock_snapshot.sensitive_supplier_mapping(), encoding="utf-8")
    bench = Bench(tmp_path, platform, mapping)
    assert bench.run("run", "demo") == 0
    assert len(bench.stored()) == ITEMS
    assert all("supplier" not in row for row in bench.stored().values())
    sent = json.dumps([e.line() for e in platform.service.events])
    assert not [name for name in SUPPLIER_OF.values() if name in sent]

    text = bench.config.read_text(encoding="utf-8")
    bench.config.write_text(
        text.replace(f"batch_size = {BATCH}", 'sensitive_fields = "hash"'), "utf-8"
    )
    assert bench.run("run", "demo") == 0
    assert len(bench.stored()) == ITEMS
    for (_, source_id), row in bench.stored().items():
        hashed = row["supplier"]
        assert isinstance(hashed, str) and hashed.startswith("sha256:")
        assert SUPPLIER_OF[source_id] not in hashed

    bench.config.write_text(
        text.replace(f"batch_size = {BATCH}", 'sensitive_fields = "keep"'), "utf-8"
    )
    assert bench.run("run", "demo") == 0
    kept = {source_id: row["supplier"] for (_, source_id), row in bench.stored().items()}
    assert kept == SUPPLIER_OF
