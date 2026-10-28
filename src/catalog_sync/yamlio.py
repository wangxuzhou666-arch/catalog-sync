"""Reading the YAML files this tool is configured with (mappings, schemas)."""

from __future__ import annotations

from collections.abc import Hashable
from pathlib import Path

import yaml
from yaml.constructor import ConstructorError

from catalog_sync.errors import ConfigError


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a key written twice: with last-one-wins an edit that pasted a
    second `name:` rule would silently disable the first."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Hashable, object]:
    seen: set[Hashable] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if isinstance(key, Hashable) and key in seen:
            raise ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_yaml_file(path: Path, *, what: str) -> object:
    """The parsed document, safely, with duplicate keys refused. `what` names the file's role
    in error messages ("mapping", "schema")."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {what} {path.name}: {exc.strerror}") from exc
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 — SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ConfigError(f"{what} {path.name} is not valid YAML: {exc}") from exc
