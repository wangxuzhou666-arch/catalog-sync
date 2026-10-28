from __future__ import annotations

from pathlib import Path

import pytest

from catalog_sync import registry
from catalog_sync.config import (
    DEFAULT_RENEW_BEFORE_DAYS,
    HOME_ENV_VAR,
    find_config,
    load_config,
    resolve_workspace,
)
from catalog_sync.errors import ConfigError
from catalog_sync.normalize.mapping import load_mapping
from tests.conftest import REPO_ROOT


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "catalog-sync.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_shipped_example_config_loads() -> None:
    config = load_config(REPO_ROOT / "catalog-sync.example.toml")
    assert config.sources["demo"].plugin == "replay"
    assert config.sources["demo"].options == {"path": "examples/synthetic"}
    assert {name: sink.kind for name, sink in config.sinks.items()} == {
        "local": "sqlite",
        "export-jsonl": "jsonl",
        "export-csv": "csv",
        "mock-materials-db": "sqlite",
        "mock-suppliers-db": "sqlite",
        "mock-platform": "http",
    }
    assert config.profiles["demo"].sinks == ("local", "export-jsonl", "export-csv")
    demo_mapping = config.profiles["demo"].mapping
    assert demo_mapping is not None and demo_mapping.is_file(), (
        "a replay ships no mapping of its own"
    )
    assert load_mapping(demo_mapping).source == "mock.materials"
    assert config.profiles["demo"].incremental is True
    assert config.profiles["demo"].max_failed_ratio == 0.10, "the documented default"
    assert config.workspace_home is not None


def test_the_example_config_ships_the_mock_demo_ready_to_run() -> None:
    config = load_config(REPO_ROOT / "catalog-sync.example.toml")
    for name, plugin in (
        ("mock-materials", "mock.materials"),
        ("mock-suppliers", "mock.suppliers"),
    ):
        source = config.sources[name]
        assert source.plugin == plugin and source.compliance is not None
        assert source.compliance["authorization"] == "own_account"
        assert source.compliance["tos_url"] == "http://127.0.0.1:8765/terms"
        profile = config.profiles[name]
        assert profile.source == name
        # No source-tree path: what runs is the mapping the connector ships in its own package.
        assert profile.mapping is None
        assert (default := registry.default_mapping(plugin)) is not None and default.is_file()
    assert config.credentials["mock"].kind == "mock"


def test_a_compliance_table_is_kept_apart_from_the_plugin_options(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        '[sources.s]\nplugin = "p"\nper_page = 5\n'
        '[sources.s.compliance]\ndata_owner = "Example Co"\n',
    )
    source = load_config(path).sources["s"]
    assert source.options == {"per_page": 5}, "the plugin must never see the declaration"
    assert source.compliance == {"data_owner": "Example Co"}


def test_a_source_without_a_compliance_table_loads_fine(tmp_path: Path) -> None:
    source = load_config(write(tmp_path, '[sources.s]\nplugin = "p"\n')).sources["s"]
    assert source.compliance is None


def test_a_compliance_entry_that_is_not_a_table_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="'compliance' must be a table"):
        load_config(write(tmp_path, '[sources.s]\nplugin = "p"\ncompliance = "yes"\n'))


def test_the_workspace_keeps_the_audit_log_and_the_hash_salt(tmp_path: Path) -> None:
    workspace = resolve_workspace(tmp_path, None)
    assert workspace.audit_path == workspace.home / "audit.jsonl"
    assert workspace.hash_salt_path == workspace.home / "secrets" / "hash.salt"


