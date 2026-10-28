"""Entity schemas: the YAML format, the record types built from it, and what rides on it.

`material` (built in) and `supplier` (registered by the mock plugin) are the two real schemas; the
rest of the file is about what a schema file may and may not say.
"""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalog_sync.errors import ConfigError, RecordFormatError
from catalog_sync.models import MATERIAL_SCHEMA, MaterialRecord, new_record, record_class
from catalog_sync.normalize.engine import Provenance, normalize
from catalog_sync.normalize.mapping import load_mapping, parse_mapping
from catalog_sync.normalize.output import (
    NormalizedFile,
    meta_path,
    read_normalized,
    write_normalized,
)
from catalog_sync.schema import (
    BUILTIN_SCHEMAS,
    EntitySchema,
    FieldKind,
    NestedKind,
    builtin_schema,
    load_schema_file,
    parse_schema,
)
from catalog_sync.sinks.records import record_batch
from catalog_sync.sources.base import ListedItem, RawBundle

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

SUPPLIER_MAPPING = """
    version: 1
    source: demo.suppliers
    schema: supplier
    fields:
      name:
        from: [list.name]
      country:
        from: [list.country]
      rating:
        from: [list.rating]
      lead_time_days:
        from: [list.lead_time_days]
      certifications:
        from: [list.certifications]
      contact_email:
        from: [list.contact_email]
        sensitive: true
      address:
        from: [list.address]
      listed_at:
        from: [list.listed_at]
"""


def supplier_schema() -> EntitySchema:
    from catalog_sync import registry

    return load_schema_file(registry.schema_path("supplier"))


def supplier_bundle(**overrides: object) -> RawBundle:
    payload: dict[str, object] = {
        "name": "Demo Mill Co.",
        "country": "XX",
        "rating": "4.5",
        "lead_time_days": "14",
        "certifications": ["Demo Cert A", "Demo Cert B"],
        "contact_email": "demo-mill@example.com",
        "address": {"city": "Demo City"},
        "listed_at": "2026-02-01T00:00:00Z",
    }
    payload.update(overrides)
    return RawBundle(ListedItem("sup-1", "v1", payload, {}), None, ())


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def schema_text(fields: str = "name: {kind: text}", **top: str) -> str:
    keys = {"version": "1", "entity": "thing", "schema_version": "1", "key": "[source, source_id]"}
    keys.update(top)
    head = "\n".join(f"{key}: {value}" for key, value in keys.items() if value != "<absent>")
    return f"{head}\nfields:\n" + textwrap.indent(textwrap.dedent(fields), "  ")


# --- the two shipped schemas ----------------------------------------------------------------------


def test_the_material_preset_is_the_schema_the_first_release_hard_coded() -> None:
    assert [
        (s.name, s.kind.value, s.nested.value if s.nested else None) for s in MATERIAL_SCHEMA.fields
    ] == [
        ("code", "text", None),
        ("name", "text", None),
        ("composition", "json", "composition"),
        ("composition_raw", "text", None),
        ("weight_gsm", "real", None),
        ("width_cm", "real", None),
        ("price", "json", "money"),
        ("category", "text", None),
        ("use_cases", "json", "texts"),
        ("tags", "json", "texts"),
        ("supplier", "text", None),
        ("folder_paths", "json", "texts"),
        ("images", "json", "images"),
    ]
    assert [name for name in MATERIAL_SCHEMA.kinds][-7:] == [
        "extra",
        "issues",
        "source_version",
        "raw_ref",
        "fetched_at",
        "normalized_at",
        "schema_version",
    ]
    assert MATERIAL_SCHEMA.schema_version == 1
    assert BUILTIN_SCHEMAS == ("material",) and builtin_schema("material") is MATERIAL_SCHEMA


