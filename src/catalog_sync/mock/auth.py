"""Login for the Mock SaaS: a bearer token (a signed, short-lived JWT) and a cookie session.

The token is HS256 with a random secret chosen when the server starts, so a token never outlives
the process that issued it and nothing here is a real credential. The lifetime is configurable and
short on purpose: an expired token (HTTP 401) is one of the traps a pull has to survive.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from catalog_sync.errors import ConfigError

USER_ENV = "MOCK_SAAS_USER"
PASSWORD_ENV = "MOCK_SAAS_PASSWORD"  # noqa: S105 — the name of an environment variable
# Demo-only defaults, documented in the README. The server refuses to listen beyond loopback with
# these in place (see server.py), so they never protect anything but a local demo.
DEMO_USER = "demo"
DEMO_PASSWORD = "demo-password"  # noqa: S105 — demo-only default, not a secret
_SECRET_BYTES = 32
_JWT_PARTS = 3


class Unauthorized(Exception):
    """A request carried no valid proof of login. `reason` is safe to show the client."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Account:
    user: str
    password: str

    @property
    def is_demo_default(self) -> bool:
        return self.password == DEMO_PASSWORD


def account_from_env(env: Mapping[str, str]) -> Account:
    """The account the server accepts: both variables, or neither (then the public demo account)."""
    user, password = env.get(USER_ENV, ""), env.get(PASSWORD_ENV, "")
    if bool(user) != bool(password):
        raise ConfigError(f"{USER_ENV} and {PASSWORD_ENV} must both be set, or neither")
    return Account(user, password) if user else Account(DEMO_USER, DEMO_PASSWORD)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Authority:
    """Issues and checks tokens, sessions and signed picture links for one server process."""

    def __init__(self, *, token_ttl_s: float, clock: Callable[[], float]) -> None:
        self._ttl_s = token_ttl_s
        self._clock = clock
        self._secret = secrets.token_bytes(_SECRET_BYTES)
        self._sessions: dict[str, tuple[str, float]] = {}

    # --- bearer tokens ----------------------------------------------------------------------

    def issue_token(self, user: str) -> tuple[str, float]:
        """A token for `user`, and the time (epoch seconds) it stops being accepted."""
        issued = self._clock()
        expires = int(issued + self._ttl_s)
        claims = {"sub": user, "iat": int(issued), "exp": expires, "jti": secrets.token_hex(8)}
        head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        body = _b64(json.dumps(claims).encode())
        return f"{head}.{body}.{self._sign(f'{head}.{body}')}", float(expires)

    def verify_token(self, token: str) -> str:
        """The user a valid token was issued to. Raises Unauthorized (expired, or not ours)."""
        parts = token.split(".")
        if len(parts) != _JWT_PARTS or not hmac.compare_digest(
            parts[2], self._sign(f"{parts[0]}.{parts[1]}")
        ):
            raise Unauthorized("token invalid")
        try:
            claims = json.loads(_unb64(parts[1]))
            user, expires = str(claims["sub"]), float(claims["exp"])
        except (ValueError, KeyError, TypeError) as exc:
            raise Unauthorized("token invalid") from exc
        if self._clock() >= expires:
            raise Unauthorized("token expired")
        return user

    def _sign(self, signing_input: str) -> str:
        return _b64(hmac.new(self._secret, signing_input.encode("ascii"), hashlib.sha256).digest())

    # --- cookie sessions --------------------------------------------------------------------

    def open_session(self, user: str) -> tuple[str, float]:
        session_id = secrets.token_hex(16)
        expires = self._clock() + self._ttl_s
        self._sessions[session_id] = (user, expires)
        return session_id, expires

    def session_user(self, session_id: str) -> str:
        """The user of a live session. Raises Unauthorized (unknown, or expired)."""
        found = self._sessions.get(session_id)
        if found is None:
            raise Unauthorized("session invalid")
        user, expires = found
        if self._clock() >= expires:
            del self._sessions[session_id]
            raise Unauthorized("session expired")
        return user

    # --- signed picture links ---------------------------------------------------------------

    def sign_link(self, path: str, expires: int) -> str:
        message = f"link:{path}:{expires}".encode("ascii")
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()[:32]

    def is_link_valid(self, path: str, expires: int, signature: str) -> bool:
        return hmac.compare_digest(signature, self.sign_link(path, expires))

    def is_link_expired(self, expires: int) -> bool:
        return self._clock() >= expires
