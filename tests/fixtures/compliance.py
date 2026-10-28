"""A valid compliance declaration for tests that configure a networked source."""

from __future__ import annotations

import textwrap
from pathlib import Path

# 60000 requests a minute is a 1 ms minimum gap, so tests that use the real clock stay fast; the
# review window is huge so the date never turns into a warning on a future test run.
BLOCK = """
    [sources.{name}.compliance]
    data_owner = "Example Co"
    authorization = "own_account"
    authorization_ref = "TICKET-1"
    tos_url = "https://vendor.example.invalid/terms"
    tos_reviewed_on = 2026-10-01
    tos_review_max_age_days = 36500
    max_requests_per_minute = 60000
    retention_days = 90
"""
GAP_S = 0.001


def block(name: str) -> str:
    return BLOCK.format(name=name)


def write_stub_config(directory: Path, name: str = "x") -> Path:
    """A config whose source `name` is a stand-in plugin that still needs the declaration."""
    path = directory / "stub-config.toml"
    path.write_text(f'[sources.{name}]\nplugin = "stub"\n' + textwrap.dedent(block(name)), "utf-8")
    return path
