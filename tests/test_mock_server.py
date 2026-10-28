"""The Mock SaaS behind a real HTTP server on loopback, and the commands that run it.

Only 127.0.0.1 is reachable in these tests (the `mock` marker): the server is started here, in
this process or as a child of it, and nothing else is.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

from catalog_sync import cli
from catalog_sync.credentials.jwt import decode_claims
from catalog_sync.errors import ConfigError
from catalog_sync.mock.auth import DEMO_PASSWORD, DEMO_USER, Account
from catalog_sync.mock.server import MockSaasServer, is_loopback
from catalog_sync.mock.service import MockOptions, RateLimit

pytestmark = pytest.mark.mock

ALL_INTERFACES = "0.0.0.0"  # noqa: S104 — the address these tests refuse or merely construct for


@pytest.fixture
def server() -> Iterator[MockSaasServer]:
    with MockSaasServer() as running:
        yield running


def login(base_url: str, session: requests.Session | None = None) -> requests.Response:
    caller = session or requests
    return caller.post(
        f"{base_url}/api/login",
        json={"username": DEMO_USER, "password": DEMO_PASSWORD},
        timeout=5,
    )


# --- over a real connection -----------------------------------------------------------------------


def test_login_then_the_api_with_a_bearer_token(server: MockSaasServer) -> None:
    reply = login(server.base_url)
    assert reply.status_code == 200
    token = reply.json()["data"]["token"]
    data = requests.get(
        f"{server.base_url}/api/v1/materials",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    assert data.status_code == 200 and data.json()["data"]["total"] > 0


def test_the_cookie_the_server_sets_is_enough_on_its_own(server: MockSaasServer) -> None:
    with requests.Session() as session:
        assert login(server.base_url, session).status_code == 200
        reply = session.get(f"{server.base_url}/api/v1/suppliers", timeout=5)
        assert reply.status_code == 200 and reply.json()["data"]["items"]
    with requests.Session() as stranger:
        assert stranger.get(f"{server.base_url}/api/v1/suppliers", timeout=5).status_code == 401


def test_one_connection_serves_many_requests(server: MockSaasServer) -> None:
    with requests.Session() as session:
        statuses = [
            session.get(f"{server.base_url}/terms", timeout=5).status_code for _ in range(5)
        ]
    assert statuses == [200] * 5
    assert len(server.request_log) == 5


def test_the_images_are_served_as_images(server: MockSaasServer) -> None:
    reply = requests.get(f"{server.base_url}/media/thumb/mat-0001.png", timeout=5)
    assert reply.headers["Content-Type"] == "image/png"
    assert int(reply.headers["Content-Length"]) == len(reply.content)


def test_retry_after_arrives_as_a_header_over_the_wire() -> None:
    with MockSaasServer(MockOptions(rate_limit=RateLimit(1, 30))) as limited:
        token = login(limited.base_url).json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert requests.get(f"{limited.base_url}/api/v1/materials", headers=headers, timeout=5).ok
        refused = requests.get(f"{limited.base_url}/api/v1/materials", headers=headers, timeout=5)
        assert refused.status_code == 429
        assert 1 <= int(refused.headers["Retry-After"]) <= 30


def test_an_oversized_body_does_not_hang_or_crash_the_server(server: MockSaasServer) -> None:
    reply = requests.post(f"{server.base_url}/api/login", data=b"x" * 200_000, timeout=5)
    assert reply.status_code == 413, "refused whole, not read in part and taken for a login"
    assert requests.get(f"{server.base_url}/terms", timeout=5).status_code == 200


def test_the_token_the_server_issues_is_readable_by_the_tools_own_freshness_check(
    server: MockSaasServer,
) -> None:
    token = login(server.base_url).json()["data"]["token"]
    claims = decode_claims(f"Bearer {token}")
    assert claims.expires_at is not None and claims.token_id is not None


def test_stopping_the_server_releases_the_port() -> None:
    first = MockSaasServer().start()
    port = first.port
    first.stop()
    with pytest.raises(requests.ConnectionError):
        requests.get(f"http://127.0.0.1:{port}/terms", timeout=2)
    again = MockSaasServer(port=port).start()  # bind succeeds only if the port was released
    try:
        assert requests.get(f"{again.base_url}/terms", timeout=5).ok
    finally:
        again.stop()


def test_a_request_callback_sees_each_request(server: MockSaasServer) -> None:
    seen: list[str] = []
    with MockSaasServer(on_request=lambda entry: seen.append(f"{entry.method} {entry.path}")) as s:
        requests.get(f"{s.base_url}/terms", timeout=5)
    assert seen == ["GET /terms"]


# --- who may listen where -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        (ALL_INTERFACES, False),
        ("10.0.0.5", False),
    ],
)
def test_loopback_detection(host: str, expected: bool) -> None:
    assert is_loopback(host) is expected


def test_the_public_demo_password_never_listens_beyond_loopback() -> None:
    with pytest.raises(ConfigError, match="the demo password is public"):
        MockSaasServer(host=ALL_INTERFACES)


def test_a_private_password_may_listen_elsewhere() -> None:
    options = MockOptions(account=Account("ops", "not-the-demo-one"))
    MockSaasServer(options, host=ALL_INTERFACES)


# --- the commands ---------------------------------------------------------------------------------


def run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in {"MOCK_SAAS_USER", "MOCK_SAAS_PASSWORD"}}
    return subprocess.run(
        [sys.executable, "-m", "catalog_sync", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def address_in(pid_file: Path) -> str:
    return pid_file.read_text(encoding="utf-8").splitlines()[1]


def test_serve_detached_then_stop(tmp_path: Path) -> None:
    pid_file = tmp_path / "mock.pid"
    started = run_cli(
        "mock", "serve", "--detach", "--port", "0", "--pid-file", str(pid_file), cwd=tmp_path
    )
    assert started.returncode == 0, started.stderr
    assert started.stdout.startswith("mock SaaS started: http://127.0.0.1:")
    address = address_in(pid_file)
    try:
        assert requests.get(f"{address}/terms", timeout=5).status_code == 200
        assert "listening on" in (tmp_path / "mock.log").read_text(encoding="utf-8")
        again = run_cli("mock", "serve", "--detach", "--pid-file", str(pid_file), cwd=tmp_path)
        assert again.returncode == 1 and "already running" in again.stderr
    finally:
        stopped = run_cli("mock", "stop", "--pid-file", str(pid_file), cwd=tmp_path)
    assert stopped.returncode == 0 and "stopped" in stopped.stdout
    assert not pid_file.exists()
    with pytest.raises(requests.ConnectionError):
        requests.get(f"{address}/terms", timeout=2)


def test_stop_is_harmless_when_there_is_nothing_to_stop(tmp_path: Path) -> None:
    missing = run_cli("mock", "stop", "--pid-file", str(tmp_path / "none.pid"), cwd=tmp_path)
    assert missing.returncode == 0 and "nothing to stop" in missing.stdout
    stale = tmp_path / "stale.pid"
    stale.write_text("999999\nhttp://127.0.0.1:1\n", encoding="utf-8")
    result = run_cli("mock", "stop", "--pid-file", str(stale), cwd=tmp_path)
    assert result.returncode == 0 and "not running" in result.stdout
    assert not stale.exists()


def test_a_stale_pid_file_does_not_block_a_new_detached_start(tmp_path: Path) -> None:
    pid_file = tmp_path / "mock.pid"
    pid_file.write_text("999999\nhttp://127.0.0.1:1\n", encoding="utf-8")
    started = run_cli(
        "mock", "serve", "--detach", "--port", "0", "--pid-file", str(pid_file), cwd=tmp_path
    )
    try:
        assert started.returncode == 0, started.stderr
        assert address_in(pid_file) != "http://127.0.0.1:1"
    finally:
        run_cli("mock", "stop", "--pid-file", str(pid_file), cwd=tmp_path)


def test_serve_in_the_foreground_ends_cleanly_on_sigterm(tmp_path: Path) -> None:
    pid_file = tmp_path / "fg.pid"
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "catalog_sync",
            "mock",
            "serve",
            "--port",
            "0",
            "--pid-file",
            str(pid_file),
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert requests.get(f"{address_in(pid_file)}/terms", timeout=5).ok
        child.send_signal(signal.SIGTERM)
        out, _ = child.communicate(timeout=20)
    finally:
        if child.poll() is None:
            child.kill()
    assert child.returncode == 0
    assert f"account: {DEMO_USER} / {DEMO_PASSWORD}" in out, "the public demo account is announced"
    assert not pid_file.exists(), "the pid file goes away with the server"


@pytest.mark.parametrize(
    ("args", "complaint"),
    [
        (["--detach"], "--detach needs --pid-file"),
        (["--token-ttl", "0"], "--token-ttl must be positive"),
        (["--error-every", "-1"], "cannot be negative"),
        (["--gone-every", "-1"], "cannot be negative"),
        (["--expire-after", "-1"], "cannot be negative"),
        (["--materials", "-1"], "cannot be negative"),
        (["--signed-urls", "0"], "positive number of seconds"),
        (["--host", ALL_INTERFACES], "the demo password is public"),
    ],
)
def test_bad_serve_options_fail_before_anything_listens(
    args: list[str],
    complaint: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MOCK_SAAS_PASSWORD", raising=False)
    monkeypatch.delenv("MOCK_SAAS_USER", raising=False)
    assert cli.main(["mock", "serve", *args]) == 1
    assert complaint in capsys.readouterr().err


def test_a_malformed_rate_limit_is_an_argument_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["mock", "serve", "--rate-limit", "fast"])
    assert stopped.value.code == 2
    assert "COUNT/SECONDS" in capsys.readouterr().err


def test_a_port_that_is_taken_is_a_config_error_not_a_traceback(
    server: MockSaasServer, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ConfigError, match=rf"cannot listen on 127\.0\.0\.1:{server.port}"):
        MockSaasServer(port=server.port).bind()
    assert cli.main(["mock", "serve", "--port", str(server.port)]) == 1
    assert "cannot listen on" in capsys.readouterr().err


def test_the_serving_loop_polls_often_so_stopping_does_not_cost_half_a_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`shutdown()` blocks for up to one poll interval, and the library default is 0.5 s: with it,
    every test (and every `mock serve` stop) that ended a server paid that. Recorded rather than
    timed, so a loaded machine cannot make the check flaky."""
    asked: list[float] = []
    original = ThreadingHTTPServer.serve_forever

    def recording(self: ThreadingHTTPServer, poll_interval: float = 0.5) -> None:
        asked.append(poll_interval)
        original(self, poll_interval)

    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", recording)
    with MockSaasServer() as running:
        assert requests.get(f"{running.base_url}/terms", timeout=5).ok
    assert asked and max(asked) <= 0.1, asked
