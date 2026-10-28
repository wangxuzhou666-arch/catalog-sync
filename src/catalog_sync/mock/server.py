"""The Mock SaaS over HTTP: a standard-library server around ``MockService``.

``MockSaasServer`` can run in a background thread (tests, the in-process demo) or in the
foreground (``catalog-sync mock serve``). It listens on loopback unless told otherwise.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Protocol, Self

from catalog_sync.errors import ConfigError
from catalog_sync.http import is_loopback_host as is_loopback
from catalog_sync.mock.service import LoggedRequest, MockOptions, MockService, Request, Response

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_MAX_BODY_BYTES = 64 * 1024
# How often the serving loop looks for a stop request. `shutdown()` blocks for up to one interval;
# the library default (0.5 s) made every test that stopped a server wait half a second.
_POLL_S = 0.05
_HTTP_PAYLOAD_TOO_LARGE = 413


class Handles(Protocol):
    """What the HTTP skin needs of a service: a request in, a response out."""

    def handle(self, request: Request) -> Response: ...


class MockSaasServer:
    def __init__(
        self,
        options: MockOptions | None = None,
        *,
        host: str = DEFAULT_HOST,
        port: int = 0,
        clock: Callable[[], float] = time.time,
        on_request: Callable[[LoggedRequest], None] | None = None,
    ) -> None:
        resolved = options or MockOptions()
        if not is_loopback(host) and resolved.account.is_demo_default:
            raise ConfigError(
                f"refusing to listen on {host}: the demo password is public. Set "
                "MOCK_SAAS_PASSWORD (and MOCK_SAAS_USER) first, or listen on 127.0.0.1"
            )
        self.service = MockService(resolved, clock=clock, on_request=on_request)
        self._host = host
        self._port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop_lock = threading.Lock()

    @property
    def request_log(self) -> list[LoggedRequest]:
        return self.service.request_log

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self.port}"

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("the server is not bound yet; call bind() or start()")
        return int(self._httpd.server_address[1])

    def bind(self) -> ThreadingHTTPServer:
        """Open the listening socket. Requests wait in its backlog until something serves them."""
        if self._httpd is None:
            try:
                self._httpd = ThreadingHTTPServer(
                    (self._host, self._port), handler_for(self.service)
                )
            except OSError as exc:
                raise ConfigError(
                    f"cannot listen on {self._host}:{self._port}: {exc.strerror}"
                ) from exc
            self._httpd.daemon_threads = True
        return self._httpd

    def serve_forever(self) -> None:
        self.bind().serve_forever(poll_interval=_POLL_S)

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
        self._thread = threading.Thread(target=self.serve_forever, name="mock-saas", daemon=True)
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


def handler_for(
    service: Handles, *, max_body_bytes: int = _MAX_BODY_BYTES
) -> type[BaseHTTPRequestHandler]:
    """The request handler class that gives `service` an HTTP skin.

    A body larger than `max_body_bytes` is answered with 413 and the connection is closed, never
    read in part: a truncated body would be taken for a whole one.
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive: every reply below carries a Content-Length

        def do_GET(self) -> None:
            self._serve()

        def do_POST(self) -> None:
            self._serve()

        def _serve(self) -> None:
            declared = self.headers.get("Content-Length", "")
            length = int(declared) if declared.isdigit() else 0
            if length > max_body_bytes:
                self._reject_oversized()
                return
            request = Request(
                method=self.command,
                target=self.path,
                headers=dict(self.headers.items()),
                host=self.headers.get("Host") or "localhost",
                body=self.rfile.read(length),
            )
            self._send(service.handle(request))

        def _send(self, response: Response) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for name, value in response.headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.body)

        def _reject_oversized(self) -> None:
            self.close_connection = True  # the unread body would be mistaken for the next request
            body = b'{"error":"the request body is too large"}'
            self.send_response(_HTTP_PAYLOAD_TOO_LARGE)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return  # the service's own request log is the record

    return Handler
