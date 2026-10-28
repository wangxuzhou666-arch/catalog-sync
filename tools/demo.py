"""Run the installed package against synthetic services and export a verified report only."""

from __future__ import annotations

import html
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from catalog_sync import __version__
from catalog_sync.sinks.sqlite import read_table

WORK = Path("/data")
FIELDS = ("source", "source_id", "code", "name", "weight_gsm", "width_cm", "price", "composition")


def command(*args: str) -> None:
    """Stop the demo at the first failed command, rather than producing a success page."""
    print("> catalog-sync " + " ".join(args), flush=True)
    subprocess.run(["catalog-sync", *args], check=True, timeout=180)


def select_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in FIELDS}


def verify_records(
    records: list[dict[str, Any]],
    local: dict[Any, dict[str, Any]],
    receiver: dict[Any, dict[str, Any]],
    suppliers: dict[Any, dict[str, Any]],
) -> None:
    """Check identities and mapped values, not just a successful process exit."""
    if (len(records), len(local), len(receiver), len(suppliers)) != (24, 24, 24, 9):
        raise ValueError("Expected 24 materials in both destinations and 9 local suppliers.")
    keys = {(row["source"], row["source_id"]) for row in records}
    if len(keys) != len(records) or keys != set(local) or keys != set(receiver):
        raise ValueError("Missing, duplicated or mismatched source identities.")
    for row in records:
        key = (row["source"], row["source_id"])
        expected = select_fields(row)
        if select_fields(local[key]) != expected or select_fields(receiver[key]) != expected:
            raise ValueError("Stored fields differ from normalized records.")


def stored_rows() -> tuple[dict[Any, Any], dict[Any, Any], dict[Any, Any]]:
    local = read_table(WORK / "out/mock.db", "materials")
    receiver = read_table(WORK / "out/platform.db", "material")
    suppliers = read_table(WORK / "out/mock.db", "suppliers")
    return local, receiver, suppliers


def report_html(report: dict[str, Any]) -> str:
    """Render only selected synthetic fields; never copy the credential or raw-cache directories."""

    def pretty(value: object) -> str:
        return html.escape(json.dumps(value, ensure_ascii=False, indent=2))

    cards = "".join(
        f"<section><h2>{title}</h2><pre>{pretty(report[key])}</pre></section>"
        for title, key in [
            ("1. Source response", "raw"),
            ("2. Normalized record", "normalized"),
            ("3. Imported record", "stored"),
        ]
    )
    rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(row.get(k)))}</td>"
            for k in ("source_id", "code", "name", "weight_gsm", "width_cm")
        )
        + "</tr>"
        for row in report["sample_rows"]
    )
    return (
        """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>catalog-sync demo results</title>
<style>body{font:16px/1.6 system-ui,sans-serif;color:#20372e;background:#f3f6f4;margin:0}
main{max-width:1160px;margin:auto;padding:36px}h1{font-size:36px;line-height:1.2}h2{font-size:19px}
.badge{color:#175b38;background:#d7efdf;padding:6px 12px;border-radius:20px}.cards{display:grid;
grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}section{background:white;padding:20px;
border:1px solid #d9e2dc;border-radius:12px;margin:16px 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;
font-size:13px}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:10px;
border-bottom:1px solid #d9e2dc}.scroll{overflow:auto}a{color:#156749}
@media(max-width:850px){.cards{grid-template-columns:1fr}main{padding:20px}}</style></head><body><main>
<span class="badge">PASS · synthetic data only</span>
<h1>From external data to your application.</h1>
<p>The installed package collected, normalized and imported the data
in an isolated Docker container.</p>
"""
        + f"<p>Version {html.escape(report['version'])} · {html.escape(report['completed_at'])}</p>"
        + """
<section><h2>Verified results</h2><p><strong>24 materials + 9 suppliers</strong> in SQLite.
<strong>24 matching materials</strong> in the HTTP receiver.</p>
<p>All 24 material identities and selected mapped fields match both destinations.
Importing the same file again left both stores unchanged.</p></section>
"""
        + f"<p>Data quality notes recorded: {report['data_quality_notes']}. "
        "The synthetic dataset includes values that need attention; a completed import does not "
        "mean every source value is clean.</p>"
        + '<div class="cards">'
        + cards
        + """</div>
<p>Raw values above came from this run's synthetic source. The middle card shows parsed units and
structured values; the final card was read back from SQLite and checked against the HTTP receiver.
Missing values, where present, remain explicit; the pipeline does not invent values.</p>
<section><h2>Imported records: first five materials</h2><div class="scroll"><table><thead><tr>
<th>Source ID</th><th>Code</th><th>Name</th><th>Weight (gsm)</th><th>Width (cm)</th>
</tr></thead><tbody>"""
        + rows
        + """</tbody></table></div></section>
<p><a href="report.json">Download the machine-readable verification report</a></p>
<p>The demo container and its temporary data are removed by the launcher. The report and cached
Docker image remain. Real services require authorization, a compatible connector
and destination configuration.</p>
</main></body></html>"""
    )


