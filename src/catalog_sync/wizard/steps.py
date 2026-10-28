"""The questions about the connector, its settings, the compliance block and the login.

Each function asks what it needs through a ``Prompter`` and returns plain values; none of them
writes a file. What is asked comes from declarations (``ConnectorInfo``, ``SettingSpec``, the
compliance field list), not from knowledge of any one connector.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from catalog_sync import registry
from catalog_sync.cli_credentials import ResolvedCredential, cmd_login, resolve_credential
from catalog_sync.compliance import (
    AUTHORIZATION_HELP,
    AUTHORIZATIONS,
    FIELD_HELP,
    check_field,
)
from catalog_sync.connector_info import ConnectorInfo, SettingSpec
from catalog_sync.credentials.freshness import Freshness
from catalog_sync.errors import ComplianceError, ConfigError, CredentialMissing
from catalog_sync.sdk.credentials import EnvAccount
from catalog_sync.sdk.options import OptionReader
from catalog_sync.wizard.prompts import (
    Parser,
    Prompter,
    choice,
    non_empty_text,
    whole_number,
    yes_no,
)

# A conservative start for the two numbers a person has to take from the vendor's terms; only ever
# offered as a suggestion in the terminal, never applied by a script.
SUGGESTED_REQUESTS_PER_MINUTE = "60"
SUGGESTED_RETENTION_DAYS = "90"
_COUNT_FIELDS = frozenset({"max_requests_per_minute", "retention_days", "tos_review_max_age_days"})
_SOURCE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")

if TYPE_CHECKING:
    from catalog_sync.cli import Context


@dataclass(frozen=True)
class SourceChoice:
    plugin: str
    info: ConnectorInfo
    name: str  # the [sources.NAME] key, which the profile is named after as well
    options: Mapping[str, object]  # the asked settings, typed
    credential_options: Mapping[str, object]


# --- 1. the connector ---------------------------------------------------------------------------


def connector_names() -> list[str]:
    """The source plugins that contact a service (a local one has nothing to log in to)."""
    local = set(registry.available(registry.LOCAL_SOURCES_GROUP))
    return sorted(set(registry.available(registry.SOURCES_GROUP)) - local)


def choose_connector(prompter: Prompter) -> tuple[str, ConnectorInfo]:
    names = connector_names()
    declared = set(registry.available(registry.CONNECTORS_GROUP))
    prompter.say("Connectors:")
    for number, name in enumerate(names, start=1):
        summary = (
            registry.connector_info(name).summary
            if name in declared
            else "(declares no settings; its [sources.NAME] table has to be written by hand)"
        )
        prompter.say(f"  {number}. {name} - {summary}")
    plugin = prompter.ask("connector", "Which connector? (name or number)", choice(names))
    return plugin, registry.connector_info(plugin)


def name_the_source(prompter: Prompter, plugin: str) -> str:
    def parse(text: str) -> str:
        if _SOURCE_NAME.fullmatch(text.strip()) is None:
            raise ConfigError(
                "use lowercase letters, digits, '-' and '_', starting with a letter or digit"
            )
        return text.strip()

    return prompter.ask(
        "name", "Name for this source in the config", parse, default=plugin.replace(".", "-")
    )


# --- 2. its settings ----------------------------------------------------------------------------


def ask_settings(
    prompter: Prompter, plugin: str, info: ConnectorInfo
) -> tuple[dict[str, object], dict[str, object]]:
    """The settings `init` asks about (see ``SettingSpec.is_asked``), typed, and the subset the
    credential kind shares. Everything else keeps its default; the written file says how to
    change it."""
    asked = [spec for spec in info.settings if spec.is_asked]
    if asked:
        prompter.say("Settings:")
    settings = prompter.child("settings")
    options: dict[str, object] = {}
    for spec in asked:
        value = ask_setting(settings, plugin, spec)
        if value is not None:
            options[spec.key] = value
    shared = {key: options[key] for key in info.credential_settings if key in options}
    return options, shared


def ask_setting(
    prompter: Prompter, label: str, spec: SettingSpec, *, default: str | None = None
) -> object:
    """One setting, typed. A setting with no default that is not required (a token variable a
    platform may not need) can be left empty, and then is not written. `default` replaces the
    declared one where the wizard knows better (a table named after the entity)."""

    def parse(text: str) -> object:
        return OptionReader({spec.key: text}, label=label).read(spec)

    question = spec.key
    hint = f"  {spec.help}"
    if spec.default is None and not spec.required:
        return prompter.ask_optional(spec.key, question, parse, help_text=hint)
    chosen = (
        default if default is not None else (None if spec.default is None else str(spec.default))
    )
    return prompter.ask(spec.key, question, parse, default=chosen, help_text=hint)


# --- 3. the compliance block --------------------------------------------------------------------


def ask_compliance(
    prompter: Prompter,
    info: ConnectorInfo,
    settings: Mapping[str, object],
    *,
    label: str,
    today: date,
) -> dict[str, object]:
    """The declaration, field by field. Each answer is checked by the very rules the compliance
    gate applies (``check_field``); the result is what goes in ``[sources.NAME.compliance]``."""
    prompter.say("Compliance declaration (the tool refuses to contact a service without it):")
    asked = prompter.child("compliance")

    def field(name: str, suggestion: str | None = None) -> object:
        return asked.ask(
            name,
            name,
            _field_parser(name, label, today),
            suggestion=suggestion,
            help_text=f"  {FIELD_HELP[name]}",
        )

    declaration: dict[str, object] = {"data_owner": field("data_owner")}
    declaration["authorization"] = asked.ask(
        "authorization",
        "authorization",
        choice(AUTHORIZATIONS),
        help_text=_authorization_help(),
    )
    declaration["authorization_ref"] = field("authorization_ref")
    tos_url = field("tos_url", suggestion=_terms_hint(info, settings))
    declaration["tos_url"] = tos_url
    declaration["tos_reviewed_on"] = _review_date(asked, str(tos_url), label, today)
    declaration["max_requests_per_minute"] = field(
        "max_requests_per_minute", SUGGESTED_REQUESTS_PER_MINUTE
    )
    declaration["retention_days"] = field("retention_days", SUGGESTED_RETENTION_DAYS)
    return declaration


def _field_parser(name: str, label: str, today: date) -> Parser[object]:
    """Text to the value `check_field` accepts for this field (the counts are numbers)."""
    is_count = name in _COUNT_FIELDS

    def parse(text: str) -> object:
        value: object = whole_number(1)(text) if is_count else text.strip()
        return check_field(name, value, label=label, today=today)

    return parse


def _authorization_help() -> str:
    lines = [f"  {FIELD_HELP['authorization']}"]
    lines += [f"    {name}: {AUTHORIZATION_HELP[name]}" for name in AUTHORIZATIONS]
    return "\n".join(lines)


def _terms_hint(info: ConnectorInfo, settings: Mapping[str, object]) -> str | None:
    if info.terms_url is None:
        return None
    return info.terms_url.format(**{key: str(value) for key, value in settings.items()})


def _review_date(asked: Prompter, tos_url: str, label: str, today: date) -> date:
    """Today, but only once the person has said they read the terms; otherwise nothing is
    written: a date the tool picked would be a claim nobody made."""
    reviewed = asked.ask(
        "tos_reviewed",
        f"Have you read the terms of service at {tos_url}? (yes/no)",
        yes_no,
        help_text="  The date goes into the declaration, and the audit log keeps its hash.",
    )
    if not reviewed:
        raise ComplianceError(
            f"read the terms at {tos_url} first, then run `catalog-sync init` again: the "
            "declaration records the day you reviewed them, and the tool will not pick one for you"
        )
    stamp = asked.ask(
        "tos_reviewed_on",
        "On which day (YYYY-MM-DD)?",
        lambda text: check_field("tos_reviewed_on", text.strip(), label=label, today=today),
        default=today.isoformat(),
    )
    if not isinstance(stamp, date):
        raise TypeError("tos_reviewed_on is checked into a date")
    return stamp


# --- 4. the login -------------------------------------------------------------------------------


def login_methods(resolved: ResolvedCredential, *, stored_days: float | None) -> dict[str, str]:
    """The ways to log in this credential kind offers, each with a line saying what it does."""
    kind = resolved.kind
    methods: dict[str, str] = {}
    if stored_days is not None:
        methods["stored"] = f"use the credential already stored ({stored_days:.1f} day(s) left)"
    if kind.form_login is not None:
        methods["form"] = f"an HTTP login, no browser{_account_hint(resolved)}"
    if kind.browser is not None and kind.verify is not None:
        methods["browser"] = (
            "log in through a browser (needs the 'browser' extra); headed, you type the password"
            f" yourself{_account_hint(resolved)}"
        )
    methods["paste"] = "paste a copied 'Copy as cURL' command from the browser's network tab"
    return methods


def _account_hint(resolved: ResolvedCredential) -> str:
    account = resolved.kind.account
    if not isinstance(account, EnvAccount):
        return ""
    return f"; the account is read from ${account.user_env} and ${account.password_env}"


def stored_days_left(resolved: ResolvedCredential) -> float | None:
    """Days a stored credential is still good for, or None when there is none or it is not fresh."""
    try:
        credential = resolved.load()
    except CredentialMissing:
        return None
    report = resolved.assess(credential)
    return report.days_left if report.state is Freshness.FRESH else None


def log_in(prompter: Prompter, context: Context, credential_name: str) -> None:
    resolved = resolve_credential(credential_name, context)
    methods = login_methods(resolved, stored_days=stored_days_left(resolved))
    prompter.say("Login methods:")
    for number, (name, text) in enumerate(methods.items(), start=1):
        prompter.say(f"  {number}. {name} - {text}")
    ask = prompter.child("login")
    method = ask.ask("method", "How do you log in? (name or number)", choice(list(methods)))
    if method == "stored":
        prompter.say("  using the stored credential")
        return
    paste_file = ""
    if method == "paste":
        paste_file = ask.ask(
            "paste_file",
            "File with the copied cURL command ('-' to paste it here, then Ctrl-D)",
            non_empty_text,
        )
    headed = method == "browser" and ask.ask(
        "headed", "Open a visible window and log in yourself? (yes/no)", yes_no, default="no"
    )
    arguments = argparse.Namespace(
        credential=credential_name,
        paste_curl=paste_file or None,
        form=method == "form",
        browser=method == "browser",
        headed=headed,
        force=False,
    )
    cmd_login(arguments, context)


# --- the sample ---------------------------------------------------------------------------------


def ask_sample_size(prompter: Prompter) -> int:
    return prompter.child("sample").ask(
        "items",
        "How many items should the sample pull?",
        whole_number(1, 100),
        default="5",
    )


def confirm_sample(prompter: Prompter) -> None:
    ok = prompter.child("sample").ask(
        "looks_right", "Does the sample look right? (yes/no)", yes_no, default="yes"
    )
    if not ok:
        raise ConfigError(
            "stopped as asked: change the mapping (docs/connector-guide.md) and run init again; "
            "nothing was written"
        )
