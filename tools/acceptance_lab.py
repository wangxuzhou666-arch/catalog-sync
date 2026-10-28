"""What a scenario works with: the installed program, its servers, its workspace, its requests.

A **Harness** holds what one acceptance run shares (the output directory, the installed virtual
environment, the throw-away credentials). A **Lab** is one scenario's private corner of it: an
empty working directory, its own workspace, its own Mock source and reference platform, and the
config that ties them together. Labs never share state, so scenarios cannot disturb each other,
and every command a lab runs is the installed ``catalog-sync`` from the fresh virtual environment.

The request counts come from the Mock source's own access log (``mock serve --verbose`` prints one
line per request: method, path, status; never a query, a header or a body).

Scenarios register themselves with ``@scenario`` (see the docstring of ``acceptance.py``).
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Self

from acceptance_report import Masker, Recorder
from catalog_sync.testing.workspace_guard import Fingerprint

HOST = "127.0.0.1"
SEED = 1
MATERIAL_COUNT = 24
SUPPLIER_COUNT = 9

USER_ENV = "MOCK_SAAS_USER"
PASSWORD_ENV = "MOCK_SAAS_PASSWORD"  # noqa: S105 — the name of an environment variable
PLATFORM_TOKEN_ENV = "ACCEPTANCE_PLATFORM_TOKEN"  # noqa: S105 — the name of an environment variable
POSTGRES_DSN_ENV = "ACCEPTANCE_POSTGRES_DSN"  # the commands' variable, and the flag's default

START_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 15.0
COMMAND_TIMEOUT_S = 300.0
_POLL_S = 0.05
_LOG_TAIL_CHARS = 1500
_PID_FILE_LINES = 2  # the pid file is "<pid>\n<address>"
_WATCH_S = 0.01  # how often a scenario looks at the source's log while it waits for a request count
_SETTLE_QUIET_S = 0.4  # a log that did not grow for this long has stopped growing


class HarnessError(Exception):
    """The harness itself could not do something (a server did not start, a file is missing)."""


def real_workspace_locations() -> list[Path]:
    """Where a run with no flag and no variable would keep its state: the default, and the XDG
    data directory's `catalog-sync` when one is set."""
    locations = [Path("~/.local/share/catalog-sync").expanduser()]
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        locations.append(Path(xdg) / "catalog-sync")
    return locations


# --- what the run shares ------------------------------------------------------------------------


@dataclass(frozen=True)
class Secrets:
    """Throw-away values made for this run. They travel in environment variables only."""

    user: str
    password: str
    wrong_password: str
    platform_token: str

    def values(self) -> tuple[str, ...]:
        return (self.user, self.password, self.wrong_password, self.platform_token)


def generate_secrets() -> Secrets:
    return Secrets(
        user=f"acc-{secrets.token_hex(4)}",
        password=f"pw-{secrets.token_hex(12)}",
        wrong_password=f"bad-{secrets.token_hex(12)}",
        platform_token=f"tok-{secrets.token_hex(12)}",
    )


@dataclass(frozen=True)
class Installed:
    """A virtual environment the program was installed into."""

    venv: Path
    site_packages: Path

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def cli(self) -> Path:
        return self.venv / "bin" / "catalog-sync"


