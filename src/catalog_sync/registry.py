"""Plugin discovery through Python entry points.

A package adds a source, a sink or a credential kind by registering a callable under one of
three groups in its pyproject.toml::

    [project.entry-points."catalog_sync.sources"]
    my-source = "my_package.source:MySource.from_options"

The callable is invoked as ``factory(options, base_dir=...)`` where ``options`` is the table from
the user's config and ``base_dir`` is the directory relative paths in it are resolved against.

One convention on top: when a *source's* options contain ``credential``, the CLI replaces the
credential's name with a ``CredentialHandle`` before calling the factory, so the source never has
to know where credentials live.

Four more groups carry facts rather than factories:

* ``catalog_sync.schemas``: ``"<entity>" = "pkg.module:function"`` where the function returns the
  ``Path`` of an entity schema file (see schema.py); a mapping refers to it by entity name.
* ``catalog_sync.code_tables``: ``"<scope>.<kind>" = "pkg.module:function"`` where the function
  returns the ``Path`` of a code table file (see normalize/codetables.py).
* ``catalog_sync.connectors``: ``"<source name>" = "pkg.module:function"`` where the function
  returns the source's ``ConnectorInfo`` (connector_info.py): what ``catalog-sync init`` needs to
  set it up, such as its settings, its credential kind and a fitting mapping.
* ``catalog_sync.local_sources``: the names of sources that only read local files and make no
  network request. Only those are exempt from the compliance declaration (compliance.py); a source
  that is not listed there is assumed to talk to a service and is gated. They are still audited.

This module deliberately knows nothing about any concrete plugin: core code never imports
``catalog_sync.plugins`` (a test enforces it), it only asks the registry by name.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Protocol, cast

from catalog_sync.connector_info import ConnectorInfo
from catalog_sync.credentials.kind import CredentialKind
from catalog_sync.errors import ConfigError
from catalog_sync.sinks.base import Sink
from catalog_sync.sources.base import Source

SOURCES_GROUP = "catalog_sync.sources"
SINKS_GROUP = "catalog_sync.sinks"
CREDENTIALS_GROUP = "catalog_sync.credentials"
CODE_TABLES_GROUP = "catalog_sync.code_tables"
SCHEMAS_GROUP = "catalog_sync.schemas"
LOCAL_SOURCES_GROUP = "catalog_sync.local_sources"
CONNECTORS_GROUP = "catalog_sync.connectors"


class SourceFactory(Protocol):
    def __call__(self, options: Mapping[str, object], *, base_dir: Path) -> Source: ...


class SinkFactory(Protocol):
    def __call__(self, options: Mapping[str, object], *, base_dir: Path) -> Sink: ...


class CredentialKindFactory(Protocol):
    def __call__(self, options: Mapping[str, object], *, base_dir: Path) -> CredentialKind: ...


def available(group: str) -> list[str]:
    return sorted({entry.name for entry in entry_points(group=group)})


def source_factory(name: str) -> SourceFactory:
    # A plugin is trusted to honour the factory signature; there is nothing to check it against.
    return cast("SourceFactory", _load(SOURCES_GROUP, "source", name))


def sink_factory(name: str) -> SinkFactory:
    return cast("SinkFactory", _load(SINKS_GROUP, "sink", name))


def credential_kind_factory(name: str) -> CredentialKindFactory:
    return cast("CredentialKindFactory", _load(CREDENTIALS_GROUP, "credential", name))


def connector_info(source_name: str) -> ConnectorInfo:
    """What the source plugin `source_name` declares about itself for setup (see ``init``)."""
    if source_name not in available(CONNECTORS_GROUP):
        raise ConfigError(
            f"the source {source_name!r} does not declare its settings, so it cannot be set up "
            "interactively. Write its [sources.NAME] table by hand (see the connector's README)"
        )
    provider = cast(
        "Callable[[], ConnectorInfo]", _load(CONNECTORS_GROUP, "connector", source_name)
    )
    return provider()


def default_mapping(source_name: str) -> Path | None:
    """The mapping file the source plugin `source_name` ships for itself.

    ``None`` when the plugin declares no connector info (``replay``, a plugin written without
    one): it is not an error to ask, because the caller then falls back to asking for a mapping.
    """
    if source_name not in available(CONNECTORS_GROUP):
        return None
    return connector_info(source_name).mapping


def is_local_source(name: str) -> bool:
    """True when the source plugin `name` declares that it reads local files only."""
    return name in available(LOCAL_SOURCES_GROUP)


def schema_path(entity: str) -> Path:
    """The file of the entity schema a plugin registered under this name."""
    provider = cast("Callable[[], Path]", _load(SCHEMAS_GROUP, "schema", entity))
    return provider()


def code_table_path(table_id: str) -> Path:
    """The file of the code table registered as ``<scope>.<kind>``."""
    provider = cast("Callable[[], Path]", _load(CODE_TABLES_GROUP, "code table", table_id))
    return provider()


def _load(group: str, kind: str, name: str) -> Callable[..., object]:
    matches = [entry for entry in entry_points(group=group) if entry.name == name]
    if not matches:
        known = ", ".join(available(group)) or "none installed"
        raise ConfigError(f"no {kind} plugin named {name!r} (available: {known})")
    if len(matches) > 1:
        origins = ", ".join(entry.value for entry in matches)
        raise ConfigError(f"{kind} plugin name {name!r} is registered more than once: {origins}")
    return _import(matches[0], kind)


def _import(entry: EntryPoint, kind: str) -> Callable[..., object]:
    try:
        factory: Callable[..., object] = entry.load()
    except (ImportError, AttributeError) as exc:
        raise ConfigError(
            f"{kind} plugin {entry.name!r} ({entry.value}) failed to load: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    else:
        return factory
