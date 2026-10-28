from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

# urllib3 probes for IPv6 with a throw-away socket the first time it is imported. Import it at
# collection time so that probe never runs inside a test with sockets disabled, where it would
# raise a warning about a socket nobody asked for.
import requests  # noqa: F401

from catalog_sync.config import HOME_ENV_VAR
from catalog_sync.state import StateStore
from catalog_sync.testing.workspace_guard import RealWorkspaceGuard, real_default_workspace

REPO_ROOT = Path(__file__).resolve().parent.parent
# The replay snapshot the generic tests use: Mock-shaped, made by tools/make_synthetic.py.
MOCK_SNAPSHOT_DIR = REPO_ROOT / "examples" / "synthetic"


def pytest_configure(config: pytest.Config) -> None:
    """Watch the real default workspace for the whole session (testing.workspace_guard)."""
    config.pluginmanager.register(
        RealWorkspaceGuard(real_default_workspace()), "real-workspace-guard"
    )


@pytest.fixture(autouse=True)
def isolated_workspace(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every test gets its own workspace home, so that a command run without ``--workspace`` can
    never read or write the user's real one. A test that needs another value sets or clears the
    variable itself (monkeypatch undoes it). The directory itself is not created, like a first
    run's."""
    home = tmp_path_factory.mktemp("workspace") / "home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[StateStore]:
    with StateStore(tmp_path / "state.sqlite") as opened:
        yield opened


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    return tmp_path / "runs"


# --- Postgres -------------------------------------------------------------------------------------

POSTGRES_DSN_ENV = "CATALOG_SYNC_TEST_POSTGRES_DSN"
POSTGRES_REQUIRED_ENV = "CATALOG_SYNC_REQUIRE_POSTGRES"


def _unavailable(reason: str) -> None:
    """Skip the Postgres tests, unless the job exists to run them: then a skip is a failure."""
    if os.environ.get(POSTGRES_REQUIRED_ENV):
        pytest.fail(f"{reason}, but {POSTGRES_REQUIRED_ENV} says these tests must run")
    pytest.skip(reason)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """The Postgres tests talk to the server named in their DSN on purpose, and the driver
    resolves host names in Python, which pytest-socket would block like any other lookup.
    The mock tests talk to a Mock SaaS they started themselves: loopback, and nothing else."""
    for item in items:
        if item.get_closest_marker("postgres"):
            item.add_marker(pytest.mark.enable_socket)
        if item.get_closest_marker("mock"):
            item.add_marker(pytest.mark.allow_hosts(["127.0.0.1"]))
            item.add_marker(pytest.mark.usefixtures("loopback_without_proxy"))


@pytest.fixture
def loopback_without_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy variable in the developer's shell would send loopback requests elsewhere."""
    for name in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    """The connection string of a throw-away server; the tests only ever touch one schema in it."""
    try:
        import psycopg  # noqa: F401
    except ImportError:
        _unavailable("psycopg is not installed (uv sync --extra postgres)")
    dsn = os.environ.get(POSTGRES_DSN_ENV)
    if not dsn:
        _unavailable(f"{POSTGRES_DSN_ENV} is not set: no throw-away Postgres to test against")
    return str(dsn)


@pytest.fixture(scope="session")
def postgres_schema(postgres_dsn: str) -> Iterator[str]:
    """A schema made for this test session and dropped at its end, so nothing else is touched."""
    import psycopg

    schema = f"cs_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(postgres_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        try:
            yield schema
        finally:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
