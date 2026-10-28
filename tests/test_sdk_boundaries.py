"""Who may import whom around the SDK (the scanner is ``catalog_sync.testing.boundaries``).

The SDK is what the shipped connectors and the scaffold share, so each connector may use it and the
SDK may use the core, but the connectors do not reach into each other (what they share belongs in
the SDK, which is how the duplication this package removed would otherwise grow back), and the SDK
does not reach into the command line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import catalog_sync.sdk
from catalog_sync.testing.boundaries import (
    PLUGIN_ROOT,
    imported_modules,
    other_plugins_reached,
    python_files,
    reaches,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "catalog_sync"


@pytest.mark.parametrize("plugin", ["mock"])
def test_a_connector_does_not_import_another_connector(plugin: str) -> None:
    files = python_files(SRC / "plugins" / plugin)
    assert len(files) >= 5, "the scan must really see the connector's modules"
    offenders = {
        str(path.relative_to(SRC)): other_plugins_reached(path.read_text("utf-8"), plugin)
        for path in files
        if other_plugins_reached(path.read_text("utf-8"), plugin)
    }
    assert offenders == {}


def test_the_sdk_does_not_import_the_command_line_or_a_connector() -> None:
    files = python_files(SRC / "sdk")
    assert len(files) >= 7
    for path in files:
        source = path.read_text("utf-8")
        assert not reaches(source, PLUGIN_ROOT), f"{path.name} imports a connector"
        cli = {m for m in imported_modules(source) if m.startswith("catalog_sync.cli")}
        assert cli == set(), f"{path.name} imports the command line: {cli}"


def test_the_mock_connector_really_uses_the_sdk() -> None:
    users = [
        path.name
        for path in python_files(SRC / "plugins" / "mock")
        if reaches(path.read_text("utf-8"), "catalog_sync.sdk")
    ]
    assert {"source.py", "settings.py", "api.py", "login.py"} <= set(users)


# --- the scanner itself must be able to fail (a check that never failed proves nothing) ---------


@pytest.mark.parametrize(
    "source",
    [
        "from catalog_sync.plugins.mock import api",
        "from catalog_sync.plugins.mock.api import MockApi",
        "import catalog_sync.plugins.mock.source",
        "from catalog_sync.plugins import mock",
    ],
)
def test_the_scanner_sees_a_connector_module_reaching_into_another(source: str) -> None:
    assert other_plugins_reached(source, "other") == ["mock"]


@pytest.mark.parametrize(
    "source",
    [
        "from catalog_sync.plugins.other import api",
        "from catalog_sync.sdk import FolderWalker",
        "from catalog_sync import registry",
        "import my_plugins.mock",
        "name = 'catalog_sync.plugins.mock'",
    ],
)
def test_the_scanner_lets_a_connector_use_itself_and_the_core(source: str) -> None:
    assert other_plugins_reached(source, "other") == []


def test_the_scanner_sees_the_sdk_reaching_for_the_command_line() -> None:
    assert reaches("from catalog_sync.sdk import x", "catalog_sync.sdk")
    assert {m for m in imported_modules("from catalog_sync.cli import main") if "cli" in m}
    assert not reaches("from catalog_sync.state import Checkpoint", "catalog_sync.sdk")


def test_the_sdk_describes_itself_as_shared_by_connectors_and_scaffold_not_by_two_connectors() -> (
    None
):
    """A piece only one connector uses may stay: the scaffold is a tested second consumer, and the
    docstring must not promise "what two connectors had in common"."""
    doc = " ".join((catalog_sync.sdk.__doc__ or "").split())
    assert "shared by the shipped connectors and the generated scaffold" in doc
    assert "two connectors" not in doc
