"""What this connector sends, and the credential file layout that feeds it.

Unlike a connector for a real service, nothing here imitates a browser: the Mock SaaS has no use
for a disguise, and a connector should say what it is. The user agent names the tool and its
version, never a person or an account.
"""

from __future__ import annotations

from collections.abc import Mapping

from catalog_sync.credentials.base import Credential, CredentialLayout
from catalog_sync.sdk import USER_AGENT, header_layout

TOKEN_KEY = "AUTHORIZATION"  # noqa: S105 — the name of a key in the credential file


def credential_layout(host: str) -> CredentialLayout:
    """The credential is one bearer token; a pasted cURL command must be for `host`."""
    return header_layout(host, key=TOKEN_KEY, header="authorization")


def api_headers(credential: Credential) -> Mapping[str, str]:
    return {"accept": "application/json", "user-agent": USER_AGENT, **credential.headers}