@dataclass
class Harness:
    out: Path
    repo: Path
    installed: Installed
    secrets: Secrets
    masker: Masker
    today: str  # YYYY-MM-DD (UTC): the review date written into the compliance blocks
    python: str  # the Python the fresh environments were made with, as asked of uv
    postgres_dsn: str | None = None
    private_terms: Path | None = None  # the maintainer's private terms file (dist-check), if any
    postgres_schema: str = field(default_factory=lambda: f"acc_{secrets.token_hex(4)}")
    sdist_installed: Installed | None = None
    real_workspaces_before: dict[Path, Fingerprint] = field(default_factory=dict)
    labs: list[Lab] = field(default_factory=list)

    @property
    def fake_home(self) -> Path:
        return self.out / "fake-home"

    def lab(self, name: str) -> Lab:
        lab = Lab(self, name)
        self.labs.append(lab)
        return lab

    def environment(
        self,
        workspace: Path,
        extra: Mapping[str, str] | None = None,
        *,
        installed: Installed | None = None,
    ) -> dict[str, str]:
        """The environment of a command under test: no PYTHONPATH, no inherited variable, a home
        directory that is not the user's (so a default-workspace write cannot hit the real one)."""
        base = {
            "PATH": os.pathsep.join(
                [str((installed or self.installed).venv / "bin"), "/usr/bin", "/bin"]
            ),
            "HOME": str(self.fake_home),
            "CATALOG_SYNC_HOME": str(workspace),
            "TMPDIR": str(self.out / "tmp"),
            "PYTHON_DOTENV_DISABLED": "1",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            USER_ENV: self.secrets.user,
            PASSWORD_ENV: self.secrets.password,
            PLATFORM_TOKEN_ENV: self.secrets.platform_token,
        }
        if self.postgres_dsn is not None:
            base[POSTGRES_DSN_ENV] = self.postgres_dsn
        return {**base, **(extra or {})}


# --- requests -----------------------------------------------------------------------------------

_REQUEST_LINE = re.compile(r"^(?P<method>[A-Z]+) (?P<path>\S+) -> (?P<status>\d{3})$")
_ASSET_ID = re.compile(r"^/media/[a-z]+/(?P<id>[^/.]+)\.[a-z]+$")


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    status: int

    @property
    def kind(self) -> str:
        """login, list, detail, asset or other: the roles a Mock request can have."""
        if self.path == "/api/login":
            return "login"
        if self.path in ("/api/v1/materials", "/api/v1/suppliers"):
            return "list"
        if self.path.startswith("/api/v1/materials/"):
            return "detail"
        if self.path.startswith("/media/"):
            return "asset"
        return "other"

    @property
    def item(self) -> str | None:
        """The item a detail or asset request is about."""
        if self.kind == "detail":
            return self.path.rsplit("/", 1)[1]
        asset = _ASSET_ID.match(self.path)
        return asset["id"] if asset and self.kind == "asset" else None


def parse_request_lines(text: str) -> list[Request]:
    """The requests in a Mock source's `--verbose` output; its other lines are skipped."""
    requests = []
    for line in text.splitlines():
        matched = _REQUEST_LINE.match(line.strip())
        if matched:
            requests.append(Request(matched["method"], matched["path"], int(matched["status"])))
    return requests


def count_kinds(requests: Sequence[Request]) -> dict[str, int]:
    return dict(Counter(request.kind for request in requests))


def per_item(requests: Sequence[Request], kind: str) -> dict[str, int]:
    """How many requests of this kind each item got."""
    return dict(Counter(r.item for r in requests if r.kind == kind and r.item is not None))


_PLATFORM_LINE = re.compile(
    r"^(?P<kind>ingested|replayed|rejected)(?: key (?P<key>[0-9a-f]{12}))?"
    r"(?: run \S+ request (?P<batch>\d+/\d+))?, (?P<records>\d+) record\(s\), "
    r"HTTP (?P<status>\d{3})"
)


@dataclass(frozen=True)
class PlatformEvent:
    kind: str  # ingested, replayed or rejected
    key_prefix: str | None
    batch: str | None
    records: int
    status: int


def parse_platform_lines(text: str) -> list[PlatformEvent]:
    events = []
    for line in text.splitlines():
        matched = _PLATFORM_LINE.match(line.strip())
        if matched:
            events.append(
                PlatformEvent(
                    matched["kind"],
                    matched["key"],
                    matched["batch"],
                    int(matched["records"]),
                    int(matched["status"]),
                )
            )
    return events


# --- servers ------------------------------------------------------------------------------------


