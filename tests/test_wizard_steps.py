"""The wizard's questions one at a time: no service, no terminal, no files.

Each case feeds a ``Prompter`` the way a script (an answers table) or a person (typed lines) would
and checks what comes back, or what is refused and how.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from catalog_sync import registry
from catalog_sync.cli_credentials import ResolvedCredential
from catalog_sync.compliance import AUTHORIZATIONS
from catalog_sync.connector_info import SettingKind, SettingSpec
from catalog_sync.credentials.kind import CredentialKind
from catalog_sync.errors import ComplianceError, ConfigError
from catalog_sync.wizard import steps
from catalog_sync.wizard.flow import Wizard, _refuse_to_overwrite
from catalog_sync.wizard.prompts import Prompter

TODAY = date(2026, 10, 26)
COMPLIANCE: dict[str, object] = {
    "data_owner": "Demo Co",
    "authorization": "own_account",
    "authorization_ref": "ticket-123",
    "tos_url": "https://vendor.example.invalid/terms",
    "tos_reviewed": True,
    "max_requests_per_minute": 60,
    "retention_days": 30,
}


SETTINGS = {
    "base_url": "https://app.example.invalid"
}  # what the connector's terms hint is built on


def scripted(**answers: object) -> tuple[Prompter, list[str]]:
    said: list[str] = []
    return Prompter(answers, interactive=False, write=said.append), said


def declare(**overrides: object) -> dict[str, object]:
    prompter, _ = scripted(compliance={**COMPLIANCE, **overrides})
    return steps.ask_compliance(
        prompter,
        registry.connector_info("mock.materials"),
        SETTINGS,
        label="sources.x",
        today=TODAY,
    )


def typed(*lines: str) -> tuple[Prompter, list[str], list[str]]:
    """A person at a terminal who types `lines`; also the prompts shown and the lines said."""
    remaining: Iterator[str] = iter(lines)
    prompts: list[str] = []
    said: list[str] = []

    def read_line(prompt: str) -> str:
        prompts.append(prompt)
        return next(remaining)

    return Prompter({}, interactive=True, read_line=read_line, write=said.append), prompts, said


# --- the connector --------------------------------------------------------------------------------


def test_the_connectors_offered_are_the_networked_sources_with_a_line_each() -> None:
    names = steps.connector_names()
    assert names == sorted(names) and "mock.materials" in names
    assert not set(names) & set(registry.available(registry.LOCAL_SOURCES_GROUP))
    prompter, said = scripted(connector="mock.materials")
    plugin, info = steps.choose_connector(prompter)
    assert plugin == "mock.materials" and info.credential == "mock"
    names = steps.connector_names()
    listing = [line for line in said if line.startswith("  ") and ". " in line]
    assert len(listing) == len(names)
    assert any(". mock.materials - " in line for line in listing)
    assert all(" - " in line for line in listing), "a line of description for each"


def test_a_connector_can_be_chosen_by_its_number() -> None:
    prompter, _ = scripted(connector="1")
    plugin, _ = steps.choose_connector(prompter)
    assert plugin == steps.connector_names()[0]


def test_a_source_name_is_a_toml_friendly_word() -> None:
    assert steps.name_the_source(scripted()[0], "mock.materials") == "mock-materials"
    for bad in ("Has Space", "UPPER", "-lead", "a.b"):
        with pytest.raises(ConfigError, match="lowercase"):
            steps.name_the_source(scripted(name=bad)[0], "mock.materials")


# --- the settings ---------------------------------------------------------------------------------

BASE_URL = SettingSpec("base_url", SettingKind.URL, "The address of the service.", required=True)
TOKEN_ENV = SettingSpec("token_env", SettingKind.ENV_NAME, "Where the token is.", required=True)


@pytest.mark.parametrize(
    ("bad", "complaint"),
    [
        ("not a url", "must look like"),
        ("ftp://files.example", "must look like"),
        ("https://app.example/with/a/path", "must look like"),
        ("http://[::1", r"is not a valid http\(s\) address"),
        ("https://ops:hunter2-canary" + "@app.example", "must not carry a user name"),
    ],
)
def test_a_base_url_that_is_not_an_http_address_is_refused_and_a_password_in_it_not_repeated(
    bad: str, complaint: str
) -> None:
    prompter, _ = scripted(base_url=bad)
    with pytest.raises(ConfigError, match=complaint) as caught:
        steps.ask_setting(prompter, "mock.materials", BASE_URL)
    assert "base_url" in str(caught.value) and "hunter2-canary" not in str(caught.value)


def test_at_a_terminal_a_bad_address_is_explained_and_asked_again() -> None:
    prompter, prompts, said = typed("nope", "https://app.example.invalid/")
    value = steps.ask_setting(prompter, "mock.materials", BASE_URL)
    assert value == "https://app.example.invalid" and len(prompts) == 2
    assert any("must look like" in line for line in said)


def test_a_secret_where_the_name_of_a_variable_belongs_is_refused_and_not_repeated() -> None:
    secret = "sk-live-" + "abcdef123456"
    prompter, _ = scripted(token_env=secret)
    with pytest.raises(ConfigError, match="NAME of an environment variable") as caught:
        steps.ask_setting(prompter, "mock.materials", TOKEN_ENV)
    assert secret not in str(caught.value)
    assert steps.ask_setting(scripted(token_env="SAAS_TOKEN")[0], "x", TOKEN_ENV) == "SAAS_TOKEN"


def test_a_setting_with_no_default_that_nothing_requires_may_stay_unset() -> None:
    optional = SettingSpec("note", SettingKind.TEXT, "Anything.")
    assert steps.ask_setting(scripted()[0], "x", optional) is None
    assert steps.ask_setting(scripted(note="hi")[0], "x", optional) == "hi"


# --- the compliance block -------------------------------------------------------------------------


def test_a_good_declaration_comes_back_typed_with_the_review_dated_today() -> None:
    declaration = declare()
    assert declaration == {
        "data_owner": "Demo Co",
        "authorization": "own_account",
        "authorization_ref": "ticket-123",
        "tos_url": "https://vendor.example.invalid/terms",
        "tos_reviewed_on": TODAY,
        "max_requests_per_minute": 60,
        "retention_days": 30,
    }


def test_a_review_day_the_person_gives_is_kept_not_replaced_by_today() -> None:
    assert declare(tos_reviewed_on="2026-10-01")["tos_reviewed_on"] == date(2026, 10, 1)


@pytest.mark.parametrize(
    ("overrides", "complaint"),
    [
        ({"authorization": "owner"}, "choose one of: own_account"),
        ({"authorization": ""}, "authorization"),
        ({"data_owner": "<the organisation>"}, "'data_owner' must be a real"),
        ({"authorization_ref": "TODO"}, "'authorization_ref' must be a real"),
        ({"tos_url": "ftp://vendor.example/terms"}, "'tos_url' must be an http"),
        ({"tos_url": "vendor.example/terms"}, "'tos_url' must be an http"),
        ({"tos_url": "https://ops:pw" + "@vendor.example/terms"}, "'tos_url' must not carry"),
        ({"tos_reviewed_on": "2026-10-27"}, "is in the future"),
        ({"tos_reviewed_on": "last spring"}, "must be a date"),
        ({"max_requests_per_minute": "fast"}, "whole number"),
        ({"max_requests_per_minute": 0}, "whole number"),
        ({"retention_days": -3}, "whole number"),
    ],
)
def test_a_bad_declaration_answer_is_refused_naming_the_key(
    overrides: dict[str, object], complaint: str
) -> None:
    key = next(iter(overrides))
    with pytest.raises((ComplianceError, ConfigError), match=complaint) as caught:
        declare(**overrides)
    assert f"compliance.{key}" in str(caught.value)


def test_every_authorization_the_gate_knows_is_accepted_and_nothing_else() -> None:
    for name in AUTHORIZATIONS:
        assert declare(authorization=name)["authorization"] == name
    with pytest.raises(ConfigError, match="choose one of"):
        declare(authorization="OWN_ACCOUNT")


@pytest.mark.parametrize("answer", [False, "no"])
def test_not_having_read_the_terms_refuses_and_no_date_is_picked(answer: object) -> None:
    with pytest.raises(ComplianceError, match="will not pick one for you"):
        declare(tos_reviewed=answer)


def test_without_the_confirmation_nobody_is_assumed_to_have_read_them() -> None:
    answers = {key: value for key, value in COMPLIANCE.items() if key != "tos_reviewed"}
    with pytest.raises(ConfigError, match=r"no answer for 'compliance\.tos_reviewed'"):
        steps.ask_compliance(
            scripted(compliance=answers)[0],
            registry.connector_info("mock.materials"),
            SETTINGS,
            label="sources.x",
            today=TODAY,
        )


@pytest.mark.parametrize("missing", ["max_requests_per_minute", "retention_days", "tos_url"])
def test_a_suggested_value_is_never_applied_by_a_script(missing: str) -> None:
    answers = {key: value for key, value in COMPLIANCE.items() if key != missing}
    with pytest.raises(ConfigError, match=f"no answer for 'compliance.{missing}'"):
        steps.ask_compliance(
            scripted(compliance=answers)[0],
            registry.connector_info("mock.materials"),
            SETTINGS,
            label="sources.x",
            today=TODAY,
        )


def test_at_a_terminal_the_review_day_is_offered_as_today_only_after_yes() -> None:
    lines = ("Demo Co", "own_account", "ticket-123", "https://v.example/terms", "y", "", "60", "30")
    prompter, prompts, said = typed(*lines)
    declaration = steps.ask_compliance(
        prompter, registry.connector_info("mock.materials"), SETTINGS, label="x", today=TODAY
    )
    assert declaration["tos_reviewed_on"] == TODAY
    asked = [p for p in prompts if p.startswith("On which day")]
    assert asked == ["On which day (YYYY-MM-DD)? [2026-10-26]: "]
    assert prompts.index(asked[0]) > next(
        i for i, p in enumerate(prompts) if "terms of service" in p
    )
    assert any("own_account:" in line for line in said), "each authorization is explained"


def test_at_a_terminal_saying_no_to_the_terms_stops_before_asking_for_a_date() -> None:
    lines = ("Demo Co", "own_account", "ticket-123", "https://v.example/terms", "n")
    prompter, prompts, _ = typed(*lines)
    with pytest.raises(ComplianceError, match=r"read the terms at https://v\.example/terms"):
        steps.ask_compliance(
            prompter, registry.connector_info("mock.materials"), SETTINGS, label="x", today=TODAY
        )
    assert not any(p.startswith("On which day") for p in prompts)


# --- the login methods ----------------------------------------------------------------------------


@pytest.fixture
def resolved(tmp_path: Path) -> Callable[..., ResolvedCredential]:
    kind = registry.credential_kind_factory("mock")({}, base_dir=tmp_path)

    def make(**changes: object) -> ResolvedCredential:
        changed: CredentialKind = replace(kind, **changes)
        return ResolvedCredential("mock", changed, tmp_path / "mock.json", 1.0, None)

    return make


def test_form_and_browser_are_offered_only_when_the_credential_kind_has_them(
    resolved: Callable[..., ResolvedCredential],
) -> None:
    assert list(steps.login_methods(resolved(), stored_days=None)) == ["form", "browser", "paste"]
    assert list(steps.login_methods(resolved(form_login=None), stored_days=None)) == [
        "browser",
        "paste",
    ]
    assert list(steps.login_methods(resolved(browser=None), stored_days=None)) == ["form", "paste"]
    assert list(steps.login_methods(resolved(verify=None), stored_days=None)) == ["form", "paste"]
    bare = resolved(form_login=None, browser=None, verify=None)
    assert list(steps.login_methods(bare, stored_days=None)) == ["paste"]


def test_a_fresh_stored_credential_is_the_first_offer_and_says_how_long_it_lasts(
    resolved: Callable[..., ResolvedCredential],
) -> None:
    methods = steps.login_methods(resolved(), stored_days=6.5)
    assert next(iter(methods)) == "stored" and "6.5 day(s) left" in methods["stored"]


def test_a_credential_that_was_never_stored_has_no_days_left(
    resolved: Callable[..., ResolvedCredential],
) -> None:
    assert steps.stored_days_left(resolved()) is None


# --- the file is not overwritten ------------------------------------------------------------------


def test_an_existing_file_is_refused_unless_replacing_it_was_asked_for(tmp_path: Path) -> None:
    path = tmp_path / "catalog-sync.toml"
    wizard = Wizard(config_path=path, answers={}, is_interactive=False, may_overwrite=False)
    _refuse_to_overwrite(wizard)  # nothing there yet
    path.write_text("# mine\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"already exists.*--force"):
        _refuse_to_overwrite(wizard)
    _refuse_to_overwrite(replace(wizard, may_overwrite=True))
