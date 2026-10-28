"""Log in through a real browser and capture the bearer token the site's own page sends.

This is for services whose tokens cannot be obtained any other way. The capture is *passive*: the
browser loads the service's web app exactly as a person would, and the first request that carries
a token has its headers read. Nothing about the browser is disguised (no anti-detection flags):
if the service refuses automated logins, the supported route is a visible window in which a
person logs in once (``headless=False``), after which only the token is taken.

Playwright is an optional dependency (the ``browser`` extra) and is imported only when a browser
is actually launched. The profile directory holds the live login session, which is equivalent to
a credential: it is created owner-only and belongs in the workspace, never in a repository.

Everything site-specific lives in ``BrowserSite``; the services' plugins provide one.
"""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from catalog_sync.credentials.base import Credential, CredentialLayout
from catalog_sync.credentials.jwt import decode_claims
from catalog_sync.credentials.store import (
    credential_from_values,
    load_credential,
    read_values,
    write_values,
)
from catalog_sync.errors import ConfigError, CredentialMissing, LoginFailed
from catalog_sync.fsutil import ensure_private_dir

logger = logging.getLogger(__name__)

_GOTO_TIMEOUT_MS = 60_000
_POLL_MS = 500
_VIEWPORT = {"width": 1440, "height": 900}
INSTALL_HINT = (
    "Browser login needs the optional 'browser' extra: "
    "pip install 'catalog-sync[browser]' && python -m playwright install chromium"
)


@dataclass(frozen=True)
class BrowserSite:
    """What is specific to one service's web login."""

    landing_url: str  # a page that fires authenticated API calls as soon as it loads
    host_suffix: str  # only requests to this host (or a subdomain) are inspected
    locale: str
    user_selector: str
    password_selector: str
    submit_selector: str
    login_url_markers: tuple[str, ...]  # lower-case fragments that identify the login page URL
    token_header: str = "authorization"  # noqa: S105 — a header name, not a secret
    token_prefix: str = "Bearer "  # noqa: S105 — the scheme word, not a secret

    def is_login_url(self, url: str) -> bool:
        lowered = url.lower()
        return any(marker in lowered for marker in self.login_url_markers)

    def is_site_host(self, url: str) -> bool:
        host = urlsplit(url).hostname or ""
        return host == self.host_suffix or host.endswith("." + self.host_suffix)

    def carries_token(self, headers: Mapping[str, str]) -> bool:
        return headers.get(self.token_header, "").startswith(self.token_prefix)


@dataclass(frozen=True, repr=False)
class LoginAccount:
    user: str
    password: str

    def __repr__(self) -> str:
        return "LoginAccount(<hidden>)"


# The slice of Playwright's API this module uses. Playwright's own objects satisfy these
# structurally; tests substitute fakes, so no browser is ever started in the test suite.


class BrowserRequest(Protocol):
    @property
    def url(self) -> str: ...

    def all_headers(self) -> dict[str, str]: ...


class BrowserPage(Protocol):
    @property
    def url(self) -> str: ...

    def goto(self, url: str, *, wait_until: str, timeout: float) -> object: ...

    def fill(self, selector: str, value: str) -> None: ...

    def click(self, selector: str) -> None: ...

    def evaluate(self, expression: str) -> object: ...

    def wait_for_timeout(self, timeout: float) -> None: ...


class BrowserContext(Protocol):
    @property
    def pages(self) -> list[BrowserPage]: ...

    def new_page(self) -> BrowserPage: ...

    def on(self, event: str, handler: Callable[[BrowserRequest], None]) -> None: ...

    def clear_cookies(self) -> None: ...


Launcher = Callable[[BrowserSite, Path, bool], AbstractContextManager[BrowserContext]]
Grabber = Callable[..., dict[str, str]]


@contextmanager
def launch_playwright(
    site: BrowserSite, profile_dir: Path, headless: bool
) -> Iterator[BrowserContext]:
    """Open Chromium with a persistent profile; always close it.

    The browser is started as it comes, user agent included: a headless Chromium says so, and the
    service is entitled to see that. Overriding it would be hiding the automation, which this
    project does not do; a service that refuses headless logins is served by ``--headed``.
    """
    try:
        module = importlib.import_module("playwright.sync_api")
    except ModuleNotFoundError as exc:
        if exc.name is not None and not exc.name.startswith("playwright"):
            raise
        raise ConfigError(INSTALL_HINT) from exc
    try:
        with module.sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=headless,
                viewport=_VIEWPORT,
                locale=site.locale,
            )
            try:
                yield context
            finally:
                context.close()
    except module.Error as exc:
        # Translation at the library boundary; the first line names the failure without the
        # call log, which can contain URLs.
        first_line = (str(exc).splitlines() or [""])[0][:200]
        raise LoginFailed(f"the browser failed ({type(exc).__name__}): {first_line}") from exc


