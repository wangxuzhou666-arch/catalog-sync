from __future__ import annotations

import socket

import pytest
from pytest_socket import SocketBlockedError


@pytest.mark.filterwarnings("ignore:A test tried to use socket")  # pytest-socket warns as it blocks
def test_the_suite_cannot_reach_the_network() -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET)


def test_unix_sockets_stay_available_for_local_tooling() -> None:
    left, right = socket.socketpair()
    left.close()
    right.close()
