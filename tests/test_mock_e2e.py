"""The whole tool against a live Mock SaaS on loopback: login, pull, normalize, load, audit.

The server runs in this process on 127.0.0.1 and is the only host these tests can reach (the
`mock` marker). Every command is the real CLI; the SQLite rows it writes are compared with the
seeded dataset, which is the oracle.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from catalog_sync import cli, cli_credentials
from catalog_sync.credentials.jwt import decode_claims
from catalog_sync.credentials.store import read_values
from catalog_sync.http import RateLimiter
from catalog_sync.mock.server import MockSaasServer
from catalog_sync.mock.service import MockOptions, RateLimit
from catalog_sync.sinks.sqlite import read_table
from tests.conftest import REPO_ROOT
from tests.fixtures.mock_saas import expected_material, expected_supplier, picked

pytestmark = pytest.mark.mock

MAPPINGS = REPO_ROOT / "src/catalog_sync/plugins/mock/mappings"
COMPLIANCE_EXIT = 4
AUTH_EXIT = 2


# The pause between requests is at least 60 / ceiling seconds, and it is slept for real. A ceiling
# this high makes it 0.1 ms, so a pull of a few dozen requests against the server on this machine
# costs milliseconds instead of most of a second; the declaration is under test, not the pace.
CEILING_PER_MINUTE = 600_000
MIN_GAP_S = 60 / CEILING_PER_MINUTE


def compliance_block(name: str, base_url: str) -> str:
    return f"""
[sources.{name}.compliance]
data_owner = "Demo Co (invented data)"
authorization = "own_account"
authorization_ref = "demo/mock-saas"
tos_url = "{base_url}/terms"
tos_reviewed_on = 2026-10-26
tos_review_max_age_days = 36500
max_requests_per_minute = {CEILING_PER_MINUTE}
retention_days = 7
"""


def config_text(workspace: Path, base_url: str, *, with_compliance: bool, gap: str) -> str:
    text = f"""
[workspace]
home = "{workspace}"

[credentials.mock]
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
"""
    # The profiles name no mapping: each connector's own packaged one applies, the way a user who
    # installed the wheel (and has no source tree to point at) runs the demo.
    for name, plugin, page_size in (
        ("mock-materials", "mock.materials", 10),
        ("mock-suppliers", "mock.suppliers", 4),
    ):
        entity = name.removeprefix("mock-")
        text += f"""
[sources.{name}]
plugin = "{plugin}"
credential = "mock"
base_url = "{base_url}"
page_size = {page_size}
request_gap_s = {gap}

