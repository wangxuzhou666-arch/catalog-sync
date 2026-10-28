"""The config writer and the preview table: what the wizard writes and shows."""

from __future__ import annotations

import tomllib
from datetime import date
from decimal import Decimal

import pytest

from catalog_sync.models import MATERIAL_SCHEMA, Composition, ImageRef, MaterialRecord, Money
from catalog_sync.wizard.preview import CELL_WIDTH, MAX_COLUMNS, MAX_ROWS, preview_lines
from catalog_sync.wizard.tomlwrite import Table, dumps

# --- TOML -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        'with "quotes" and \\ backslash',
        "line\nbreak\tand tab",
        "unicode café ☃ and an emoji \U0001f600",
        "\x7f delete and \x01 control",
        "",
        0,
        -7,
        3.5,
        1e-9,
        True,
        False,
        date(2026, 10, 26),
        ["a", "b"],
        [],
        [1, 2, 3],
    ],
)
def test_every_value_type_survives_a_round_trip_through_a_real_toml_reader(value: object) -> None:
    text = dumps([Table(("t",), [("k", value)])])
    assert tomllib.loads(text) == {"t": {"k": value}}


def test_nested_headers_keys_that_need_quoting_and_comments() -> None:
    text = dumps(
        [
            Table(
                ("sources", "my-source", "compliance"),
                [("a b", 1), ("plain_key", "x")],
                comments={"plain_key": "why this is here"},
                note="about the table\nsecond line",
            ),
            Table(("weird.name",), [("k", 1)]),
        ],
        preamble="first line\nsecond line",
    )
    parsed = tomllib.loads(text)
    assert parsed["sources"]["my-source"]["compliance"] == {"a b": 1, "plain_key": "x"}
    assert parsed["weird.name"] == {"k": 1}
    assert text.startswith("# first line\n# second line\n\n# about the table\n# second line\n[")
    assert "# why this is here\nplain_key" in text and text.endswith("\n")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {"a": 1}, None, b"x", object()])
def test_a_value_toml_cannot_hold_is_a_bug_in_the_caller(value: object) -> None:
    with pytest.raises(ValueError, match="TOML"):
        dumps([Table(("t",), [("k", value)])])


def test_a_bool_is_written_as_a_bool_not_as_an_integer() -> None:
    assert "k = true" in dumps([Table(("t",), [("k", True)])])


# --- the preview ----------------------------------------------------------------------------------


def material(number: int, **fields: object) -> MaterialRecord:
    return MaterialRecord("demo", f"m-{number:03d}", **fields)  # type: ignore[arg-type]


OWNED = frozenset(spec.name for spec in MATERIAL_SCHEMA.columns)


def test_an_empty_sample_says_so() -> None:
    assert preview_lines([], MATERIAL_SCHEMA, owned=OWNED, sensitive=frozenset()) == [
        "  (no records)"
    ]


def test_the_table_has_the_id_the_first_owned_fields_and_an_issue_count() -> None:
    record = material(
        1, code="C-1", name="Plain weave", weight_gsm=150.0, issues=("w:zero", "c:unk")
    )
    lines = preview_lines([record], MATERIAL_SCHEMA, owned=OWNED, sensitive=frozenset())
    header = lines[0].split()
    assert header[0] == "source_id" and header[-1] == "issues"
    assert len(header) <= MAX_COLUMNS + 1
    row = lines[2]
    assert "m-001" in row and "C-1" in row and "Plain weave" in row and row.split()[-1] == "2"
    assert all(line == line.rstrip() for line in lines), "no trailing spaces"


def test_a_field_marked_sensitive_is_not_shown_and_is_named_as_hidden() -> None:
    record = material(1, code="C-1", name="Plain weave", supplier="Secret Supplier Ltd")
    lines = preview_lines(
        [record], MATERIAL_SCHEMA, owned=OWNED, sensitive=frozenset({"supplier", "name"})
    )
    text = "\n".join(lines)
    assert "Secret Supplier Ltd" not in text and "Plain weave" not in text
    assert "hidden, marked sensitive in the mapping: name, supplier" in text


def test_a_field_the_mapping_does_not_own_is_not_shown() -> None:
    record = material(1, code="C-1", name="Plain weave")
    lines = preview_lines(
        [record], MATERIAL_SCHEMA, owned=frozenset({"code"}), sensitive=frozenset()
    )
    assert "Plain weave" not in "\n".join(lines) and "C-1" in "\n".join(lines)


def test_long_values_are_cut_and_a_token_in_a_value_is_masked() -> None:
    token = "Bearer " + "Ab1" * 8
    record = material(1, code="C" * 80, name=token)
    lines = preview_lines([record], MATERIAL_SCHEMA, owned=OWNED, sensitive=frozenset())
    text = "\n".join(lines)
    assert "Ab1Ab1Ab1" not in text, "masked by the same redaction as every other output"
    assert "C" * 80 not in text and "..." in text
    assert all(
        len(cell) <= CELL_WIDTH for line in lines[2:3] for cell in line.split("  ") if cell.strip()
    )


def test_only_the_first_rows_are_shown_and_the_rest_counted() -> None:
    records = [material(n, code=f"C-{n}") for n in range(MAX_ROWS + 3)]
    lines = preview_lines(records, MATERIAL_SCHEMA, owned=OWNED, sensitive=frozenset())
    assert sum(1 for line in lines if line.strip().startswith("m-")) == MAX_ROWS
    assert lines[-1] == "  ... and 3 more record(s)"


def test_money_compositions_lists_and_pictures_are_readable() -> None:
    record = material(
        1,
        price=Money(Decimal("12.50"), "USD"),
        composition=(Composition("Cotton", 60.0, "60%CO"), Composition(None, 40.0, "40%XX")),
        use_cases=("shirt", "dress"),
        images=(ImageRef(role="thumb"), ImageRef(role="swatch")),
    )
    names = ("price", "composition", "use_cases", "images")
    lines = preview_lines([record], MATERIAL_SCHEMA, owned=frozenset(names), sensitive=frozenset())
    text = "\n".join(lines)
    assert "12.50 USD" in text and "60%CO, 40%XX" in text and "shirt, dress" in text
    assert "2 image(s)" in text
