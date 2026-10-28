"""Regenerate examples/contract: the output contract, shown side by side, from a real run.

It does what tests/test_mock_e2e.py does, in this process: a Mock SaaS and the reference platform on
loopback, the real CLI (login, pull, normalize, load), a SQLite sink and the HTTP sink.
Everything it writes is something the tool printed, stored or received; the only invented parts
are the few raw responses the Mock SaaS is told to shape (see the *_SHAPES tables), and the README
it writes lists them.

Volatile values are made deterministic so that a test can regenerate the directory and compare it
byte for byte with the committed copy: a stepping clock (one minute per CLI call), fixed ports and
paths shown as placeholders, run-id suffixes and salted hashes masked.

    uv run python tools/make_contract_example.py [--out examples/contract]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from catalog_sync import cli
from catalog_sync.mock.data import MockMaterial
from catalog_sync.mock.platform_server import PlatformServer
from catalog_sync.mock.server import MockSaasServer
from catalog_sync.mock.service import MockOptions, MockService, Request, Response
from catalog_sync.sinks.sqlite import read_table

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "examples" / "contract"
MAPPINGS = REPO_ROOT / "src" / "catalog_sync" / "plugins" / "mock" / "mappings"

START = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
SAAS_PORT = 8765  # what `catalog-sync mock serve` listens on by default; the real port is masked
PLATFORM_PORT = 8780
PLATFORM_TOKEN = "example-platform-token"  # noqa: S105 — a public placeholder, not a credential
MATERIAL_COUNT = 8
SUPPLIER_COUNT = 4
EDITED_AT = "2026-01-02T00:00:00Z"  # mat-0001's change marker after the source edited it

_GONE = object()  # in a shape: delete this key from the response
FAILING_DETAIL = "mat-0007"  # its detail answers 404 in run 1
VANISHING = "mat-0008"  # gone from the listing in run 2
EDITED = "mat-0001"  # its pictures are removed upstream in run 2
# Raw responses the Mock SaaS is told to reshape, because it never sends a null or leaves a key
# out: material id (detail) or supplier id (listing row) -> {key: new value or _GONE}.
DETAIL_SHAPES: Mapping[str, Mapping[str, object]] = {
    "mat-0002": {"width": _GONE},  # the key is missing
    "mat-0003": {"width": None},  # the key is null
    "mat-0004": {"width": ""},  # the key is an empty string
    "mat-0005": {"width": "wide"},  # a value the width parser cannot read
}
SUPPLIER_SHAPES: Mapping[str, Mapping[str, object]] = {
    "sup-003": {"rating": ""},
    "sup-004": {"listed_at": "yesterday"},
}
MATERIAL_QUERY = (
    "SELECT source_id, code, weight_gsm, width_cm, price, supplier, issues, raw_ref "
    "FROM materials ORDER BY source_id"
)
SUPPLIER_QUERY = (
    "SELECT source_id, name, rating, lead_time_days, listed_at, issues FROM suppliers "
    "ORDER BY source_id"
)
_RUN_ID = re.compile(r"(\d{8}T\d{6}Z)-[0-9a-f]{6}")
_BACKUP_STAMP = re.compile(r"\.bak-\d{8}T\d{12}Z")
_SALTED_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_HOST_PATH = re.compile(r"/(?:Users|home|private)/")  # a home directory or a macOS temp root


# --- the Mock SaaS, with a few responses shaped ---------------------------------------------------


class ShapedService(MockService):
    """The Mock SaaS, answering as it always does except for the shapes named above, and keeping
    what it sent so that the example can show the raw response next to the record built from it."""

    def __init__(self, options: MockOptions) -> None:
        super().__init__(options)
        self.phase = 1
        self.list_rows: dict[tuple[int, str], object] = {}
        self.details: dict[tuple[int, str], object] = {}

    def handle(self, request: Request) -> Response:
        response = super().handle(request)
        if response.status != 200 or not request.path.startswith("/api/v1/"):
            return response
        detail = re.fullmatch(r"/api/v1/materials/(?P<id>[\w-]+)", request.path)
        if detail is not None:
            return self._detail(detail["id"], response)
        if request.path == "/api/v1/materials":
            return self._rows(
                "rows", response, {EDITED: ("thumbnail_url",)} if self.phase == 2 else {}
            )
        if request.path == "/api/v1/suppliers":
            return self._rows("items", response, {})
        return response

    def _detail(self, material_id: str, response: Response) -> Response:
        if material_id == FAILING_DETAIL and self.phase == 1:
            failed = {"state": False, "msg": "no such material"}
            self.details[(self.phase, material_id)] = {"http_status": 404, "body": failed}
            return Response(404, _compact(failed), response.content_type)
        body = json.loads(response.body)
        record = body["data"]["data"]
        for key, value in DETAIL_SHAPES.get(material_id, {}).items():
            _shape(record, key, value)
        if self.phase == 2 and material_id == EDITED:
            record.pop("swatch_url")
        self.details[(self.phase, material_id)] = {"http_status": 200, "body": body}
        return replace(response, body=_compact(body))

    def _rows(
        self, name: str, response: Response, removals: Mapping[str, Sequence[str]]
    ) -> Response:
        body = json.loads(response.body)
        rows = body["data"][name]
        for row in rows:
            if row.get("type") == "folder":
                continue
            source = SUPPLIER_SHAPES if name == "items" else {}
            for key, value in source.get(row["id"], {}).items():
                _shape(row, key, value)
            for key in removals.get(row["id"], ()):
                row.pop(key)
            self.list_rows[(self.phase, row["id"])] = row
        return replace(response, body=_compact(body))

    def start_second_phase(self) -> None:
        """The source changes between the runs: one record edited, one record gone."""
        self.phase = 2
        kept = [m for m in self.dataset.materials if m.id != VANISHING]
        edited: list[MockMaterial] = [
            replace(m, updated_at=EDITED_AT) if m.id == EDITED else m for m in kept
        ]
        self.dataset = replace(self.dataset, materials=tuple(edited))


def _shape(row: dict[str, Any], key: str, value: object) -> None:
    if value is _GONE:
        row.pop(key)
    else:
        row[key] = value


def _compact(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


# --- driving the CLI ------------------------------------------------------------------------------


class StepClock:
    """One instant per CLI call, a minute after the last: the same on every machine."""

    def __init__(self) -> None:
        self._now = START - timedelta(minutes=1)

    def advance(self) -> None:
        self._now += timedelta(minutes=1)

    def __call__(self) -> datetime:
        return self._now


@dataclass
class Step:
    command: str
    exit_code: int
    stdout: str
    stderr: str

    def text(self) -> str:
        lines = [f"$ catalog-sync {self.command}"]
        lines += self.stdout.splitlines()
        lines += [f"[stderr] {line}" for line in self.stderr.splitlines()]
        lines.append(f"[exit {self.exit_code}]")
        return "\n".join(lines) + "\n"


@dataclass
class Bench:
    work: Path
    service: ShapedService
    saas: MockSaasServer
    platform: PlatformServer
    clock: StepClock = field(default_factory=StepClock)

    @property
    def config(self) -> Path:
        return self.work / "catalog-sync.toml"

    @property
    def workspace(self) -> Path:
        return self.work / "workspace"

    def run(self, *args: str) -> Step:
        self.clock.advance()
        out, err = io.StringIO(), io.StringIO()
        argv = ["--config", str(self.config), "--workspace", str(self.workspace), *args]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return Step(" ".join(args), code, out.getvalue(), err.getvalue())

    def rows(self, table: str) -> dict[str, dict[str, object]]:
        found = read_table(self.work / "out" / "mock.db", table)
        return {source_id: row for (_, source_id), row in found.items()}

    def platform_rows(self, entity: str) -> dict[str, dict[str, object]]:
        found = read_table(self.work / "platform" / "platform.db", entity)
        return {source_id: row for (_, source_id), row in found.items()}


def config_text(base_url: str, platform_url: str) -> str:
    from tests.test_mock_e2e import MIN_GAP_S, compliance_block  # the Mock test helpers

    text = f"""[credentials.mock]
