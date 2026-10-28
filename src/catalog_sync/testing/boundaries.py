"""Who imports whom: a scanner for a connector's import boundaries, for tests of the connector.

A connector may use the core and the SDK, and must not reach into another connector: what two
connectors share belongs in ``catalog_sync.sdk``. These helpers read the source with ``ast`` (no
import happens), so they work on any directory, in this repository or in a connector package of
its own::

    files = python_files(Path("src/catalog_sync_acme"))
    assert not any(other_plugins_reached(f.read_text("utf-8")) for f in files)
    assert reaches(source_text, "catalog_sync.sdk")
"""

from __future__ import annotations

import ast
from pathlib import Path

# The built-in connectors live under this package; a connector shipped as its own package is
# imported by its own name and is not reachable through it.
PLUGIN_ROOT = "catalog_sync.plugins"


def imported_modules(source: str) -> set[str]:
    """Every module name that `source` imports, including each ``from m import name`` as m.name."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def reaches(source: str, prefix: str) -> bool:
    """Does `source` import `prefix` or anything below it?"""
    return any(m == prefix or m.startswith(prefix + ".") for m in imported_modules(source))


def other_plugins_reached(source: str, own: str = "") -> list[str]:
    """The built-in connector packages (other than `own`) that this module imports from."""
    reached = {
        m.split(".")[2]
        for m in imported_modules(source)
        if m.startswith(PLUGIN_ROOT + ".") and m.count(".") >= 2
    }
    return sorted(reached - {own})


def python_files(directory: Path) -> list[Path]:
    """Every ``.py`` file under `directory`, sorted, so a failure lists them in a stable order."""
    return sorted(directory.rglob("*.py"))
