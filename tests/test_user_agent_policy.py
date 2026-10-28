"""One User-Agent policy: the SDK default names the tool; a browser string needs a listed reason.

docs/compliance.md states the policy and lists the exceptions. These tests tie that page to the
code, so a connector cannot start posing as a browser without the page saying so.
"""

from __future__ import annotations

import re
from pathlib import Path

from catalog_sync import __version__
from catalog_sync.credentials.base import Credential
from catalog_sync.plugins.mock.headers import api_headers
from catalog_sync.sdk import USER_AGENT
from catalog_sync.testing.user_agent import (
    BROWSER_MARKER,
    connectors_sending_a_browser_string,
    policy_section,
)
from tests.conftest import REPO_ROOT

PLUGINS = REPO_ROOT / "src" / "catalog_sync" / "plugins"
COMPLIANCE = (REPO_ROOT / "docs" / "compliance.md").read_text(encoding="utf-8")


def test_the_sdk_default_names_the_tool_its_version_and_where_to_find_it() -> None:
    assert re.fullmatch(rf"catalog-sync/{re.escape(__version__)} \(\+https?://\S+\)", USER_AGENT)


def test_the_default_carries_no_person_account_or_browser_claim() -> None:
    assert "@" not in USER_AGENT
    assert BROWSER_MARKER not in USER_AGENT


def test_a_connector_without_an_exception_sends_the_default() -> None:
    sent = api_headers(Credential({"authorization": "Bearer x"}, {}, None, None))
    assert sent["user-agent"] == USER_AGENT


def test_the_scan_for_browser_strings_finds_one_and_names_its_connector(tmp_path: Path) -> None:
    (tmp_path / "quiet").mkdir()
    (tmp_path / "quiet" / "headers.py").write_text("UA = 'catalog-sync/0'\n", encoding="utf-8")
    (tmp_path / "posing").mkdir()
    (tmp_path / "posing" / "headers.py").write_text(
        f"UA = '{BROWSER_MARKER}5.0 (X11)'\n", encoding="utf-8"
    )
    assert connectors_sending_a_browser_string(tmp_path) == ["posing"]


def test_every_connector_that_sends_a_browser_string_is_listed_with_its_reason() -> None:
    section = policy_section(COMPLIANCE)
    assert section, "docs/compliance.md has no 'User-Agent policy' section"
    for connector in connectors_sending_a_browser_string(PLUGINS):
        assert f"`{connector}." in section or f"`{connector}`" in section, (
            f"the {connector} connector sends a browser user-agent; docs/compliance.md "
            "(User-Agent policy) must list it and say why"
        )
