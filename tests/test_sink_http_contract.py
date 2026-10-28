"""The HTTP sink against the reference platform: the shared contract suite, and its mutants.

Every check of ``catalog_sync.testing.sink_contract`` runs with the sink talking, over loopback, to
a ``PlatformServer`` that implements docs/platform-contract.md and stores into SQLite. The rows are
read back from that database, so the whole chain is under test: the sink's requests, the wire
format, the receiver's validation and its partial upsert.

The suite must also be able to fail for this sink (CODING-STYLE R.7): each mutant below breaks one
rule of the contract on the sink's side, and the check that guards it must turn red.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from catalog_sync.http import HttpSeams
from catalog_sync.mock.platform_server import PlatformServer
from catalog_sync.sinks.base import BackupReceipt, Sink
from catalog_sync.sinks.http import HttpSink, HttpSinkSettings
from catalog_sync.sinks.sqlite import read_table
from catalog_sync.testing.sink_contract import (
    CONTRACT_CHECKS,
    GENERIC_SHAPE,
    ContractShape,
    HarnessFactory,
    Rows,
    SinkHarness,
)
from tests.sink_harnesses import MUTANTS, SHAPES, Mutant, check_fails

pytestmark = pytest.mark.mock

TOKEN = "contract-canary-" + "0c4e8a"
# The mutants that break a rule on the sink's side. The two that reach into a local file
# (handing out the live file as a backup, deleting rows from it) have no counterpart here: the
# platform owns the rows, and its own deleting is what the receiver tests above cover.
NOT_FOR_HTTP = {
    "hands out the live file as its backup",
    "deletes rows the source no longer returns",
}
HTTP_MUTANTS = [m for m in MUTANTS if m[0] not in NOT_FOR_HTTP]


class Platforms:
    """The receivers a test started, stopped together when the test ends."""

    def __init__(self) -> None:
        self._servers: list[PlatformServer] = []

    def start(self, db_path: Path) -> PlatformServer:
        server = PlatformServer(db_path, token=TOKEN).start()
        self._servers.append(server)
        return server

    def stop_all(self) -> None:
        for server in self._servers:
            server.stop()


@pytest.fixture
def platforms() -> Iterator[Platforms]:
    pool = Platforms()
    yield pool
    pool.stop_all()


def http_harness(
    platforms: Platforms,
    shape: ContractShape = GENERIC_SHAPE,
    wrap: Callable[[HttpSink], Sink] | None = None,
) -> HarnessFactory:
    def make(workdir: Path) -> SinkHarness:
        server = platforms.start(workdir / "platform.db")
        settings = HttpSinkSettings(url=server.ingest_url, auth_header_env="PLATFORM_TOKEN")
        real = HttpSink(settings, environ={"PLATFORM_TOKEN": TOKEN}, seams=HttpSeams())
        sink = wrap(real) if wrap else real

        def read_rows() -> Rows:
            return read_table(workdir / "platform.db", shape.label)

        def read_backup(_receipt: BackupReceipt) -> Rows:
            raise AssertionError("an http sink takes no backup, so there is nothing to read back")

        return SinkHarness(sink, read_rows, read_backup, shape)

    return make


@pytest.mark.parametrize("shape", SHAPES.values(), ids=SHAPES.keys())
@pytest.mark.parametrize("check", CONTRACT_CHECKS, ids=lambda check: check.__name__)
def test_the_http_sink_honours_the_contract_against_the_reference_platform(
    shape: ContractShape,
    check: Callable[[HarnessFactory, Path], None],
    tmp_path: Path,
    platforms: Platforms,
) -> None:
    check(http_harness(platforms, shape), tmp_path)


def failing_checks(tmp_path: Path, platforms: Platforms, build: Callable[..., Mutant]) -> set[str]:
    failed: set[str] = set()
    for number, check in enumerate(CONTRACT_CHECKS):
        factory = http_harness(platforms, wrap=lambda sink: build(sink, "http"))
        if check_fails(check, factory, tmp_path / str(number)):
            failed.add(check.__name__)
    return failed


def test_the_real_http_sink_fails_no_check(tmp_path: Path, platforms: Platforms) -> None:
    def honest(sink: HttpSink, name: str) -> Mutant:
        return Mutant(sink, name)  # type: ignore[arg-type]

    assert failing_checks(tmp_path, platforms, honest) == set()


@pytest.mark.parametrize(
    ("build", "caught_by"),
    [(build, caught_by) for _, build, caught_by in HTTP_MUTANTS],
    ids=[description for description, _, _ in HTTP_MUTANTS],
)
def test_each_contract_violation_turns_the_intended_check_red_for_the_http_sink(
    build: Callable[..., Mutant], caught_by: str, tmp_path: Path, platforms: Platforms
) -> None:
    assert caught_by in failing_checks(tmp_path, platforms, build)


# --- what only the platform can show --------------------------------------------------------------


def test_a_dry_run_reaches_the_platform_with_nothing(tmp_path: Path, platforms: Platforms) -> None:
    from catalog_sync.testing.sink_contract import contract_batch, contract_row

    harness = http_harness(platforms)(tmp_path)
    harness.sink.open()
    harness.sink.upsert(contract_batch([contract_row("A", name="a")]), dry_run=True)
    [server] = platforms._servers
    assert server.service.events == [], "not one request reached the platform"


def test_loading_the_same_batch_again_is_a_replay_the_platform_answers_from_its_store(
    tmp_path: Path, platforms: Platforms
) -> None:
    from catalog_sync.testing.sink_contract import contract_batch, contract_row

    harness = http_harness(platforms)(tmp_path)
    harness.sink.open()
    harness.sink.backup()
    batch = contract_batch([contract_row("A", name="a")])
    first = harness.sink.upsert(batch)
    again = harness.sink.upsert(batch)
    assert (first.inserted, again.inserted, again.unchanged) == (1, 0, 1)
    [server] = platforms._servers
    assert [e.kind for e in server.service.events] == ["ingested", "replayed"]