base_url = "{base_url}"

[sinks.materials-db]
kind = "sqlite"
path = "out/mock.db"
table = "materials"

[sinks.suppliers-db]
kind = "sqlite"
path = "out/mock.db"
table = "suppliers"

[sinks.suppliers-hashed]
kind = "sqlite"
path = "out/mock.db"
table = "suppliers_hashed"
sensitive_fields = "hash"

[sinks.platform]
kind = "http"
url = "{platform_url}/api/v1/ingest"
auth_header_env = "PLATFORM_TOKEN"
batch_size = 5
"""
    for name, page_size in (("mock-materials", 10), ("mock-suppliers", 4)):
        text += f"""
[sources.{name}]
plugin = "mock.{name.removeprefix("mock-")}"
credential = "mock"
base_url = "{base_url}"
page_size = {page_size}
request_gap_s = {MIN_GAP_S}
{compliance_block(name, base_url)}"""
    return text


@contextlib.contextmanager
def bench(work: Path) -> Iterator[Bench]:
    service = ShapedService(MockOptions(materials=MATERIAL_COUNT, suppliers=SUPPLIER_COUNT))
    saas = MockSaasServer(MockOptions(materials=MATERIAL_COUNT, suppliers=SUPPLIER_COUNT))
    saas.service = service  # before start(): the HTTP skin is built around it
    platform = PlatformServer(work / "platform" / "platform.db", token=PLATFORM_TOKEN)
    clock = StepClock()
    env = {"PLATFORM_TOKEN": PLATFORM_TOKEN, "PYTHON_DOTENV_DISABLED": "1"}
    with (
        saas,
        platform,
        mock.patch.dict(os.environ, env),
        mock.patch.object(cli, "Context", _context_with(clock)),
    ):
        for name in ("MOCK_SAAS_USER", "MOCK_SAAS_PASSWORD"):
            os.environ.pop(name, None)  # the demo account, whatever the shell holds
        made = Bench(work, service, saas, platform, clock)
        made.config.write_text(config_text(saas.base_url, platform.base_url), encoding="utf-8")
        yield made


def _context_with(ticking: StepClock) -> type[cli.Context]:
    """The CLI's context, with the stepping clock where the wall clock would be."""

    @dataclass(frozen=True)
    class Clocked(cli.Context):
        clock: Callable[[], datetime] = field(default=ticking)

    return Clocked


