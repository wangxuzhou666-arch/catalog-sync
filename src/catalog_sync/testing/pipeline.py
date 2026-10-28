"""Run a source through pull and normalize in a test, with nothing but a source and a mapping.

``pull_and_normalize`` is what a connector's end-to-end test needs and nothing more: the state
database and the run cache go into ``workdir``, the clock is fixed so records are reproducible, and
the result holds both the pull report (counts, failures) and the normalised records.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from catalog_sync.normalize.mapping import load_mapping
from catalog_sync.normalize.run import NormalizedRun, find_run, normalize_run
from catalog_sync.puller import Puller, PullReport
from catalog_sync.sources.base import Source
from catalog_sync.state import StateStore

FIXED_NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class PipelineResult:
    report: PullReport
    normalized: NormalizedRun

    def rows(self) -> dict[str, dict[str, object]]:
        """The normalised records as JSON-form dicts, by ``source_id``."""
        return {record.source_id: record.to_dict() for record in self.normalized.records}


def pull_and_normalize(source: Source, *, mapping: Path, workdir: Path) -> PipelineResult:
    """Pull everything `source` lists, then normalise the run with the mapping file."""
    runs_root = workdir / "runs"
    with StateStore(workdir / "state.sqlite") as store:
        report = Puller(source, store, runs_root).pull()
        run = find_run(store, [source.name], report.run_id)
        normalized = normalize_run(
            store, runs_root, run, load_mapping(mapping), clock=lambda: FIXED_NOW
        )
    return PipelineResult(report, normalized)
