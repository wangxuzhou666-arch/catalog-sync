"""The reference platform over HTTP: a standard-library server around ``PlatformService``.

Same shape as ``MockSaasServer`` (background thread for tests, foreground for ``catalog-sync mock
platform serve``), listening on loopback unless told otherwise, and refusing to listen anywhere
else while the public demo token is in place.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Self

from catalog_sync.errors import ConfigError
from catalog_sync.http import is_loopback_host
from catalog_sync.mock.platform import (
    DEMO_TOKEN,
    MAX_BODY_BYTES,
    PlatformEvent,
    PlatformService,
)
from catalog_sync.mock.server import handler_for

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8780
_POLL_S = 0.05


class PlatformServer:
    def __init__(
        self,
        db_path: Path,
        *,
        token: str = DEMO_TOKEN,
        host: str = DEFAULT_HOST,
        port: int = 0,
        on_event: Callable[[PlatformEvent], None] | None = None,
    ) -> None:
        if not is_loopback_host(host) and token == DEMO_TOKEN:
            raise ConfigError(
                f"refusing to listen on {host}: the demo token is public. Set the token "
                "variable first, or listen on 127.0.0.1"
            )
        self.service = PlatformService(db_path, token=token, on_event=on_event)
        self._host = host
        self._port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop_lock = threading.Lock()

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("the server is not bound yet; call bind() or start()")
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self.port}"

    @property
    def ingest_url(self) -> str:
        return f"{self.base_url}/api/v1/ingest"

    def bind(self) -> ThreadingHTTPServer:
        """Open the listening socket. Requests wait in its backlog until something serves them."""
        if self._httpd is None:
            handler = handler_for(self.service, max_body_bytes=MAX_BODY_BYTES)
            try:
                self._httpd = ThreadingHTTPServer((self._host, self._port), handler)
            except OSError as exc:
                raise ConfigError(
                    f"cannot listen on {self._host}:{self._port}: {exc.strerror}"
                ) from exc
            self._httpd.daemon_threads = True
        return self._httpd

    def serve_forever(self) -> None:
        self.bind().serve_forever(poll_interval=_POLL_S)  # shutdown waits up to one interval

    def shutdown(self) -> None:
        """Stop `serve_forever` (callable from another thread) and release the port."""
        # The signal thread (cli_servers.stop_on_signal) and the main thread's `finally` both end
        # up here. Without the lock the second one finds `_httpd` still set between the other's
        # `shutdown()` and `server_close()`, and the first then closes `None`.
        with self._stop_lock:
            httpd, self._httpd = self._httpd, None
            if httpd is None:
                return
            httpd.shutdown()
            httpd.server_close()

    def start(self) -> Self:
        self.bind()
        self._thread = threading.Thread(
            target=self.serve_forever, name="mock-platform", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.stop()
