"""The ``mock platform serve`` command: the reference receiver of the HTTP sink.

Started like the Mock SaaS (foreground, or ``--detach --pid-file F`` for scripts and the README;
``mock stop --pid-file F`` ends either). The token it accepts comes from an environment variable,
``PLATFORM_TOKEN`` unless ``--token-env`` names another; when that is not set it is the public demo
token, which is why it then listens on loopback only.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from catalog_sync.cli_servers import serve_detached, stop_on_signal
from catalog_sync.errors import ConfigError
from catalog_sync.fsutil import atomic_write_bytes
from catalog_sync.mock.platform import DEMO_TOKEN, TOKEN_ENV, PlatformEvent
from catalog_sync.mock.platform_server import DEFAULT_HOST, DEFAULT_PORT, PlatformServer

if TYPE_CHECKING:
    from catalog_sync.cli import Context

DEFAULT_DB = Path("platform.db")


def add_platform_commands(
    mock_actions: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    platform = mock_actions.add_parser(
        "platform",
        help="run the reference receiver of the HTTP sink (a stand-in for your platform)",
        description="A platform that implements docs/platform-contract.md and stores what it "
        "receives in SQLite, so the HTTP sink can be tried, tested and read against something "
        "real. It checks every rule of the contract on the way in.",
    ).add_subparsers(dest="platform_command", required=True, metavar="ACTION")
    serve = platform.add_parser(
        "serve",
        help="start it (foreground, or --detach for the background)",
        description=f"Listens on loopback. It accepts the bearer token held in ${TOKEN_ENV} "
        "(or the variable --token-env names); without one it accepts the public demo token "
        f"{DEMO_TOKEN}, shown on start, and refuses to listen beyond loopback.",
    )
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help="0 picks a free port")
    serve.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        metavar="PATH",
        help=f"the SQLite file it stores into (default: ./{DEFAULT_DB})",
    )
    serve.add_argument(
        "--token-env",
        default=TOKEN_ENV,
        metavar="NAME",
        help=f"the environment variable that holds the token (default: {TOKEN_ENV})",
    )
    serve.add_argument("--pid-file", type=Path, metavar="FILE", help="write the pid and address")
    serve.add_argument(
        "--detach", action="store_true", help="run in the background; needs --pid-file"
    )
    serve.add_argument(
        "--verbose", action="store_true", help="print a line for every request, not only replays"
    )
    serve.set_defaults(run_command=cmd_platform_serve)


def cmd_platform_serve(args: argparse.Namespace, _context: Context) -> int:
    if args.detach:
        return serve_detached(
            args,
            what="mock platform",
            command=("mock", "platform", "serve"),
            arguments=_serve_arguments(args),
        )
    token = os.environ.get(args.token_env) or DEMO_TOKEN
    verbose: bool = args.verbose
    server = PlatformServer(
        args.db,
        token=token,
        host=args.host,
        port=args.port,
        on_event=lambda event: _print_event(event, verbose=verbose),
    )
    server.bind()  # an address already in use fails here, before anything is announced
    if args.pid_file is not None:
        atomic_write_bytes(args.pid_file, f"{os.getpid()}\n{server.base_url}\n".encode())
    _announce(server, args, is_demo_token=token == DEMO_TOKEN)
    stop_on_signal(server)
    try:
        server.serve_forever()
    finally:
        server.shutdown()
        if args.pid_file is not None:
            args.pid_file.unlink(missing_ok=True)
    return 0


def _serve_arguments(args: argparse.Namespace) -> list[str]:
    """The `serve` command line for the background process: this one, minus --detach."""
    if args.port < 0:
        raise ConfigError("--port cannot be negative")
    given = [
        "--host", args.host,
        "--port", str(args.port),
        "--db", str(args.db),
        "--token-env", args.token_env,
        "--pid-file", str(args.pid_file),
    ]  # fmt: skip
    if args.verbose:
        given.append("--verbose")
    return given


def _announce(server: PlatformServer, args: argparse.Namespace, *, is_demo_token: bool) -> None:
    print(f"mock platform listening on {server.base_url} (pid {os.getpid()})", flush=True)
    print(f"  send batches to: {server.ingest_url}", flush=True)
    if is_demo_token:
        print(
            f"  token: {DEMO_TOKEN} (the public demo default; set ${args.token_env} to change it)",
            flush=True,
        )
    else:
        print(f"  token: from ${args.token_env}", flush=True)
    print(f"  stores into: {args.db}", flush=True)


def _print_event(event: PlatformEvent, *, verbose: bool) -> None:
    if verbose or event.kind != "ingested":
        print(event.line(), file=sys.stdout, flush=True)
