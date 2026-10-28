"""Credential building blocks for a connector: the file layout, the account, the login, the check.

A connector describes its credential in a ``CredentialKind`` (``catalog_sync.credentials.kind``);
these are the parts that were the same in every connector written so far:

* ``header_layout``: a credential that is one header (a bearer token, a session cookie, an API key).
* ``EnvAccount``: the login account, read from two environment variables and never from a file.
* ``FormLogin``: log in with one HTTP request and keep the token (or the session cookie) it answers
  with. No browser, so it also runs in CI.
* ``verify_with_probe``: prove a freshly obtained credential works before it replaces a stored one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from catalog_sync.credentials.base import CredentialLayout
from catalog_sync.credentials.browser import LoginAccount
from catalog_sync.envelope import require_state_ok
from catalog_sync.errors import (
    AuthExpired,
    ConfigError,
    HttpStatusError,
    LoginFailed,
    SourceShapeError,
)
from catalog_sync.http import HttpClient, HttpSeams, HttpSettings
from catalog_sync.normalize.mapping import pick
from catalog_sync.sdk.connection import USER_AGENT

_HTTP_UNAUTHORIZED = 401


def header_layout(host: str, *, key: str, header: str) -> CredentialLayout:
    """A credential that is a single header: ``key`` is its name in the credential file (upper
    snake case), ``header`` the request header it becomes. A pasted cURL command must be for
    ``host`` (or a subdomain), or its token would later be sent to a service it was not issued by.
    """
    return CredentialLayout(
        headers={key: header.lower()},
        token_key=key,
        required=frozenset({key}),
        host_suffixes=(host,),
    )


@dataclass(frozen=True)
class EnvAccount:
    """The login account from the environment: both variables, or neither.

    With neither set the result is ``default`` (None unless the connector has a public demo
    account). Half an account is an error, never a silent "no account".
    """

    user_env: str
    password_env: str
    default: LoginAccount | None = None

    def __call__(self, env: Mapping[str, str]) -> LoginAccount | None:
        user, password = env.get(self.user_env, ""), env.get(self.password_env, "")
        if bool(user) != bool(password):
            raise ConfigError(
                f"{self.user_env} and {self.password_env} must both be set, or neither"
            )
        return LoginAccount(user, password) if user else self.default


@dataclass(frozen=True)
class FormLogin:
    """POST the account to ``url`` and keep what the answer carries.

    ``token_path`` (``data.token``) says where the token sits in the JSON answer, and the stored
    value is ``token_prefix`` + the token (``Bearer `` for an Authorization header, empty for an
    API key). For a service that answers with a ``Set-Cookie`` instead, name the cookie in
    ``session_cookie`` and the stored value is ``name=value``. ``credential_key`` is the key of the
    credential file the value is stored under. ``service`` names the service in messages
    ("the Acme API").

    ``expects_state`` is for services that wrap every answer as ``{"state": true, ...}``.
    """

    service: str
    url: str
    account: EnvAccount
    credential_key: str
    user_field: str = "username"
    password_field: str = "password"  # noqa: S105 — the name of a field in the request body
    encoding: Literal["json", "form"] = "json"
    token_path: str = "data.token"  # noqa: S105 — a path into the answer, not a token
    token_prefix: str = "Bearer "  # noqa: S105 — the scheme word, not a secret
    session_cookie: str | None = None
    expects_state: bool = True

    def __call__(
        self, env: Mapping[str, str], *, seams: HttpSeams | None = None
    ) -> Mapping[str, str]:
        """The credential file's values. Raises LoginFailed when the service refuses."""
        account = self.account(env)
        if account is None:
            raise ConfigError(
                f"set {self.account.user_env} and {self.account.password_env} to log in with --form"
            )
        client = HttpClient(
            {"accept": "application/json", "user-agent": USER_AGENT},
            credentialed=False,
            settings=HttpSettings(gap_s=0.0, max_attempts=3),
            seams=seams,
        )
        fields = {self.user_field: account.user, self.password_field: account.password}
        try:
            answer = self._post(client, fields)
        except HttpStatusError as exc:
            if exc.status == _HTTP_UNAUTHORIZED:
                raise LoginFailed(
                    f"{self.service} rejected the user name or password "
                    f"({self.account.user_env} / {self.account.password_env})"
                ) from exc
            raise
        else:
            cookie = client.cookie(self.session_cookie) if self.session_cookie else None
        finally:
            client.close()
        if self.session_cookie:
            return {self.credential_key: self._cookie_value(cookie)}
        return {self.credential_key: self.token_prefix + self._token(answer)}

    def _post(self, client: HttpClient, fields: Mapping[str, str]) -> Mapping[str, object]:
        if self.encoding == "form":
            return client.post_form(self.url, fields=fields)
        return client.post_json(self.url, body=fields, headers={"content-type": "application/json"})

    def _token(self, answer: Mapping[str, object]) -> str:
        if self.expects_state:
            try:
                require_state_ok(answer)
            except SourceShapeError as exc:
                raise LoginFailed(
                    f"{self.service}'s login answer is not what was expected: {exc}"
                ) from exc
        token = pick(answer, self.token_path)
        if not isinstance(token, str) or not token:
            raise LoginFailed(f"{self.service}'s login answer carries no token")
        return token

    def _cookie_value(self, cookie: str | None) -> str:
        if not cookie:
            raise LoginFailed(f"{self.service}'s login answer sets no {self.session_cookie} cookie")
        return f"{self.session_cookie}={cookie}"


def verify_with_probe(
    headers: Mapping[str, str],
    probe: Callable[[HttpClient], object],
    *,
    seams: HttpSeams | None = None,
) -> None:
    """Prove a credential works by making one cheap, read-only call with it.

    Capturing a token is not the same as having a usable one; checking now moves a failure from
    "the next scheduled pull" to the moment someone is looking. ``probe`` receives a client that
    carries ``headers`` and makes the call; HTTP 200 alone proves nothing, so the probe must go
    through the connector's own response check (a wrong shape raises SourceShapeError).
    """
    client = HttpClient(
        headers,
        credentialed=True,
        settings=HttpSettings(gap_s=0.0, max_attempts=3),
        seams=seams,
    )
    try:
        probe(client)
    except AuthExpired as exc:
        raise LoginFailed(
            "the token was captured but the service rejected it (HTTP 401/403): the login did "
            "not produce a usable session"
        ) from exc
    except SourceShapeError as exc:
        raise LoginFailed(
            f"the token was captured but the service's answer is wrong: {exc}"
        ) from exc
    finally:
        client.close()
