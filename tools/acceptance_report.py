"""The acceptance record: what each step and scenario found, and how it is written down.

No process, no network and no file is touched here, so the rules that matter for an honest record
are tested without a run:

* a scenario that recorded no check proves nothing, so it is a FAIL, never a PASS;
* a NOT_RUN keeps its reason and is counted apart from the passes;
* host paths and the run's throw-away secrets are masked before anything is rendered.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

REPORT_FORMAT = 1
MAX_SHOWN_MISMATCHES = 20
_MIN_SECRET_LENGTH = 8  # a shorter "secret" would also match ordinary text
# Whatever else slipped through that looks like a user directory on this or another host.
_USER_DIRECTORY = re.compile(r"(?:/Users|/home)/[^/\s\"'`]+")

JsonValue = object  # anything json.dumps takes; kept loose on purpose, the report is a record


class Status(StrEnum):
    PASS = "PASS"  # noqa: S105 — a status name, not a credential
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


@dataclass(frozen=True)
class Check:
    """One thing a scenario asserted, with the observation behind it."""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class Table:
    """A table the record keeps whole (the licence report): a title, its columns and its rows."""

    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass
class Outcome:
    """What one step or scenario found. `status` follows from the checks unless it was not run."""

    id: str
    title: str
    checks: list[Check] = field(default_factory=list)
    evidence: dict[str, JsonValue] = field(default_factory=dict)
    tables: list[Table] = field(default_factory=list)
    not_run_reason: str | None = None
    error: str | None = None  # an exception that ended the scenario early

    @property
    def status(self) -> Status:
        if self.not_run_reason is not None:
            return Status.NOT_RUN
        if self.error is not None or not self.checks:
            return Status.FAIL  # no check recorded = nothing proven, which is not a pass
        return Status.PASS if all(check.passed for check in self.checks) else Status.FAIL

    @property
    def reason(self) -> str:
        """Why it is not a pass: the reason it was not run, the error, or the failed checks."""
        if self.not_run_reason is not None:
            return self.not_run_reason
        if self.error is not None:
            return self.error
        if not self.checks:
            return "no check was recorded"
        failed = [check.name for check in self.checks if not check.passed]
        return f"failed: {'; '.join(failed)}" if failed else ""


class Recorder:
    """What a scenario function writes into: checks, evidence, or a refusal to run."""

    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome

    def check(self, name: str, passed: bool, detail: str = "") -> bool:
        self.outcome.checks.append(Check(name, passed, detail))
        return passed

    def evidence(self, key: str, value: JsonValue) -> None:
        self.outcome.evidence[key] = value

    def table(self, title: str, columns: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
        self.outcome.tables.append(Table(title, tuple(columns), tuple(tuple(row) for row in rows)))


class NotRun(Exception):
    """Raised by a scenario that cannot run here. The reason is what the record shows."""


@dataclass(frozen=True)
class Report:
    provenance: Mapping[str, str | bool]
    steps: Sequence[Outcome]
    scenarios: Sequence[Outcome]

    def counts(self) -> dict[str, int]:
        everything = [*self.steps, *self.scenarios]
        return {s.value: sum(1 for o in everything if o.status is s) for s in Status}

    @property
    def overall(self) -> str:
        """FAIL when anything failed; PASS only when nothing was left out; else PARTIAL."""
        counts = self.counts()
        if counts[Status.FAIL]:
            return "FAIL"
        return "PARTIAL" if counts[Status.NOT_RUN] else "PASS"

    @property
    def exit_code(self) -> int:
        """Non-zero when anything failed. A NOT_RUN is listed, never counted as a pass."""
        return 1 if self.counts()[Status.FAIL] else 0


# --- masking ------------------------------------------------------------------------------------


class Masker:
    """Replaces host paths with placeholders and throw-away secrets with `<secret>`."""

    def __init__(self, paths: Sequence[tuple[Path, str]] = (), secrets: Sequence[str] = ()) -> None:
        pairs: dict[str, str] = {}
        for path, placeholder in paths:
            pairs[str(path)] = placeholder
            pairs[str(path.resolve())] = placeholder  # /var and /private/var are one place
        # Longest first, so that <out> wins over the home directory it sits in.
        self._paths = sorted(pairs.items(), key=lambda pair: len(pair[0]), reverse=True)
        self._secrets = [value for value in secrets if len(value) >= _MIN_SECRET_LENGTH]

    def text(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "<secret>")
        for real, placeholder in self._paths:
            text = text.replace(real, placeholder)
        return _USER_DIRECTORY.sub("<home>", text)

    def value(self, value: JsonValue) -> JsonValue:
        """The same JSON value with every string masked (keys too)."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {self.text(str(key)): self.value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value


