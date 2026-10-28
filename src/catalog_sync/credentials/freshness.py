"""Is a stored credential still good, or should it be renewed? Reports; the caller decides.

Failing in the middle of a scheduled run is the worst way to find out a token expired, so the
state has four values, not two. ``UNKNOWN`` (the expiry cannot be read) is deliberately not
``FRESH``: an unreadable token is not evidence of a valid one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from catalog_sync.credentials.base import Credential

_SECONDS_PER_DAY = 86_400


class Freshness(StrEnum):
    FRESH = "fresh"
    RENEW_SOON = "renew_soon"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FreshnessReport:
    state: Freshness
    days_left: float | None  # negative once expired; None when the expiry is unknown


def assess(credential: Credential, *, now: datetime, renew_before_days: float) -> FreshnessReport:
    """Expired at or after the expiry instant; renew-soon strictly inside the renewal window."""
    if renew_before_days < 0:
        raise ValueError(f"renew_before_days must not be negative, got {renew_before_days}")
    if credential.expires_at is None:
        return FreshnessReport(Freshness.UNKNOWN, None)
    days_left = (credential.expires_at - now).total_seconds() / _SECONDS_PER_DAY
    if days_left <= 0:
        return FreshnessReport(Freshness.EXPIRED, days_left)
    if days_left < renew_before_days:
        return FreshnessReport(Freshness.RENEW_SOON, days_left)
    return FreshnessReport(Freshness.FRESH, days_left)


def renewal_window_warning(
    *, renew_before_days: float, schedule_interval_days: float | None
) -> str | None:
    """Warn when the renewal window is shorter than the gap between scheduled runs.

    A run only sees "renew soon" when it starts inside the window. If runs are further apart than
    the window is long, a token can run out between two runs without any run ever having seen it
    approach expiry, and the next scheduled run starts with a dead credential.
    """
    if schedule_interval_days is None or renew_before_days >= schedule_interval_days:
        return None
    return (
        f"renew_before_days ({renew_before_days:g}) is shorter than the schedule interval "
        f"({schedule_interval_days:g} days): a token can expire between two runs without any run "
        f"seeing it approach expiry. Set renew_before_days to at least {schedule_interval_days:g}."
    )
