"""The documents promise commands and options; make sure the parser still accepts them."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

from catalog_sync import cli
from catalog_sync.config import load_config
from catalog_sync.plugins.mock.login import mock_credential_kind
from catalog_sync.plugins.mock.settings import MockSettings
from catalog_sync.schema import parse_schema
from tests.conftest import REPO_ROOT
from tests.fixtures.mock_saas import World

GUIDE = REPO_ROOT / "docs" / "connector-guide.md"
COMMAND = re.compile(r"^\$ (?:pbpaste \| )?uv run (catalog-sync .+)$")


def documented_commands(document: Path) -> list[str]:
    commands = []
    for line in document.read_text(encoding="utf-8").splitlines():
        match = COMMAND.match(line.strip())
        if match:
            text = re.sub(r"\s+#.*$", "", match.group(1))  # trailing comment
            text = re.sub(r"\s*\^C.*$", "", text)
            commands.append(re.sub(r"<[A-Z_]+>", "x", text))
    return commands


def test_the_shipped_example_config_is_accepted_by_the_mock_plugin() -> None:
    config = load_config(REPO_ROOT / "catalog-sync.example.toml")
    options = config.sources["mock-materials"].options
    settings = MockSettings.from_options(
        {**options, "credential": World().handle()}, source="mock.materials"
    )
    assert (settings.root_folder_id, settings.max_depth, settings.page_size) == ("0", 6, 10)

    credential = config.credentials["mock"]
    mock_credential_kind(credential.options, base_dir=config.base_dir)


# --- the public documents -------------------------------------------------------------------------

DOCS = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "README.zh-CN.md",
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "SECURITY.md",
    REPO_ROOT / "docs" / "compliance.md",
    REPO_ROOT / "docs" / "publishing.md",
    GUIDE,
    REPO_ROOT / "docs" / "platform-contract.md",
    REPO_ROOT / "docs" / "deploy.md",
]
README = REPO_ROOT / "README.md"
README_ZH = REPO_ROOT / "README.zh-CN.md"
POSITIONING = (
    "A compliance-first pipeline to integrate your company's data from the SaaS tools it uses "
    "into your own platform: collect → clean → normalize → load."
)
LINK = re.compile(r"\]\((?!https?:|mailto:|#)([^)#\s]+)(?:#[^)]*)?\)")


@pytest.mark.parametrize("path", DOCS, ids=lambda path: path.name)
def test_every_relative_link_points_at_a_file_that_exists(path: Path) -> None:
    for target in LINK.findall(path.read_text(encoding="utf-8")):
        assert (path.parent / target).exists(), f"{path.name} links to {target}, which is missing"


def test_the_readme_states_the_positioning_and_has_the_promised_sections() -> None:
    text = README.read_text(encoding="utf-8")
    assert POSITIONING in text
    headings = re.findall(r"(?m)^## (.+)$", text)
    for wanted in (
        "Why",
        "How it works",
        "Quick start",
        "Try it in 60 seconds",
        "Connectors",
        "Configuration reference",
        "Command reference",
        "Writing your own source or sink",
        "Responsible use",
        "Security",
        "Development",
    ):
        assert wanted in headings, f"README lacks the section {wanted!r}"
    assert "not affiliated with" in text and "trademarks" in text


def test_the_chinese_readme_has_the_same_structure_and_the_same_code() -> None:
    english, chinese = (path.read_text(encoding="utf-8") for path in (README, README_ZH))
    assert len(re.findall(r"(?m)^## ", chinese)) == len(re.findall(r"(?m)^## ", english))
    assert len(re.findall(r"(?m)^### ", chinese)) == len(re.findall(r"(?m)^### ", english))
    code = re.compile(r"(?s)```(?:console|toml|yaml|python|)\n(.*?)```")
    assert code.findall(chinese) == code.findall(english), "a code block differs between the two"
    assert "<!-- ci:run -->" in chinese


def test_documents_name_no_tools_or_people_outside_the_tech_stack() -> None:
    forbidden = re.compile(
        r"(?i)\b(claude|anthropic|chatgpt|openai|copilot|cursor|gemini|codex|vscode|vs code)\b"
    )
    for path in DOCS:
        hits = forbidden.findall(path.read_text(encoding="utf-8"))
        assert hits == [], f"{path.name} names {hits}"


def test_documents_hold_no_local_paths_or_personal_addresses() -> None:
    local = re.compile(r"(?:^|[^A-Za-z0-9_.~-])/(?:Users|home)/[A-Za-z0-9._-]+/")
    address = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
    allowed = ("@example.com", "@example.org", "@example.net", "@users.noreply.github.com")
    for path in DOCS:
        text = path.read_text(encoding="utf-8")
        assert local.search(text) is None, f"{path.name} holds a home-directory path"
        assert [a for a in address.findall(text) if not a.endswith(allowed)] == []


def test_the_compliance_page_keeps_both_columns_and_every_non_evasion_rule() -> None:
    text = (REPO_ROOT / "docs" / "compliance.md").read_text(encoding="utf-8")
    assert "What the tool enforces" in text and "What remains your responsibility" in text
    for rule in ("No anti-detection", "No rotation", "Back off", "No CAPTCHA", "Stop on a refusal"):
        assert rule in text, f"docs/compliance.md lost the non-evasion rule {rule!r}"
    for topic in ("Declaration", "Terms of service", "Request rate", "Retention", "Audit trail"):
        assert f"**{topic}**" in text


def test_contributing_states_the_connector_rules_and_the_gate_rule() -> None:
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for phrase in (
        "Declare its compliance defaults",
        "Use synthetic fixtures only",
        "Never include a captured real response",
        "Not evade anything",
        "every new gate",
    ):
        assert phrase in text, f"CONTRIBUTING.md lost: {phrase}"


FENCED_YAML = re.compile(r"(?s)```yaml\n(version: 1\nentity: .*?)```")


@pytest.mark.parametrize("path", [README, README_ZH], ids=lambda path: path.name)
def test_the_schema_example_in_the_readme_is_a_valid_schema(path: Path) -> None:
    """A documented schema that the loader refuses would be a quiet lie."""
    blocks = FENCED_YAML.findall(path.read_text(encoding="utf-8"))
    assert len(blocks) == 1, "the README shows exactly one schema file"
    schema = parse_schema(yaml.safe_load(blocks[0]), "the README example")
    assert schema.entity == "supplier" and {s.name for s in schema.fields} >= {
        "rating",
        "listed_at",
    }


COMMAND_REFERENCE = re.compile(r"(?s)## Command reference\n\n```\n(.*?)```")


def reference_lines() -> list[str]:
    block = COMMAND_REFERENCE.search(README.read_text(encoding="utf-8"))
    assert block is not None, "the README has no command reference"
    return [
        line
        for line in block.group(1).splitlines()
        if line and not line.startswith("catalog-sync ")
    ]


def test_the_command_reference_lists_every_subcommand_the_tool_has() -> None:
    lines = reference_lines()
    verbs = {
        tuple(re.match(r"([a-z][a-z-]*(?: [a-z]+)?)", line).group(1).split()) for line in lines
    }  # type: ignore[union-attr]
    first_words = {verb[0] for verb in verbs}
    assert first_words == {
        "init",
        "login",
        "auth",
        "inspect",
        "pull",
        "normalize",
        "load",
        "run",
        "schedule",
        "health",
        "audit",
        "runs",
        "cache",
        "mock",
        "new-connector",
    }


@pytest.mark.parametrize("line", reference_lines())
def test_every_option_in_the_command_reference_exists(
    line: str, capsys: pytest.CaptureFixture[str]
) -> None:
    words = re.match(r"((?:[a-z][a-z-]* ?)+)", line)
    assert words is not None
    verb = words.group(1).split()
    nested = verb[:2] if verb[0] in {"auth", "audit", "runs", "cache", "mock"} else verb[:1]
    if verb[:2] == ["mock", "platform"]:
        nested = verb[:3]  # `mock platform` is itself a group: its options belong to `serve`
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([*nested, "--help"])
    help_text = capsys.readouterr().out
    for flag in re.findall(r"--[a-z][a-z-]*", line):
        listed = re.search(re.escape(flag) + r"(?![a-z-])", help_text)
        assert listed, f"`{' '.join(nested)}` has no {flag}"


# --- the connector guide --------------------------------------------------------------------------


@pytest.mark.parametrize("command", documented_commands(GUIDE))
def test_every_command_in_the_connector_guide_is_accepted_by_the_parser(command: str) -> None:
    arguments = shlex.split(command)[1:]
    parsed = cli.build_parser().parse_args(arguments)
    assert callable(parsed.run_command)


def test_the_connector_guide_walks_the_whole_way_and_names_the_rules_and_the_limits() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    for step in (
        "Fetch/XHR",
        "new-connector",
        "--paste-curl",
        "TODO(connector)",
        "synthetic fixtures",
        "compliance block",
        "inspect",
    ):
        assert step in text.lower() or step in text, f"the connector guide lost: {step}"
    for rule in (
        "No anti-detection",
        "No rotation or disguise",
        "No CAPTCHA solving",
        "No reverse-engineering of request signatures",
        "Back off, do not push",
        "Invented fixtures only",
    ):
        assert rule in text, f"the connector guide lost the contributor rule {rule!r}"
    for limit in (
        "Requests signed by JavaScript",
        "HTML-only sites",
        "CAPTCHAs and second factors",
    ):
        assert limit in text, f"the connector guide no longer admits: {limit}"


@pytest.mark.parametrize(
    "path", [README, README_ZH, REPO_ROOT / "CONTRIBUTING.md"], ids=lambda p: p.name
)
def test_the_guide_is_linked_from_where_a_connector_author_starts(path: Path) -> None:
    assert "docs/connector-guide.md" in path.read_text(encoding="utf-8")