# --- collecting what the example shows ----------------------------------------------------------


def read_lines(path: Path) -> dict[str, dict[str, object]]:
    found: dict[str, dict[str, object]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        found[record["source_id"]] = record
    return found


def query(database: Path, sql: str) -> dict[str, object]:
    with contextlib.closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as conn:
        cursor = conn.execute(sql)
        names = [column[0] for column in cursor.description]
        return {"sql": sql, "columns": names, "rows": [list(row) for row in cursor.fetchall()]}


def dump(payload: object) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def pulled_run(step: Step) -> str:
    found = re.search(r"^run (\S+) \(", step.stdout, re.MULTILINE)
    if found is None:
        raise RuntimeError(f"no run id in the output of `{step.command}`")
    return found[1]


@dataclass
class Capture:
    """The state of one entity after one run: what the sinks hold and what was normalized."""

    normalized: dict[str, dict[str, object]]
    sqlite: dict[str, dict[str, object]]
    platform: dict[str, dict[str, object]]
    hashed: dict[str, dict[str, object]] = field(default_factory=dict)


def load_all(
    tool: Bench, source: str, mapping: str, out: Path, sinks: Sequence[str]
) -> tuple[list[Step], str]:
    steps = [tool.run("pull", source)]
    run_id = pulled_run(steps[0])
    out.parent.mkdir(parents=True, exist_ok=True)
    steps.append(
        tool.run("normalize", source, "--mapping", str(MAPPINGS / mapping), "--out", str(out))
    )
    steps += [tool.run("load", "--sink", sink, "--input", str(out)) for sink in sinks]
    return steps, run_id


def add_platform_owned_column(tool: Bench) -> None:
    """What a platform adds by itself: a column no mapping owns, with a value in it."""
    for path, table in (
        (tool.work / "out" / "mock.db", "materials"),
        (tool.work / "platform" / "platform.db", "material"),
    ):
        with contextlib.closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN ops_note TEXT')
            conn.execute(
                f'UPDATE "{table}" SET ops_note = ? WHERE source_id = ?',  # noqa: S608 — literal names
                ("checked by hand, keep", EDITED),
            )


def build(out_dir: Path) -> Path:
    """Run the whole scenario and write the example to `out_dir` (replacing it)."""
    with tempfile.TemporaryDirectory(prefix="contract-example-") as raw:
        work = Path(raw).resolve()
        files = _scenario(work)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    for relative, text in sorted(files.items()):
        target = out_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return out_dir


def _scenario(work: Path) -> dict[str, str]:
    outputs: dict[str, str] = {}
    with bench(work) as tool:
        tool.run("login", "mock", "--form")  # first, and not shown: its output names a random token
        material_sinks = ("materials-db", "platform")
        first, run1 = load_all(
            tool,
            "mock-materials",
            "materials.yaml",
            work / "run-1" / "normalized.jsonl",
            material_sinks,
        )
        first.append(tool.run("runs", "show", run1))
        after1 = Capture(
            read_lines(work / "run-1" / "normalized.jsonl"),
            tool.rows("materials"),
            tool.platform_rows("material"),
        )
        add_platform_owned_column(tool)
        tool.service.start_second_phase()
        second, run2 = load_all(
            tool,
            "mock-materials",
            "materials.yaml",
            work / "run-2" / "normalized.jsonl",
            material_sinks,
        )
        second.append(tool.run("runs", "show", run2))
        after2 = Capture(
            read_lines(work / "run-2" / "normalized.jsonl"),
            tool.rows("materials"),
            tool.platform_rows("material"),
        )
        supplier_steps, _ = load_all(
            tool,
            "mock-suppliers",
            "suppliers.yaml",
            work / "suppliers" / "normalized.jsonl",
            ("suppliers-db", "suppliers-hashed", "platform"),
        )
        suppliers = Capture(
            read_lines(work / "suppliers" / "normalized.jsonl"),
            tool.rows("suppliers"),
            tool.platform_rows("supplier"),
            tool.rows("suppliers_hashed"),
        )
        outputs.update(_material_files(tool, work, first, second, after1, after2))
        outputs.update(_supplier_files(tool, work, supplier_steps, suppliers))
        outputs["audit.jsonl"] = _audit(tool)
        outputs["catalog-sync.toml"] = tool.config.read_text(encoding="utf-8")
        outputs["README.md"] = README
        scrubber = _Scrubber(work, tool.saas.base_url, tool.platform.base_url)
    return {name: scrubber.scrub(text) for name, text in outputs.items()}


def _material_files(
    tool: Bench, work: Path, first: list[Step], second: list[Step], one: Capture, two: Capture
) -> dict[str, str]:
    files: dict[str, str] = {}
    database = work / "out" / "mock.db"
    for number, steps, state in ((1, first, one), (2, second, two)):
        base = f"materials/run-{number}"
        files[f"{base}/transcript.txt"] = "".join(step.text() for step in steps)
        for name in ("normalized.jsonl", "normalized.jsonl.meta.json"):
            files[f"{base}/{name}"] = (work / f"run-{number}" / name).read_text(encoding="utf-8")
        files[f"{base}/platform-rows.json"] = dump(list(state.platform.values()))
        files[f"{base}/sqlite-rows.json"] = dump(list(state.sqlite.values()))
    # The query result after run 2 is the one at the end of the scenario, the database as it is now.
    files["materials/run-2/sqlite-query.json"] = dump(query(database, MATERIAL_QUERY))
    for item_id in sorted(set(one.normalized) | set(two.normalized)):
        files[f"materials/cases/{item_id}.json"] = dump(_material_case(tool, item_id, one, two))
    return files


def _material_case(tool: Bench, item_id: str, one: Capture, two: Capture) -> dict[str, object]:
    case: dict[str, object] = {}
    for number, state in ((1, one), (2, two)):
        case[f"run_{number}"] = {
            "raw_list_row": tool.service.list_rows.get((number, item_id)),
            "raw_detail_response": tool.service.details.get((number, item_id)),
            "normalized_record": state.normalized.get(item_id),
            "sqlite_row": state.sqlite.get(item_id),
            "platform_row": state.platform.get(item_id),
        }
    return case


def _supplier_files(tool: Bench, work: Path, steps: list[Step], state: Capture) -> dict[str, str]:
    files = {"suppliers/transcript.txt": "".join(step.text() for step in steps)}
    for name in ("normalized.jsonl", "normalized.jsonl.meta.json"):
        files[f"suppliers/{name}"] = (work / "suppliers" / name).read_text(encoding="utf-8")
    files["suppliers/sqlite-query.json"] = dump(query(work / "out" / "mock.db", SUPPLIER_QUERY))
    files["suppliers/platform-rows.json"] = dump(list(state.platform.values()))
    for item_id, record in state.normalized.items():
        files[f"suppliers/cases/{item_id}.json"] = dump(
            {
                "raw_list_row": tool.service.list_rows.get((1, item_id)),
                "normalized_record": record,
                "sqlite_row_default_policy_drop": state.sqlite.get(item_id),
                "sqlite_row_policy_hash": state.hashed.get(item_id),
                "platform_row": state.platform.get(item_id),
            }
        )
    return files


def _audit(tool: Bench) -> str:
    """The audit log, minus the two hashes that depend on the port and the token this run drew."""
    lines = []
    for raw in (tool.workspace / "audit.jsonl").read_text(encoding="ascii").splitlines():
        entry = json.loads(raw)
        for key in ("compliance_sha256", "credential_hash"):
            if entry[key] is not None:
                entry[key] = "<masked>"
        lines.append(json.dumps(entry, ensure_ascii=True))
    return "\n".join(lines) + "\n"


@dataclass
class _Scrubber:
    work: Path
    saas_url: str
    platform_url: str

    def scrub(self, text: str) -> str:
        replaced = text.replace(self.saas_url, f"http://127.0.0.1:{SAAS_PORT}")
        replaced = replaced.replace(self.platform_url, f"http://127.0.0.1:{PLATFORM_PORT}")
        replaced = replaced.replace(f"{self.work}/", "").replace(str(self.work), "<work>")
        replaced = replaced.replace(f"{REPO_ROOT}/", "")
        replaced = _RUN_ID.sub(r"\1-xxxxxx", replaced)
        replaced = _BACKUP_STAMP.sub(".bak-<UTC-STAMP>", replaced)
        replaced = _SALTED_HASH.sub("sha256:<salted-hash>", replaced)
        if _HOST_PATH.search(replaced) or str(self.work) in replaced:
            raise RuntimeError("a host path is left in the example output")
        return replaced


README = """# Output contract, side by side

Generated by `tools/make_contract_example.py`; a test regenerates it and fails on any difference,
so it cannot drift from the code. It is a real run: the Mock SaaS and the reference platform on
loopback, the real CLI, a SQLite sink and the HTTP sink. See `docs/data-contract.md`.

## Layout

| Path | What it is |
|---|---|
| `catalog-sync.toml` | The config the run used (ports masked). |
| `audit.jsonl` | The audit log of the whole scenario. |
| `materials/run-1/`, `materials/run-2/` | Two runs of the materials source: the commands and what they printed (`transcript.txt`), the normalized file and its sidecar, the SQLite rows and the rows the reference platform stored after the run. |
| `materials/cases/<id>.json` | One material, run 1 and run 2: the raw list row, the raw detail response, the normalized record, the SQLite row and the platform row, side by side. |
| `suppliers/` | The same for the second entity, one run, loaded into three sinks (default policy `drop`, policy `hash`, and the platform). `cases/<id>.json` per supplier. |

## What each material shows

| Item | Shows |
|---|---|
| `mat-0001` | Clean record. Run 2: edited upstream, pictures removed: `images` is empty and keeps the stored value (`on_null: keep_existing`), a platform-owned column `ops_note` survives both loads. |
| `mat-0002` ... `mat-0005` | The four cases for `width_cm`: key missing, `null`, `""`, `"wide"` (unparseable). Only the last has an issue. |
| `mat-0006` | A fibre code the code table lacks: `composition:unknown_code:ZZ`. |
| `mat-0007` | Its detail request answers 404 in run 1: the item is isolated, listed by `runs show`, absent from every sink. In run 2 it succeeds and is inserted. |
| `mat-0008` | `weight_gsm:source_zero`. Gone from the source in run 2: reported as no longer in the source, row kept. |

Suppliers: `sup-001` has a sensitive `contact_email` (plain in the normalized file, dropped in the
default SQLite table and the platform, hashed in `suppliers_hashed`); `sup-003` has `rating: ""`;
`sup-004` has `listed_at: "yesterday"` (unparseable).

## What is shaped, and what is masked

The Mock SaaS never sends a null or leaves a key out, so the run tells it to reshape: the `width` of
`mat-0002` ... `mat-0005` in the detail response; the `thumbnail_url` of `mat-0001`'s listing row and
its `swatch_url` in run 2; the detail of `mat-0007` (404) in run 1; `rating` of `sup-003` and
`listed_at` of `sup-004`. Run 2 also edits `mat-0001`'s change marker and drops `mat-0008`. The
`raw_*` fields in the case files are what the tool received after that.

Masked so the files are the same on every machine: ports (`8765` is the Mock SaaS, `8780` the
platform), the random suffix of run ids (`xxxxxx`), the backup stamp, the salted hashes, and the
`compliance_sha256` and `credential_hash` of the audit lines. The clock is fixed, one minute per
command, from 2026-10-27T09:00:00Z.
"""  # noqa: E501 — Markdown table rows cannot be wrapped


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(REPO_ROOT))  # for `tests.*`, which is not an installed package
    print(f"wrote {build(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
