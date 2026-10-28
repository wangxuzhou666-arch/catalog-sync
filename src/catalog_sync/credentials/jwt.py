"""Read the expiry and the id out of a bearer token, to schedule renewal. Nothing more.

The payload is decoded without verifying the signature: the service verifies tokens, this tool
only needs to know *when to ask for a new one*. A token whose claims cannot be read yields
"unknown" (None), never a guess, and the token itself is never logged or put in an exception.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class TokenClaims:
    expires_at: datetime | None
    token_id: str | None


_UNREADABLE = TokenClaims(None, None)
_JWT_SEGMENTS = 3


def decode_claims(authorization: str) -> TokenClaims:
    """Claims of ``Bearer <jwt>`` (or a bare ``<jwt>``); empty claims when unreadable."""
    token = authorization.strip().rsplit(None, 1)[-1] if authorization.strip() else ""
    segments = token.split(".")
    if len(segments) != _JWT_SEGMENTS:
        return _UNREADABLE
    payload = _payload(segments[1])
    if payload is None:
        return _UNREADABLE
    return TokenClaims(_expiry(payload.get("exp")), _token_id(payload.get("jti")))


def describe_token_id(token_id: str | None) -> str:
    """A form of the token id that is safe to print: a short prefix and the length."""
    if token_id is None:
        return "unknown"
    visible = min(6, len(token_id) // 3)
    return f"{token_id[:visible]}...({len(token_id)} chars)"


def _payload(segment: str) -> dict[str, object] | None:
    padded = segment + "=" * (-len(segment) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(padded))
    except ValueError:  # bad base64, bad UTF-8 and bad JSON are all ValueErrors
        return None
    return decoded if isinstance(decoded, dict) else None


def _expiry(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):  # a number outside what datetime can hold
        return None


def _token_id(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value:
        return value
    return str(value) if isinstance(value, int) else None
