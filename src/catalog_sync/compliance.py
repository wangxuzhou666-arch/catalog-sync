"""The compliance declaration every networked source must carry, checked in code.

The tool cannot make a pull lawful; it can refuse to start one until the operator has said, in
the config, who owns the data, on what authority it is read, which terms were reviewed and how
fast the service may be called. The declaration is a required table next to the source::

    [sources.my-source.compliance]
    data_owner = "Example Co"
    authorization = "own_account"     # or customer_authorized, contract, public_data
    authorization_ref = "wiki/data-access/12"   # a pointer to the grant, never its contents
    tos_url = "https://vendor.example/terms"
    tos_reviewed_on = 2026-10-26
    max_requests_per_minute = 120
    retention_days = 90

Everything here is local and cheap, so a refusal happens before a credential is read or a socket
is opened. What the declaration does *not* do is prove any of it: whether the authorisation
exists, or the terms allow this use, stays the operator's responsibility (docs/compliance.md).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final
from urllib.parse import urlsplit

from catalog_sync import registry
from catalog_sync.config import SourceConfig
from catalog_sync.errors import ComplianceError
from catalog_sync.snapshot import canonical_json, sha256_hex
from catalog_sync.state import RunRow

AUTHORIZATIONS: Final = ("own_account", "customer_authorized", "contract", "public_data")
DEFAULT_TOS_REVIEW_MAX_AGE_DAYS: Final = 180
REQUIRED_FIELDS: Final = (
    "data_owner",
    "authorization",
    "authorization_ref",
    "tos_url",
    "tos_reviewed_on",
    "max_requests_per_minute",
    "retention_days",
)
OPTIONAL_FIELDS: Final = ("tos_review_max_age_days",)

# What each field means, in the words `catalog-sync init` shows next to the question and the docs
# repeat. They live here, beside the list they explain, so a field cannot be added without one
# (a test compares the keys).
FIELD_HELP: Final[Mapping[str, str]] = {
    "data_owner": "The organisation whose data this is (not necessarily the one running the tool).",
    "authorization": "On what authority the data is read. Choose one of the values below.",
    "authorization_ref": (
        "Where the grant is recorded: a ticket, a wiki page, a contract number. A pointer to it, "
        "never its content."
    ),
    "tos_url": "The terms of service of the service you read from, which you have reviewed.",
    "tos_reviewed_on": "The day you reviewed those terms (YYYY-MM-DD).",
    "max_requests_per_minute": (
        "A ceiling on requests per minute, enforced on every request of this source, retries "
        "included. Start low; raise it only if the service says it can take more."
    ),
    "retention_days": (
        "How long the raw responses cached in the workspace may be kept. `run` warns when they "
        "are older and `cache purge` deletes them."
    ),
    "tos_review_max_age_days": "After this many days the terms count as stale and a warning shows.",
}
AUTHORIZATION_HELP: Final[Mapping[str, str]] = {
    "own_account": "your organisation's own account on the service, reading your own data",
    "customer_authorized": "a customer has authorised you to read their data; keep the grant",
    "contract": "a contract or agreement with the service or its owner covers this access",
    "public_data": "the data is public and its terms allow this use",
}
_SECONDS_PER_MINUTE: Final = 60.0


@dataclass(frozen=True)
class Compliance:
    data_owner: str
    authorization: str
    authorization_ref: str
    tos_url: str
    tos_reviewed_on: date
    max_requests_per_minute: int
    retention_days: int
    tos_review_max_age_days: int = DEFAULT_TOS_REVIEW_MAX_AGE_DAYS

    @property
    def minimum_gap_s(self) -> float:
        """The shortest pause between two requests that keeps the rate under the ceiling."""
        return _SECONDS_PER_MINUTE / self.max_requests_per_minute

    @property
    def digest(self) -> str:
        """sha256 of the declaration, so the audit log can show which one a run was under."""
        return sha256_hex(
            canonical_json(
                {
                    "data_owner": self.data_owner,
                    "authorization": self.authorization,
                    "authorization_ref": self.authorization_ref,
                    "tos_url": self.tos_url,
                    "tos_reviewed_on": self.tos_reviewed_on.isoformat(),
                    "max_requests_per_minute": self.max_requests_per_minute,
                    "retention_days": self.retention_days,
                    "tos_review_max_age_days": self.tos_review_max_age_days,
                }
            )
        )

    def tos_review_warning(self, today: date) -> str | None:
        age_days = (today - self.tos_reviewed_on).days
        if age_days <= self.tos_review_max_age_days:
            return None
        return (
            f"the terms of service were last reviewed {age_days} days ago "
            f"(limit {self.tos_review_max_age_days}): review {self.tos_url} again and update "
            "tos_reviewed_on"
        )


def parse_compliance(table: Mapping[str, object] | None, *, label: str, today: date) -> Compliance:
    """Validate a `[sources.NAME.compliance]` table. `label` names it in error messages."""
    if table is None:
        raise ComplianceError(
            f"{label} has no compliance declaration. A source that contacts a service needs a "
            f"[{label}.compliance] table (data_owner, authorization, authorization_ref, tos_url, "
            "tos_reviewed_on, max_requests_per_minute, retention_days); see "
            "catalog-sync.example.toml"
        )
    unknown = sorted(set(table) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS))
    if unknown:
        raise ComplianceError(f"{label}.compliance: unknown key(s) {unknown}")
    missing = [key for key in REQUIRED_FIELDS if key not in table]
    if missing:
        raise ComplianceError(f"{label}.compliance: missing required key(s) {missing}")
    authorization = _authorization(table, label)
    return Compliance(
        data_owner=_text(table, "data_owner", label),
        authorization=authorization,
        authorization_ref=_text(table, "authorization_ref", label),
        tos_url=_url(table, label),
        tos_reviewed_on=_review_date(table, label, today),
        max_requests_per_minute=_count(table, "max_requests_per_minute", label),
        retention_days=_count(table, "retention_days", label),
        tos_review_max_age_days=(
            _count(table, "tos_review_max_age_days", label)
            if "tos_review_max_age_days" in table
            else DEFAULT_TOS_REVIEW_MAX_AGE_DAYS
        ),
    )


def check_field(name: str, value: object, *, label: str, today: date) -> object:
    """Validate one field of the declaration on its own, by the very rules `parse_compliance`
    applies to the whole, and return it as it is stored (a date for ``tos_reviewed_on``).

    This is what lets `catalog-sync init` ask for the fields one at a time and refuse a bad
    answer on the spot, without a second set of rules that could drift from these.
    """
    table = {name: value}
    checks: dict[str, Callable[[], object]] = {
        "data_owner": lambda: _text(table, name, label),
        "authorization": lambda: _authorization(table, label),
        "authorization_ref": lambda: _text(table, name, label),
        "tos_url": lambda: _url(table, label),
        "tos_reviewed_on": lambda: _review_date(table, label, today),
        "max_requests_per_minute": lambda: _count(table, name, label),
        "retention_days": lambda: _count(table, name, label),
        "tos_review_max_age_days": lambda: _count(table, name, label),
    }
    if name not in checks:
        raise ComplianceError(f"{label}.compliance: unknown key {name!r}")
    return checks[name]()


def check_request_pacing(
    compliance: Compliance, options: Mapping[str, object], *, label: str
) -> None:
    """Refuse a configured `request_gap_s` shorter than the declared rate allows.

    A source that does not set it falls back to its own default; the HTTP client's sliding-window
    ceiling holds the declared rate whatever the gap is.
    """
    raw = options.get("request_gap_s")
    if raw is None:
        return
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ComplianceError(f"{label}: request_gap_s must be a number, got {raw!r}")
    try:
        gap = float(raw)  # a TOML number, or the string a --option KEY=VALUE gives
    except ValueError as exc:
        raise ComplianceError(f"{label}: request_gap_s must be a number, got {raw!r}") from exc
    if not math.isfinite(gap) or gap < compliance.minimum_gap_s:
        raise ComplianceError(
            f"{label}: request_gap_s = {raw} is shorter than 60 / max_requests_per_minute = "
            f"{compliance.minimum_gap_s:g} s. Raise request_gap_s or the declared limit "
            "(and only raise the limit if the service allows it)"
        )


@dataclass(frozen=True)
class SourceGate:
    """What the compliance check decided about one source."""

    compliance: Compliance | None  # None only for a source that reads local files
    is_local: bool


def gate_source(
    name: str,
    entry: SourceConfig | None,
    *,
    plugin: str,
    options: Mapping[str, object],
    today: date,
) -> SourceGate:
    """Decide whether a pull of this source may start. Raises ComplianceError when it may not.

    `plugin` is the installed plugin the source runs; a plugin that declares itself local
    (registry.is_local_source) needs no declaration. Everything else does, including a source
    named only on the command line, which can have no config table to hold one. `options` are
    the source's options after command-line overrides, so `--option request_gap_s=0` cannot slip
    past the pacing check.
    """
    if registry.is_local_source(plugin):
        return SourceGate(None, is_local=True)
    label = f"sources.{name}"
    if entry is None:
        raise ComplianceError(
            f"source {name!r} is not defined in the config, so it has no compliance declaration. "
            f"Define it as [sources.{name}] with a [sources.{name}.compliance] table"
        )
    compliance = parse_compliance(entry.compliance, label=label, today=today)
    check_request_pacing(compliance, options, label=label)
    return SourceGate(compliance, is_local=False)


def stale_runs(runs: Iterable[RunRow], retention_days: int, *, now: datetime) -> list[RunRow]:
    """Runs whose cache is older than the declared retention."""
    cutoff = now - timedelta(days=retention_days)
    return [run for run in runs if datetime.fromisoformat(run.started_at) < cutoff]


_BLANK_OR_PLACEHOLDER = re.compile(r"\s*(?:<[^>]*>|todo|tbd|changeme|\.\.\.)?\s*", re.IGNORECASE)


def _text(table: Mapping[str, object], key: str, label: str) -> str:
    value = table[key]
    if not isinstance(value, str) or _BLANK_OR_PLACEHOLDER.fullmatch(value):
        raise ComplianceError(f"{label}.compliance: '{key}' must be a real, non-empty string")
    return value.strip()


def _authorization(table: Mapping[str, object], label: str) -> str:
    authorization = _text(table, "authorization", label)
    if authorization not in AUTHORIZATIONS:
        raise ComplianceError(
            f"{label}.compliance: 'authorization' must be one of {', '.join(AUTHORIZATIONS)}"
        )
    return authorization


def _url(table: Mapping[str, object], label: str) -> str:
    value = _text(table, "tos_url", label)
    try:
        parts = urlsplit(value)
    except ValueError as exc:  # e.g. an unclosed '[' of an IPv6 host
        raise ComplianceError(f"{label}.compliance: 'tos_url' must be an http(s) URL") from exc
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ComplianceError(f"{label}.compliance: 'tos_url' must be an http(s) URL")
    if parts.username is not None or parts.password is not None:
        raise ComplianceError(
            f"{label}.compliance: 'tos_url' must not carry a user name or password"
        )
    return value


def _review_date(table: Mapping[str, object], label: str, today: date) -> date:
    value = table["tos_reviewed_on"]
    if type(value) is date:
        reviewed = value
    elif isinstance(value, str):
        try:
            reviewed = date.fromisoformat(value)
        except ValueError as exc:
            raise ComplianceError(
                f"{label}.compliance: 'tos_reviewed_on' must be a date like 2026-10-26"
            ) from exc
    else:
        raise ComplianceError(
            f"{label}.compliance: 'tos_reviewed_on' must be a date like 2026-10-26"
        )
    if reviewed > today:
        raise ComplianceError(f"{label}.compliance: 'tos_reviewed_on' is in the future")
    return reviewed


def _count(table: Mapping[str, object], key: str, label: str) -> int:
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ComplianceError(f"{label}.compliance: '{key}' must be a whole number >= 1")
    return value