[profiles.{name}]
source = "{name}"
sinks = ["{entity}-db"]
"""
        if with_compliance:
            text += compliance_block(name, base_url)
    return text


@dataclass
class Tool:
    """The CLI, pointed at one config and one workspace."""

    config: Path
    workspace: Path
    capsys: pytest.CaptureFixture[str]
    server: MockSaasServer

    def run(self, *args: str) -> int:
        return cli.main(["--config", str(self.config), "--workspace", str(self.workspace), *args])

    def output(self) -> tuple[str, str]:
        captured = self.capsys.readouterr()
        return captured.out, captured.err

    @property
    def database(self) -> Path:
        return self.config.parent / "out" / "mock.db"

    def table(self, name: str) -> dict[tuple[str, str], dict[str, object]]:
        return read_table(self.database, name)

    def audit_lines(self) -> list[str]:
        return (self.workspace / "audit.jsonl").read_text(encoding="utf-8").splitlines()

    def data_requests(self) -> list[str]:
        return [e.path for e in self.server.request_log if e.path.startswith("/api/v1")]


@pytest.fixture
def start(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Iterator[Callable[..., Tool]]:
    servers: list[MockSaasServer] = []

    def launch(
        options: MockOptions | None = None,
        *,
        with_compliance: bool = True,
        gap: str = str(MIN_GAP_S),
        clock: Callable[[], float] = time.time,
    ) -> Tool:
        server = MockSaasServer(options, clock=clock).start()
        servers.append(server)
        config = tmp_path / "catalog-sync.toml"
        config.write_text(
            config_text(
                tmp_path / "workspace", server.base_url, with_compliance=with_compliance, gap=gap
            ),
            encoding="utf-8",
        )
        return Tool(config, tmp_path / "workspace", capsys, server)

    yield launch
    for server in servers:
        server.stop()


# --- login ----------------------------------------------------------------------------------------


def test_login_over_http_stores_a_private_credential_the_tool_can_read(
    start: Callable[..., Tool],
) -> None:
    tool = start()
    assert tool.run("login", "mock", "--form") == 0
    out, _ = tool.output()
    assert "stored (HTTP login)" in out
    stored = tool.workspace / "credentials" / "mock.cred"
    assert stored.stat().st_mode & 0o077 == 0, "owner-only"
    token = read_values(stored)["AUTHORIZATION"]
    assert token.startswith("Bearer ") and decode_claims(token).token_id
    assert token not in out
    assert tool.run("auth", "status", "mock") == 0
    assert "state:      fresh" in tool.output()[0]


def test_a_valid_login_is_kept_until_forced_and_a_forced_one_is_a_new_token(
    start: Callable[..., Tool],
) -> None:
    tool = start()
    tool.run("login", "mock", "--form")
    first = read_values(tool.workspace / "credentials" / "mock.cred")["AUTHORIZATION"]
    tool.output()
    assert tool.run("login", "mock", "--form") == 0
    assert "still valid" in tool.output()[0]
    assert read_values(tool.workspace / "credentials" / "mock.cred")["AUTHORIZATION"] == first
    assert tool.run("login", "mock", "--form", "--force") == 0
    second = read_values(tool.workspace / "credentials" / "mock.cred")["AUTHORIZATION"]
    assert decode_claims(second).token_id != decode_claims(first).token_id


def test_a_wrong_password_is_exit_2_and_nothing_is_stored(
    start: Callable[..., Tool], monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = start()
    monkeypatch.setenv("MOCK_SAAS_USER", "demo")
    monkeypatch.setenv("MOCK_SAAS_PASSWORD", "not-the-password")
    assert tool.run("login", "mock", "--form") == AUTH_EXIT
    _, err = tool.output()
    assert "rejected the user name or password" in err and "not-the-password" not in err
    assert not (tool.workspace / "credentials" / "mock.cred").exists()


def test_an_audit_line_names_the_login_but_never_holds_the_token(
    start: Callable[..., Tool],
) -> None:
    tool = start()
    tool.run("login", "mock", "--form")
    token = read_values(tool.workspace / "credentials" / "mock.cred")["AUTHORIZATION"]
    audit = (tool.workspace / "audit.jsonl").read_text(encoding="utf-8")
    entry = json.loads(audit.splitlines()[0])
    assert (entry["command"], entry["source"], entry["outcome"]) == ("login", "mock", "ok")
    assert entry["credential_hash"] and token.split(".")[1] not in audit


# --- the run --------------------------------------------------------------------------------------


def test_run_materials_leaves_the_seeded_rows_in_sqlite(start: Callable[..., Tool]) -> None:
    tool = start()
    assert tool.run("login", "mock", "--form") == 0
    assert tool.run("run", "mock-materials") == 0
    rows = tool.table("materials")
    dataset = tool.server.service.dataset
    assert len(rows) == len(dataset.materials) == 24
    for material in dataset.materials:
        row = rows[("mock.materials", material.id)]
        expected = expected_material(tool.server.base_url, dataset, material)
        assert picked(row, expected) == expected, material.id
    media = [e for e in tool.server.request_log if e.path.startswith("/media/")]
    assert len(media) == 48 and not any(e.sent_authorization for e in media), (
        "pictures come from the storage side, which never sees the token"
    )
    api = [e for e in tool.server.request_log if e.path.startswith("/api/v1")]
    assert api and all(e.sent_authorization and not e.sent_cookie for e in api)


def test_run_suppliers_leaves_the_seeded_rows_and_drops_the_sensitive_field(
    start: Callable[..., Tool],
) -> None:
    tool = start()
    tool.run("login", "mock", "--form")
    logged_in = len(tool.server.request_log)  # the login also proved itself with one request
    assert tool.run("run", "mock-suppliers") == 0
    out, _ = tool.output()
    assert "sensitive fields (drop): contact_email" in out
    rows = tool.table("suppliers")
    dataset = tool.server.service.dataset
    assert len(rows) == len(dataset.suppliers) == 9
    for supplier in dataset.suppliers:
        row = rows[("mock.suppliers", supplier.id)]
        expected = expected_supplier(supplier)
        assert picked(row, expected) == expected, supplier.id
        assert "contact_email" not in row, "a sensitive field is not written unless asked for"
    pages = [e.path for e in tool.server.request_log[logged_in:] if e.path == "/api/v1/suppliers"]
    assert len(pages) == 3, "nine suppliers, four to a page, cursors to the end"


def test_the_steps_by_hand_give_the_same_rows_as_run(
    start: Callable[..., Tool], tmp_path: Path
) -> None:
    tool = start()
    tool.run("login", "mock", "--form")
    records = tmp_path / "materials.jsonl"
    assert tool.run("pull", "mock-materials") == 0
    assert tool.run("normalize", "mock-materials", "--out", str(records)) == 0, (
        "no --mapping: the connector's own"
    )
    assert tool.run("load", "--sink", "materials-db", "--input", str(records), "--dry-run") == 0
    assert not tool.database.exists(), "a dry run writes nothing, not even the database"
    assert tool.run("load", "--sink", "materials-db", "--input", str(records)) == 0
    meta = json.loads((tmp_path / "materials.jsonl.meta.json").read_text(encoding="utf-8"))
    assert meta["schema"]["entity"] == "material" and meta["is_full_snapshot"] is True
    assert len(tool.table("materials")) == 24
    commands = [json.loads(line)["command"] for line in tool.audit_lines()]
    assert commands == ["login", "pull", "normalize", "load", "load"]


def test_two_entities_share_one_database_in_two_tables(start: Callable[..., Tool]) -> None:
    tool = start()
    tool.run("login", "mock", "--form")
    assert tool.run("run", "mock-materials") == 0 and tool.run("run", "mock-suppliers") == 0
    assert len(tool.table("materials")) == 24 and len(tool.table("suppliers")) == 9
    materials, suppliers = (
        set(next(iter(tool.table(t).values()))) for t in ("materials", "suppliers")
    )
    assert "weight_gsm" in materials and "weight_gsm" not in suppliers
    assert "lead_time_days" in suppliers and "lead_time_days" not in materials


def test_a_hashing_sink_keeps_the_sensitive_field_as_a_salted_hash(
    start: Callable[..., Tool], tmp_path: Path
) -> None:
    tool = start()
    tool.run("login", "mock", "--form")
    records = tmp_path / "suppliers.jsonl"
    tool.run("pull", "mock-suppliers")
    tool.run(
        "normalize",
        "mock-suppliers",
        "--mapping",
        str(MAPPINGS / "suppliers.yaml"),
        "--out",
        str(records),
    )
    assert tool.run("load", "--sink", "suppliers-hashed", "--input", str(records)) == 0
    rows = tool.table("suppliers_hashed")
    emails = [row["contact_email"] for row in rows.values()]
    assert all(str(email).startswith("sha256:") for email in emails)
    assert not any("example.com" in str(email) for email in emails)
    assert len(set(emails)) == len(emails), "different suppliers hash differently"


def test_a_second_run_asks_for_nothing_again_and_changes_nothing(
    start: Callable[..., Tool],
) -> None:
    tool = start()
    tool.run("login", "mock", "--form")
    tool.run("run", "mock-materials")
    first_requests = len(tool.data_requests())
    tool.output()
    assert tool.run("run", "mock-materials") == 0
    out, _ = tool.output()
    second = tool.data_requests()[first_requests:]
    assert not [path for path in second if path.startswith("/api/v1/materials/")], (
        "no detail record is fetched again"
    )
    assert "inserted 0, updated 0, unchanged 24" in out
    assert not [e for e in tool.server.request_log[-len(second) :] if e.path.startswith("/media/")]


def test_inspect_shows_what_the_service_holds_without_pulling_it(
    start: Callable[..., Tool],
) -> None:
    tool = start()
    tool.run("login", "mock", "--form")
    assert tool.run("inspect", "mock-materials") == 0
    out, _ = tool.output()
    assert "total:  " in out and "Demo Woven [f-woven]" in out and "detail record fields:" in out
    assert tool.run("inspect", "mock-suppliers") == 0
    assert "total:  unknown" in tool.output()[0]
    assert not (tool.workspace / "cache").exists(), "nothing was stored"


# --- the compliance gate --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        ["pull", "mock-materials"],
        ["inspect", "mock-materials"],
        ["run", "mock-materials"],
        ["login", "mock", "--form"],
    ],
    ids=lambda command: command[0],
)
def test_without_a_compliance_block_nothing_reaches_the_service(
    start: Callable[..., Tool], command: list[str]
) -> None:
    """The point of the demo: the mock is a real networked source, so it is gated like one. So is
    a login that talks to it: the password would otherwise go to a service nobody declared."""
    tool = start(with_compliance=False)
    assert tool.run(*command) == COMPLIANCE_EXIT
    _, err = tool.output()
    assert "no compliance declaration" in err
    assert tool.server.request_log == [], "zero requests after the refusal"


def test_the_requests_of_a_form_login_count_against_the_ceiling_the_source_declared(
    start: Callable[..., Tool], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The login post and the connector's own check go through clients the CLI does not build:
    they still take their place in the declared rate window."""
    acquired: list[int] = []

    class Counting(RateLimiter):
        def acquire(self) -> None:
            acquired.append(self.limit_per_minute)

    monkeypatch.setattr(cli_credentials, "RateLimiter", Counting)
    tool = start()
    assert tool.run("login", "mock", "--form") == 0
    assert len(acquired) == len(tool.server.request_log) >= 2
    assert set(acquired) == {CEILING_PER_MINUTE}, "the ceiling this test's declaration states"