def test_relative_paths_resolve_against_the_config_file(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        '[workspace]\nhome = "ws"\n[sources.s]\nplugin = "p"\n'
        '[sinks.k]\nkind = "jsonl"\n'
        '[profiles.x]\nsource = "s"\nsinks = ["k"]\nmapping = "maps/m.yaml"\n',
    )
    config = load_config(path)
    assert config.base_dir == tmp_path.resolve()
    assert config.workspace_home == config.base_dir / "ws"
    assert config.profiles["x"].mapping == config.base_dir / "maps" / "m.yaml"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("this is = not [valid", "not valid TOML"),
        ("[mystery]\nx = 1\n", "unknown key"),
        ("[workspace]\nfolder = 'x'\n", "unknown key"),
        ("[sources.s]\nroot = '0'\n", "missing required key 'plugin'"),
        ("[sources.s]\nplugin = 3\n", "non-empty string"),
        ("[sources]\ns = 'text'\n", r"\[sources.s\] must be a table"),
        ("[sinks.k]\npath = 'x'\n", "missing required key 'kind'"),
        ("[sources.s]\nplugin='p'\n[profiles.x]\nsource='s'\nsinks=[]\n", "non-empty list"),
        ("[sources.s]\nplugin='p'\n[profiles.x]\nsource='s'\nsinks=[1]\n", "non-empty list"),
        (
            "[sources.s]\nplugin='p'\n[sinks.k]\nkind='j'\n[profiles.x]\nsource='s'\nsinks=['k']\nincremental='yes'\n",
            "true or false",
        ),
        (
            "[sinks.k]\nkind='j'\n[profiles.x]\nsource='ghost'\nsinks=['k']\n",
            "unknown source 'ghost'",
        ),
        (
            "[sources.s]\nplugin='p'\n[profiles.x]\nsource='s'\nsinks=['ghost']\n",
            "unknown sink 'ghost'",
        ),
        (
            "[sources.s]\nplugin='p'\n[sinks.k]\nkind='j'\n[profiles.x]\nsource='s'\nsinks=['k']\nspeed=1\n",
            "unknown key",
        ),
        *(
            (
                "[sources.s]\nplugin='p'\n[sinks.k]\nkind='j'\n[profiles.x]\nsource='s'\n"
                f"sinks=['k']\nmax_failed_ratio={bad}\n",
                "'max_failed_ratio' must be a number from 0 to 1",
            )
            for bad in ("1.5", "-0.1", "true", "'ten percent'")
        ),
    ],
)
def test_invalid_configs_are_config_errors(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


def test_a_missing_config_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config"):
        load_config(tmp_path / "absent.toml")


def test_plugin_options_pass_through_untouched(tmp_path: Path) -> None:
    config = load_config(
        write(tmp_path, '[sources.s]\nplugin = "p"\nimages = { max_edge = 1600 }\nflag = true\n')
    )
    assert config.sources["s"].options == {"images": {"max_edge": 1600}, "flag": True}


def test_workspace_precedence_is_cli_then_env_then_config_then_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(write(tmp_path, '[workspace]\nhome = "from-config"\n'))
    monkeypatch.delenv(HOME_ENV_VAR, raising=False)
    assert resolve_workspace(None, config).home == tmp_path.resolve() / "from-config"
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "from-env"))
    assert resolve_workspace(None, config).home == (tmp_path / "from-env").resolve()
    assert (
        resolve_workspace(tmp_path / "from-cli", config).home == (tmp_path / "from-cli").resolve()
    )
    monkeypatch.delenv(HOME_ENV_VAR)
    default = resolve_workspace(None, None).home
    assert default.name == "catalog-sync" and default.is_absolute()


def test_workspace_layout_lives_under_home(tmp_path: Path) -> None:
    workspace = resolve_workspace(tmp_path, None)
    assert workspace.state_path == workspace.home / "state.sqlite"
    assert workspace.runs_root == workspace.home / "cache" / "runs"
    assert workspace.credentials_dir == workspace.home / "credentials"


def test_config_discovery_prefers_the_explicit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert find_config(None) is None
    local = write(tmp_path, "")
    assert find_config(None) == local
    explicit = tmp_path / "other.toml"
    assert find_config(explicit) == explicit


# --- credentials ------------------------------------------------------------------------------


def test_a_credential_table_names_its_kind_and_renewal_window(tmp_path: Path) -> None:
    config = load_config(
        write(
            tmp_path,
            "[credentials.main]\nkind = 'demo'\nrenew_before_days = 3\n"
            "schedule_interval_days = 7\nlanding_url = 'https://x.invalid'\n",
        )
    )
    credential = config.credentials["main"]
    assert (credential.kind, credential.renew_before_days) == ("demo", 3.0)
    assert credential.schedule_interval_days == 7.0
    assert credential.options == {"landing_url": "https://x.invalid"}


def test_a_credential_defaults_its_kind_to_its_name_and_the_window_to_two_days(
    tmp_path: Path,
) -> None:
    credential = load_config(write(tmp_path, "[credentials.mock]\n")).credentials["mock"]
    assert (credential.kind, credential.renew_before_days) == ("mock", DEFAULT_RENEW_BEFORE_DAYS)
    assert (credential.schedule_interval_days, dict(credential.options)) == (None, {})
    assert DEFAULT_RENEW_BEFORE_DAYS == 2.0


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[credentials.c]\nrenew_before_days = -1\n", "non-negative number"),
        ("[credentials.c]\nrenew_before_days = 'soon'\n", "non-negative number"),
        ("[credentials.c]\nrenew_before_days = true\n", "non-negative number"),
        ("[credentials.c]\nschedule_interval_days = 0\n", "greater than zero"),
        ("[credentials.c]\nkind = 3\n", "non-empty string"),
        ("[credentials]\nc = 'x'\n", r"\[credentials.c\] must be a table"),
    ],
)
def test_invalid_credential_tables_are_config_errors(
    tmp_path: Path, text: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


def test_the_browser_profile_lives_in_the_workspace_per_credential(tmp_path: Path) -> None:
    workspace = resolve_workspace(tmp_path, None)
    assert workspace.browser_profile_dir("mock") == workspace.home / "browser" / "mock"
