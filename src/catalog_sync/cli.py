"""Command-line entry point.

Exit codes: 0 ok, 1 generic failure (including runs that finished with failed items), 2 the
credential is missing, expired or could not be obtained (the message says how to renew it),
3 the failure fuse tripped, 4 the source's compliance declaration is missing or invalid (no
request was made).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from catalog_sync import __version__
from catalog_sync.cli_credentials import cmd_auth_status, cmd_login
from catalog_sync.cli_init import add_init_command
from catalog_sync.cli_mock import add_mock_commands
from catalog_sync.cli_pipeline import (
    add_max_failed_ratio_argument,
    cmd_audit_show,
    cmd_load,
    cmd_normalize,
    cmd_run,
    pull_with_audit,
)
from catalog_sync.cli_runs import cmd_cache_purge, cmd_runs_list, cmd_runs_show, format_counts
from catalog_sync.cli_scaffold import add_scaffold_commands
from catalog_sync.cli_schedule import add_schedule_commands
from catalog_sync.cli_sources import build_source as _build_source
from catalog_sync.cli_sources import check_gate, describe_source, print_inspection
from catalog_sync.config import Config, Workspace, find_config, load_config, resolve_workspace
from catalog_sync.errors import EXIT_COMPLETED_WITH_FAILURES, CatalogSyncError, ConfigError
from catalog_sync.puller import ALL_STAGES, PullReport
from catalog_sync.redact import redact_text, redacted_logging
from catalog_sync.sources.base import Inspectable
from catalog_sync.state import Stage

_STAGE_CHOICES = ("list", "detail", "assets", "all")
logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Context:
    config: Config | None
    workspace: Workspace
    clock: Callable[[], datetime] = field(default=_utc_now)


CommandRunner = Callable[[argparse.Namespace, Context], int]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    with redacted_logging():
        return _run(args)


def _run(args: argparse.Namespace) -> int:
    try:
        config_path = find_config(args.config)
        # `init` writes the config; an existing one (maybe broken, maybe about to be replaced)
        # is not its business until it decides whether to overwrite it.
        loads_config = getattr(args, "loads_config", True)
        config = load_config(config_path) if config_path is not None and loads_config else None
        context = Context(config, resolve_workspace(args.workspace, config))
        run_command: CommandRunner = args.run_command
        return run_command(args, context)
    except CatalogSyncError as exc:
        # Messages are written not to carry secrets; masking them is the second line (redact.py).
        print(f"error: {redact_text(str(exc))}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted; a pull can be continued with --resume <run id>", file=sys.stderr)
        return 130
    except Exception:  # top-level handler: the traceback is logged, masked, before exit 1
        logger.exception(
            "unexpected error: this is a bug in catalog-sync or a plugin, not bad input"
        )
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="catalog-sync",
        description=(
            "Compliance-first pipeline to integrate your company's data from the SaaS tools it "
            "uses into your own platform: collect, clean, normalize, load."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, help="TOML config (default: ./catalog-sync.toml)")
    parser.add_argument(
        "--workspace",
        type=Path,
        help="directory for state, cache and credentials (default: $CATALOG_SYNC_HOME or "
        "~/.local/share/catalog-sync)",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    login = commands.add_parser(
        "login",
        help="store a credential",
        description="CREDENTIAL is a [credentials.NAME] entry of the config, or the name of an "
        "installed credential kind. Give the credential as a copied cURL command, by an HTTP "
        "login (--form, for services that offer one) or by logging in through a browser (needs "
        "the 'browser' extra).",
    )
    login.add_argument("credential")
    login.add_argument(
        "--paste-curl",
        metavar="FILE",
        help="read a browser's 'Copy as cURL' text from FILE ('-': stdin)",
    )
    login.add_argument(
        "--form",
        action="store_true",
        help="log in with a plain HTTP request, for services that support it (no browser needed)",
    )
    login.add_argument("--browser", action="store_true", help="log in through a browser")
    login.add_argument(
        "--headed",
        action="store_true",
        help="with --browser: show the window and log in by hand (the tool only takes the token)",
    )
    login.add_argument("--force", action="store_true", help="log in again even if still valid")
    login.set_defaults(run_command=cmd_login)

    auth = commands.add_parser("auth", help="credential utilities").add_subparsers(
        dest="auth_command", required=True, metavar="ACTION"
    )
    auth_status = auth.add_parser(
        "status", help="show whether a credential is still valid (exit 2 when expired)"
    )
    auth_status.add_argument("credential")
    auth_status.set_defaults(run_command=cmd_auth_status)

    inspect = commands.add_parser(
        "inspect",
        help="look at a source without pulling it",
        description="Totals and folder labels for one folder, plus the names of the fields a "
        "record has. Makes a couple of requests; stores nothing.",
    )
    inspect.add_argument("source")
    inspect.add_argument("--folder", metavar="ID", help="the source's own folder id")
    _add_option_argument(inspect)
    inspect.set_defaults(run_command=_cmd_inspect)

    pull = commands.add_parser(
        "pull",
        help="pull a source into a new (or resumed) run",
        description="SOURCE is a [sources.NAME] entry of the config, or the name of an installed "
        "source plugin, in which case its options come from --option.",
    )
    pull.add_argument("source")
    pull.add_argument("--stage", choices=_STAGE_CHOICES, default="all")
    pull.add_argument("--resume", metavar="RUN", help="continue an unfinished run")
    pull.add_argument("--max-items", type=int, metavar="N", help="stop listing after N items")
    pull.add_argument("--full", action="store_true", help="ignore stored versions; refetch all")
    _add_option_argument(pull)
    pull.set_defaults(run_command=_cmd_pull)

    normalize = commands.add_parser(
        "normalize",
        help="turn a run's raw responses into records (offline, from the cache)",
        description="Reads the stored responses of a run and applies a mapping; makes no network "
        "request, so a changed mapping can be tried again at once. SOURCE finds the newest run "
        "when --run is 'latest' (a [sources.*] entry or the name runs are recorded under, e.g. "
        "mock.materials); it is ignored when --run names a run. Writes FILE.jsonl and "
        "FILE.jsonl.meta.json, which `load` needs.",
    )
    normalize.add_argument("source")
    normalize.add_argument("--run", default="latest", metavar="RUN", help="a run id, or 'latest'")
    normalize.add_argument(
        "--mapping",
        metavar="FILE",
        help="the mapping to apply; without it, the mapping the connector of SOURCE ships "
        "(SOURCE names a connector, or a [sources.*] entry that runs one)",
    )
    normalize.add_argument("--out", required=True, metavar="FILE.jsonl")
    normalize.set_defaults(run_command=cmd_normalize)

    load = commands.add_parser(
        "load",
        help="load normalised records into a configured sink",
        description="SINK is a [sinks.NAME] entry of the config. Takes a backup first; never "
        "deletes a row. --dry-run prints what would change and writes nothing, not even a backup.",
    )
    load.add_argument("--sink", required=True)
    load.add_argument("--input", required=True, metavar="FILE.jsonl")
    load.add_argument("--dry-run", action="store_true")
    load.set_defaults(run_command=cmd_load)

    run = commands.add_parser(
        "run",
        help="run a profile: pull, normalize, load",
        description="PROFILE is a [profiles.NAME] entry of the config: its source, mapping and "
        "sinks. Equivalent to pull, normalize and load in order, with the same audit entries. "
        f"Exits {EXIT_COMPLETED_WITH_FAILURES} when it finished but some items failed (up to "
        "--max-failed-ratio of those fetched) and 1 when more than that failed.",
    )
    run.add_argument("profile")
    add_max_failed_ratio_argument(run)
    run.set_defaults(run_command=cmd_run)

    audit = commands.add_parser("audit", help="the audit log").add_subparsers(
        dest="audit_command", required=True, metavar="ACTION"
    )
    audit_show = audit.add_parser("show", help="print the audit log, oldest first")
    audit_show.add_argument("--last", type=int, default=0, metavar="N", help="only the last N")
    audit_show.add_argument("--json", action="store_true", help="one JSON object per line")
    audit_show.set_defaults(run_command=cmd_audit_show)

    runs = commands.add_parser("runs", help="inspect past runs").add_subparsers(
        dest="runs_command", required=True, metavar="ACTION"
    )
    runs_list = runs.add_parser("list", help="list runs, newest first")
    runs_list.add_argument("--source", metavar="SOURCE", help="only runs of this source")
    runs_list.set_defaults(run_command=cmd_runs_list)
    runs_show = runs.add_parser("show", help="show one run in detail")
    runs_show.add_argument("run_id")
    runs_show.set_defaults(run_command=cmd_runs_show)

    cache = commands.add_parser("cache", help="manage the snapshot cache").add_subparsers(
        dest="cache_command", required=True, metavar="ACTION"
    )
    purge = cache.add_parser(
        "purge",
        help="delete cached runs that are past their retention",
        description="Deletes the raw responses of runs older than the retention each source "
        "declared in its compliance block (or older than --older-than). A run that unchanged "
        "items still depend on is never deleted: it is listed as kept, with the reason. "
        "Writes an audit line.",
    )
    purge.add_argument(
        "--older-than",
        metavar="AGE",
        help="use this age instead of each source's declared retention, in days, for example 30d",
    )
    purge.add_argument("--source", metavar="SOURCE", help="only runs of this source")
    purge.add_argument(
        "--include-running",
        action="store_true",
        help="also purge runs still marked 'running' (a killed pull stays marked that way)",
    )
    purge.add_argument("--dry-run", action="store_true", help="say what would go, delete nothing")
    purge.set_defaults(run_command=cmd_cache_purge)

    add_schedule_commands(commands)
    add_init_command(commands)
    add_mock_commands(commands)
    add_scaffold_commands(commands)
    return parser


def _add_option_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--option",
        action="append",
        default=[],
        type=_parse_option,
        metavar="KEY=VALUE",
        help="plugin option (repeatable); overrides the config entry",
    )


def _parse_option(text: str) -> tuple[str, str]:
    key, separator, value = text.partition("=")
    if not separator or not key:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    return key, value


# --- pull -----------------------------------------------------------------------------------


def _cmd_pull(args: argparse.Namespace, context: Context) -> int:
    stages = ALL_STAGES if args.stage == "all" else (Stage(args.stage),)
    pulled = pull_with_audit(
        context,
        args.source,
        dict(args.option),
        build=_build_source,
        stages=stages,
        resume=args.resume,
        max_items=args.max_items,
        incremental=not args.full,
    )
    _print_report(pulled.report)
    return EXIT_COMPLETED_WITH_FAILURES if pulled.report.failures else 0


def _print_report(report: PullReport) -> None:
    print(f"run {report.run_id} ({report.source}): {report.status.replace('_', ' ')}")
    print(
        f"  items: {report.item_count}"
        + ("  (listing stopped at --max-items)" if report.is_truncated else "")
    )
    for stage in Stage:
        counts = report.stage_counts[stage]
        print(f"  {stage:<7}: {format_counts(counts)}")
    print(f"  fetched this time: {report.detail_fetches} detail, {report.asset_fetches} assets")
    for failure in report.failures[:10]:
        print(f"  failed {failure.stage} {failure.source_id}: {failure.error}", file=sys.stderr)
    if len(report.failures) > 10:
        print(
            f"  ... and {len(report.failures) - 10} more (catalog-sync runs show)", file=sys.stderr
        )


# --- inspect --------------------------------------------------------------------------------


def _cmd_inspect(args: argparse.Namespace, context: Context) -> int:
    check_gate(describe_source(args.source, dict(args.option), context), context, should_warn=True)
    source = _build_source(args.source, dict(args.option), context)
    if not isinstance(source, Inspectable):
        raise ConfigError(f"source {args.source!r} does not support inspect")
    print_inspection(args.source, source.name, source.inspect(args.folder))
    return 0