class ServerProcess:
    """One installed ``catalog-sync mock ...`` server in the foreground, stopped in `finally`."""

    def __init__(self, argv: Sequence[str | Path], env: Mapping[str, str], cwd: Path, log: Path):
        self.argv = [str(part) for part in argv]
        self.env = dict(env)
        self.cwd = cwd
        self.log = log
        self.base_url = ""
        self._process: subprocess.Popen[bytes] | None = None
        self._pid_file = Path(self.argv[self.argv.index("--pid-file") + 1])

    def start(self) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("wb") as log_file:
            self._process = subprocess.Popen(
                self.argv,
                cwd=self.cwd,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.base_url = self._wait_for_address()

    def _wait_for_address(self) -> str:
        assert self._process is not None
        waited = 0.0
        while waited < START_TIMEOUT_S:
            if self._process.poll() is not None:
                raise HarnessError(f"{self._label} exited early; its log ends with: {self._tail()}")
            lines = self._pid_file.read_text().splitlines() if self._pid_file.exists() else []
            if len(lines) >= _PID_FILE_LINES:
                return lines[1]
            time.sleep(_POLL_S)
            waited += _POLL_S
        raise HarnessError(f"{self._label} did not listen within {START_TIMEOUT_S:g} s")

    @property
    def _label(self) -> str:
        return " ".join(self.argv[1:4])

    def _tail(self) -> str:
        return self.log.read_text(errors="replace")[-_LOG_TAIL_CHARS:] if self.log.exists() else ""

    @property
    def pid(self) -> int:
        assert self._process is not None
        return self._process.pid

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def returncode(self) -> int | None:
        """How the process ended; None while it runs."""
        assert self._process is not None
        return self._process.poll()

    @property
    def port(self) -> int:
        return int(self.base_url.rsplit(":", 1)[1])

    def stop(self) -> None:
        """SIGTERM, then wait: the server finishes its request and closes its port."""
        if not self.is_running:
            return
        assert self._process is not None
        self._process.send_signal(signal.SIGTERM)
        try:
            self._process.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()

    def kill(self) -> None:
        """SIGKILL, for a scenario that needs a crash."""
        if self.is_running:
            assert self._process is not None
            self._process.kill()
            self._process.wait()

    def is_listening(self) -> bool:
        """A TCP connect to the server's port (no HTTP request, so the access log is untouched)."""
        with socket.socket() as probe:
            probe.settimeout(2.0)
            return probe.connect_ex((HOST, self.port)) == 0


class SourceProcess(ServerProcess):
    """The Mock source. Its access log is the proof of what a command asked it."""

    def requests(self) -> list[Request]:
        return parse_request_lines(self._tail_all())

    def _tail_all(self) -> str:
        return self.log.read_text(errors="replace") if self.log.exists() else ""

    def mark(self) -> int:
        """A position in the log; `since(mark)` are the requests made after it."""
        return len(self.requests())

    def since(self, mark: int) -> list[Request]:
        return self.requests()[mark:]

    def wait_for(
        self,
        kind: str,
        count: int,
        *,
        since: int,
        is_alive: Callable[[], bool],
        timeout_s: float = COMMAND_TIMEOUT_S,
    ) -> list[Request]:
        """Block until `count` requests of `kind` were made after `since`.

        This is how a scenario acts on an *observed* point of a command's progress instead of on a
        sleep. If the command ends before the point is reached, that is the harness's problem to
        report (the point was never there to act on), not a hang.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            seen = self.since(since)
            if sum(1 for request in seen if request.kind == kind) >= count:
                return seen
            if not is_alive():
                raise HarnessError(
                    f"the command ended after {len(seen)} request(s), before {count} {kind} "
                    "request(s) were made: nothing to interrupt"
                )
            if time.monotonic() > deadline:
                raise HarnessError(f"{count} {kind} request(s) did not arrive in {timeout_s:g} s")
            time.sleep(_WATCH_S)

    def settle(self) -> int:
        """Wait until the log stopped growing (a request a killed client had sent may still be
        answered and logged a moment later); return the number of requests it holds."""
        size, quiet_since = self.mark(), time.monotonic()
        while time.monotonic() - quiet_since < _SETTLE_QUIET_S:
            time.sleep(_WATCH_S)
            now = self.mark()
            if now != size:
                size, quiet_since = now, time.monotonic()
        return size


class PlatformProcess(ServerProcess):
    """The reference platform. It stores into SQLite; its log names every ingest and replay."""

    def __init__(
        self, argv: Sequence[str | Path], env: Mapping[str, str], cwd: Path, log: Path, db: Path
    ):
        super().__init__(argv, env, cwd, log)
        self.db = db

    @property
    def ingest_url(self) -> str:
        return f"{self.base_url}/api/v1/ingest"

    def events(self) -> list[PlatformEvent]:
        text = self.log.read_text(errors="replace") if self.log.exists() else ""
        return parse_platform_lines(text)

    def idempotency_keys(self) -> list[str]:
        """The keys the platform has stored (each is a sha256 of a request body, not a secret)."""
        if not self.db.exists():
            return []
        with contextlib.closing(sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)) as conn:
            rows = conn.execute("SELECT key FROM _cs_idempotency ORDER BY key").fetchall()
        return [str(row[0]) for row in rows]


# --- config -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entity:
    name: str  # the entity as the docs say it: materials, suppliers
    source: str  # the [sources.*] and [profiles.*] name
    plugin: str
    schema: str  # the platform's table, and the schema's name
    table: str  # the SQLite sink's table
    page_size: int


ENTITIES = (
    Entity("materials", "mock-materials", "mock.materials", "material", "materials", 10),
    Entity("suppliers", "mock-suppliers", "mock.suppliers", "supplier", "suppliers", 4),
)
SINK_KINDS = ("sqlite", "platform", "jsonl")
DEFAULT_GAP_S = 0.02  # the shipped example config's pause between requests


def sink_name(kind: str, entity: Entity) -> str:
    return f"{kind}-{entity.name}"


def config_text(
    *,
    source_url: str,
    platform_url: str | None,
    today: str,
    with_compliance: bool = True,
    sinks: Sequence[str] = SINK_KINDS,
    entities: Sequence[Entity] = ENTITIES,
    gap_s: float = DEFAULT_GAP_S,
    postgres_schema: str = "",
) -> str:
    """A config the way a user of the installed package writes one: no `mapping` key anywhere, so
    every profile uses the mapping its connector ships. Credentials are named, never written."""
    parts = [f'[credentials.mock]\nbase_url = "{source_url}"\n']
    for entity in entities:
        parts.append(
            _source_block(entity, source_url, today, with_compliance=with_compliance, gap_s=gap_s)
        )
        parts += [_sink_block(kind, entity, platform_url, postgres_schema) for kind in sinks]
        names = ", ".join(f'"{sink_name(kind, entity)}"' for kind in sinks)
        parts.append(
            f'[profiles.{entity.source}]\nsource = "{entity.source}"\nsinks = [{names}]\n'
            "incremental = true\n"
        )
    return "\n".join(parts)


def _source_block(
    entity: Entity, source_url: str, today: str, *, with_compliance: bool, gap_s: float
) -> str:
    block = (
        f'[sources.{entity.source}]\nplugin = "{entity.plugin}"\ncredential = "mock"\n'
        f'base_url = "{source_url}"\npage_size = {entity.page_size}\nrequest_gap_s = {gap_s}\n'
    )
    if not with_compliance:
        return block
    return block + (
        f"\n[sources.{entity.source}.compliance]\n"
        'data_owner = "Demo Co (the mock\'s invented data)"\n'
        'authorization = "own_account"\n'
        'authorization_ref = "demo/mock-saas"\n'
        f'tos_url = "{source_url}/terms"\n'
        f"tos_reviewed_on = {today}\n"
        "max_requests_per_minute = 3000\n"
        "retention_days = 7\n"
    )


def _sink_block(kind: str, entity: Entity, platform_url: str | None, postgres_schema: str) -> str:
    head = f"[sinks.{sink_name(kind, entity)}]\n"
    if kind == "sqlite":
        return (
            head
            + f'kind = "sqlite"\npath = "{{workspace}}/out/mock.db"\ntable = "{entity.table}"\n'
        )
    if kind == "jsonl":
        return head + f'kind = "jsonl"\npath = "{{workspace}}/out/{entity.name}.jsonl"\n'
    if kind == "postgres":
        return head + (
            f'kind = "postgres"\ndsn_env = "{POSTGRES_DSN_ENV}"\n'
            f'schema = "{postgres_schema}"\ntable = "{entity.table}"\n'
        )
    assert kind == "platform", kind
    assert platform_url is not None, "the platform sink needs a running platform"
    return (
        head + f'kind = "http"\nurl = "{platform_url}"\nauth_header_env = "{PLATFORM_TOKEN_ENV}"\n'
    )


# --- a scenario's own corner --------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """One command under test and what it did."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class RunningCommand:
    """A ``catalog-sync`` command started in the background, so a scenario can act while it runs.

    Its output goes to files (a pipe nobody reads would block it), and it has a process group of its
    own so that SIGKILL takes everything it started with it. Whatever way it ends, `finish()` turns
    it into an ordinary ``Run`` in the lab's command list and log.
    """

    def __init__(
        self, lab: Lab, number: int, args: Sequence[str | Path], env: Mapping[str, str] | None
    ) -> None:
        self._lab = lab
        self._out, self._err = (lab.state / f"background-{number}.{s}" for s in ("out", "err"))
        self.args = tuple(str(a) for a in args)
        with self._out.open("wb") as out, self._err.open("wb") as err:
            self._process = subprocess.Popen(
                [str(lab.harness.installed.cli), *self.args],
                cwd=lab.work,
                env=lab.harness.environment(lab.home, env),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )

    @property
    def is_running(self) -> bool:
        return self._process.poll() is None

    def kill(self) -> None:
        """SIGKILL the whole process group: no handler runs, nothing is flushed or cleaned up."""
        if self.is_running:
            os.killpg(self._process.pid, signal.SIGKILL)

    def finish(self, timeout: float = COMMAND_TIMEOUT_S) -> Run:
        self._process.wait(timeout=timeout)
        run = Run(
            self.args,
            self._process.returncode,
            self._out.read_text(encoding="utf-8", errors="replace"),
            self._err.read_text(encoding="utf-8", errors="replace"),
        )
        self._lab.record(run)
        return run


class Lab:
    """An empty working directory, a workspace, and the servers one scenario starts."""

    def __init__(self, harness: Harness, name: str) -> None:
        self.harness = harness
        self.name = name
        self.work = harness.out / "work" / name
        self.home = harness.out / "home" / name
        self.state = harness.out / "state" / name  # the platform's database
        self.log_path = harness.out / "logs" / f"{name}.log"
        self.commands: list[Run] = []
        self.background_count = 0
        self.source: SourceProcess | None = None
        self.platform: PlatformProcess | None = None
        for directory in (self.work, self.state, self.log_path.parent):
            directory.mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop_servers()

    def stop_servers(self) -> None:
        for server in (self.source, self.platform):
            if server is not None:
                server.stop()

    # -- servers --

    def start_source(
        self,
        *,
        token_ttl: float | None = None,
        error_every: int = 0,
        gone_every: int = 0,
        expire_after: int = 0,
        materials: int = MATERIAL_COUNT,
        port: int = 0,
        label: str = "source",
        extra: Sequence[str] = (),
    ) -> SourceProcess:
        """Start the Mock source. A second source in the same lab needs its own `label` (its log
        and pid file) and, to be found by the same config, the first one's `port`."""
        argv: list[str | Path] = [
            self.harness.installed.cli, "mock", "serve", "--port", str(port), "--verbose",
            "--seed", str(SEED), "--materials", str(materials),
            "--suppliers", str(SUPPLIER_COUNT), "--pid-file", self.state / f"{label}.pid",
        ]  # fmt: skip
        if token_ttl is not None:
            argv += ["--token-ttl", str(token_ttl)]
        if error_every:
            argv += ["--error-every", str(error_every)]
        if gone_every:
            argv += ["--gone-every", str(gone_every)]
        if expire_after:
            argv += ["--expire-after", str(expire_after)]
        argv += list(extra)
        env = self.harness.environment(self.home)
        self.source = SourceProcess(
            argv, env, self.work, self.harness.out / "logs" / f"{self.name}-{label}.log"
        )
        self.source.start()
        return self.source

    def start_platform(self) -> PlatformProcess:
        db = self.state / "platform.db"
        argv: list[str | Path] = [
            self.harness.installed.cli, "mock", "platform", "serve", "--port", "0", "--verbose",
            "--db", db, "--token-env", PLATFORM_TOKEN_ENV,
            "--pid-file", self.state / "platform.pid",
        ]  # fmt: skip
        env = self.harness.environment(self.home)
        log = self.harness.out / "logs" / f"{self.name}-platform.log"
        self.platform = PlatformProcess(argv, env, self.work, log, db)
        self.platform.start()
        return self.platform

    # -- config and commands --

    def write_config(
        self,
        *,
        with_compliance: bool = True,
        sinks: Sequence[str] = SINK_KINDS,
        entities: Sequence[Entity] = ENTITIES,
        gap_s: float = DEFAULT_GAP_S,
    ) -> Path:
        assert self.source is not None, "start the source before writing its config"
        platform_url = self.platform.ingest_url if self.platform is not None else None
        text = config_text(
            source_url=self.source.base_url,
            platform_url=platform_url,
            today=self.harness.today,
            with_compliance=with_compliance,
            sinks=sinks,
            entities=entities,
            gap_s=gap_s,
            postgres_schema=self.harness.postgres_schema,
        )
        path = self.work / "catalog-sync.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def cli(
        self,
        *args: str | Path,
        env: Mapping[str, str] | None = None,
        timeout: float = COMMAND_TIMEOUT_S,
    ) -> Run:
        """Run the installed ``catalog-sync`` from this lab's working directory."""
        command = [str(self.harness.installed.cli), *[str(a) for a in args]]
        done = subprocess.run(
            command,
            cwd=self.work,
            env=self.harness.environment(self.home, env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        return self.record(Run(tuple(command[1:]), done.returncode, done.stdout, done.stderr))

    def record(self, run: Run) -> Run:
        """Keep a finished command: in the lab's list (S7 reads it) and in its log (masked)."""
        self.commands.append(run)
        self._log(run)
        return run

    def start_command(
        self, *args: str | Path, env: Mapping[str, str] | None = None
    ) -> RunningCommand:
        """Start the installed ``catalog-sync`` in the background; see `RunningCommand`."""
        self.background_count += 1
        return RunningCommand(self, self.background_count, args, env)

    def _log(self, run: Run) -> None:
        text = (
            f"$ catalog-sync {' '.join(run.args)}\nexit {run.returncode}\n"
            f"--- stdout\n{run.stdout}--- stderr\n{run.stderr}\n"
        )
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(self.harness.masker.text(text))

    # -- the workspace --

    @property
    def sqlite_path(self) -> Path:
        return self.home / "out" / "mock.db"

    def jsonl_path(self, entity: Entity) -> Path:
        return self.home / "out" / f"{entity.name}.jsonl"


# --- scenarios ----------------------------------------------------------------------------------

ScenarioFunction = Callable[[Harness, Recorder], None]


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    order: int
    run: ScenarioFunction | None  # None: a placeholder, shown as NOT_RUN with `pending`
    pending: str | None = None


_REGISTRY: dict[str, Scenario] = {}


def scenario(
    scenario_id: str, title: str, *, order: int
) -> Callable[[ScenarioFunction], ScenarioFunction]:
    """Register a scenario function ``(harness, recorder) -> None``. It may replace a placeholder
    (``pending``) of the same id; any other duplicate id is a bug."""

    def register(function: ScenarioFunction) -> ScenarioFunction:
        existing = _REGISTRY.get(scenario_id)
        if existing is not None and existing.run is not None:
            raise ValueError(f"scenario {scenario_id} is registered twice")
        _REGISTRY[scenario_id] = Scenario(scenario_id, title, order, function)
        return function

    return register


def pending(scenario_id: str, title: str, *, order: int, reason: str) -> None:
    """List a scenario that is not written yet. It shows as NOT_RUN with this reason."""
    if scenario_id in _REGISTRY:
        raise ValueError(f"scenario {scenario_id} is registered twice")
    _REGISTRY[scenario_id] = Scenario(scenario_id, title, order, None, reason)


def registered() -> list[Scenario]:
    return sorted(_REGISTRY.values(), key=lambda s: s.order)


def forget_all() -> None:
    """For tests: empty the registry."""
    _REGISTRY.clear()