def test_the_supplier_schema_covers_every_scalar_kind_and_both_json_shapes() -> None:
    schema = supplier_schema()
    assert schema.entity == "supplier"
    declared = {spec.name: (spec.kind, spec.nested) for spec in schema.fields}
    assert declared["rating"] == (FieldKind.REAL, None)
    assert declared["lead_time_days"] == (FieldKind.INTEGER, None)
    assert declared["listed_at"] == (FieldKind.TIMESTAMP, None)
    assert declared["certifications"] == (FieldKind.JSON, NestedKind.TEXTS)
    assert declared["address"] == (FieldKind.JSON, NestedKind.OBJECT)
    assert schema.mappable == set(declared)
    assert not schema.mappable & {"source", "source_id", "extra", "issues"}


def test_the_two_schemas_do_not_share_a_record_type() -> None:
    assert record_class(MATERIAL_SCHEMA) is MaterialRecord
    assert record_class(supplier_schema()) is not MaterialRecord


# --- reading a schema file ------------------------------------------------------------------------


def test_a_schema_round_trips_through_its_json_form() -> None:
    for schema in (MATERIAL_SCHEMA, supplier_schema()):
        again = parse_schema(json.loads(json.dumps(schema.to_json())), "embedded")
        assert again == schema
        assert record_class(again) is record_class(schema), "equal schemas share one record type"


def test_a_schema_file_is_read_from_a_path(tmp_path: Path) -> None:
    path = write(tmp_path, "thing.yaml", schema_text("name: {kind: text}\nsize: {kind: real}"))
    schema = load_schema_file(path)
    assert schema.entity == "thing"
    assert [s.name for s in schema.fields] == ["name", "size"]


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        (schema_text(version="2"), "'version' must be 1"),
        (schema_text(entity="Thing"), "'entity' must match"),
        (schema_text(entity="<absent>"), "'entity' must match"),
        (schema_text(schema_version="0"), "'schema_version' must be a whole number"),
        (schema_text(schema_version="true"), "'schema_version' must be a whole number"),
        (schema_text(key="[source_id]"), "'key' must be"),
        (schema_text(key="<absent>"), "'key' must be"),
        (schema_text(fields="{}"), "at least one field"),
        (schema_text(fields="Name: {kind: text}"), "plain lower-case name"),
        (schema_text(fields="extra: {kind: text}"), "reserved"),
        (schema_text(fields="issues: {kind: text}"), "reserved"),
        (schema_text(fields="source_id: {kind: text}"), "reserved"),
        (schema_text(fields="name: text"), "write it as"),
        (schema_text(fields="name: {kind: blob}"), "'kind' must be one of"),
        (schema_text(fields="name: {}"), "'kind' must be one of"),
        (schema_text(fields="name: {kind: text, of: texts}"), "'of' goes with kind json"),
        (schema_text(fields="name: {kind: json}"), "'of' goes with kind json"),
        (schema_text(fields="name: {kind: json, of: pile}"), "'of' must be one of"),
        (schema_text(fields="name: {kind: text, size: 3}"), "unknown key"),
        (schema_text(fields="name: {kind: text, parse: 4}"), "'parse' must name a parser"),
        (schema_text(extra_key="1"), "unknown top-level key"),
    ],
)
def test_a_malformed_schema_is_refused_with_the_reason(
    tmp_path: Path, text: str, complaint: str
) -> None:
    with pytest.raises(ConfigError, match=complaint):
        load_schema_file(write(tmp_path, "bad.yaml", text))


def test_a_field_written_twice_is_refused_not_silently_overridden(tmp_path: Path) -> None:
    path = write(tmp_path, "twice.yaml", schema_text("name: {kind: text}\nname: {kind: real}"))
    with pytest.raises(ConfigError, match="duplicate key 'name'"):
        load_schema_file(path)


