"""docs/data-contract.md says what the code does: its issue codes, fields and files are checked.

The checks read the code, not a list kept here: the issue codes are found by walking the syntax
tree of src/catalog_sync for the strings appended to an `issues` list, and the fields come from the
two shipped schema files. Each check is also run on a mutated copy (a code left out of the doc, a
field renamed, a new code added to the source) and must fail there, so a check that has stopped
looking cannot pass for a clean doc.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from catalog_sync.schema import (
    EXTRA_FIELD,
    IDENTITY_FIELDS,
    PROVENANCE_FIELDS,
    EntitySchema,
    builtin_schema,
    load_schema_file,
)
from tests.conftest import REPO_ROOT

DOC = REPO_ROOT / "docs" / "data-contract.md"
SRC = REPO_ROOT / "src" / "catalog_sync"
EXAMPLE = REPO_ROOT / "examples" / "contract"
SUPPLIER_SCHEMA = SRC / "plugins" / "mock" / "schemas" / "supplier.yaml"
# The lists the engine and the parsers append issue codes to.
ISSUE_LISTS = frozenset({"issues", "leftover"})
KNOWN_CODES = {
    "<>:unparseable",
    "<>:source_zero",
    "detail:missing",
    "composition:unknown_code:<>",
    "composition:yarn_count_ignored:<>",
    "composition:sum_over_100",
    "images:<>:not_a_url",
    "images:<>:unreadable",
}
_PLACEHOLDER = re.compile(r"<[^>]*>")
_BACKTICKED = re.compile(r"`([^`\n]+)`")


# --- issue codes ----------------------------------------------------------------------------------


def template(node: ast.expr) -> str | None:
    """The text an expression appends, with every interpolated part shown as `<>`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "<>" for part in node.values
        )
    return None


def is_issue_append(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"append", "extend"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in ISSUE_LISTS
        and len(node.args) == 1
    )


def emitted_codes(root: Path) -> set[str]:
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if is_issue_append(node):
                assert isinstance(node, ast.Call)
                code = template(node.args[0])
                if code is not None:  # `extend(parsed.issues)` forwards codes found elsewhere
                    found.add(code)
    return found


def documented_codes(doc: str) -> set[str]:
    return {_PLACEHOLDER.sub("<>", span) for span in _BACKTICKED.findall(doc)}


def undocumented_codes(doc: str, root: Path) -> set[str]:
    return emitted_codes(root) - documented_codes(doc)


def test_the_scan_still_finds_the_codes_the_engine_and_the_parsers_emit() -> None:
    """A floor, so that a scan that stopped matching cannot pass for 'nothing is undocumented'."""
    assert emitted_codes(SRC) >= KNOWN_CODES


def test_every_issue_code_the_code_can_emit_is_in_the_doc() -> None:
    assert undocumented_codes(DOC.read_text(encoding="utf-8"), SRC) == set()


def test_a_code_left_out_of_the_doc_is_noticed() -> None:
    doc = DOC.read_text(encoding="utf-8")
    for code in ("`composition:sum_over_100`", "`images:<role>:unreadable`", "`detail:missing`"):
        assert code in doc
        assert undocumented_codes(doc.replace(code, "`x`"), SRC) != set()


def test_a_new_code_in_the_source_is_noticed(tmp_path: Path) -> None:
    (tmp_path / "engine.py").write_text(
        'issues = []\nissues.append(f"images:{role}:brand_new")\nissues.append("late:code")\n',
        encoding="utf-8",
    )
    doc = DOC.read_text(encoding="utf-8")
    assert undocumented_codes(doc, tmp_path) == {"images:<>:brand_new", "late:code"}


# --- fields ---------------------------------------------------------------------------------------


def table_rows(doc: str, heading: str) -> dict[str, str]:
    """Field name -> kind cell, for the rows of the table under `### heading`."""
    lines = doc.splitlines()
    start = lines.index(f"### {heading}") + 1
    rows: dict[str, str] = {}
    for line in lines[start:]:
        if line.startswith(("## ", "### ")):
            break
        if line.startswith("| `"):
            cells = [cell.strip().strip("`") for cell in line.strip("| ").split("|")]
            rows[cells[0]] = cells[1]
    return rows


def kind_cell(schema: EntitySchema, name: str) -> str:
    spec = schema.spec(name)
    assert spec is not None
    return spec.kind.value if spec.nested is None else f"{spec.kind.value}: {spec.nested.value}"


def missing_fields(doc: str, heading: str, schema: EntitySchema) -> list[str]:
    rows = table_rows(doc, heading)
    return [
        spec.name for spec in schema.fields if rows.get(spec.name) != kind_cell(schema, spec.name)
    ]


def test_every_field_of_both_entity_schemas_is_in_its_table_with_its_kind() -> None:
    doc = DOC.read_text(encoding="utf-8")
    assert missing_fields(doc, "Material fields", builtin_schema("material")) == []
    assert missing_fields(doc, "Supplier fields", load_schema_file(SUPPLIER_SCHEMA)) == []


def test_the_tables_hold_no_field_the_schemas_lack() -> None:
    doc = DOC.read_text(encoding="utf-8")
    for heading, schema in (
        ("Material fields", builtin_schema("material")),
        ("Supplier fields", load_schema_file(SUPPLIER_SCHEMA)),
    ):
        assert set(table_rows(doc, heading)) == {spec.name for spec in schema.fields}


def test_a_missing_or_changed_field_row_is_noticed() -> None:
    doc = DOC.read_text(encoding="utf-8")
    material = builtin_schema("material")
    supplier = load_schema_file(SUPPLIER_SCHEMA)
    assert missing_fields(
        doc.replace("| `width_cm` |", "| `width_in` |"), "Material fields", material
    ) == ["width_cm"]
    assert missing_fields(
        doc.replace("| `price` | `json: money` |", "| `price` | `real` |"),
        "Material fields",
        material,
    ) == ["price"]
    assert missing_fields(
        doc.replace("| `listed_at` | timestamp |", "| `listed_at` | text |"),
        "Supplier fields",
        supplier,
    ) == ["listed_at"]


def test_every_identity_extra_and_provenance_field_has_a_row() -> None:
    rows = table_rows(
        DOC.read_text(encoding="utf-8"), "Identity, extra and provenance (every entity)"
    )
    assert set(rows) == {*IDENTITY_FIELDS, EXTRA_FIELD, *PROVENANCE_FIELDS}


def test_every_key_of_the_sidecar_is_described() -> None:
    sidecar = json.loads((EXAMPLE / "materials/run-1/normalized.jsonl.meta.json").read_text())
    section = (
        DOC.read_text(encoding="utf-8").split("## 2. The normalized file")[1].split("## 3.")[0]
    )
    described = {cell.strip("`") for cell in re.findall(r"^\| (`\w+`) \|", section, re.MULTILINE)}
    assert set(sidecar) <= described


# --- the files the doc points at ------------------------------------------------------------------


def test_every_path_and_link_in_the_doc_exists() -> None:
    doc = DOC.read_text(encoding="utf-8")
    roots = ("examples/contract/", "src/catalog_sync/", "tools/", "docs/")
    paths = [span for span in _BACKTICKED.findall(doc) if span.startswith(roots)]
    assert paths, "the doc names the files it is about"
    for path in paths:
        assert (REPO_ROOT / path).exists(), path
    for link in re.findall(r"\]\((?!http|#)([^)#]+)", doc):
        assert (DOC.parent / link).exists(), link
