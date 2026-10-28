"""The mock servers stopped by a signal: the signal thread and the serve loop's `finally` both ask
the server to shut down, and neither may trip over the other.

The bug was a race between two threads, so the test makes it certain instead of likely: whichever
thread comes out of `BaseServer.shutdown` is held for a moment, long enough for the other thread
to run its whole shutdown. Before the fix the first thread then closed a server that was gone.
"""

from __future__ import annotations

import os
import signal
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Protocol

import pytest

from catalog_sync.cli_servers import stop_on_signal
from catalog_sync.mock.platform_server import PlatformServer
from catalog_sync.mock.server import MockSaasServer

pytestmark = pytest.mark.mock

HOLD_S = 0.3


class Server(Protocol):
    @property
    def port(self) -> int: ...
    def bind(self) -> object: ...
    def serve_forever(self) -> None: ...
    def shutdown(self) -> None: ...


def make_mock(_tmp_path: Path) -> Server:
    return MockSaasServer()


def make_platform(tmp_path: Path) -> Server:
    return PlatformServer(tmp_path / "platform.db")


@pytest.fixture
def thread_crashes(monkeypatch: pytest.MonkeyPatch) -> list[threading.ExceptHookArgs]:
    seen: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, "excepthook", seen.append)
    return seen


@pytest.fixture(autouse=True)
def keep_signal_handlers() -> Iterator[None]:
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield
    for sig, handler in before.items():
        signal.signal(sig, handler)


def hold_after_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    original = socketserver.BaseServer.shutdown

    def held(self: socketserver.BaseServer) -> None:
        original(self)
        time.sleep(HOLD_S)

    monkeypatch.setattr(socketserver.BaseServer, "shutdown", held)


def wait_for_stop_threads() -> None:
    for thread in threading.enumerate():
        if thread is not threading.main_thread() and not isinstance(thread, threading.Timer):
            thread.join(timeout=10)


@pytest.mark.parametrize("make", [make_mock, make_platform], ids=["mock-saas", "platform"])
def test_a_signal_and_the_end_of_the_serve_loop_both_stopping_the_server_is_clean(
    make: Callable[[Path], Server],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    thread_crashes: list[threading.ExceptHookArgs],
) -> None:
    server = make(tmp_path)
    server.bind()
    port = server.port
    hold_after_shutdown(monkeypatch)
    stop_on_signal(server)
    threading.Timer(0.2, os.kill, args=(os.getpid(), signal.SIGTERM)).start()
    try:
        server.serve_forever()
    finally:
        server.shutdown()  # the cmd_*_serve `finally`, which runs while the signal thread is held
    wait_for_stop_threads()
    assert thread_crashes == [], [str(c.exc_value) for c in thread_crashes]
    with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
        pass  # the port is closed


@pytest.mark.parametrize("make", [make_mock, make_platform], ids=["mock-saas", "platform"])
def test_shutdown_is_safe_to_call_twice_and_from_two_threads(
    make: Callable[[Path], Server],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    thread_crashes: list[threading.ExceptHookArgs],
) -> None:
    server = make(tmp_path)
    server.bind()
    hold_after_shutdown(monkeypatch)
    serving = threading.Thread(target=server.serve_forever)
    serving.start()
    stoppers = [threading.Thread(target=server.shutdown) for _ in range(3)]
    for stopper in stoppers:
        stopper.start()
    for stopper in stoppers:
        stopper.join(timeout=10)
    serving.join(timeout=10)
    server.shutdown()
    assert thread_crashes == [] and not serving.is_alive()