def test_a_missing_or_unparseable_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"cannot read schema nope\.yaml"):
        load_schema_file(tmp_path / "nope.yaml")
    with pytest.raises(ConfigError, match="is not valid YAML"):
        load_schema_file(write(tmp_path, "broken.yaml", "fields: [unclosed"))
    with pytest.raises(ConfigError, match="must be a YAML mapping"):
        parse_schema(["not", "a", "mapping"], "list")


# --- records of a schema --------------------------------------------------------------------------


def test_a_record_gets_the_default_its_field_type_implies() -> None:
    record = new_record(supplier_schema(), "demo", "s1", {})
    assert record.to_dict() == {
        "source": "demo",
        "source_id": "s1",
        "name": None,
        "country": None,
        "rating": None,
        "lead_time_days": None,
        "certifications": [],
        "contact_email": None,
        "address": {},
        "listed_at": None,
        "extra": {},
        "issues": [],
        "source_version": None,
        "raw_ref": None,
        "fetched_at": None,
        "normalized_at": None,
        "schema_version": 1,
    }


def test_a_supplier_record_round_trips_and_is_strict() -> None:
    schema = supplier_schema()
    record = new_record(
        schema,
        "demo",
        "s1",
        {
            "name": "Demo Mill Co.",
            "rating": 4.5,
            "lead_time_days": 14,
            "certifications": ("A", "B"),
            "address": {"city": "Demo City"},
            "listed_at": datetime(2026, 2, 1, tzinfo=UTC),
        },
    )
    encoded = json.loads(json.dumps(record.to_dict(), allow_nan=False))
    assert record_class(schema).from_dict(encoded) == record
    with pytest.raises(RecordFormatError, match="unknown fields"):
        record_class(schema).from_dict({**encoded, "weight_gsm": 1.0})
    with pytest.raises(RecordFormatError, match="expected int"):
        record_class(schema).from_dict({**encoded, "lead_time_days": 1.5})
    with pytest.raises(RecordFormatError, match="schema_version"):
        record_class(schema).from_dict({**encoded, "schema_version": 2})
    with pytest.raises(RecordFormatError, match="KeyError"):
        record_class(schema).from_dict({k: v for k, v in encoded.items() if k != "address"})


def test_a_material_row_is_not_a_supplier_record() -> None:
    material = MaterialRecord("demo", "m1", code="X").to_dict()
    with pytest.raises(RecordFormatError, match="unknown fields"):
        record_class(supplier_schema()).from_dict(material)


def test_a_naive_timestamp_in_a_declared_timestamp_field_is_refused() -> None:
    with pytest.raises(ValueError, match="listed_at must be timezone-aware"):
        new_record(supplier_schema(), "demo", "s1", {"listed_at": datetime(2026, 2, 1)})


# --- mappings against a schema --------------------------------------------------------------------


def test_a_mapping_without_a_schema_line_means_material(tmp_path: Path) -> None:
    path = write(
        tmp_path, "m.yaml", "version: 1\nsource: s\nfields:\n  code:\n    from: [list.c]\n"
    )
    assert load_mapping(path).schema is MATERIAL_SCHEMA


def test_a_mapping_can_name_the_builtin_a_plugin_schema_or_a_file(tmp_path: Path) -> None:
    def spec(schema_line: str):  # type: ignore[no-untyped-def]
        return parse_mapping(
            {
                "version": 1,
                "source": "s",
                "schema": schema_line,
                "fields": {"name": {"from": ["list.n"]}},
            },
            "m.yaml",
            base_dir=tmp_path,
        )

    assert spec("material").schema is MATERIAL_SCHEMA
    assert spec("supplier").schema == supplier_schema()
    write(tmp_path, "own.yaml", schema_text("name: {kind: text}"))
    assert spec("own.yaml").schema.entity == "thing"


def test_an_unknown_schema_name_lists_what_exists(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigError, match=r"unknown schema 'gizmo' \(available: material, supplier"
    ):
        parse_mapping(
            {
                "version": 1,
                "source": "s",
                "schema": "gizmo",
                "fields": {"name": {"from": ["list.n"]}},
            },
            "m.yaml",
            base_dir=tmp_path,
        )


