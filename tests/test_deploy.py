"""The compose stack of docs/deploy.md: its pieces must agree with each other and with the tool.

The stack itself is run end to end by the `container` job of CI. These tests catch the cheaper
drift without Docker: a variable the compose file asks for that `.env.example` does not list, a
secret written inline, a command the parser no longer accepts, a sink whose environment variable the
scheduler is never given.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pytest
import yaml

from catalog_sync import cli, registry
from catalog_sync.config import load_config
from tests.conftest import REPO_ROOT

DEPLOY = REPO_ROOT / "deploy"
COMPOSE = DEPLOY / "compose.yaml"
ENV_EXAMPLE = DEPLOY / ".env.example"
GUIDE = REPO_ROOT / "docs" / "deploy.md"

REQUIRED_VARIABLE = re.compile(r"\$\{([A-Z][A-Z_]*):\?")
SECRET_NAME = re.compile(r"PASSWORD|TOKEN|DSN|SECRET")
GRACE = re.compile(r"(\d+)([smh])")

Service = dict[str, object]


def services() -> dict[str, Service]:
    loaded = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return cast("dict[str, Service]", loaded["services"])


def strings(service: Service, key: str) -> list[str]:
    return [str(item) for item in cast("list[object]", service.get(key, []))]


def environment(service: Service) -> dict[str, str]:
    return {k: str(v) for k, v in cast("dict[str, object]", service.get("environment", {})).items()}


def image_services() -> dict[str, Service]:
    """The services made from the catalog-sync image: they are the ones with a build section."""
    return {name: service for name, service in services().items() if "build" in service}


def scheduler() -> Service:
    (found,) = [s for s in services().values() if strings(s, "command")[:1] == ["schedule"]]
    return found


def test_env_example_lists_exactly_the_variables_compose_requires_and_gives_none_a_value() -> None:
    lines = [
        line
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert all(line.endswith("=") for line in lines), "a value in .env.example is a secret"
    listed = {line.removesuffix("=") for line in lines}
    code = [
        line
        for line in COMPOSE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    required = set(REQUIRED_VARIABLE.findall("\n".join(code)))
    assert listed == required


def test_no_secret_is_written_into_the_compose_file() -> None:
    for name, service in services().items():
        for key, value in environment(service).items():
            if SECRET_NAME.search(key):
                assert REQUIRED_VARIABLE.search(value), f"{name}: {key} must come from deploy/.env"


def test_each_service_is_handed_only_the_secrets_it_needs() -> None:
    handed = {name: set(environment(service)) for name, service in services().items()}
    assert "MOCK_SAAS_PASSWORD" not in handed["catalog-sync"]
    assert "POSTGRES_PASSWORD" not in handed["catalog-sync"]
    assert "PLATFORM_TOKEN" not in handed["login"]
    assert "CATALOG_SYNC_DB_PASSWORD" not in handed["mock-platform"]


def test_the_stack_config_loads_and_the_scheduler_has_what_its_sinks_name() -> None:
    config = load_config(DEPLOY / "catalog-sync.compose.toml")
    command = strings(scheduler(), "command")
    profile = config.profiles[command[1]]
    assert {"postgres", "http"} <= {config.sinks[name].kind for name in profile.sinks}
    handed = set(environment(scheduler()))
    for name in profile.sinks:
        options = config.sinks[name].options
        for key in ("dsn_env", "auth_header_env"):
            if key in options:
                assert options[key] in handed, (
                    f"sink {name}: {options[key]} is not in the environment"
                )


def test_the_profile_uses_the_connectors_own_mapping_so_nothing_is_mounted_for_it() -> None:
    config = load_config(DEPLOY / "catalog-sync.compose.toml")
    profile = config.profiles[strings(scheduler(), "command")[1]]
    assert profile.mapping is None
    assert registry.default_mapping(config.sources[profile.source].plugin) is not None
    for name, service in image_services().items():
        for volume in strings(service, "volumes"):
            source, _, target = volume.partition(":")
            assert not source.startswith("../src"), f"{name} mounts source code: {volume}"
            assert "/config/mappings" not in target, f"{name} mounts a mapping: {volume}"


def test_every_command_the_stack_runs_is_accepted_by_the_parser() -> None:
    for name, service in image_services().items():
        entrypoint = strings(service, "entrypoint") or ["catalog-sync"]
        arguments = [*entrypoint[1:], *strings(service, "command")]
        parsed = cli.build_parser().parse_args(arguments)
        assert callable(parsed.run_command), f"{name}: {arguments}"


def test_the_scheduler_health_check_is_a_command_of_the_tool() -> None:
    test = strings(cast("Service", scheduler()["healthcheck"]), "test")
    assert test[:2] == ["CMD", "catalog-sync"]
    assert callable(cli.build_parser().parse_args(test[2:]).run_command)


def test_only_the_scheduler_keeps_a_health_check_that_needs_a_scheduled_cycle() -> None:
    for name, service in image_services().items():
        check = cast("Service", service.get("healthcheck"))
        assert check is not None, f"{name} would inherit the image's health check"
        if strings(service, "command")[:1] != ["schedule"]:
            assert check.get("disable") is True or "health" not in strings(check, "test"), name


def test_every_container_of_the_image_is_locked_down() -> None:
    for name, service in image_services().items():
        assert service["read_only"] is True, name
        assert strings(service, "cap_drop") == ["ALL"], name
        assert "no-new-privileges:true" in strings(service, "security_opt"), name


MEMORY_LIMIT = re.compile(r"[1-9]\d*[mg]")


def test_no_container_can_gain_privileges_or_use_unbounded_memory_and_processes() -> None:
    """Postgres included: it is not built from the image, so the test above does not see it."""
    for name, service in services().items():
        assert "no-new-privileges:true" in strings(service, "security_opt"), name
        assert MEMORY_LIMIT.fullmatch(str(service.get("mem_limit"))), f"{name}: no memory limit"
        pids = service.get("pids_limit")
        assert isinstance(pids, int) and pids > 0, f"{name}: no process limit"


def networks_of(name: str) -> set[str]:
    return set(strings(services()[name], "networks"))


def scheduler_name() -> str:
    (name,) = [n for n, s in services().items() if strings(s, "command")[:1] == ["schedule"]]
    return name


def test_every_service_names_its_networks_so_none_joins_the_shared_default() -> None:
    for name in services():
        assert networks_of(name), f"{name} would join the default network, with everyone else"


def test_only_the_scheduler_can_reach_the_database() -> None:
    reachable_from_postgres = networks_of("postgres")
    assert reachable_from_postgres, "postgres is on a network of its own"
    for name in services():
        shares = bool(networks_of(name) & reachable_from_postgres)
        assert shares is (name in {"postgres", scheduler_name()}), name


def test_the_database_network_has_no_way_out() -> None:
    declared = cast(
        "dict[str, dict[str, object]]", yaml.safe_load(COMPOSE.read_text("utf-8"))["networks"]
    )
    for network in networks_of("postgres"):
        assert declared[network].get("internal") is True, network
    assert set(declared) == {n for name in services() for n in networks_of(name)}, "unused network"


def test_a_restart_of_the_scheduler_is_capped_and_a_stop_is_given_time_to_finish() -> None:
    assert re.fullmatch(r"on-failure:[1-9]\d*", str(scheduler()["restart"]))
    match = GRACE.fullmatch(str(scheduler()["stop_grace_period"]))
    assert match is not None
    seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    assert seconds >= 600


def test_postgres_is_pinned_by_the_digest_of_its_image() -> None:
    assert re.fullmatch(r"postgres:\d+@sha256:[0-9a-f]{64}", str(services()["postgres"]["image"]))


def test_the_guide_only_names_services_the_stack_has() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    command = re.compile(
        r"compose\.yaml (?:--profile tools )?(?:exec|run --rm|restart|stop|start)(?: -T)? ([a-z-]+)"
    )
    named = set(command.findall(text))
    assert {"catalog-sync", "login", "postgres", "mock-platform"} <= named
    assert named <= set(services()), named - set(services())


@pytest.mark.parametrize(
    "path", [COMPOSE, ENV_EXAMPLE, DEPLOY / "postgres-init.sh"], ids=lambda p: p.name
)
def test_the_deploy_files_hold_no_local_paths(path: Path) -> None:
    assert re.search(r"/(?:Users|home)/[A-Za-z0-9._-]+/", path.read_text(encoding="utf-8")) is None
