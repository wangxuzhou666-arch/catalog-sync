"""Code tables: a source's own abbreviations (fibre codes) mapped to plain names.

A table belongs to one *scope*, normally one endpoint or sub-library of one service
(``mock.saas/materials``). Tables are never merged across scopes, because the same code can mean
different things in two sub-libraries; a mapping names the scope it is written for, and a code the
table does not list stays unresolved (the normaliser records an issue) instead of being guessed.

A table is a small YAML file::

    scope: mock.saas/materials
    kind: composition
    codes: {PES: Polyester, CO: Cotton}

Lookup ignores case and surrounding whitespace; two codes that differ only by case are an error in
the file, since they could not be told apart.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from catalog_sync import registry
from catalog_sync.errors import ConfigError

_SCOPE = re.compile(r"[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*")
_KINDS = frozenset({"composition"})
_KEYS = frozenset({"scope", "kind", "description", "codes"})


@dataclass(frozen=True)
class CodeTable:
    scope: str
    kind: str
    codes: Mapping[str, str]
    _folded: Mapping[str, str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        folded: dict[str, str] = {}
        for code, name in self.codes.items():
            key = _fold(code)
            if key in folded:
                raise ConfigError(
                    f"code table {self.scope} ({self.kind}): {code!r} collides with another code "
                    "that differs only by case or spacing"
                )
            folded[key] = name
        object.__setattr__(self, "_folded", folded)

    def lookup(self, code: str) -> str | None:
        return self._folded.get(_fold(code))

    def to_json(self) -> dict[str, object]:
        """What identifies this table's content, for the mapping digest."""
        return {"scope": self.scope, "kind": self.kind, "codes": dict(sorted(self.codes.items()))}


def _fold(code: str) -> str:
    return code.strip().casefold()


def load_table_file(path: Path, *, kind: str) -> CodeTable:
    """Read and validate one table file; `kind` is what the caller needs it to be."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read code table {path.name}: {exc.strerror}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"code table {path.name} is not valid YAML: {exc}") from exc
    return _table_from_yaml(raw, path.name, kind)


def table_for(scope: str, kind: str) -> CodeTable:
    """The table an installed plugin registered for this scope (never a merge of several)."""
    path = registry.code_table_path(f"{scope}.{kind}")
    table = load_table_file(path, kind=kind)
    if table.scope != scope:
        raise ConfigError(
            f"code table {path.name} is registered for scope {scope!r} but declares {table.scope!r}"
        )
    return table


def _table_from_yaml(raw: object, name: str, kind: str) -> CodeTable:
    if not isinstance(raw, dict):
        raise ConfigError(f"code table {name} must be a YAML mapping")
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise ConfigError(f"code table {name}: unknown key(s) {unknown}")
    scope, table_kind, codes = raw.get("scope"), raw.get("kind"), raw.get("codes")
    if not isinstance(scope, str) or not _SCOPE.fullmatch(scope):
        raise ConfigError(f"code table {name}: 'scope' must look like 'service.library/endpoint'")
    if table_kind != kind or kind not in _KINDS:
        raise ConfigError(f"code table {name}: expected kind {kind!r}, found {table_kind!r}")
    if not isinstance(codes, dict) or not codes:
        raise ConfigError(f"code table {name}: 'codes' must be a non-empty mapping")
    for code, value in codes.items():
        if not (isinstance(code, str) and code.strip() and isinstance(value, str) and value):
            raise ConfigError(f"code table {name}: every code and name must be a non-empty string")
    return CodeTable(scope=scope, kind=kind, codes=dict(codes))
