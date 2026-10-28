"""TOML configuration and the workspace directory layout.

The config names sources, sinks and profiles; it never holds credentials. Option tables of
sources and sinks are passed through untouched and validated by the plugin that owns them.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from catalog_sync.errors import ConfigError

DEFAULT_HOME = Path("~/.local/share/catalog-sync")
# Days before expiry at which a credential counts as "renew soon". Two days leaves room for one
# missed weekly run to be noticed before the token dies.
DEFAULT_RENEW_BEFORE_DAYS = 2.0
HOME_ENV_VAR = "CATALOG_SYNC_HOME"
LOCAL_CONFIG_NAME = "catalog-sync.toml"


@dataclass(frozen=True)
class SourceConfig:
    name: str
    plugin: str
    options: Mapping[str, object]
    # The raw `[sources.NAME.compliance]` table, validated when a pull is about to start (see
    # compliance.py), so that commands which never contact the service work without one.
    compliance: Mapping[str, object] | None = None


@dataclass(frozen=True)
class SinkConfig:
    name: str
    kind: str
    options: Mapping[str, object]


@dataclass(frozen=True)
class CredentialConfig:
    """A named credential: which kind of login it uses and when it counts as due for renewal.

    ``options`` holds every other key of the table; the credential kind validates them.
    """

    name: str
    kind: str
    renew_before_days: float
    schedule_interval_days: float | None
    options: Mapping[str, object]


DEFAULT_MAX_FAILED_RATIO = 0.10


@dataclass(frozen=True)
class ProfileConfig:
    name: str
    source: str
    sinks: tuple[str, ...]
    mapping: Path | None
    incremental: bool
    max_failed_ratio: float = DEFAULT_MAX_FAILED_RATIO


@dataclass(frozen=True)
class Config:
    base_dir: Path
    workspace_home: Path | None
    sources: Mapping[str, SourceConfig]
    sinks: Mapping[str, SinkConfig]
    profiles: Mapping[str, ProfileConfig]
    credentials: Mapping[str, CredentialConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class Workspace:
    """Where this tool keeps its own files. Always outside the repository."""

    home: Path

    @property
    def state_path(self) -> Path:
        return self.home / "state.sqlite"

    @property
    def runs_root(self) -> Path:
        return self.home / "cache" / "runs"

    @property
    def credentials_dir(self) -> Path:
        return self.home / "credentials"

    @property
    def audit_path(self) -> Path:
        return self.home / "audit.jsonl"

    @property
    def hash_salt_path(self) -> Path:
        """The secret mixed into hashed sensitive values; see sinks/sensitivity.py."""
        return self.home / "secrets" / "hash.salt"

    def browser_profile_dir(self, credential: str) -> Path:
        """The logged-in browser session for one credential. As sensitive as the credential."""
        return self.home / "browser" / credential


def resolve_workspace(cli_value: Path | None, config: Config | None) -> Workspace:
    """Precedence: --workspace, then $CATALOG_SYNC_HOME, then the config file, then the default."""
    env_value = os.environ.get(HOME_ENV_VAR)
    if cli_value is not None:
        home = cli_value
    elif env_value:
        home = Path(env_value)
    elif config is not None and config.workspace_home is not None:
        home = config.workspace_home
    else:
        home = DEFAULT_HOME
    return Workspace(home.expanduser().resolve())


WORKSPACE_PLACEHOLDER = "{workspace}"
_WORKSPACE_PREFIX = WORKSPACE_PLACEHOLDER + "/"


def expand_sink_paths(options: Mapping[str, object], workspace: Workspace) -> Mapping[str, object]:
    """Resolve a sink's ``path`` that starts with ``{workspace}/`` against the workspace home.

    That is where a run's output belongs: next to its state and cache, outside the checkout and
    outside any folder a cloud service syncs. The home is the one this run uses (``--workspace``,
    else ``$CATALOG_SYNC_HOME``, else the config's ``[workspace] home``, else the default). Any
    other relative path keeps resolving against the config file's directory, which is the sink's
    own job. The sinks do not see this: the path they are handed is already absolute.
    """
    raw = options.get("path")
    if not isinstance(raw, str) or not raw.startswith(WORKSPACE_PLACEHOLDER):
        return options
    name = raw.removeprefix(_WORKSPACE_PREFIX)
    if not raw.startswith(_WORKSPACE_PREFIX) or not name:
        # Without this a typo such as "{workspace}" would be a folder of that name in the checkout,
        # the very place the placeholder is there to keep output out of.
        raise ConfigError(f"sink path {raw!r}: write it as '{_WORKSPACE_PREFIX}<file>'")
    return {**options, "path": str(workspace.home / name)}


def find_config(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    local = Path.cwd() / LOCAL_CONFIG_NAME
    return local if local.is_file() else None


def load_config(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path.name}: {exc.strerror}") from exc
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid TOML: {exc}") from exc
    return parse_config(raw, base_dir=path.resolve().parent, where=path.name)


def parse_config(raw: Mapping[str, object], *, base_dir: Path, where: str) -> Config:
    """Validate the contents of a config file already read into a table.

    `where` names the file in messages. `catalog-sync init` builds the config it is about to write
    in memory and checks it here, so what it writes is read back by the same code.
    """
    _reject_unknown(raw, {"workspace", "sources", "sinks", "profiles", "credentials"}, where)

    workspace = _table(raw, "workspace", "top level")
    _reject_unknown(workspace, {"home"}, "[workspace]")
    home = _optional_str(workspace, "home", "[workspace]")

    sources = {
        name: _source(name, table, where) for name, table in _tables(raw, "sources", where).items()
    }
    sinks = {
        name: _sink(name, table, where) for name, table in _tables(raw, "sinks", where).items()
    }
    profiles = {
        name: _profile(name, table, base_dir, where)
        for name, table in _tables(raw, "profiles", where).items()
    }
    credentials = {
        name: _credential(name, table, where)
        for name, table in _tables(raw, "credentials", where).items()
    }
    for profile in profiles.values():
        if profile.source not in sources:
            raise ConfigError(f"profile {profile.name!r} uses unknown source {profile.source!r}")
        for sink in profile.sinks:
            if sink not in sinks:
                raise ConfigError(f"profile {profile.name!r} uses unknown sink {sink!r}")
    return Config(
        base_dir=base_dir,
        workspace_home=(base_dir / Path(home).expanduser()) if home is not None else None,
        sources=sources,
        sinks=sinks,
        profiles=profiles,
        credentials=credentials,
    )


def _credential(name: str, table: Mapping[str, object], where: str) -> CredentialConfig:
    label = f"[credentials.{name}] in {where}"
    kind = _optional_str(table, "kind", label) or name
    renew_before_days = _optional_days(table, "renew_before_days", label)
    schedule_interval_days = _optional_days(table, "schedule_interval_days", label)
    if schedule_interval_days is not None and schedule_interval_days == 0:
        raise ConfigError(f"{label}: 'schedule_interval_days' must be greater than zero")
    options = {
        key: value
        for key, value in table.items()
        if key not in {"kind", "renew_before_days", "schedule_interval_days"}
    }
    return CredentialConfig(
        name=name,
        kind=kind,
        renew_before_days=(
            DEFAULT_RENEW_BEFORE_DAYS if renew_before_days is None else renew_before_days
        ),
        schedule_interval_days=schedule_interval_days,
        options=options,
    )


def _optional_days(table: Mapping[str, object], key: str, label: str) -> float | None:
    value = table.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"{label}: '{key}' must be a non-negative number of days")
    return float(value)


def _source(name: str, table: Mapping[str, object], where: str) -> SourceConfig:
    label = f"[sources.{name}] in {where}"
    plugin = _required_str(table, "plugin", label)
    compliance = table.get("compliance")
    if compliance is not None and not isinstance(compliance, dict):
        raise ConfigError(f"{label}: 'compliance' must be a table")
    options = {k: v for k, v in table.items() if k not in {"plugin", "compliance"}}
    return SourceConfig(name, plugin, options, compliance)


def _sink(name: str, table: Mapping[str, object], where: str) -> SinkConfig:
    kind = _required_str(table, "kind", f"[sinks.{name}] in {where}")
    return SinkConfig(name, kind, {k: v for k, v in table.items() if k != "kind"})


def _profile(name: str, table: Mapping[str, object], base_dir: Path, where: str) -> ProfileConfig:
    label = f"[profiles.{name}] in {where}"
    _reject_unknown(table, {"source", "sinks", "mapping", "incremental", "max_failed_ratio"}, label)
    sinks = table.get("sinks")
    if not isinstance(sinks, list) or not sinks or not all(isinstance(s, str) for s in sinks):
        raise ConfigError(f"{label}: 'sinks' must be a non-empty list of sink names")
    mapping = _optional_str(table, "mapping", label)
    incremental = table.get("incremental", True)
    if not isinstance(incremental, bool):
        raise ConfigError(f"{label}: 'incremental' must be true or false")
    return ProfileConfig(
        name=name,
        source=_required_str(table, "source", label),
        sinks=tuple(sinks),
        mapping=(base_dir / Path(mapping).expanduser()) if mapping is not None else None,
        incremental=incremental,
        max_failed_ratio=_ratio(table, "max_failed_ratio", DEFAULT_MAX_FAILED_RATIO, label),
    )


def _ratio(table: Mapping[str, object], key: str, default: float, label: str) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ConfigError(f"{label}: '{key}' must be a number from 0 to 1")
    return float(value)


def _reject_unknown(table: Mapping[str, object], allowed: set[str], label: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{label}: unknown key(s) {unknown}")


def _table(parent: Mapping[str, object], key: str, label: str) -> Mapping[str, object]:
    value = parent.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{label}: '{key}' must be a table")
    return value


def _tables(parent: Mapping[str, object], key: str, label: str) -> dict[str, dict[str, object]]:
    tables: dict[str, dict[str, object]] = {}
    for name, value in _table(parent, key, label).items():
        if not isinstance(value, dict):
            raise ConfigError(f"{label}: [{key}.{name}] must be a table")
        tables[name] = value
    return tables


def _required_str(table: Mapping[str, object], key: str, label: str) -> str:
    value = _optional_str(table, key, label)
    if value is None:
        raise ConfigError(f"{label}: missing required key '{key}'")
    return value


def _optional_str(table: Mapping[str, object], key: str, label: str) -> str | None:
    value = table.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise ConfigError(f"{label}: '{key}' must be a non-empty string")
    return value
