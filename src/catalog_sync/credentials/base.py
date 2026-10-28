"""What a credential is, how a service describes the one it needs, and how a source receives it."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, repr=False)
class Credential:
    """What a source needs to authenticate requests.

    The representation shows header *names* only: a credential must be safe to print by accident
    (a log line, a traceback, a failed assertion).
    """

    headers: Mapping[str, str]
    params: Mapping[str, str]  # for services that authenticate through the query string
    expires_at: datetime | None
    token_id: str | None

    def __repr__(self) -> str:
        names = ", ".join(sorted(self.headers))
        return f"Credential(headers=[{names}], expires_at={self.expires_at}, token_id=<hidden>)"


class CredentialProvider(Protocol):
    """Obtains credentials. Implementations: CurlPasteProvider, BrowserLoginProvider."""

    def load(self) -> Credential:
        """The stored credential. Raises CredentialMissing when there is none."""
        ...

    def refresh(self, *, force: bool) -> Credential:
        """Obtain a credential from the live source and store it.

        With `force`, the result must be a different credential than the stored one: a provider
        that can only re-read what is already valid (a browser session) clears it first.
        """
        ...


@dataclass(frozen=True)
class CredentialLayout:
    """The keys of a credential file and how each one becomes a request header.

    A plugin declares one of these per service; the generic store, cURL parser and browser
    grabber work from it, so none of them has to know a service's header names.
    """

    headers: Mapping[str, str]  # file key -> header name, e.g. {"AUTHORIZATION": "authorization"}
    token_key: str  # the file key whose value is a JWT bearer token
    required: frozenset[str]  # keys without which the credential is unusable
    host_suffixes: tuple[str, ...]  # a pasted cURL command must target one of these hosts

    def __post_init__(self) -> None:
        if self.token_key not in self.headers or self.token_key not in self.required:
            raise ValueError("the token key must be one of the layout's required header keys")

    def key_for_header(self, header: str) -> str | None:
        wanted = header.lower()
        return next((key for key, name in self.headers.items() if name == wanted), None)


@dataclass(frozen=True)
class CredentialHandle:
    """What a source receives for its ``credential`` option.

    The CLI resolves the credential name from the config into one of these before it builds the
    source, so a source never touches the workspace layout. ``load`` is called lazily, on the
    first request.
    """

    name: str
    load: Callable[[], Credential]
    renewal_hint: str


def renewal_hint(name: str) -> str:
    return (
        f"Renew it with: catalog-sync login {name} --paste-curl - (paste a copied cURL command) "
        f"or catalog-sync login {name} --browser"
    )
