"""The public declaration of settings: the spec itself, the reader that validates against it, and
the three connectors that ship one (whose declarations must be what their settings classes read).

A declaration that nothing checks would drift from the code it describes, and `catalog-sync init`
asks its questions from it, so the parity tests below are the ones that matter.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from catalog_sync import registry
from catalog_sync.compliance import (
    AUTHORIZATION_HELP,
    AUTHORIZATIONS,
    FIELD_HELP,
    OPTIONAL_FIELDS,
    REQUIRED_FIELDS,
)
from catalog_sync.connector_info import ENV_NAME, ConnectorInfo, SettingKind, SettingSpec
from catalog_sync.credentials.base import Credential, CredentialHandle
from catalog_sync.errors import ConfigError
from catalog_sync.normalize.mapping import load_mapping
from catalog_sync.sdk import OptionReader, declared_keys

HANDLE = CredentialHandle("c", lambda: Credential({}, {}, None, None), "renew")
LABEL = "acme.items"


def reader(**options: object) -> OptionReader:
    return OptionReader(options, label=LABEL)


# --- the declaration refuses to be inconsistent ---------------------------------------------------


@pytest.mark.parametrize(
    ("build", "complaint"),
    [
        (lambda: SettingSpec("k", SettingKind.CHOICE, "h"), "'choices' go with the choice kind"),
        (
            lambda: SettingSpec("k", SettingKind.TEXT, "h", choices=("a",)),
            "'choices' go with the choice kind",
        ),
        (
            lambda: SettingSpec("k", SettingKind.TEXT, "h", required=True, default="x"),
            "a required setting has no default",
        ),
        (
            lambda: SettingSpec("k", SettingKind.TABLE, "h", essential=True),
            "a table is edited in the file",
        ),
        (lambda: SettingSpec("k", SettingKind.INTEGER, "h", default="3"), "not a integer value"),
        (lambda: SettingSpec("k", SettingKind.INTEGER, "h", default=True), "not a integer value"),
        (lambda: SettingSpec("k", SettingKind.NUMBER, "h", default=False), "not a number value"),
        (lambda: SettingSpec("k", SettingKind.BOOLEAN, "h", default=1), "not a boolean value"),
        (lambda: SettingSpec("k", SettingKind.TABLE, "h", default={}), "not a table value"),
    ],
)
def test_a_contradictory_declaration_is_refused_when_it_is_written(
    build: object, complaint: str
) -> None:
    with pytest.raises(ValueError, match=complaint):
        build()  # type: ignore[operator]


def test_only_what_is_required_or_essential_is_asked_for() -> None:
    plain = SettingSpec("a", SettingKind.TEXT, "h", default="x")
    assert not plain.is_asked
    assert SettingSpec("b", SettingKind.TEXT, "h", required=True).is_asked
    assert SettingSpec("c", SettingKind.URL, "h", default="http://x", essential=True).is_asked


def test_connector_info_refuses_a_duplicate_key_and_a_credential_setting_it_does_not_declare() -> (
    None
):
    one = SettingSpec("a", SettingKind.TEXT, "h", default="x")
    with pytest.raises(ValueError, match="declared twice"):
        ConnectorInfo("s", "c", (one, one), Path("m.yaml"))
    with pytest.raises(ValueError, match=r"credential_settings \['b'\]"):
        ConnectorInfo("s", "c", (one,), Path("m.yaml"), credential_settings=("b",))


# --- reading by declaration -----------------------------------------------------------------------

INTEGER = SettingSpec("n", SettingKind.INTEGER, "h", default=5, minimum=1, maximum=9)
FLAG = SettingSpec("f", SettingKind.BOOLEAN, "h", default=False)
CHOICE = SettingSpec("c", SettingKind.CHOICE, "h", default="a", choices=("a", "b"))
SECRET_NAME = SettingSpec("token_env", SettingKind.ENV_NAME, "h", required=True)
URL = SettingSpec("u", SettingKind.URL, "h", default="https://d.invalid")


def test_an_absent_option_reads_as_its_default_and_a_given_one_as_what_was_given() -> None:
    assert reader().read(INTEGER) == 5
    assert reader(n="7").read(INTEGER) == 7, "a command-line string is accepted"
    assert reader(n=7).read(INTEGER) == 7
    assert reader().read(URL) == "https://d.invalid"
    assert reader(u="http://127.0.0.1:9/").read(URL) == "http://127.0.0.1:9"


@pytest.mark.parametrize("value", [0, 10, "x", True, 2.5])
def test_an_integer_outside_its_declared_bounds_is_refused_naming_the_key(value: object) -> None:
    with pytest.raises(ConfigError, match=f"{LABEL}: 'n' must be an integer between 1 and 9"):
        reader(n=value).read(INTEGER)


@pytest.mark.parametrize(("value", "expected"), [(True, True), ("true", True), (" False ", False)])
def test_a_flag_is_a_real_boolean_or_the_words_true_and_false(
    value: object, expected: bool
) -> None:
    assert reader(f=value).read(FLAG) is expected


@pytest.mark.parametrize("value", [1, "yes", "", None])
def test_anything_else_is_not_a_flag(value: object) -> None:
    with pytest.raises(ConfigError, match="'f' must be true or false"):
        reader(f=value).read(FLAG)


def test_a_choice_names_the_ones_there_are() -> None:
    assert reader(c="b").read(CHOICE) == "b"
    with pytest.raises(ConfigError, match="'c' must be one of a, b, got 'z'"):
        reader(c="z").read(CHOICE)


def test_a_required_option_that_is_absent_is_an_error_saying_what_it_is_for() -> None:
    with pytest.raises(ConfigError, match=f"{LABEL}: 'token_env' is required"):
        reader().read(SECRET_NAME)


def test_an_environment_variable_name_is_accepted_and_a_secret_is_refused_unechoed() -> None:
    assert reader(token_env="PLATFORM_TOKEN").read(SECRET_NAME) == "PLATFORM_TOKEN"
    pasted = "sk-live-9f8e7d6c5b4a/with spaces"
    for value in (pasted, "Bearer abc", "has-dash", "1STARTS_WITH_DIGIT", "", 5):
        with pytest.raises(ConfigError, match="must be the NAME of an environment variable") as e:
            reader(token_env=value).read(SECRET_NAME)
        assert pasted not in str(e.value), "the value may be the secret and must not be echoed"


# Token-shaped samples are assembled here, never written out, so that this file holds nothing a
# secret scanner would (rightly) flag.
SECRET_SHAPED = {
    "a GitHub token, as pasted": "ghp" + "_" + "aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5",
    "a GitHub token, shouted": "GHP" + "_" + "AB3DE5FG7HI9JK1LM3NO5PQ7RS9TU1VW3XY5",
    "a Stripe key, as pasted": "sk" + "_live_" + "51H" + "x7Yb2Qz9" * 3,
    "a Stripe key, shouted": "SK" + "_LIVE_" + "51HX7YB2QZ9" * 2,
    "an AWS key id": "AKIA" + "IOSFODNN7EXAMPLE",
    "a long hex key": "9F8E7D6C" * 4,
    "a key with a prefix and no underscore": "XOXB" + "1234567890123",
    "a name over the length cap": "A_" * 33,
    "lower case": "my_service_token",
    "mixed case": "MyServiceToken",
    "a dash": "MY-SERVICE-TOKEN",
    "a digit first": "1PASSWORD",
    "a space": "MY TOKEN",
}
ACCEPTABLE_NAMES = [
    "PLATFORM_TOKEN",
    "SERVICE_PASSWORD",
    "MOCK_SAAS_USER",
    "HOME",
    "A",
    "_PRIVATE_TOKEN",
    "ABCDEFGHIJKLMNO",  # fifteen unbroken characters is the longest a name without a break gets
    "A_" * 32,
    "SKIP_LIVE_CHECK_TOKEN",
    "GHPAGES_DEPLOY_KEY",
]


@pytest.mark.parametrize("pasted", SECRET_SHAPED.values(), ids=SECRET_SHAPED.keys())
def test_a_pasted_secret_is_not_a_variable_name_and_is_not_echoed(pasted: str) -> None:
    assert ENV_NAME.fullmatch(pasted) is None
    with pytest.raises(ConfigError, match="must be the NAME of an environment variable") as e:
        reader(token_env=pasted).read(SECRET_NAME)
    assert pasted not in str(e.value)


@pytest.mark.parametrize("name", ACCEPTABLE_NAMES)
def test_the_usual_variable_names_pass(name: str) -> None:
    assert ENV_NAME.fullmatch(name) is not None
    assert reader(token_env=name).read(SECRET_NAME) == name


def test_declared_keys_are_the_settings_plus_the_credential() -> None:
    assert declared_keys([INTEGER, FLAG]) == {"n", "f", "credential"}


# --- the shipped connectors -----------------------------------------------------------------------

NETWORKED = sorted(set(registry.available(registry.SOURCES_GROUP)) - {"replay"})


def test_every_networked_source_plugin_declares_its_connector_info() -> None:
    assert set(NETWORKED) <= set(registry.available(registry.CONNECTORS_GROUP))
    assert {"mock.materials", "mock.suppliers"} <= set(NETWORKED)


def test_a_source_without_a_declaration_is_refused_with_the_way_out() -> None:
    with pytest.raises(ConfigError, match=r"'replay' does not declare its settings.*by hand"):
        registry.connector_info("replay")


def build_settings(name: str, **options: object) -> object:
    """The settings object of source `name` built from `options` (the credential is supplied)."""
    from catalog_sync.plugins.mock.settings import MockSettings

    full = {"credential": HANDLE, **options}
    if name.startswith("mock."):
        return MockSettings.from_options(full, source=name)
    # Any other installed connector: its source keeps the settings it was built from.
    return registry.source_factory(name)(full, base_dir=Path()).__dict__["_settings"]


def sample_value(spec: SettingSpec) -> object:
    """A valid value that differs from the default, so that reading it back proves it was read."""
    if spec.kind is SettingKind.INTEGER:
        return (spec.default if isinstance(spec.default, int) else spec.minimum) + 1
    if spec.kind is SettingKind.NUMBER:
        return float(spec.default or 0) + 1.5  # type: ignore[arg-type]
    if spec.kind is SettingKind.CHOICE:
        return next(c for c in spec.choices if c != spec.default)
    if spec.kind is SettingKind.URL:
        return "http://127.0.0.1:9999"
    if spec.kind is SettingKind.TABLE:
        return {}
    return "other-value"


@pytest.mark.parametrize("name", NETWORKED)
def test_each_declared_setting_is_really_read_by_the_settings_class(name: str) -> None:
    """Drift guard: a key in the declaration that the class ignores, or reads under another name,
    shows up here. A setting set alone must come back as itself."""
    info = registry.connector_info(name)
    gap_floor = {"max_requests_per_minute": 1}  # lets any request_gap_s through
    for spec in info.settings:
        if spec.kind is SettingKind.TABLE:
            continue  # structured values have their own validators and tests
        value = sample_value(spec)
        if spec.key == "max_requests_per_minute":
            value = 6
        options = (
            {**gap_floor, spec.key: value} if spec.key != "request_gap_s" else {spec.key: value}
        )
        built = build_settings(name, **options)
        attribute = getattr(built, spec.key)
        assert attribute == value, (
            f"{name}: {spec.key} was set to {value!r} and read as {attribute!r}"
        )


@pytest.mark.parametrize("name", NETWORKED)
def test_every_option_the_settings_class_has_is_declared(name: str) -> None:
    """The other direction of the drift guard: an option that exists but is not declared would
    never be asked about, and would be refused as unknown when set."""
    declared = {spec.key for spec in registry.connector_info(name).settings}
    fields = {f.name for f in dataclasses.fields(build_settings(name))}  # type: ignore[arg-type]
    # MockSettings is shared by both mock sources; the supplier list has no folders or pictures.
    undeclared = {"mock.suppliers": {"root_folder_id", "max_depth", "images"}}.get(name, set())
    assert fields - {"credential"} - declared == undeclared
    assert declared <= fields


@pytest.mark.parametrize("name", NETWORKED)
def test_the_declared_defaults_are_the_defaults_of_the_settings_class(name: str) -> None:
    """Two places state a default: the declaration (what the reader falls back to) and the
    dataclass field (what code that builds the settings directly gets). They must agree."""
    info = registry.connector_info(name)
    built = build_settings(name)
    declared = {f.name: f.default for f in dataclasses.fields(built)}  # type: ignore[arg-type]
    for spec in info.settings:
        if spec.kind is SettingKind.TABLE or spec.default is None:
            continue
        assert getattr(built, spec.key) == spec.default, f"{name}: {spec.key} as read"
        assert declared[spec.key] == spec.default, f"{name}: {spec.key} as a field default"


@pytest.mark.parametrize("name", NETWORKED)
def test_an_undeclared_option_is_still_refused(name: str) -> None:
    with pytest.raises(ConfigError, match=r"unknown option\(s\) \['not_declared'\]"):
        build_settings(name, not_declared=1)


@pytest.mark.parametrize("name", NETWORKED)
def test_the_rest_of_the_info_points_at_things_that_exist(name: str, tmp_path: Path) -> None:
    info = registry.connector_info(name)
    assert info.summary and len(info.summary.splitlines()) == 1
    assert info.credential in registry.available(registry.CREDENTIALS_GROUP)
    assert load_mapping(info.mapping).source == name
    # The credential kind accepts exactly the settings it is said to share with the source.
    shared = {
        key: sample_value(next(s for s in info.settings if s.key == key))
        for key in info.credential_settings
    }
    registry.credential_kind_factory(info.credential)(shared, base_dir=tmp_path)


def test_essential_settings_are_the_addresses_and_nothing_with_a_secret() -> None:
    asked = {
        name: [spec.key for spec in registry.connector_info(name).settings if spec.is_asked]
        for name in NETWORKED
    }
    assert asked["mock.materials"] == ["base_url"]
    assert asked["mock.suppliers"] == ["base_url"]


# --- the compliance block explains itself ---------------------------------------------------------


def test_every_compliance_field_and_every_authorization_has_its_help_text() -> None:
    assert set(FIELD_HELP) == set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
    assert set(AUTHORIZATION_HELP) == set(AUTHORIZATIONS)
    assert all(text.strip() for text in [*FIELD_HELP.values(), *AUTHORIZATION_HELP.values()])
