"""The ``new-connector`` command: write the skeleton of a connector package."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from catalog_sync.scaffold import (
    AUTH_KINDS,
    CORE_SOURCES,
    DEFAULT_AUTH,
    DEFAULT_CORE_SOURCE,
    DEFAULT_PAGINATION,
    PAGINATIONS,
    ConnectorSpec,
    default_directory,
    generate,
    resolve_core,
)

if TYPE_CHECKING:
    from catalog_sync.cli import Context


def add_scaffold_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "new-connector",
        help="write the skeleton of a connector for a new SaaS",
        description="Generates a Python package with its own pyproject.toml and entry points: "
        "settings, API calls, credential kind, source, mapping, schema, a fake service and tests "
        "that pass from the first minute, and a compliance block that the compliance gate "
        "refuses until its placeholders are replaced. Everything to change is marked "
        "TODO(connector). Makes no request.",
    )
    parser.add_argument("name", help="lowercase letters, digits and underscores, e.g. acme")
    parser.add_argument(
        "--dir",
        type=Path,
        metavar="PATH",
        help="where to write it (default: ./catalog-sync-NAME); must not exist or be empty",
    )
    parser.add_argument(
        "--auth",
        choices=AUTH_KINDS,
        default=DEFAULT_AUTH,
        help="how the service authenticates: bearer token (paste or browser login), session "
        "cookie or bearer token from an HTTP login (form), or an API key header "
        f"(default: {DEFAULT_AUTH})",
    )
    parser.add_argument(
        "--pagination",
        choices=PAGINATIONS,
        default=DEFAULT_PAGINATION,
        help="how the list endpoint pages: page number with a total, next-page token, or "
        f"offset and limit (default: {DEFAULT_PAGINATION})",
    )
    parser.add_argument(
        "--catalog-sync-source",
        choices=CORE_SOURCES,
        default=DEFAULT_CORE_SOURCE,
        help="where the new package gets catalog-sync from: the package index (pypi), the source "
        "checkout this command runs from (path, written as a relative [tool.uv.sources] entry so "
        "that `uv sync` works before catalog-sync is published), or whichever applies (auto, the "
        "default: path from a checkout or an editable install, pypi from an installed wheel)",
    )
    parser.set_defaults(run_command=cmd_new_connector)


def cmd_new_connector(args: argparse.Namespace, context: Context) -> int:
    del context  # generating a package reads neither the config nor the workspace
    spec = ConnectorSpec(args.name, args.auth, args.pagination)
    directory = args.dir or default_directory(spec, cwd=Path.cwd())
    core = resolve_core(args.catalog_sync_source)
    written = generate(spec, directory, core)
    print(
        f"wrote {spec.distribution} ({spec.auth} credential, {spec.pagination} pagination): "
        f"{len(written)} files in {directory}"
    )
    print("next:")
    print(f"  cd {directory}")
    print("  uv sync && uv run pytest -q          # green from the start, against a fake service")
    print('  grep -rn "TODO(connector)" .         # what is left to fill in for the real service')
    print(core.describe())
    print("The guide, docs/connector-guide.md in the catalog-sync repository, explains each step.")
    return 0