def test_a_gap_shorter_than_the_declared_ceiling_allows_is_refused_before_any_request(
    start: Callable[..., Tool],
) -> None:
    tool = start()
    tool.run("login", "mock", "--form")
    logged_in = len(tool.server.request_log)
    # The ceiling allows a gap of MIN_GAP_S at the shortest; a tenth of that on the command line
    # contradicts it.
    too_short = f"request_gap_s={MIN_GAP_S / 10}"
    assert tool.run("pull", "mock-materials", "--option", too_short) == COMPLIANCE_EXIT
    assert "shorter than 60 / max_requests_per_minute" in tool.output()[1]
    assert len(tool.server.request_log) == logged_in


# --- traps survived through the real CLI ----------------------------------------------------------


def test_an_expired_token_is_exit_2_after_exactly_one_request(start: Callable[..., Tool]) -> None:
    clock = _Clock()
    tool = start(MockOptions(token_ttl_s=3600), clock=clock)
    tool.run("login", "mock", "--form")
    logged_in = len(tool.server.request_log)
    clock.now += 3601
    assert tool.run("pull", "mock-materials") == AUTH_EXIT
    assert "catalog-sync login mock" in tool.output()[1]
    refused = [(e.path, e.status) for e in tool.server.request_log[logged_in:]]
    assert refused == [("/api/v1/materials", 401)], "401 is final: no retry, no second call"
    assert tool.run("login", "mock", "--form", "--force") == 0
    assert tool.run("run", "mock-suppliers") == 0


def test_a_rate_limit_is_waited_out_for_real_and_the_run_completes(
    start: Callable[..., Tool],
) -> None:
    tool = start(MockOptions(materials=5, suppliers=3, rate_limit=RateLimit(6, 1)))
    tool.run("login", "mock", "--form")
    began = time.monotonic()
    assert tool.run("run", "mock-materials") == 0
    elapsed = time.monotonic() - began
    statuses = [e.status for e in tool.server.request_log if e.path.startswith("/api/v1")]
    assert 429 in statuses, "the limit really was hit"
    assert elapsed >= 1.0, "and the client really waited for Retry-After"
    assert len(tool.table("materials")) == 5
    out, _ = tool.output()
    assert "inserted 5" in out


class _Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now
