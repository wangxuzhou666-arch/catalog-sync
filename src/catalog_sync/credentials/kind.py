"""Everything a service plugin tells the core about its credential, in one value."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from catalog_sync.credentials.base import Credential, CredentialLayout
from catalog_sync.credentials.browser import BrowserSite, LoginAccount


@dataclass(frozen=True)
class CredentialKind:
    """One service's credential: its file layout, how a browser logs in, how to check it works.

    ``browser`` and ``verify`` are None for a kind that can only be pasted. ``account`` reads the
    login from the environment (never from a file the tool parses); it returns None when none is
    configured, in which case a browser login needs a person at the window.

    ``form_login`` is for a service whose login is a plain HTTP call (a form or JSON post): given
    the environment it performs the login and returns the credential file's values. It is None
    for a service that needs a browser, and it needs no browser, so it also runs in CI.
    """

    name: str
    layout: CredentialLayout
    browser: BrowserSite | None
    verify: Callable[[Credential], None] | None
    account: Callable[[Mapping[str, str]], LoginAccount | None]
    form_login: Callable[[Mapping[str, str]], Mapping[str, str]] | None = None
