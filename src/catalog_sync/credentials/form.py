"""Log in with a plain HTTP request and store what the service answers.

For services whose login is a form or a JSON post, no browser is needed: the kind's `form_login`
makes the request (reading the account from the environment) and returns the credential values.
Like every provider it can be handed `verify`, which proves the stored credential works before
anyone relies on it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from catalog_sync.credentials.base import Credential, CredentialLayout
from catalog_sync.credentials.store import credential_from_values, load_credential, write_values


class FormLoginProvider:
    def __init__(
        self,
        *,
        path: Path,
        layout: CredentialLayout,
        login: Callable[[], Mapping[str, str]],
        verify: Callable[[Credential], None] | None,
    ) -> None:
        self._path = path
        self._layout = layout
        self._login = login
        self._verify = verify

    def load(self) -> Credential:
        return load_credential(self._path, self._layout)

    def refresh(self, *, force: bool) -> Credential:
        # `force` changes nothing: a form login always asks the service for a new credential.
        del force
        values = self._login()
        # Verified before it is saved: a token the service rejects must not replace a stored one.
        candidate = credential_from_values(values, self._layout, name=self._path.stem)
        if self._verify is not None:
            self._verify(candidate)
        write_values(self._path, values)
        return self.load()
