"""Every plugin and source name the example config and the README show is registered here.

A name that only a separately installed connector registers would make the first command of the
README fail for someone who installed just this package. The registered side is read from the root
pyproject.toml, not from the installed metadata, because the environment may also hold connector
packages; this package's own promise is what its own entry points say. No connector is named here.
"""

from __future__ import annotations

import re
import tomllib

import pytest

from tests.conftest import REPO_ROOT

PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
ENTRY_POINTS = PYPROJECT["project"]["entry-points"]
EXAMPLE_TEXT = (REPO_ROOT / "catalog-sync.example.toml").read_text(encoding="utf-8")
README_TEXT = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

CODE_BLOCK = re.compile(r"(?s)```(?:console|toml|yaml|python|)\n(.*?)```")
PLUGIN_LINE = re.compile(r'(?m)^\s*plugin\s*=\s*"([^"]+)"')
# A command line of the README: the global options (`--config FILE`) come before the subcommand.
COMMAND_PREFIX = r"(?m)^\$ .*?\bcatalog-sync (?:--[\w-]+ \S+ )*"
LOGIN_COMMAND = re.compile(COMMAND_PREFIX + r"login ([\w.-]+)")
# The commands whose first argument is a source or profile of the config, or a source plugin.
SOURCE_COMMAND = re.compile(COMMAND_PREFIX + r"(?:pull|normalize|run|inspect|schedule) ([\w.-]+)")


def registered(group: str) -> set[str]:
    return set(ENTRY_POINTS[f"catalog_sync.{group}"])


def code_of(markdown: str) -> str:
    return "\n".join(CODE_BLOCK.findall(markdown))


def unregistered_plugins(text: str) -> list[str]:
    """`plugin = "..."` values in `text` that no source entry point of this package registers."""
    return sorted(set(PLUGIN_LINE.findall(text)) - registered("sources"))


def unregistered_logins(text: str) -> list[str]:
    return sorted(set(LOGIN_COMMAND.findall(text)) - registered("credentials"))


def unknown_source_arguments(commands: str, defined: set[str]) -> list[str]:
    """Names a command takes as its source that the config does not define and no plugin has."""
    return sorted(set(SOURCE_COMMAND.findall(commands)) - defined - registered("sources"))


def names_defined_by_the_example() -> set[str]:
    config = tomllib.loads(EXAMPLE_TEXT)
    return set(config["sources"]) | set(config["profiles"])


def test_the_registered_groups_are_really_read() -> None:
    assert registered("sources"), "the scan must see the entry points"
    assert registered("credentials")


def test_every_plugin_in_the_example_config_is_registered() -> None:
    assert PLUGIN_LINE.findall(EXAMPLE_TEXT), "the scan must see the plugin lines"
    assert unregistered_plugins(EXAMPLE_TEXT) == []


def test_every_plugin_in_the_readme_code_blocks_is_registered() -> None:
    assert PLUGIN_LINE.findall(code_of(README_TEXT)), "the scan must see the plugin lines"
    assert unregistered_plugins(code_of(README_TEXT)) == []


def test_every_credential_the_example_config_defines_is_a_registered_kind() -> None:
    credentials = tomllib.loads(EXAMPLE_TEXT)["credentials"]
    assert credentials, "the scan must see the credentials"
    kinds = {table.get("kind", name) for name, table in credentials.items()}
    assert kinds - registered("credentials") == set()


def test_every_credential_the_readme_logs_into_is_a_registered_kind() -> None:
    assert LOGIN_COMMAND.findall(code_of(README_TEXT)), "the scan must see the login commands"
    assert unregistered_logins(code_of(README_TEXT)) == []


def test_every_source_the_readme_commands_name_is_defined_or_registered() -> None:
    commands = code_of(README_TEXT)
    assert SOURCE_COMMAND.findall(commands), "the scan must see the pull and run commands"
    assert unknown_source_arguments(commands, names_defined_by_the_example()) == []


# --- the checks go red when they should (CODING-STYLE R.7) -------------------------------------


@pytest.mark.parametrize("name", ["a.service", "mock.materials-typo", "mock"])
def test_an_unregistered_plugin_in_the_example_config_is_found(name: str) -> None:
    mutated = EXAMPLE_TEXT.replace('plugin = "mock.suppliers"', f'plugin = "{name}"')
    assert mutated != EXAMPLE_TEXT
    assert unregistered_plugins(mutated) == [name]


def test_an_unregistered_plugin_in_a_readme_code_block_is_found() -> None:
    mutated = README_TEXT.replace('plugin = "mock.materials"', 'plugin = "a.service"')
    assert mutated != README_TEXT
    assert unregistered_plugins(code_of(mutated)) == ["a.service"]


def test_a_plugin_named_only_in_prose_is_not_a_code_example() -> None:
    assert unregistered_plugins(code_of('plugin = "a.service" in prose\n```\nx\n```\n')) == []


def test_an_unregistered_login_and_an_unknown_source_are_found() -> None:
    mutated = README_TEXT.replace("catalog-sync login mock --form", "catalog-sync login a.service")
    assert mutated != README_TEXT
    assert unregistered_logins(code_of(mutated)) == ["a.service"]
    mutated = README_TEXT.replace("catalog-sync pull mock-materials", "catalog-sync pull a-source")
    assert mutated != README_TEXT
    assert unknown_source_arguments(code_of(mutated), names_defined_by_the_example()) == [
        "a-source"
    ]
