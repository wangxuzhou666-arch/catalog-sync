"""A file sink's ``path`` may start with ``{workspace}/``: the output then lands in the workspace.

Without it the only choices were a path relative to the config file (which is usually the checkout,
or a folder a cloud service syncs) and an absolute path nobody can share a config with. The rule is
applied once, where a sink is built for a load, so sqlite, jsonl and csv all get it and a sink
written by someone else gets it without knowing.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest

from catalog_sync import cli
from catalog_sync.config import HOME_ENV_VAR, Workspace, expand_sink_paths, resolve_workspace
from catalog_sync.errors import ConfigError
from tests.conftest import MOCK_SNAPSHOT_DIR, REPO_ROOT
from tests.fixtures import mock_snapshot

DEFAULT_MAPPING = mock_snapshot.MAPPING
HOME = Path("/workspace-home")  # never touched: expand_sink_paths only joins


# --- the rule, alone ---------------------------------------------------------------------------


def test_the_prefix_is_replaced_by_the_workspace_home() -> None:
    options = {"path": "{workspace}/out/catalog.db", "table": "materials"}
    assert expand_sink_paths(options, Workspace(HOME)) == {
        "path": str(HOME / "out" / "catalog.db"),
        "table": "materials",
    }


@pytest.mark.parametrize("path", ["./out/x.db", "out/x.db", "/abs/x.db", "~/x.db", "../x.db"])
def test_every_other_path_is_left_for_the_sink_to_resolve_against_the_config(path: str) -> None:
    options = {"path": path}
    assert expand_sink_paths(options, Workspace(HOME)) is options


def test_options_without_a_text_path_are_left_alone() -> None:
    """A sink that has no path (http, postgres) or a wrong-typed one reports it itself."""
    for options in ({"url": "https://x.example/api"}, {"path": 3}):
        assert expand_sink_paths(options, Workspace(HOME)) is options


@pytest.mark.parametrize("path", ["{workspace}", "{workspace}/", "{workspace}x/y.db"])
def test_a_placeholder_that_is_not_followed_by_a_file_is_refused_not_made_a_folder(
    path: str,
) -> None:
    """Taken literally, "{workspace}" would be a folder of that name next to the config: the
    checkout, which is the place the placeholder exists to keep output out of."""
    with pytest.raises(ConfigError, match=r"\{workspace\}/<file>"):
        expand_sink_paths({"path": path}, Workspace(HOME))


# --- through the real command line, for every file sink ----------------------------------------


def write_config(directory: Path, body: str = "") -> Path:
    config = directory / "config" / "catalog-sync.toml"
    config.parent.mkdir()
    config.write_text(
        textwrap.dedent(
            f"""
            [sources.demo]
            plugin = "replay"
            path = "{MOCK_SNAPSHOT_DIR}"

            [sinks.db]
            kind = "sqlite"
            path = "{{workspace}}/out/catalog.db"

            [sinks.lines]
            kind = "jsonl"
            path = "{{workspace}}/out/catalog.jsonl"

            [sinks.sheet]
            kind = "csv"
            path = "{{workspace}}/out/catalog.csv"

            [profiles.demo]
            source = "demo"
            mapping = "{DEFAULT_MAPPING}"
            sinks = ["db", "lines", "sheet"]
            """
        )
        + body,
        encoding="utf-8",
    )
    return config


def test_all_three_file_sinks_write_into_the_workspace_and_nothing_next_to_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(HOME_ENV_VAR, raising=False)
    config = write_config(tmp_path)
    home = tmp_path / "ws"

    assert cli.main(["--config", str(config), "--workspace", str(home), "run", "demo"]) == 0

    out = home.resolve() / "out"
    written = [path.name for path in out.iterdir() if path.suffix != ".lock"]
    assert sorted(written) == [
        "catalog.csv",
        "catalog.db",
        "catalog.jsonl",
    ]
    with sqlite3.connect(out / "catalog.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM materials").fetchone()[0] > 0
    assert [path.name for path in config.parent.iterdir()] == ["catalog-sync.toml"], (
        "no out/ folder, and nothing called {workspace}, beside the config"
    )


def test_the_workspace_is_the_one_the_run_uses_whichever_way_it_was_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--workspace, then $CATALOG_SYNC_HOME, then the config's [workspace] home: the same order the
    state database follows, so output and state never end up in different places."""
    config_home = tmp_path / "from-config"
    env_home = tmp_path / "from-env"
    flag_home = tmp_path / "from-flag"
    config = write_config(tmp_path, body=f'\n[workspace]\nhome = "{config_home}"\n')

    monkeypatch.delenv(HOME_ENV_VAR, raising=False)
    assert cli.main(["--config", str(config), "run", "demo"]) == 0
    assert (config_home / "out" / "catalog.db").is_file()

    monkeypatch.setenv(HOME_ENV_VAR, str(env_home))
    assert cli.main(["--config", str(config), "run", "demo"]) == 0
    assert (env_home / "out" / "catalog.db").is_file()

    assert cli.main(["--config", str(config), "--workspace", str(flag_home), "run", "demo"]) == 0
    assert (flag_home / "out" / "catalog.db").is_file()
    for home in (config_home, env_home, flag_home):
        assert (home / "state.sqlite").is_file(), "state sits where the output sits"


def test_the_default_workspace_is_what_the_placeholder_means_when_nothing_chooses_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(HOME_ENV_VAR, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    options = {"path": "{workspace}/out/catalog.db"}
    workspace = resolve_workspace(None, None)
    assert workspace.home == (tmp_path / "home" / ".local/share/catalog-sync").resolve()
    assert expand_sink_paths(options, workspace) == {"path": str(workspace.home / "out/catalog.db")}


def test_a_relative_path_still_resolves_against_the_config_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(HOME_ENV_VAR, raising=False)
    config = write_config(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            '"{workspace}/out/catalog.jsonl"', '"./beside/catalog.jsonl"'
        ),
        encoding="utf-8",
    )
    home = tmp_path / "ws"
    assert cli.main(["--config", str(config), "--workspace", str(home), "run", "demo"]) == 0
    assert (config.parent / "beside" / "catalog.jsonl").is_file()
    assert (home.resolve() / "out" / "catalog.db").is_file()
    assert not (home.resolve() / "out" / "catalog.jsonl").exists()


def test_the_shipped_example_config_keeps_every_file_sink_inside_the_workspace() -> None:
    from catalog_sync.config import load_config

    config = load_config(REPO_ROOT / "catalog-sync.example.toml")
    file_sinks = {
        name: sink.options["path"] for name, sink in config.sinks.items() if "path" in sink.options
    }
    assert file_sinks, "the example has file sinks"
    for name, path in file_sinks.items():
        assert str(path).startswith("{workspace}/"), f"sink {name} writes {path!r}"
