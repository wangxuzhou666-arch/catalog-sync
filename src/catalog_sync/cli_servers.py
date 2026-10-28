"""What the mock servers (the Mock SaaS and the reference platform) share on the command line:
running in the background with a pid file, stopping on a signal, and stopping by pid file.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from catalog_sync.errors import CatalogSyncError, ConfigError

_READY_TIMEOUT_S = 15.0
_STOP_TIMEOUT_S = 10.0
_POLL_S = 0.05
_LOG_TAIL_CHARS = 2000


class Stoppable(Protocol):
    def shutdown(self) -> None: ...


def stop_on_signal(server: Stoppable) -> None:
    """SIGINT and SIGTERM end the serve loop. `shutdown` waits for that loop, which runs in this
    very thread, so it must be asked from another one. That thread is not a daemon: the process
    waits for it, so it is never cut off in the middle of closing the socket at exit."""

    def request_stop(_signal: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown).start()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


# --- background ---------------------------------------------------------------------------------


def serve_detached(
    args: argparse.Namespace, *, what: str, command: Sequence[str], arguments: Sequence[str]
) -> int:
    """Start `catalog-sync <command> <arguments>` in the background and return once its pid file
    appears. Shared by every mock server, which all stop with `mock stop --pid-file`."""
    pid_file: Path | None = args.pid_file
    if pid_file is None:
        raise ConfigError("--detach needs --pid-file, which is how `mock stop` finds the server")
    if pid_file.exists():
        if is_running(read_pid(pid_file)):
            raise ConfigError(
                f"a mock server is already running ({pid_file}); `mock stop` it first"
            )
        pid_file.unlink()  # left behind by a server that was killed
    log_file = pid_file.with_suffix(".log")
    with log_file.open("ab") as log:
        child = subprocess.Popen(  # noqa: S603 — our own interpreter and a fixed module
            [sys.executable, "-m", "catalog_sync", *command, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while not pid_file.exists():
        if child.poll() is not None or time.monotonic() > deadline:
            raise CatalogSyncError(
                f"the mock server did not start; its log ({log_file.name}) ends with:\n"
                f"{tail(log_file)}"
            )
        time.sleep(_POLL_S)
    print(f"{what} started: {pid_file.read_text(encoding='utf-8').splitlines()[1]}")
    return 0


def stop_by_pid_file(pid_file: Path) -> None:
    """End the server whose pid file this is, and remove the file."""
    if not pid_file.exists():
        print(f"no pid file {pid_file}: nothing to stop")
        return
    pid = read_pid(pid_file)
    if not is_running(pid):
        pid_file.unlink(missing_ok=True)
        print(f"process {pid} is not running; removed the stale {pid_file.name}")
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _STOP_TIMEOUT_S
    while is_running(pid):
        if time.monotonic() > deadline:
            raise CatalogSyncError(f"process {pid} did not stop within {_STOP_TIMEOUT_S:g} s")
        time.sleep(_POLL_S)
    pid_file.unlink(missing_ok=True)
    print(f"mock server stopped (pid {pid})")


def read_pid(pid_file: Path) -> int:
    first_line = pid_file.read_text(encoding="utf-8").splitlines()[:1]
    try:
        return int(first_line[0])
    except (IndexError, ValueError) as exc:
        raise ConfigError(f"{pid_file.name} does not start with a process id") from exc


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # signal 0 only asks whether the process exists
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, it is just not ours
    return True


def tail(path: Path) -> str:
    with contextlib.suppress(OSError):
        return path.read_text(encoding="utf-8", errors="replace")[-_LOG_TAIL_CHARS:]
    return "(no log)"