# --- rendering ----------------------------------------------------------------------------------


def outcome_json(outcome: Outcome) -> dict[str, JsonValue]:
    return {
        "id": outcome.id,
        "title": outcome.title,
        "status": outcome.status.value,
        "reason": outcome.reason,
        "checks": [
            {"name": c.name, "passed": c.passed, "detail": c.detail} for c in outcome.checks
        ],
        "evidence": outcome.evidence,
        "tables": [
            {"title": t.title, "columns": list(t.columns), "rows": [list(r) for r in t.rows]}
            for t in outcome.tables
        ],
    }


def report_json(report: Report, masker: Masker) -> str:
    document: dict[str, JsonValue] = {
        "format": REPORT_FORMAT,
        "overall": report.overall,
        "counts": report.counts(),
        "provenance": dict(report.provenance),
        "steps": [outcome_json(o) for o in report.steps],
        "scenarios": [outcome_json(o) for o in report.scenarios],
    }
    masked = masker.value(document)
    return json.dumps(masked, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def report_markdown(report: Report, masker: Masker) -> str:
    counts = report.counts()
    lines = [
        "# Acceptance record",
        "",
        f"**Result: {report.overall}** "
        f"({counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['NOT_RUN']} NOT_RUN)",
        "",
        "A NOT_RUN is a check that did not happen. It is listed with its reason and is never a "
        "pass.",
        "",
        "## Provenance",
        "",
        *[f"- {key}: `{value}`" for key, value in report.provenance.items()],
        "",
        "## Steps",
        "",
        *_summary_table(report.steps),
        "",
        "## Scenarios",
        "",
        *_summary_table(report.scenarios),
        "",
        "## Details",
    ]
    for outcome in [*report.steps, *report.scenarios]:
        lines += _details(outcome)
    return masker.text("\n".join(lines)) + "\n"


def _summary_table(outcomes: Sequence[Outcome]) -> list[str]:
    rows = ["| ID | What | Status | Reason |", "|---|---|---|---|"]
    for outcome in outcomes:
        reason = _cell(outcome.reason) if outcome.status is not Status.PASS else ""
        rows.append(
            f"| {outcome.id} | {_cell(outcome.title)} | {outcome.status.value} | {reason} |"
        )
    return rows


def _details(outcome: Outcome) -> list[str]:
    lines = ["", f"### {outcome.id}: {outcome.title}: {outcome.status.value}", ""]
    if outcome.status is Status.NOT_RUN:
        return [*lines, f"Not run: {outcome.reason}"]
    if outcome.error is not None:
        lines += [f"Error: {outcome.error}", ""]
    if outcome.checks:
        lines += ["| Check | Result | Observed |", "|---|---|---|"]
        for check in outcome.checks:
            result = "pass" if check.passed else "FAIL"
            lines.append(f"| {_cell(check.name)} | {result} | {_cell(check.detail)} |")
        lines.append("")
    for key, value in outcome.evidence.items():
        lines.append(f"- {key}: `{_inline(value)}`")
    for table in outcome.tables:
        lines += ["", f"**{_cell(table.title)}**", "", _row(table.columns)]
        lines += [_row(["---"] * len(table.columns)), *[_row(r) for r in table.rows]]
    return lines


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _inline(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
