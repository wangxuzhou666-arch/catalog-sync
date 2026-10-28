"""The option reader every connector builds its settings with."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from catalog_sync.connector_info import ENV_NAME_RULE, SettingKind, SettingSpec
from catalog_sync.credentials.base import Credential, CredentialHandle
from catalog_sync.errors import ComplianceError, ConfigError
from catalog_sync.sdk import OptionReader

HANDLE = CredentialHandle("c", lambda: Credential({}, {}, None, None), "renew")


def reader(**options: object) -> OptionReader:
    return OptionReader(options, label="acme.items")


def test_scalars_are_accepted_as_toml_values_and_as_command_line_strings() -> None:
    options = reader(page_size="25", pause=0.5, retries=3, name="x")
    assert options.integer("page_size", 10, minimum=1) == 25
    assert options.number("pause", 0.3) == 0.5
    assert options.integer("retries", 1, minimum=0) == 3
    assert options.text("name", "d") == "x"
    assert reader().integer("page_size", 10, minimum=1) == 10, "the default when absent"


@pytest.mark.parametrize(
    ("value", "call", "complaint"),
    [
        (0, lambda r: r.integer("n", 1, minimum=1), "'n' must be an integer at least 1, got 0"),
        (9, lambda r: r.integer("n", 1, minimum=1, maximum=5), "'n' must be an integer between 1"),
        (-1, lambda r: r.number("n", 1.0), "'n' must be a number >= 0, got -1"),
        ("", lambda r: r.text("n", "d"), "'n' must be a non-empty string"),
        (3, lambda r: r.table("n"), "'n' must be a table"),
    ],
)
def test_a_wrong_value_names_the_source_and_the_key(
    value: object, call: Callable[[OptionReader], object], complaint: str
) -> None:
    with pytest.raises(ConfigError, match=re.escape(f"acme.items: {complaint}")):
        call(reader(n=value))


@pytest.mark.parametrize("value", ["my_token", "has space", "9LIVES", "X" * 65])
def test_an_env_name_the_rule_refuses_quotes_the_rule_itself_and_never_the_value(
    value: str,
) -> None:
    """The reader and the wizard prompt share ENV_NAME_RULE, so the two cannot describe different
    rules (the reader once said "letters, digits and underscores" after the rule had tightened)."""
    spec = SettingSpec("token_env", SettingKind.ENV_NAME, "Where the token is.")
    with pytest.raises(ConfigError) as caught:
        reader(token_env=value).read(spec)
    assert ENV_NAME_RULE in str(caught.value)
    assert value not in str(caught.value)


@pytest.mark.parametrize("value", [True, False, 1.5, [1], "many"])
def test_a_value_of_the_wrong_type_is_not_coerced(value: object) -> None:
    with pytest.raises(ConfigError, match="must be an integer"):
        reader(n=value).integer("n", 1, minimum=0)


def test_numbers_refuse_nan_and_booleans() -> None:
    for bad in (float("nan"), True, "nan", "fast"):
        with pytest.raises(ConfigError, match="must be a number >= 0"):
            reader(n=bad).number("n", 1.0)


def test_unknown_options_are_an_error_that_lists_them() -> None:
    options = reader(page_size=1, pagesize=2, nope=3)
    with pytest.raises(
        ConfigError, match=r"acme.items: unknown option\(s\) \['nope', 'pagesize'\]"
    ):
        options.reject_unknown({"page_size"})
    options.reject_unknown({"page_size", "pagesize", "nope"})


def test_the_credential_option_must_be_a_handle_not_a_name() -> None:
    assert reader(credential=HANDLE).credential() is HANDLE
    for options in ({}, {"credential": "acme"}):
        with pytest.raises(ConfigError, match=r"acme\.items needs `credential"):
            OptionReader(options, label="acme.items").credential()


@pytest.mark.parametrize("value", ["ftp://x", "localhost:8", "http://x/api", "http://x/?a=1"])
def test_a_base_url_is_scheme_and_host_only(value: str) -> None:
    with pytest.raises(
        ConfigError, match=r"'base_url' must look like https://app\.example\.invalid"
    ):
        reader(base_url=value).base_url("https://app.example.invalid")


def test_a_base_url_loses_its_trailing_slash_and_defaults() -> None:
    assert (
        reader(base_url="http://127.0.0.1:9/").base_url("https://d.invalid") == "http://127.0.0.1:9"
    )
    assert reader().base_url("https://d.invalid/") == "https://d.invalid"


def test_the_request_ceiling_is_absent_or_a_positive_integer() -> None:
    assert reader().request_ceiling() is None
    assert reader(max_requests_per_minute=None).request_ceiling() is None
    assert reader(max_requests_per_minute="60").request_ceiling() == 60
    for bad in (0, -3, "many", 1.5, True):
        with pytest.raises(ConfigError, match="max_requests_per_minute"):
            reader(max_requests_per_minute=bad).request_ceiling()


def test_an_unset_gap_is_raised_to_what_the_ceiling_needs_and_never_lowered() -> None:
    assert reader().request_gap(0.3, ceiling=None) == 0.3
    assert reader().request_gap(0.3, ceiling=60) == 1.0
    assert reader().request_gap(0.3, ceiling=1000) == 0.3


def test_an_explicit_gap_shorter_than_the_ceiling_allows_is_refused() -> None:
    with pytest.raises(ComplianceError, match="shorter than 60 / max_requests_per_minute"):
        reader(request_gap_s=0.5).request_gap(0.3, ceiling=60)
    assert reader(request_gap_s=1).request_gap(0.3, ceiling=60) == 1.0
    assert reader(request_gap_s="2").request_gap(0.3, ceiling=60) == 2.0
    assert reader(request_gap_s=0).request_gap(0.3, ceiling=None) == 0.0, "no ceiling, no floor"
