"""The ``mock`` credential kind: log in with a plain HTTP request, by browser, or by cURL paste.

The Mock SaaS offers all three on purpose, so each path of the tool can be tried without a real
service:

* ``login mock --form`` posts the user name and password to ``/api/login`` and stores the bearer
  token it answers with. No browser; this is what CI runs.
* ``login mock --browser`` opens the service's login page and takes the token its own app sends
  (``--headed`` to type the password yourself).
* ``login mock --paste-curl -`` stores a token copied from the browser's network tab.

The account is read from ``MOCK_SAAS_USER`` / ``MOCK_SAAS_PASSWORD``. When they are not set, the
Mock SaaS's public demo account is used: it protects nothing, which is the point of a demo.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from catalog_sync.credentials.base import Credential
from catalog_sync.credentials.browser import BrowserSite, LoginAccount
from catalog_sync.credentials.kind import CredentialKind
from catalog_sync.errors import ConfigError
from catalog_sync.http import HttpSeams
from catalog_sync.plugins.mock.api import MockApi
from catalog_sync.plugins.mock.headers import TOKEN_KEY, api_headers, credential_layout
from catalog_sync.plugins.mock.settings import DEFAULT_BASE_URL
from catalog_sync.sdk import EnvAccount, FormLogin, verify_with_probe

USER_ENV = "MOCK_SAAS_USER"
PASSWORD_ENV = "MOCK_SAAS_PASSWORD"  # noqa: S105 — the name of an environment variable
# The Mock SaaS's documented demo account; the server and this connector agree on it (a test
# checks that they do).
DEMO_USER = "demo"
DEMO_PASSWORD = "demo-password"  # noqa: S105 — demo-only default, not a secret

USER_SELECTOR = "#username"
PASSWORD_SELECTOR = "#password"  # noqa: S105 — a CSS selector
SUBMIT_SELECTOR = "#sign-in"
LOGIN_URL_MARKERS = ("/login",)
_OPTIONS = frozenset({"base_url"})

# Both variables, or neither (then the public demo account).
account_from_env = EnvAccount(
    USER_ENV, PASSWORD_ENV, default=LoginAccount(DEMO_USER, DEMO_PASSWORD)
)


def mock_credential_kind(options: Mapping[str, object], *, base_dir: Path) -> CredentialKind:
    """Entry point of the ``mock`` credential kind."""
    del base_dir  # nothing here is a path
    unknown = sorted(set(options) - _OPTIONS)
    if unknown:
        raise ConfigError(f"credential kind mock: unknown option(s) {unknown}")
    base_url = _base_url(options)
    host = urlsplit(base_url).hostname or ""
    return CredentialKind(
        name="mock",
        layout=credential_layout(host),
        browser=BrowserSite(
            landing_url=f"{base_url}/app",
            host_suffix=host,
            locale="en-US",
            user_selector=USER_SELECTOR,
            password_selector=PASSWORD_SELECTOR,
            submit_selector=SUBMIT_SELECTOR,
            login_url_markers=LOGIN_URL_MARKERS,
        ),
        verify=partial(verify_live, base_url=base_url),
        account=account_from_env,
        form_login=partial(form_login, base_url=base_url),
    )


def form_login(
    env: Mapping[str, str], *, base_url: str, seams: HttpSeams | None = None
) -> Mapping[str, str]:
    """Post the account to ``/api/login`` and return the credential file's values."""
    login = FormLogin(
        service="the Mock SaaS",
        url=f"{base_url}/api/login",
        account=account_from_env,
        credential_key=TOKEN_KEY,
    )
    return login(env, seams=seams)


def verify_live(credential: Credential, *, base_url: str, seams: HttpSeams | None = None) -> None:
    """Prove a credential works by asking for one supplier. HTTP 200 alone is not enough: the
    answer must have the shape of a supplier page (parse_supplier_page)."""
    verify_with_probe(
        api_headers(credential),
        lambda client: MockApi(client, base_url=base_url, page_size=1).list_suppliers(None),
        seams=seams,
    )


def _base_url(options: Mapping[str, object]) -> str:
    value = options.get("base_url", DEFAULT_BASE_URL)
    if not isinstance(value, str) or not urlsplit(value).netloc:
        raise ConfigError(
            "credential kind mock: 'base_url' must be a URL such as " + DEFAULT_BASE_URL
        )
    return value.rstrip("/")