def test_a_missing_schema_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"cannot read schema absent\.yaml"):
        parse_mapping(
            {
                "version": 1,
                "source": "s",
                "schema": "absent.yaml",
                "fields": {"a": {"from": ["list.n"]}},
            },
            "m.yaml",
            base_dir=tmp_path,
        )


def test_a_rule_for_a_field_the_schema_does_not_declare_is_refused(tmp_path: Path) -> None:
    """The point of declaring a schema: a typo, or a material field in a supplier mapping, fails
    when the mapping is read, before any data is touched."""
    text = """
        version: 1
        source: demo.suppliers
        schema: supplier
        fields:
          weight_gsm:
            from: [list.rating]
    """
    with pytest.raises(ConfigError, match=r"field 'weight_gsm' is not a field.*schema 'supplier'"):
        load_mapping(write(tmp_path, "m.yaml", text))


@pytest.mark.parametrize("name", ["source_id", "extra", "issues", "schema_version"])
def test_provenance_and_identity_can_never_be_mapped(tmp_path: Path, name: str) -> None:
    text = f"version: 1\nsource: s\nschema: supplier\nfields:\n  {name}:\n    from: [list.x]\n"
    with pytest.raises(ConfigError, match="is not a field a mapping can fill"):
        load_mapping(write(tmp_path, "m.yaml", text))


def test_a_parser_that_does_not_fit_the_schema_type_is_refused(tmp_path: Path) -> None:
    text = """
        version: 1
        source: s
        schema: supplier
        fields:
          lead_time_days:
            from: [list.x]
            parse: gsm
    """
    with pytest.raises(ConfigError, match="parser 'gsm' does not produce a integer value"):
        load_mapping(write(tmp_path, "m.yaml", text))


def test_the_digest_follows_the_schema(tmp_path: Path) -> None:
    def digest(kind: str) -> str:
        path = write(tmp_path, "s.yaml", schema_text(f"name: {{kind: {kind}}}"))
        return parse_mapping(
            {
                "version": 1,
                "source": "s",
                "schema": path.name,
                "fields": {"name": {"from": ["list.n"]}},
            },
            "m.yaml",
            base_dir=tmp_path,
        ).digest

    assert digest("text") == digest("text")
    assert digest("text") != digest("real"), "a schema change is a mapping change"


def test_a_supplier_record_comes_out_of_the_engine_typed_and_parsed(tmp_path: Path) -> None:
    spec = load_mapping(write(tmp_path, "m.yaml", SUPPLIER_MAPPING))
    provenance = Provenance("v1", "run-1:sup-1", NOW, NOW)
    record = normalize(supplier_bundle(), spec, provenance)
    assert record.to_dict() == {
        "source": "demo.suppliers",
        "source_id": "sup-1",
        "name": "Demo Mill Co.",
        "country": "XX",
        "rating": 4.5,
        "lead_time_days": 14,
        "certifications": ["Demo Cert A", "Demo Cert B"],
        "contact_email": "demo-mill@example.com",
        "address": {"city": "Demo City"},
        "listed_at": "2026-02-01T00:00:00+00:00",
        "extra": {},
        "issues": [],
        "source_version": "v1",
        "raw_ref": "run-1:sup-1",
        "fetched_at": NOW.isoformat(),
        "normalized_at": NOW.isoformat(),
        "schema_version": 1,
    }
    assert spec.sensitive_fields == {"contact_email"}
    assert spec.record_fields == set(spec.schema.mappable)


