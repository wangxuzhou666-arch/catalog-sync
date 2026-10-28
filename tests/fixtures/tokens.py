"""Synthetic bearer tokens, assembled at run time so no token-shaped literal sits in the source."""

from __future__ import annotations

import base64
import json


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_jwt(
    *, exp: float | None, jti: str | None = "jti-demo-0001", extra: dict[str, object] | None = None
) -> str:
    """An unsigned three-part token with the given claims."""
    claims: dict[str, object] = {**(extra or {})}
    if exp is not None:
        claims["exp"] = exp
    if jti is not None:
        claims["jti"] = jti
    header = b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = b64url(json.dumps(claims).encode())
    return f"{header}.{payload}.{b64url(b'not-a-real-signature')}"


def bearer(token: str) -> str:
    return "Bearer " + token
