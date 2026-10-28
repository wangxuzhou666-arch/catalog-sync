"""``catalog-sync mock platform serve``: started, announced and stopped like the Mock SaaS."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

from catalog_sync import cli
from catalog_sync.mock.platform import DEMO_TOKEN

pytestmark = pytest.mark.mock

SECRET = "operator-canary-" + "31ab9c"  # split: not a real secret for scanners
ALL_INTERFACES = "0.0.0.0"  # noqa: S104 — the address these tests refuse


def run_cli(*args: str, cwd: Path, token: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PLATFORM_TOKEN"}
    if token is not None:
        env["PLATFORM_TOKEN"] = token
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


def start_detached(tmp_path: Path, *extra: str, token: str | None = None) -> tuple[Path, str]:
    pid_file = tmp_path / "platform.pid"
    started = run_cli(
        "mock", "platform", "serve", "--detach", "--port", "0",
        "--db", str(tmp_path / "platform.db"), "--pid-file", str(pid_file), *extra,
        cwd=tmp_path, token=token,
    )  # fmt: skip
    assert started.returncode == 0, started.stderr
    assert started.stdout.startswith("mock platform started: http://127.0.0.1:")
    return pid_file, address_in(pid_file)


def test_started_with_the_token_from_the_environment_and_stopped_by_pid_file(
    tmp_path: Path,
) -> None:
    pid_file, address = start_detached(tmp_path, token=SECRET)
    try:
        assert requests.get(f"{address}/health", timeout=5).json() == {"status": "ok"}
        ingest = f"{address}/api/v1/ingest"
        assert requests.post(ingest, data=b"{}", timeout=5).status_code == 401
        demo = {"Authorization": f"Bearer {DEMO_TOKEN}"}
        assert requests.post(ingest, data=b"{}", headers=demo, timeout=5).status_code == 401
        good = {"Authorization": f"Bearer {SECRET}"}
        assert requests.post(ingest, data=b"{}", headers=good, timeout=5).status_code == 400
        log = (tmp_path / "platform.log").read_text(encoding="utf-8")
        assert "listening on" in log and "token: from $PLATFORM_TOKEN" in log
        assert SECRET not in log, "the announcement names the variable, never the token"
        assert "rejected" in log, "a refusal is logged"
    finally:
        stopped = run_cli("mock", "stop", "--pid-file", str(pid_file), cwd=tmp_path)
    assert stopped.returncode == 0 and "stopped" in stopped.stdout
    assert not pid_file.exists()
    with pytest.raises(requests.ConnectionError):
        requests.get(f"{address}/health", timeout=2)


def test_without_a_token_variable_it_announces_and_accepts_the_demo_token(tmp_path: Path) -> None:
    pid_file, address = start_detached(tmp_path)
    try:
        demo = {"Authorization": f"Bearer {DEMO_TOKEN}"}
        reply = requests.post(f"{address}/api/v1/ingest", data=b"{}", headers=demo, timeout=5)
        assert reply.status_code == 400, "authorised, then refused for lacking an Idempotency-Key"
        assert DEMO_TOKEN in (tmp_path / "platform.log").read_text(encoding="utf-8")
    finally:
        run_cli("mock", "stop", "--pid-file", str(pid_file), cwd=tmp_path)


def test_another_variable_can_be_named(tmp_path: Path) -> None:
    pid_file = tmp_path / "other.pid"
    env = {**os.environ, "MY_PLATFORM_KEY": SECRET}
    started = subprocess.run(
        [
            sys.executable, "-m", "catalog_sync", "mock", "platform", "serve", "--detach",
            "--port", "0", "--db", str(tmp_path / "o.db"), "--token-env", "MY_PLATFORM_KEY",
            "--pid-file", str(pid_file),
        ],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    try:
        assert started.returncode == 0, started.stderr
        good = {"Authorization": f"Bearer {SECRET}"}
        reply = requests.post(f"{address_in(pid_file)}/api/v1/ingest", headers=good, timeout=5)
        assert reply.status_code == 400
    finally:
        run_cli("mock", "stop", "--pid-file", str(pid_file), cwd=tmp_path)


def test_foreground_ends_cleanly_on_sigterm_and_leaves_its_database_private(tmp_path: Path) -> None:
    pid_file = tmp_path / "fg.pid"
    child = subprocess.Popen(
        [
            sys.executable, "-m", "catalog_sync", "mock", "platform", "serve", "--port", "0",
            "--db", str(tmp_path / "fg.db"), "--pid-file", str(pid_file),
        ],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 20
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert requests.get(f"{address_in(pid_file)}/health", timeout=5).ok
        child.send_signal(signal.SIGTERM)
        out, _ = child.communicate(timeout=20)
    finally:
        if child.poll() is None:
            child.kill()
    assert child.returncode == 0
    assert "send batches to: http://127.0.0.1:" in out and "/api/v1/ingest" in out
    assert not pid_file.exists()
    assert oct((tmp_path / "fg.db").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize(
    ("args", "complaint"),
    [
        (["--detach"], "--detach needs --pid-file"),
        (["--host", ALL_INTERFACES], "the demo token is public"),
    ],
)
def test_bad_options_fail_before_anything_listens(
    args: list[str],
    complaint: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLATFORM_TOKEN", raising=False)
    assert cli.main(["mock", "platform", "serve", "--db", str(tmp_path / "x.db"), *args]) == 1
    assert complaint in capsys.readouterr().err


def test_a_taken_port_is_a_config_error_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from catalog_sync.mock.platform_server import PlatformServer

    with PlatformServer(tmp_path / "a.db") as running:
        code = cli.main(
            [
                "mock",
                "platform",
                "serve",
                "--db",
                str(tmp_path / "b.db"),
                "--port",
                str(running.port),
            ]
        )
    assert code == 1 and "cannot listen on" in capsys.readouterr().err
