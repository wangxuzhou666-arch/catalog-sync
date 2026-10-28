"""The built-in sink kinds, with what ``catalog-sync init`` needs to set one up.

Each sink module declares its options as ``SettingSpec`` values next to the code that reads them
(``SETTINGS``); this lists the kinds in the order ``init`` offers them, with a line about each.
A sink from another package is configured by hand: its options are not known here.
"""

from __future__ import annotations

from dataclasses import dataclass

from catalog_sync.connector_info import SettingSpec
from catalog_sync.sinks import csv as csv_sink
from catalog_sync.sinks import http as http_sink
from catalog_sync.sinks import jsonl as jsonl_sink
from catalog_sync.sinks import postgres as postgres_sink
from catalog_sync.sinks import sqlite as sqlite_sink


@dataclass(frozen=True)
class SinkKind:
    name: str
    summary: str
    settings: tuple[SettingSpec, ...]


SINK_KINDS: tuple[SinkKind, ...] = (
    SinkKind(
        "sqlite",
        "A SQLite database file: the simplest place to look at the result. Takes a backup first.",
        sqlite_sink.SETTINGS,
    ),
    SinkKind(
        "postgres",
        "A Postgres table (needs the 'postgres' extra); the connection string stays in an "
        "environment variable.",
        postgres_sink.SETTINGS,
    ),
    SinkKind(
        "http",
        "Your own platform, over HTTP (docs/platform-contract.md); the token stays in an "
        "environment variable.",
        http_sink.SETTINGS,
    ),
    SinkKind("jsonl", "A JSON Lines file, one record per line.", jsonl_sink.SETTINGS),
    SinkKind("csv", "A CSV file, safe to open in a spreadsheet.", csv_sink.SETTINGS),
)
