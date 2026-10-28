"""A connector ships its own mapping, and that is the one used when none is named.

This is what lets the demo run from an installed wheel: there is no source tree to point
``--mapping`` or a profile's ``mapping`` at, but the mapping is in the package, next to the
connector that declares it (``ConnectorInfo.mapping``).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from catalog_sync import cli, registry
from catalog_sync.normalize.mapping import load_mapping
from tests.conftest import MOCK_SNAPSHOT_DIR
from tests.fixtures import mock_snapshot

SOURCE = mock_snapshot.SOURCE


class Tool:
    """A workspace and a config with a replay of the Mock snapshot, and a Mock connector entry."""

    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        self.home = tmp_path / "ws"
        self.config = tmp_path / "catalog-sync.toml"
        self.config.write_text(
            textwrap.dedent(
                f"""
                [sources.demo]
                plugin = "replay"
                path = "{MOCK_SNAPSHOT_DIR}"

                [sources.shop]
                plugin = "mock.materials"
                """
            ),
            encoding="utf-8",
        )
        assert self.run("pull", "demo") == 0

    def run(self, *args: str) -> int:
        return cli.main(["--config", str(self.config), "--workspace", str(self.home), *args])

    def normalize(self, source: str, name: str, *extra: str) -> list[dict[str, object]]:
        out = self.dir / name
        assert self.run("normalize", source, "--out", str(out), *extra) == 0
        return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def tool(tmp_path: Path) -> Tool:
    return Tool(tmp_path)


def without_clock(records: list[dict[str, object]]) -> list[dict[str, object]]:
    return [{k: v for k, v in record.items() if k != "normalized_at"} for record in records]


# --- what a connector ships -----------------------------------------------------------------------


@pytest.mark.parametrize("plugin", ["mock.materials", "mock.suppliers"])
def test_a_connector_ships_a_mapping_for_its_own_source(plugin: str) -> None:
    path = registry.default_mapping(plugin)
    assert path is not None and path.is_file()
    assert load_mapping(path).source == plugin


@pytest.mark.parametrize("plugin", ["replay", "no.such.plugin"])
def test_a_plugin_without_connector_info_ships_none(plugin: str) -> None:
    assert registry.default_mapping(plugin) is None


# --- normalize ------------------------------------------------------------------------------------


def test_normalize_without_a_mapping_gives_what_the_packaged_mapping_gives(tool: Tool) -> None:
    default = tool.normalize(SOURCE, "default.jsonl")
    explicit = tool.normalize(SOURCE, "explicit.jsonl", "--mapping", str(mock_snapshot.MAPPING))
    assert len(default) == mock_snapshot.ITEM_COUNT
    assert without_clock(default) == without_clock(explicit)
    digests = [
        json.loads((tool.dir / f"{name}.jsonl.meta.json").read_text(encoding="utf-8"))
        for name in ("default", "explicit")
    ]
    assert digests[0]["mapping_sha256"] == digests[1]["mapping_sha256"]


def test_a_config_entry_that_runs_a_connector_finds_that_connectors_mapping(tool: Tool) -> None:
    """`shop` is a [sources.*] entry of the plugin mock.materials, which the runs are recorded
    under: both the run and the mapping are found through the plugin name."""
    records = tool.normalize("shop", "shop.jsonl")
    assert {record["source"] for record in records} == {SOURCE}


def test_a_mapping_that_is_named_wins_over_the_packaged_one(tool: Tool) -> None:
    changed = tool.dir / "changed.yaml"
    changed.write_text(
        mock_snapshot.MAPPING.read_text(encoding="utf-8").replace(
            "zero_is_missing: true", "zero_is_missing: false"
        ),
        encoding="utf-8",
    )

    def zero_weights(records: list[dict[str, object]]) -> int:
        return sum(1 for record in records if record["weight_gsm"] == 0.0)

    assert zero_weights(tool.normalize(SOURCE, "a.jsonl")) == 0
    assert zero_weights(tool.normalize(SOURCE, "b.jsonl", "--mapping", str(changed))) == 2


@pytest.mark.parametrize("source", ["demo", "replay", "no.such.plugin"])
def test_normalize_without_a_mapping_for_a_source_that_ships_none_says_what_to_do(
    tool: Tool, source: str, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert tool.run("normalize", source, "--out", str(tool.dir / "x.jsonl")) == 1
    err = capsys.readouterr().err
    assert "pass --mapping FILE" in err and "Traceback" not in err
    assert not (tool.dir / "x.jsonl").exists()


# --- run ------------------------------------------------------------------------------------------


def test_run_without_a_mapping_for_a_source_that_ships_none_names_the_profile_and_the_fix(
    tool: Tool, capsys: pytest.CaptureFixture[str]
) -> None:
    tool.config.write_text(
        tool.config.read_text(encoding="utf-8")
        + '\n[sinks.out]\nkind = "jsonl"\npath = "{workspace}/out.jsonl"\n'
        + '[profiles.offline]\nsource = "demo"\nsinks = ["out"]\n',
        encoding="utf-8",
    )
    capsys.readouterr()
    assert tool.run("run", "offline") == 1
    err = capsys.readouterr().err
    assert "profile 'offline' has no 'mapping'" in err
    assert "set mapping =" in err
    assert not (tool.home / "out.jsonl").exists(), "nothing was pulled or loaded"