def test_what_cannot_be_parsed_becomes_null_and_an_issue_not_a_guess(tmp_path: Path) -> None:
    spec = load_mapping(write(tmp_path, "m.yaml", SUPPLIER_MAPPING))
    record = normalize(
        supplier_bundle(rating="n/a", lead_time_days="2 weeks", listed_at="2026-02-01"),
        spec,
        Provenance(None, None, None, NOW),
    )
    values = record.to_dict()
    assert (values["rating"], values["lead_time_days"], values["listed_at"]) == (None, None, None)
    assert values["issues"] == [
        "rating:unparseable",
        "lead_time_days:unparseable",
        "listed_at:unparseable",
    ]


def test_a_list_valued_field_without_a_value_is_empty_not_null(tmp_path: Path) -> None:
    spec = load_mapping(write(tmp_path, "m.yaml", SUPPLIER_MAPPING))
    record = normalize(
        supplier_bundle(certifications=None), spec, Provenance(None, None, None, NOW)
    )
    assert record.to_dict()["certifications"] == []


# --- the normalised file carries its schema -------------------------------------------------------


def normalized_file(tmp_path: Path) -> NormalizedFile:
    spec = load_mapping(write(tmp_path, "m.yaml", SUPPLIER_MAPPING))
    record = normalize(supplier_bundle(), spec, Provenance("v1", "run-1:sup-1", NOW, NOW))
    return NormalizedFile(
        schema=spec.schema,
        records=(record,),
        owned=spec.record_fields | spec.schema.loaded_provenance,
        keep_existing_if_null=frozenset(),
        sensitive=spec.sensitive_fields,
        is_full_snapshot=True,
        source=spec.source,
        run_id="run-1",
        mapping_digest=spec.digest,
    )


def test_a_normalised_supplier_file_reads_back_with_its_schema(tmp_path: Path) -> None:
    written = normalized_file(tmp_path)
    out = tmp_path / "suppliers.jsonl"
    write_normalized(out, written)
    assert json.loads(meta_path(out).read_text(encoding="utf-8"))["schema"]["entity"] == "supplier"
    read = read_normalized(out)
    assert read.schema == written.schema
    assert read.records == written.records
    assert read.owned == written.owned and read.sensitive == {"contact_email"}


def test_a_sidecar_from_before_schemas_existed_means_material(tmp_path: Path) -> None:
    out = tmp_path / "old.jsonl"
    record = MaterialRecord("demo", "m1", code="X", fetched_at=NOW, normalized_at=NOW)
    material = NormalizedFile(
        schema=MATERIAL_SCHEMA,
        records=(record,),
        owned=frozenset({"code"}),
        keep_existing_if_null=frozenset(),
        sensitive=frozenset(),
        is_full_snapshot=False,
        source="demo",
        run_id="r",
        mapping_digest="0" * 64,
    )
    write_normalized(out, material)
    meta = json.loads(meta_path(out).read_text(encoding="utf-8"))
    del meta["schema"]
    meta_path(out).write_text(json.dumps(meta), encoding="utf-8")
    assert read_normalized(out).schema is MATERIAL_SCHEMA


def test_a_sidecar_with_a_broken_schema_is_a_record_format_error(tmp_path: Path) -> None:
    out = tmp_path / "bad.jsonl"
    write_normalized(out, normalized_file(tmp_path))
    meta = json.loads(meta_path(out).read_text(encoding="utf-8"))
    meta["schema"]["key"] = ["id"]
    meta_path(out).write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(RecordFormatError, match="carries a schema that is not valid"):
        read_normalized(out)


# --- sinks get their kinds from the schema --------------------------------------------------------


def test_a_batch_takes_its_kinds_from_the_schema_of_the_records(tmp_path: Path) -> None:
    schema = supplier_schema()
    record = new_record(schema, "demo", "s1", {"name": "Demo Mill Co.", "lead_time_days": 3})
    batch = record_batch(
        [record],
        schema,
        owned=frozenset({"name", "lead_time_days", "certifications"}),
    )
    assert batch.kinds is schema.kinds
    assert batch.kinds["lead_time_days"] is FieldKind.INTEGER
    assert batch.rows[0]["certifications"] == []