def grab_headers(
    site: BrowserSite,
    *,
    profile_dir: Path,
    account: LoginAccount | None,
    headless: bool,
    timeout_s: float,
    force_login: bool,
    launch: Launcher = launch_playwright,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, str]:
    """Headers (lower-case names, no cookies) of the first authenticated request the site makes.

    With `force_login` the stored login state is wiped first and a real login is performed. An
    existing token that has not expired is re-sent by the page unchanged, so without this a
    "renewal" would just hand back the old token.

    With `account` set, a login page is filled in automatically; without it (a person is at the
    window) the page is only watched.
    """
    ensure_private_dir(profile_dir)
    captured: dict[str, str] = {}
    with launch(site, profile_dir, headless) as context:
        context.on("request", lambda request: _capture(site, captured, request, now()))
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(site.landing_url, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
        if force_login:
            # Wipe both stores a front end may keep its token in; clearing only one lets the page
            # read it back from the other, and the "forced" login silently does nothing.
            page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
            context.clear_cookies()
            captured.clear()  # whatever the first load already sent carried the old token
            page.goto(site.landing_url, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
        progress = _wait_for_token(page, site, captured, account, timeout_s, monotonic)
    if not captured:
        raise LoginFailed(_explain_failure(site, progress, timeout_s, account is not None))
    return captured


@dataclass
class _Progress:
    final_url: str = ""
    saw_login_page: bool = False
    submitted_form: bool = False


def _capture(
    site: BrowserSite, captured: dict[str, str], request: BrowserRequest, now: datetime
) -> None:
    if captured or not site.is_site_host(request.url):
        return
    try:
        headers = {name.lower(): value for name, value in request.all_headers().items()}
    except Exception as exc:  # noqa: BLE001 — side channel: one request that vanished mid-navigation
        # must not end the capture; the next authenticated request carries the same token.
        logger.debug("skipped a request whose headers were unavailable (%s)", type(exc).__name__)
        return
    if not site.carries_token(headers):
        return
    expiry = decode_claims(headers[site.token_header]).expires_at
    if expiry is not None and expiry <= now:
        # An app holding an expired token still sends it, right before it redirects to the login
        # page. That request must not end the wait with a token the service will refuse.
        return
    headers.pop("cookie", None)  # never needed, so never kept
    captured.update(headers)


def _wait_for_token(
    page: BrowserPage,
    site: BrowserSite,
    captured: dict[str, str],
    account: LoginAccount | None,
    timeout_s: float,
    monotonic: Callable[[], float],
) -> _Progress:
    """Poll until a token is captured. The login page is detected inside the loop: the app
    redirects to it from the client side *after* the first load, so one check right after
    ``goto`` usually still sees the old URL."""
    progress = _Progress()
    deadline = monotonic() + timeout_s
    while monotonic() < deadline and not captured:
        if not progress.saw_login_page and site.is_login_url(page.url):
            progress.saw_login_page = True
            if account is not None:
                page.fill(site.user_selector, account.user)
                page.fill(site.password_selector, account.password)
                page.click(site.submit_selector)
                progress.submitted_form = True
        page.wait_for_timeout(_POLL_MS)
    progress.final_url = page.url
    return progress


def _explain_failure(
    site: BrowserSite, progress: _Progress, timeout_s: float, has_account: bool
) -> str:
    on_login_page = site.is_login_url(progress.final_url)
    if progress.submitted_form and on_login_page:
        return (
            f"the login form was submitted but the browser was still on the login page after "
            f"{timeout_s:.0f}s: the user name or password is wrong, or the page now asks for "
            "more (a captcha, a code). Log in by hand with --headed."
        )
    if progress.saw_login_page and not has_account:
        return (
            "the session is not logged in and no account is available to log in with. Set the "
            "service's user and password environment variables, or log in by hand with --headed."
        )
    where = "the login page" if on_login_page else "another page"
    return (
        f"no authenticated request was seen within {timeout_s:.0f}s (the browser ended on "
        f"{where}). The saved session has probably expired: log in again with --force."
    )


def require_new_token(
    *, previous_id: str | None, previous_value: str | None, new_id: str | None, new_value: str
) -> None:
    """After a forced login the token must differ from the one before, or nothing was renewed.

    Judged by token id when both are known (the same id is the same token), otherwise by value.
    A check that passes when nothing changed would let a no-op login report success and the
    credential expire anyway on its original date.
    """
    if previous_id is not None and new_id is not None:
        is_same = previous_id == new_id
    else:
        is_same = previous_value is not None and previous_value == new_value
    if is_same:
        raise LoginFailed(
            "the forced login returned the token that was already stored, so nothing was renewed. "
            "The login state was probably not cleared or the login form was not accepted; "
            "try again with --headed."
        )


class BrowserLoginProvider:
    """Obtains credentials by logging in through a browser."""

    def __init__(
        self,
        *,
        path: Path,
        layout: CredentialLayout,
        site: BrowserSite,
        profile_dir: Path,
        account: LoginAccount | None,
        headless: bool,
        timeout_s: float,
        verify: Callable[[Credential], None],
        grab: Grabber = grab_headers,
    ) -> None:
        self._path = path
        self._layout = layout
        self._site = site
        self._profile_dir = profile_dir
        self._account = account
        self._headless = headless
        self._timeout_s = timeout_s
        self._verify = verify
        self._grab = grab

    def load(self) -> Credential:
        return load_credential(self._path, self._layout)

    def refresh(self, *, force: bool) -> Credential:
        stored = self._stored_values()
        headers = self._grab(
            self._site,
            profile_dir=self._profile_dir,
            account=self._account,
            headless=self._headless,
            timeout_s=self._timeout_s,
            force_login=force,
        )
        updates = {
            key: headers[name] for key, name in self._layout.headers.items() if headers.get(name)
        }
        merged = {**stored, **updates}
        candidate = credential_from_values(merged, self._layout, name=self._path.stem)
        if force:
            token_key = self._layout.token_key
            require_new_token(
                previous_id=decode_claims(stored.get(token_key, "")).token_id,
                previous_value=stored.get(token_key),
                new_id=candidate.token_id,
                new_value=merged[token_key],
            )
        # Verified before it is saved: a token the service rejects must not replace a stored one.
        self._verify(candidate)
        write_values(self._path, updates)
        return self.load()

    def _stored_values(self) -> dict[str, str]:
        try:
            return read_values(self._path)
        except CredentialMissing:
            return {}  # first login: there is nothing stored to compare with
