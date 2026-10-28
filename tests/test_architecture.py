"""Structural rules: core never imports plugins, and plugins are found only through the registry."""

from __future__ import annotations

import ast
import tomllib
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from catalog_sync import registry
from catalog_sync.errors import ConfigError
from catalog_sync.sinks.csv import CsvSink
from catalog_sync.sinks.jsonl import JsonlSink
from catalog_sync.sinks.sqlite import SqliteSink
from catalog_sync.sources.replay import ReplaySource

CORE_DIR = Path(__file__).resolve().parent.parent / "src" / "catalog_sync"
ROOT_ENTRY_POINTS = tomllib.loads(
    (CORE_DIR.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["entry-points"]
PLUGINS = "catalog_sync.plugins"


def _is_plugin_module(name: str) -> bool:
    return name == PLUGINS or name.startswith(PLUGINS + ".")


def _import_from_hits(node: ast.ImportFrom) -> list[str]:
    module = node.module or ""
    if node.level > 0:  # relative: "from .plugins.x import y" inside the package root
        return ["." * node.level + module] if module.split(".")[0] == "plugins" else []
    if _is_plugin_module(module):
        return [module]
    if module == "catalog_sync" and any(alias.name == "plugins" for alias in node.names):
        return [PLUGINS]
    return []


def plugin_imports(source: str) -> list[str]:
    """Every way a module can reach catalog_sync.plugins, including by dotted string."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if _is_plugin_module(alias.name)]
        elif isinstance(node, ast.ImportFrom):
            found += _import_from_hits(node)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _is_plugin_module(node.value)
        ):
            found.append(node.value)
    return found


# The import scanner shipped for connectors' tests has to name the package of the built-in
# connectors to look for imports of it; it imports nothing from it.
SCANNERS = {Path("testing/boundaries.py")}


def core_files() -> list[Path]:
    return [
        p
        for p in CORE_DIR.rglob("*.py")
        if "plugins" not in p.relative_to(CORE_DIR).parts
        and p.relative_to(CORE_DIR) not in SCANNERS
    ]


def test_core_never_imports_a_plugin() -> None:
    files = core_files()
    assert len(files) > 10, "the scan must actually see the core modules"
    offenders = {
        str(path.relative_to(CORE_DIR)): hits
        for path in files
        if (hits := plugin_imports(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


@pytest.mark.parametrize(
    "source",
    [
        "import catalog_sync.plugins.mock",
        "from catalog_sync.plugins.mock import api",
        "from catalog_sync.plugins import mock",
        "from catalog_sync import plugins",
        "from .plugins.mock import api",
        "import importlib\nimportlib.import_module('catalog_sync.plugins.mock.source')",
    ],
)
def test_the_scanner_catches_every_import_form(source: str) -> None:
    assert plugin_imports(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "from catalog_sync import registry",
        "from catalog_sync.sources.base import Source",
        "plugins = ['a']\nprint(plugins)",
        "import my_plugins",
        "NAME = 'catalog_sync.pluginsx'",
        "text = 'see catalog_sync.sources'",
    ],
)
def test_the_scanner_ignores_everything_else(source: str) -> None:
    assert plugin_imports(source) == []


def test_builtin_plugins_are_registered_under_their_groups() -> None:
    assert "replay" in registry.available(registry.SOURCES_GROUP)
    assert {"jsonl", "csv", "sqlite"} <= set(registry.available(registry.SINKS_GROUP))
    assert "mock" in registry.available(registry.CREDENTIALS_GROUP)
    assert {"mock.materials", "mock.suppliers"} <= set(registry.available(registry.SOURCES_GROUP))
    assert {"supplier"} <= set(registry.available(registry.SCHEMAS_GROUP))


def test_only_the_replay_source_is_exempt_from_the_compliance_declaration() -> None:
    assert registry.available(registry.LOCAL_SOURCES_GROUP) == ["replay"]
    assert registry.is_local_source("replay")
    # They only talk to localhost, but they make real HTTP calls, so they go through the gate.
    assert not registry.is_local_source("mock.materials")
    assert not registry.is_local_source("mock.suppliers")
    assert not registry.is_local_source("a-source-nobody-declared")


def test_the_registered_code_tables_are_exactly_the_root_entry_points() -> None:
    """Equality, not containment: an extra table would be a connector in the core environment."""
    from_pyproject = sorted(ROOT_ENTRY_POINTS["catalog_sync.code_tables"])
    assert from_pyproject, "the root pyproject must register a table, or this checks nothing"
    assert registry.available(registry.CODE_TABLES_GROUP) == from_pyproject
    for name in from_pyproject:
        assert registry.code_table_path(name).is_file(), name


def test_a_code_table_is_registered_under_its_scope() -> None:
    path = registry.code_table_path("mock.saas/materials.composition")
    assert path.name == "materials.composition.yaml" and path.is_file()


def test_the_mock_entry_points_resolve_to_the_plugin_factories() -> None:
    from catalog_sync.plugins.mock.login import mock_credential_kind
    from catalog_sync.plugins.mock.source import MockMaterialsSource, MockSuppliersSource

    assert registry.source_factory("mock.materials") == MockMaterialsSource.from_options
    assert registry.source_factory("mock.suppliers") == MockSuppliersSource.from_options
    assert registry.credential_kind_factory("mock") == mock_credential_kind


def test_the_registry_resolves_names_to_the_factories() -> None:
    assert registry.source_factory("replay") == ReplaySource.from_options
    assert registry.sink_factory("jsonl") == JsonlSink.from_options
    assert registry.sink_factory("csv") == CsvSink.from_options
    assert registry.sink_factory("sqlite") == SqliteSink.from_options


def test_an_unknown_plugin_name_lists_what_is_installed() -> None:
    with pytest.raises(ConfigError, match=r"no source plugin named 'nope' \(available: .*replay"):
        registry.source_factory("nope")
    with pytest.raises(ConfigError, match=r"no sink plugin named 'nope' \(available: .*jsonl"):
        registry.sink_factory("nope")
    with pytest.raises(ConfigError, match=r"no credential plugin named 'nope'.*mock"):
        registry.credential_kind_factory("nope")


def test_a_plugin_that_fails_to_import_is_a_config_error_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = EntryPoint("broken", "no_such_module_anywhere:factory", registry.SOURCES_GROUP)
    monkeypatch.setattr(registry, "entry_points", lambda group: [broken])
    with pytest.raises(ConfigError, match=r"'broken'.*failed to load.*ModuleNotFoundError"):
        registry.source_factory("broken")


def test_two_packages_claiming_one_name_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    first = EntryPoint("twin", "pkg_a:make", registry.SOURCES_GROUP)
    second = EntryPoint("twin", "pkg_b:make", registry.SOURCES_GROUP)
    monkeypatch.setattr(registry, "entry_points", lambda group: [first, second])
    with pytest.raises(ConfigError, match="registered more than once: pkg_a:make, pkg_b:make"):
        registry.source_factory("twin")
