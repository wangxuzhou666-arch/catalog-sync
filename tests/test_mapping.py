"""Mapping files: what they accept, what they refuse, and what they say a load owns."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from catalog_sync.errors import ConfigError
from catalog_sync.models import MaterialRecord
from catalog_sync.normalize.mapping import load_mapping, parse_mapping, pick
from tests.normalize_helpers import PRICE_MAPPING, spec_from, write_mapping


def fields_block(body: str) -> str:
    indented = "\n      ".join(textwrap.dedent(body).strip("\n").splitlines())
    return f"    version: 1\n    source: demo\n    fields:\n      {indented}\n"


def load(tmp_path: Path, body: str):  # type: ignore[no-untyped-def]
    return spec_from(tmp_path, fields_block(body))


# --- what a mapping owns ------------------------------------------------------------------------


def test_owned_keep_and_sensitive_sets_follow_the_rules(tmp_path: Path) -> None:
    spec = load(
        tmp_path,
        """
        name:
          from: list.name
        supplier:
          from: [detail.supplier_name]
          sensitive: true
          on_null: keep_existing
        extra.demo.a:
          from: [detail.a]
        extra.demo.b:
          from: [detail.b]
        """,
    )
    assert spec.record_fields == {"name", "supplier", "extra"}
    assert spec.keep_existing_if_null == {"supplier"}
    assert spec.sensitive_fields == {"supplier"}
    assert spec.uses_detail


def test_a_mapping_without_detail_paths_says_so(tmp_path: Path) -> None:
    spec = load(tmp_path, "name:\n  from: [list.name, context.x]")
    assert not spec.uses_detail


def test_every_mappable_field_is_a_real_record_field_and_never_provenance() -> None:
    from catalog_sync.models import ALL_FIELDS, MATERIAL_SCHEMA, PROVENANCE_FIELDS

    mappable = MATERIAL_SCHEMA.mappable
    assert mappable <= ALL_FIELDS
    assert not mappable & PROVENANCE_FIELDS
    assert "source_id" not in mappable
    assert MaterialRecord  # the record type the names refer to


# --- the digest names the rules and the code tables ---------------------------------------------


def test_the_digest_changes_with_a_rule_and_not_with_formatting(tmp_path: Path) -> None:
    base = spec_from(tmp_path, PRICE_MAPPING)
    reformatted = spec_from(tmp_path, PRICE_MAPPING.replace("        from", "        from"))
    changed = spec_from(tmp_path, PRICE_MAPPING.replace("currency: USD", "currency: EUR"))
    assert base.digest == reformatted.digest
    assert base.digest != changed.digest


def test_the_digest_covers_the_code_table_content(tmp_path: Path) -> None:
    def digest(codes: str) -> str:
        (tmp_path / "t.yaml").write_text(
            f"scope: a.b/c\nkind: composition\ncodes: {codes}\n", encoding="utf-8"
        )
        return load(tmp_path, "composition:\n  from: [detail.c]\n  table_file: t.yaml").digest

    assert digest("{P: Polyester}") != digest("{P: Nylon}")


# --- refusals -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("colour:\n  from: [list.c]", "is not a field a mapping can fill"),
        ("source_id:\n  from: [list.id]", "is not a field a mapping can fill"),
        ("issues:\n  from: [list.i]", "is not a field a mapping can fill"),
        ("extra.nonamespace:\n  from: [list.x]", "is not a field a mapping can fill"),
        ("name: list.name", "must be a mapping with at least 'from'"),
        ("name:\n  parse: text", "'from' must be a path"),
        ("name:\n  from: []", "'from' must be a path"),
        ("name:\n  from: [name]", "is not 'list.<path>'"),
        ("name:\n  from: [other.name]", "is not 'list.<path>'"),
        ("name:\n  from: [list.]", "is not 'list.<path>'"),
        ("name:\n  from: [list.a b]", "is not 'list.<path>'"),
        ("name:\n  from: [list.name]\n  parse: nosuch", "unknown parser"),
        ("name:\n  from: [list.name]\n  parse: gsm", "does not produce a text value"),
        ("weight_gsm:\n  from: [list.w]\n  parse: text", "does not produce a number value"),
        ("name:\n  from: [list.name]\n  colour: red", "do not apply to parser 'text'"),
        ("name:\n  from: [list.name]\n  zero_is_missing: true", "do not apply to parser 'text'"),
        ("weight_gsm:\n  from: [list.w]\n  zero_is_missing: 'yes'", "must be true or false"),
        ("name:\n  from: [list.name]\n  on_null: wipe", "'on_null' must be one of"),
        ("name:\n  from: [list.name]\n  sensitive: 1", "'sensitive' must be true or false"),
        ("price:\n  from: [list.p]", "needs \\['currency'\\]"),
        ("price:\n  from: [list.p]\n  currency: usd", "3-letter upper-case"),
        ("tags:\n  from: [list.t]\n  parse: split\n  separators: ''", "non-empty string"),
        ("composition:\n  from: [list.c]", "exactly one of 'scope'"),
        (
            "composition:\n  from: [list.c]\n  scope: mock.saas/materials\n  table_file: t.yaml",
            "exactly one of 'scope'",
        ),
        ("extra.demo.a:\n  from: [list.a]\n  sensitive: true", "shares the one 'extra' field"),
        (
            "extra.demo.a:\n  from: [list.a]\n  on_null: keep_existing\n"
            "extra.demo.b:\n  from: [list.b]",
            "same 'on_null'",
        ),
        ("images:\n  from: [list.a]\n  parse: text", "do not apply to pictures"),
        ("images:\n  from: [list.a]\n  roles: {thumb: [list.a]}", "either 'from'"),
        ("images:\n  on_null: keep_existing", "either 'from'"),
        ("images:\n  roles: {Thumb: [list.a]}", "picture role"),
    ],
)
def test_invalid_rules_are_refused_with_the_field_named(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load(tmp_path, body)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("- list", "must be a YAML mapping"),
        ("version: 2\nsource: s\nfields: {name: {from: list.n}}", "'version' must be 1"),
        ("version: 1\nfields: {name: {from: list.n}}", "'source' must name"),
        ("version: 1\nsource: s\nfields: {}", "at least one field"),
        ("version: 1\nsource: s\nfields: {name: {from: list.n}}\nextra: 1", "unknown top-level"),
        (
            "version: 1\nsource: s\nfields:\n  name: {from: list.a}\n  name: {from: list.b}\n",
            "duplicate key",
        ),
        ("a: [unclosed", "not valid YAML"),
    ],
)
def test_malformed_documents_are_config_errors(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_mapping(write_mapping(tmp_path, text))


def test_an_unregistered_scope_and_a_missing_file_are_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no code table plugin"):
        load(tmp_path, "composition:\n  from: [list.c]\n  scope: nope/none")
    with pytest.raises(ConfigError, match="cannot read code table"):
        load(tmp_path, "composition:\n  from: [list.c]\n  table_file: ghost.yaml")
    with pytest.raises(ConfigError, match="cannot read mapping"):
        load_mapping(tmp_path / "ghost.yaml")


def test_parse_mapping_works_on_an_already_loaded_document(tmp_path: Path) -> None:
    spec = parse_mapping(
        {"version": 1, "source": "s", "fields": {"name": {"from": "list.n"}}},
        "inline",
        base_dir=tmp_path,
    )
    assert spec.fields["name"].sources[0].path == "n"


# --- path syntax (every connector's mapping uses it) -------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("a", {"b": 1}),
        ("a.b", 1),
        ("c[0]", "x"),
        ("c[1]", "y"),
        ("d[0].e", 7),
        ("d[0].e.f", None),
        ("c[2]", None),
        ("a.missing", None),
        ("missing.deeper", None),
        ("a[0]", None),
        ("c.k", None),
    ],
)
def test_pick_walks_keys_and_indexes_and_gives_none_when_a_step_is_absent(
    path: str, expected: object
) -> None:
    document = {"a": {"b": 1}, "c": ["x", "y"], "d": [{"e": 7}]}
    assert pick(document, path) == expected


def test_pick_into_nothing_is_none() -> None:
    assert pick(None, "a.b") is None