def export_report(records: list[dict[str, Any]], local: dict[Any, Any]) -> None:
    sample = next(row for row in records if row["source_id"] == "mat-0002")
    details = list((WORK / "cache/runs").glob("*/detail/mat-0002.json"))
    if len(details) != 1:
        raise ValueError("Expected exactly one raw sample from this fresh demo run.")
    raw = json.loads(details[0].read_text())
    report = {
        "status": "PASS",
        "version": __version__,
        "completed_at": datetime.now(UTC).isoformat(),
        "counts": {"materials": 24, "suppliers": 9, "receiver_materials": 24},
        "matching_material_records": 24,
        "data_quality_notes": sum(len(row.get("issues", [])) for row in records),
        "repeat_import_unchanged": True,
        "raw": {
            key: raw.get(key)
            for key in ("id", "weight", "width", "price", "currency", "composition")
        },
        "normalized": select_fields(sample),
        "stored": select_fields(local[(sample["source"], sample["source_id"])]),
        "sample_rows": [select_fields(row) for _, row in sorted(local.items())[:5]],
    }
    dest = WORK / "report"
    dest.mkdir()
    (dest / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (dest / "index.html").write_text(report_html(report))
    print(
        "\nPASS: 24 materials, 9 suppliers; 24 matching HTTP records; repeat import unchanged.",
        flush=True,
    )


def run_demo() -> None:
    os.chdir(WORK)
    os.environ["CATALOG_SYNC_HOME"] = str(WORK)
    os.environ["PLATFORM_TOKEN"] = "demo-platform-token"  # noqa: S105 - public synthetic-service value
    template = Path("/demo/template.toml").read_text()
    (WORK / "catalog-sync.toml").write_text(template)
    command("mock", "serve", "--detach", "--pid-file", "/data/mock.pid")
    command(
        "mock",
        "platform",
        "serve",
        "--detach",
        "--pid-file",
        "/data/platform.pid",
        "--db",
        "/data/out/platform.db",
    )
    command("login", "mock", "--form")
    command("pull", "mock-materials")
    command("normalize", "mock-materials", "--out", "/data/out/materials.jsonl")
    for sink in ("mock-materials-db", "mock-platform"):
        command("load", "--sink", sink, "--input", "/data/out/materials.jsonl")
    command("run", "mock-suppliers")
    records = [json.loads(line) for line in (WORK / "out/materials.jsonl").read_text().splitlines()]
    before = stored_rows()
    verify_records(records, *before)
    for sink in ("mock-materials-db", "mock-platform"):
        command("load", "--sink", sink, "--input", "/data/out/materials.jsonl")
    after = stored_rows()
    verify_records(records, *after)
    if before != after:
        raise ValueError("Repeat import changed stored records.")
    export_report(records, after[0])


def main() -> int:
    try:
        run_demo()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Demo failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
