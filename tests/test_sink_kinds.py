"""The built-in sink kinds as ``init`` offers them: declared, buildable, and free of secrets."""

from __future__ import annotations

from pathlib import Path

import pytest

from catalog_sync import registry
from catalog_sync.connector_info import SettingKind, SettingSpec
from catalog_sync.errors import ConfigError
from catalog_sync.sdk import OptionReader
from catalog_sync.sinks.kinds import SINK_KINDS, SinkKind
from catalog_sync.sinks.sensitivity import OPTION as SENSITIVE_OPTION

KINDS = {kind.name: kind for kind in SINK_KINDS}
SAMPLES: dict[str, object] = {
    "path": "./out/x.db",
    "table": "things",
    "dsn_env": "WAREHOUSE_DSN",
    "schema": "public",
    "url": "https://platform.example/api/ingest",
    "auth_header_env": "PLATFORM_TOKEN",
    "auth_header": "X-Api-Key",
    "auth_scheme": "none",
    "batch_size": 50,
    "max_requests_per_minute": 30,
    "timeout_s": 12.5,
    "allow_plain_http": False,
}


def test_the_kinds_are_the_five_init_offers_in_its_order_and_each_is_installed() -> None:
    assert [kind.name for kind in SINK_KINDS] == ["sqlite", "postgres", "http", "jsonl", "csv"]
    assert set(KINDS) <= set(registry.available(registry.SINKS_GROUP))
    assert all(kind.summary and len(kind.summary.splitlines()) == 1 for kind in SINK_KINDS)


@pytest.mark.parametrize("kind", SINK_KINDS, ids=lambda kind: kind.name)
def test_every_declared_setting_is_accepted_by_the_sink_that_declares_it(
    kind: SinkKind, tmp_path: Path
) -> None:
    options = {spec.key: SAMPLES[spec.key] for spec in kind.settings}
    registry.sink_factory(kind.name)(options, base_dir=tmp_path)


@pytest.mark.parametrize("kind", SINK_KINDS, ids=lambda kind: kind.name)
def test_an_option_that_is_not_declared_is_refused(kind: SinkKind, tmp_path: Path) -> None:
    options = {spec.key: SAMPLES[spec.key] for spec in kind.settings if spec.is_asked}
    with pytest.raises(ConfigError, match=r"unknown option\(s\) \['not_declared'\]"):
        registry.sink_factory(kind.name)({**options, "not_declared": 1}, base_dir=tmp_path)


@pytest.mark.parametrize("kind", SINK_KINDS, ids=lambda kind: kind.name)
def test_what_init_asks_with_its_defaults_alone_is_a_buildable_sink(
    kind: SinkKind, tmp_path: Path
) -> None:
    """Whatever `init` collects by taking every default (a required setting needs an answer)
    is something the loader accepts."""
    options = {
        spec.key: spec.default if spec.default is not None else SAMPLES[spec.key]
        for spec in kind.settings
        if spec.is_asked
    }
    registry.sink_factory(kind.name)(options, base_dir=tmp_path)


def test_the_policy_option_is_every_sinks_and_so_no_ones_declared_setting() -> None:
    assert all(spec.key != SENSITIVE_OPTION for kind in SINK_KINDS for spec in kind.settings)


def specs_of_kind(wanted: SettingKind) -> list[tuple[str, SettingSpec]]:
    return [
        (kind.name, spec) for kind in SINK_KINDS for spec in kind.settings if spec.kind is wanted
    ]


def test_the_only_secrets_a_sink_takes_are_names_of_environment_variables() -> None:
    assert sorted((kind, spec.key) for kind, spec in specs_of_kind(SettingKind.ENV_NAME)) == [
        ("http", "auth_header_env"),
        ("postgres", "dsn_env"),
    ]


@pytest.mark.parametrize(("kind", "spec"), specs_of_kind(SettingKind.ENV_NAME))
def test_a_secret_where_a_variable_name_belongs_is_refused_and_not_repeated(
    kind: str, spec: SettingSpec
) -> None:
    secret = "postgresql://user:" + "hunter2@db/prod"
    with pytest.raises(ConfigError, match="NAME of an environment variable") as caught:
        OptionReader({spec.key: secret}, label=f"{kind} sink").read(spec)
    assert "hunter2" not in str(caught.value)
