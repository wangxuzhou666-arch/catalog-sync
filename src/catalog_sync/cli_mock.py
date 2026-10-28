"""The ``mock`` commands: start and stop the built-in fake SaaS.

``mock serve`` runs it in the foreground. ``mock serve --detach --pid-file F`` starts it in the
background and returns once it is listening, which is what a script or a README needs: the next
command can log in at once. ``mock stop --pid-file F`` ends it. The pid file also holds the
address, so nothing has to guess the port.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import TYPE_CHECKING

from catalog_sync.cli_platform import add_platform_commands
from catalog_sync.cli_servers import serve_detached, stop_by_pid_file, stop_on_signal
from catalog_sync.errors import ConfigError
from catalog_sync.fsutil import atomic_write_bytes
from catalog_sync.mock.auth import PASSWORD_ENV, USER_ENV, account_from_env
from catalog_sync.mock.data import DEFAULT_MATERIALS, DEFAULT_SUPPLIERS
from catalog_sync.mock.server import DEFAULT_HOST, DEFAULT_PORT, MockSaasServer
from catalog_sync.mock.service import (
    DEFAULT_TOKEN_TTL_S,
    LoggedRequest,
    MockOptions,
    RateLimit,
)

if TYPE_CHECKING:
    from catalog_sync.cli import Context


def add_mock_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    mock = commands.add_parser(
        "mock",
        help="run the built-in fake SaaS (a demo service with nothing real behind it)",
        description="A fake SaaS to run the whole pipeline against with no account, no credential "
        "and no legal question: login, paginated JSON APIs, details, pictures and the usual "
        "traps. Its data is invented and seeded.",
    ).add_subparsers(dest="mock_command", required=True, metavar="ACTION")

    serve = mock.add_parser(
        "serve",
        help="start it (foreground, or --detach for the background)",
        description=f"Listens on loopback. The demo account is set by {USER_ENV} / {PASSWORD_ENV}; "
        "without them it is the public demo default shown on start, so listening beyond loopback "
        "requires both.",
    )
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help="0 picks a free port")
    serve.add_argument("--seed", type=int, default=1, help="the same seed gives the same data")
    serve.add_argument("--materials", type=int, default=DEFAULT_MATERIALS, metavar="N")
    serve.add_argument("--suppliers", type=int, default=DEFAULT_SUPPLIERS, metavar="N")
    traps = serve.add_argument_group("traps (the token lasts a week unless you shorten it)")
    traps.add_argument(
        "--token-ttl",
        type=float,
        default=DEFAULT_TOKEN_TTL_S,
        metavar="SECONDS",
        help="how long a token or session lives; after that the API answers 401 "
        "(default: 7 days; try 20 to see it expire)",
    )
    traps.add_argument(
        "--expire-after",
        type=int,
        default=0,
        metavar="N",
        help="a login works for N data requests, then the API answers 401 until the next login "
        "(a token that dies in the middle of a run, without depending on the clock)",
    )
    traps.add_argument(
        "--rate-limit",
        type=_rate_limit,
        metavar="COUNT/SECONDS",
        help="answer 429 with Retry-After beyond COUNT requests in SECONDS, e.g. 30/10",
    )
    traps.add_argument(
        "--error-every",
        type=int,
        default=0,
        metavar="N",
        help="answer every Nth data request with 503 (a client retries it)",
    )
    traps.add_argument(
        "--gone-every",
        type=int,
        default=0,
        metavar="N",
        help="the detail of every Nth material answers 404 although the listing shows it "
        "(an item that fails for good: the run goes on without it)",
    )
    traps.add_argument(
        "--signed-urls",
        type=float,
        metavar="SECONDS",
        help="sign picture links; they stop working after SECONDS (403)",
    )
    serve.add_argument("--pid-file", type=Path, metavar="FILE", help="write the pid and address")
    serve.add_argument(
        "--detach", action="store_true", help="run in the background; needs --pid-file"
    )
    serve.add_argument("--verbose", action="store_true", help="print one line per request")
    serve.set_defaults(run_command=cmd_mock_serve)

    stop = mock.add_parser("stop", help="stop a mock server started with --pid-file")
    stop.add_argument("--pid-file", type=Path, required=True, metavar="FILE")
    stop.set_defaults(run_command=cmd_mock_stop)

    add_platform_commands(mock)


def _rate_limit(text: str) -> RateLimit:
    try:
        return RateLimit.parse(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def cmd_mock_serve(args: argparse.Namespace, _context: Context) -> int:
    if args.detach:
        return _serve_detached(args)
    options = _options(args)
    on_request = _print_request if args.verbose else None
    server = MockSaasServer(options, host=args.host, port=args.port, on_request=on_request)
    server.bind()  # an address already in use fails here, before anything is announced
    if args.pid_file is not None:
        atomic_write_bytes(args.pid_file, f"{os.getpid()}\n{server.base_url}\n".encode())
    _announce(server, options)
    stop_on_signal(server)
    try:
        server.serve_forever()
    finally:
        server.shutdown()
        if args.pid_file is not None:
            args.pid_file.unlink(missing_ok=True)
    return 0


def _options(args: argparse.Namespace) -> MockOptions:
    if args.materials < 0 or args.suppliers < 0:
        raise ConfigError("--materials and --suppliers cannot be negative")
    if args.token_ttl <= 0 or min(args.error_every, args.gone_every, args.expire_after) < 0:
        raise ConfigError(
            "--token-ttl must be positive; --error-every, --gone-every and --expire-after "
            "cannot be negative"
        )
    if args.signed_urls is not None and args.signed_urls <= 0:
        raise ConfigError("--signed-urls must be a positive number of seconds")
    return MockOptions(
        seed=args.seed,
        materials=args.materials,
        suppliers=args.suppliers,
        token_ttl_s=args.token_ttl,
        rate_limit=args.rate_limit,
        error_every=args.error_every,
        gone_every=args.gone_every,
        expire_after=args.expire_after,
        signed_url_ttl_s=args.signed_urls,
        account=account_from_env(os.environ),
    )


def _announce(server: MockSaasServer, options: MockOptions) -> None:
    account = options.account
    print(f"mock SaaS listening on {server.base_url} (pid {os.getpid()})", flush=True)
    if account.is_demo_default:
        print(
            f"  account: {account.user} / {account.password} (the public demo default; "
            f"set {USER_ENV} and {PASSWORD_ENV} to change it)",
            flush=True,
        )
    else:
        print(f"  account: {account.user} (password from {PASSWORD_ENV})", flush=True)
    print(f"  terms: {server.base_url}/terms   app: {server.base_url}/login", flush=True)


def _print_request(entry: LoggedRequest) -> None:
    print(f"{entry.method} {entry.path} -> {entry.status}", flush=True)


# --- background ---------------------------------------------------------------------------------


def _serve_detached(args: argparse.Namespace) -> int:
    return serve_detached(
        args, what="mock SaaS", command=("mock", "serve"), arguments=_serve_arguments(args)
    )


def _serve_arguments(args: argparse.Namespace) -> list[str]:
    """The `serve` command line for the background process: this one, minus --detach."""
    given = [
        "--host", args.host,
        "--port", str(args.port),
        "--seed", str(args.seed),
        "--materials", str(args.materials),
        "--suppliers", str(args.suppliers),
        "--token-ttl", str(args.token_ttl),
        "--error-every", str(args.error_every),
        "--gone-every", str(args.gone_every),
        "--expire-after", str(args.expire_after),
        "--pid-file", str(args.pid_file),
    ]  # fmt: skip
    if args.rate_limit is not None:
        given += ["--rate-limit", f"{args.rate_limit.requests}/{args.rate_limit.per_seconds}"]
    if args.signed_urls is not None:
        given += ["--signed-urls", str(args.signed_urls)]
    if args.verbose:
        given.append("--verbose")
    return given


def cmd_mock_stop(args: argparse.Namespace, _context: Context) -> int:
    stop_by_pid_file(args.pid_file)
    return 0
