"""What the documents say about exit codes, sink paths and the redirect and User-Agent policies.

Each check ties a sentence of the docs to the code (or to the other language of the README), so the
page cannot keep describing a behaviour that has gone: a new exit code that no table lists, a demo
that writes into the checkout again, a guide snippet that registers a name the scaffold does not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from catalog_sync import registry
from catalog_sync.errors import EXIT_COMPLETED_WITH_FAILURES, CatalogSyncError
from catalog_sync.http import _MAX_REDIRECTS
from catalog_sync.scaffold import ConnectorSpec
from tests.conftest import REPO_ROOT

README = REPO_ROOT / "README.md"
README_ZH = REPO_ROOT / "README.zh-CN.md"
DEPLOY = REPO_ROOT / "docs" / "deploy.md"
GUIDE = REPO_ROOT / "docs" / "connector-guide.md"
COMPLIANCE = REPO_ROOT / "docs" / "compliance.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
READMES = [README, README_ZH]
INTERRUPTED = 130  # the shell's code for SIGINT; the CLI returns it, no exception class carries it


def text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def all_subclasses(base: type[CatalogSyncError]) -> set[type[CatalogSyncError]]:
    found: set[type[CatalogSyncError]] = set()
    for child in base.__subclasses__():
        found |= {child} | all_subclasses(child)
    return found


# --- exit codes -----------------------------------------------------------------------------------


def exit_codes_of_the_tool() -> set[int]:
    raised = {CatalogSyncError.exit_code} | {
        cls.exit_code for cls in all_subclasses(CatalogSyncError)
    }
    return raised | {0, EXIT_COMPLETED_WITH_FAILURES, INTERRUPTED}


def exit_code_table(path: Path) -> str:
    """The rows of the exit code table, found by its first row (`| 0 |`), up to the blank line.

    Not by its header: the Chinese header is CJK text, which only README.zh-CN.md may hold.
    """
    _, _, rows = text_of(path).partition("\n| 0 |")
    assert rows, "no exit code table"
    return "| 0 |" + rows.split("\n\n", 1)[0]


@pytest.mark.parametrize("path", READMES, ids=lambda p: p.name)
def test_the_readme_exit_code_table_lists_every_exit_code_the_tool_has(path: Path) -> None:
    listed = {int(code) for code in re.findall(r"(?m)^\| (\d+) \|", exit_code_table(path))}
    assert listed == exit_codes_of_the_tool()


def test_exit_code_5_is_the_one_new_code_and_the_tables_know_what_it_means() -> None:
    assert EXIT_COMPLETED_WITH_FAILURES == 5
    for path in READMES:
        row = next(line for line in exit_code_table(path).splitlines() if line.startswith("| 5 |"))
        assert "--max-failed-ratio" in row and "max_failed_ratio" in row, path.name


@pytest.mark.parametrize("path", [README, README_ZH, DEPLOY], ids=lambda p: p.name)
def test_scheduling_and_health_describe_the_ratio_and_degraded_cycles(path: Path) -> None:
    text = text_of(path)
    assert "--max-failed-ratio" in text
    assert "degraded" in text
    assert "ok_with_failures" in text


@pytest.mark.parametrize("path", READMES, ids=lambda p: p.name)
def test_the_command_reference_gives_run_and_schedule_the_ratio_option(path: Path) -> None:
    lines = text_of(path).splitlines()
    run = next(line for line in lines if line.startswith("run PROFILE"))
    schedule = next(line for line in lines if line.startswith("schedule PROFILE"))
    assert "--max-failed-ratio" in run and "--max-failed-ratio" in schedule


def test_the_deploy_guide_says_what_exit_5_does_to_a_oneshot_unit_and_to_the_scheduler() -> None:
    text = text_of(DEPLOY)
    assert "Exit 5" in text and "SuccessExitStatus=5" in text
    assert "never exits with it" in text


# --- login is gated; paste-curl is not ----------------------------------------------------------


@pytest.mark.parametrize("path", [README, GUIDE, DEPLOY, COMPLIANCE], ids=lambda p: p.name)
def test_login_by_form_or_browser_is_documented_as_gated_and_paste_curl_as_not(path: Path) -> None:
    text = text_of(path)
    assert "login --form" in text or "`--form`" in text
    assert "--paste-curl" in text
    assert re.search(
        r"(?i)paste-curl`? (?:makes no request|is not gated|only reads|sends nothing)", text
    )


# --- redirects and the User-Agent ---------------------------------------------------------------


@pytest.mark.parametrize("path", [README, GUIDE, COMPLIANCE], ids=lambda p: p.name)
def test_the_redirect_policy_names_the_hop_limit_the_code_has(path: Path) -> None:
    assert f"{_MAX_REDIRECTS} redirects" in text_of(path)


@pytest.mark.parametrize("path", [README, README_ZH, GUIDE, CONTRIBUTING], ids=lambda p: p.name)
def test_the_user_agent_policy_has_one_home_that_everything_else_links_to(path: Path) -> None:
    assert "compliance.md#user-agent-policy" in text_of(path)
    assert "### User-Agent policy" in text_of(COMPLIANCE)


def test_the_guide_and_the_compliance_page_state_the_same_default_user_agent() -> None:
    default = re.compile(r"catalog-sync/<version> \(\+<?project URL>?\)")
    assert default.search(text_of(GUIDE)) and default.search(text_of(COMPLIANCE))


# --- where the demo and the sinks write ---------------------------------------------------------

CLOUD_NOTE = "never point sinks at a cloud-synced folder (icloud/dropbox/onedrive)"


@pytest.mark.parametrize("path", [README, DEPLOY], ids=lambda p: p.name)
def test_the_english_documents_warn_against_cloud_synced_sinks(path: Path) -> None:
    assert CLOUD_NOTE in " ".join(text_of(path).lower().split()), "a line break may sit inside"


def test_the_chinese_readme_carries_the_same_warning() -> None:
    assert "iCloud/Dropbox/OneDrive" in text_of(README_ZH)


@pytest.mark.parametrize("path", READMES, ids=lambda p: p.name)
def test_no_demo_sink_file_is_named_without_the_workspace(path: Path) -> None:
    """`out/mock.db` alone is a file in the checkout; the sinks write to `{workspace}/out/`."""
    bare = re.findall(r"(?<!/)\bout/(?:mock|catalog)\.\w+", text_of(path))
    assert bare == []


def test_the_scratch_workspace_of_run_readme_is_what_ci_expects_the_demo_files_in() -> None:
    ci = text_of(REPO_ROOT / ".github" / "workflows" / "ci.yml")
    for name in ("mock.db", "catalog.db", "catalog.jsonl", "catalog.csv"):
        assert f"--expect .catalog-sync-home/out/{name}" in ci
    assert ci.count("--expect out/mock.db") == 0


def test_local_agent_worktrees_are_ignored() -> None:
    assert ".claude/" in text_of(REPO_ROOT / ".gitignore").splitlines()


# --- names the docs use for the code ------------------------------------------------------------


def test_the_guide_registers_the_connector_under_the_plugin_name_the_scaffold_generates() -> None:
    spec = ConnectorSpec("acme", "bearer", "page")
    guide = text_of(GUIDE)
    assert f'"{spec.source_name}" = "{spec.package}:item_connector"' in guide
    assert '"acme-items" =' not in guide


def test_the_registry_docstring_counts_the_groups_it_lists() -> None:
    doc = registry.__doc__ or ""
    named = re.search(r"(Three|Four|Five|Six) more groups", doc)
    assert named is not None
    listed = re.findall(r"(?m)^\* ``catalog_sync\.", doc)
    assert {"Three": 3, "Four": 4, "Five": 5, "Six": 6}[named.group(1)] == len(listed)
