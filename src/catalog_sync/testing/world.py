"""A fake service and a client wired together without a network, on one shared clock.

This is the pattern the connectors' own tests use, for yours: write the service as a function from
a recorded request to a response (``FakeTransport`` calls it), build the connector with the world's
``seams``, and run it. The clock is shared on purpose. A client that is told to wait (a backoff, a
``Retry-After``, the gap a rate ceiling implies) advances the *service's* clock by exactly as much,
so a rate window or a token lifetime in the service moves as it would in real time and the test
takes none::

    world = ServiceWorld(my_service)             # my_service(RecordedRequest) -> FakeResponse
    source = MySource(settings, seams=world.seams)
    ...
    world.clock.now += 3600                      # an hour passes: the token has expired

Build the service with ``clock=world.clock`` (or make it read ``world.clock.now``) when it has
lifetimes of its own; ``ServiceWorld`` accepts a ``WorldClock`` made first for that reason.
"""

from __future__ import annotations

import time

from catalog_sync.http import HttpSeams
from catalog_sync.testing.fake_transport import FakeTransport, Responder


class WorldClock:
    """One clock for the server and the client: sleeping moves it, so waiting is instant."""

    def __init__(self) -> None:
        # Near the real time, so the expiry of a token that the tool's own checks read is sensible.
        self.now = time.time()
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ServiceWorld:
    def __init__(self, service: Responder, *, clock: WorldClock | None = None) -> None:
        self.clock = clock or WorldClock()
        self.transport = FakeTransport(service)
        self.seams = HttpSeams(
            adapter=self.transport, sleep=self.clock.sleep, monotonic=self.clock.monotonic
        )
