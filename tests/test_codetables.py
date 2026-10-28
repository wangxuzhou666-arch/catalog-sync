from __future__ import annotations

from pathlib import Path

import pytest

from catalog_sync.errors import ConfigError
from catalog_sync.normalize.codetables import CodeTable, load_table_file, table_for
from tests.conftest import REPO_ROOT

SHIPPED = REPO_ROOT / "src/catalog_sync/plugins/mock/tables/materials.composition.yaml"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "table.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_shipped_table_loads_through_the_registry() -> None:
    table = table_for("mock.saas/materials", "composition")
    assert table.scope == "mock.saas/materials"
    assert table.lookup("PES") == "Polyester"
    assert table.lookup("  li ") == "Linen"
    assert table.lookup("ZZ") is None, "a code nobody listed stays unresolved"


def test_the_shipped_table_has_english_names_only_and_a_scope() -> None:
    text = SHIPPED.read_text(encoding="utf-8")
    assert text.isascii()
    assert "scope: mock.saas/materials" in text


def test_an_unregistered_scope_is_a_config_error_listing_nothing_invented() -> None:
    with pytest.raises(ConfigError, match=r"no code table plugin named 'nope/x\.composition'"):
        table_for("nope/x", "composition")


def test_a_file_must_declare_scope_kind_and_codes(tmp_path: Path) -> None:
    table = load_table_file(
        write(tmp_path, "scope: a.b/c\nkind: composition\ncodes: {X: Wool}\n"), kind="composition"
    )
    assert table.lookup("x") == "Wool"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("- not a mapping", "must be a YAML mapping"),
        ("scope: a/b\nkind: composition\ncodes: {X: Y}\nextra: 1\n", "unknown key"),
        ("scope: A B\nkind: composition\ncodes: {X: Y}\n", "'scope' must look like"),
        ("scope: a/b\nkind: category\ncodes: {X: Y}\n", "expected kind 'composition'"),
        ("scope: a/b\nkind: composition\ncodes: {}\n", "non-empty mapping"),
        ("scope: a/b\nkind: composition\ncodes: {X: 3}\n", "non-empty string"),
        ("scope: a/b\nkind: composition\ncodes: {X: A, x: B}\n", "differs only by case"),
        ("a: [unclosed", "not valid YAML"),
    ],
)
def test_malformed_tables_are_config_errors(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_table_file(write(tmp_path, text), kind="composition")


def test_tables_of_different_scopes_are_separate_objects_with_their_own_meanings(
    tmp_path: Path,
) -> None:
    first = CodeTable("lib/a", "composition", {"V": "Viscose"})
    second = CodeTable("lib/b", "composition", {"V": "Vicuna"})
    assert (first.lookup("V"), second.lookup("V")) == ("Viscose", "Vicuna")


def test_a_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read code table"):
        load_table_file(tmp_path / "ghost.yaml", kind="composition")
