"""The browser grabber and provider, driven by fakes: no browser is ever launched."""

from __future__ import annotations

import dataclasses
import stat
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from catalog_sync.credentials.base import Credential
from catalog_sync.credentials.browser import (
    INSTALL_HINT,
    BrowserLoginProvider,
    BrowserSite,
    LoginAccount,
    grab_headers,
    launch_playwright,
    require_new_token,
)
from catalog_sync.credentials.store import read_values, write_values
from catalog_sync.errors import ConfigError, LoginFailed
from tests.fakes_browser import FakeBrowser
from tests.fixtures.tokens import bearer, make_jwt
from tests.test_credentials_basics import LAYOUT

SITE = BrowserSite(
    landing_url="https://app.example.invalid/home",
    host_suffix="example.invalid",
    locale="en-US",
    user_selector="#user",
    password_selector="#pass",
    submit_selector="button.go",
    login_url_markers=("/login", "sign-in"),
)
APP = "https://app.example.invalid/home"
LOGIN = "https://app.example.invalid/login?next=%2Fhome"
API = "https://api.example.invalid/v1/list"
ACCOUNT = LoginAccount("demo-user", "demo-password")
OLD = bearer(make_jwt(exp=1_000, jti="old"))
NEW = bearer(make_jwt(exp=2_000_000_000, jti="new"))
OLD_VALID = bearer(make_jwt(exp=1_999_999_999, jti="old-valid"))


def authed(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": token, "X-Demo-Referer": "https://app.example.invalid/p", **extra}


def run(
    browser: FakeBrowser,
    tmp_path: Path,
    *,
    account: LoginAccount | None = ACCOUNT,
    headless: bool = True,
    force: bool = False,
    timeout_s: float = 30,
) -> dict[str, str]:
    return grab_headers(
        SITE,
        profile_dir=tmp_path / "profile",
        account=account,
        headless=headless,
        timeout_s=timeout_s,
        force_login=force,
        launch=browser.launcher(),
        monotonic=browser.clock.monotonic,
    )


# --- pure parts of the site description ----------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "is_login"),
    [
        ("https://app.example.invalid/LOGIN", True),
        ("https://app.example.invalid/auth/Sign-In?x=1", True),
        ("https://app.example.invalid/home", False),
    ],
)
def test_login_pages_are_recognised_by_url_markers_ignoring_case(url: str, is_login: bool) -> None:
    assert SITE.is_login_url(url) is is_login


@pytest.mark.parametrize(
    ("url", "is_site"),
    [
        ("https://api.example.invalid/x", True),
        ("https://example.invalid/x", True),
        ("https://evil-example.invalid/x", False),
        ("https://example.invalid.attacker.test/x", False),
        ("not a url", False),
    ],
)
def test_only_the_services_own_hosts_are_inspected(url: str, is_site: bool) -> None:
    assert SITE.is_site_host(url) is is_site


def test_a_request_carries_a_token_only_with_the_bearer_scheme() -> None:
    assert SITE.carries_token({"authorization": "Bearer abc"})
    assert not SITE.carries_token({"authorization": "Basic abc"})
    assert not SITE.carries_token({"authorization": ""})
    assert not SITE.carries_token({})


def test_a_login_account_never_prints_itself() -> None:
    assert "demo-password" not in repr(ACCOUNT)
    assert "demo-user" not in repr(ACCOUNT)


@pytest.mark.parametrize(
    ("previous_id", "previous_value", "new_id", "new_value", "is_renewed"),
    [
        ("a", "tok-a", "b", "tok-b", True),
        ("a", "tok-a", "a", "tok-a", False),
        ("a", "tok-a", "a", "tok-a2", False),  # same id is the same token, whatever the bytes say
        (None, "tok-a", None, "tok-b", True),  # no ids to compare: fall back to the values
        (None, "tok-a", None, "tok-a", False),
        ("a", "tok-a", None, "tok-b", True),  # one side unreadable: values decide
        (None, None, "b", "tok-b", True),  # nothing stored before
    ],
)
def test_a_forced_login_must_yield_a_different_token(
    previous_id: str | None,
    previous_value: str | None,
    new_id: str | None,
    new_value: str,
    is_renewed: bool,
) -> None:
    kwargs = {
        "previous_id": previous_id,
        "previous_value": previous_value,
        "new_id": new_id,
        "new_value": new_value,
    }
    if is_renewed:
        require_new_token(**kwargs)
    else:
        with pytest.raises(LoginFailed, match="nothing was renewed"):
            require_new_token(**kwargs)


# --- capturing the token -------------------------------------------------------------------------


def test_an_already_logged_in_session_yields_the_first_authenticated_site_request(
    tmp_path: Path,
) -> None:
    browser = FakeBrowser(SITE)
    browser.on_goto = lambda b: (
        b.send("https://cdn.example.test/lib.js", {"authorization": "Bearer other-host"}),
        b.send("https://evil-example.invalid/x", {"authorization": "Bearer lookalike"}),
        b.send(API, {"accept": "*/*"}),
    )[0]
    browser.script = [lambda b: b.send(API, authed(NEW, Cookie="session=canary"))]

    headers = run(browser, tmp_path)

    assert headers["authorization"] == NEW
    assert headers["x-demo-referer"] == "https://app.example.invalid/p"
    assert "cookie" not in headers, "cookies are dropped at the source"
    assert [c[0] for c in browser.calls] == ["goto"], "no login, no clearing"


def test_a_vanished_request_is_skipped_and_the_next_one_is_used(tmp_path: Path) -> None:
    browser = FakeBrowser(SITE)
    browser.script = [
        lambda b: b.send(API, RuntimeError("target closed")),
        lambda b: b.send(API, authed(NEW)),
    ]
    assert run(browser, tmp_path)["authorization"] == NEW


def test_a_login_page_is_filled_in_once_and_an_expired_token_does_not_end_the_wait(
    tmp_path: Path,
) -> None:
    browser = FakeBrowser(SITE)

    def redirect_to_login(b: FakeBrowser) -> None:
        b.send(API, authed(OLD))  # the app's first call went out with the expired token
        b.url = LOGIN

    def after_login(b: FakeBrowser) -> None:
        b.url = APP
        b.send(API, authed(NEW))

    browser.script = [redirect_to_login, lambda b: None, after_login]

    headers = run(browser, tmp_path)

    assert headers["authorization"] == NEW
    assert [c for c in browser.calls if c[0] in ("fill", "click")] == [
        ("fill", "#user", "demo-user"),
        ("fill", "#pass", "demo-password"),
        ("click", "button.go"),
    ]


def test_forcing_a_login_wipes_both_stores_and_ignores_the_first_load(tmp_path: Path) -> None:
    browser = FakeBrowser(SITE)
    loads: list[str] = []

    def on_goto(b: FakeBrowser) -> None:
        loads.append(b.url)
        # Every load of the app re-sends whatever token is in the browser's storage.
        b.send(API, authed(NEW if "evaluate" in {c[0] for c in b.calls} else OLD_VALID))

    browser.on_goto = on_goto

    headers = run(browser, tmp_path, force=True)

    assert headers["authorization"] == NEW, "the old token from the first load must be discarded"
    assert [c[0] for c in browser.calls] == ["goto", "evaluate", "clear_cookies", "goto"]
    storage_call = next(c for c in browser.calls if c[0] == "evaluate")
    assert "localStorage.clear()" in storage_call[1]
    assert "sessionStorage.clear()" in storage_call[1]
    assert loads == [APP, APP]


def test_without_force_the_stored_session_is_left_alone(tmp_path: Path) -> None:
    browser = FakeBrowser(SITE)
    browser.on_goto = lambda b: b.send(API, authed(OLD_VALID))
    run(browser, tmp_path)
    assert [c[0] for c in browser.calls] == ["goto"]


# --- failures say what to do ---------------------------------------------------------------------


def test_a_headless_session_on_the_login_page_without_an_account_says_so(tmp_path: Path) -> None:
    browser = FakeBrowser(SITE)
    browser.script = [lambda b: setattr(b, "url", LOGIN)]
    with pytest.raises(LoginFailed, match=r"no account is available.*--headed"):
        run(browser, tmp_path, account=None, timeout_s=3)
    assert not [c for c in browser.calls if c[0] in ("fill", "click")]


def test_a_submitted_form_that_goes_nowhere_points_at_the_credentials_or_a_captcha(
    tmp_path: Path,
) -> None:
    browser = FakeBrowser(SITE)
    browser.script = [lambda b: setattr(b, "url", LOGIN)]
    with pytest.raises(LoginFailed, match=r"submitted.*still on the login page.*--headed"):
        run(browser, tmp_path, timeout_s=3)


def test_silence_is_reported_as_an_expired_session(tmp_path: Path) -> None:
    browser = FakeBrowser(SITE)
    with pytest.raises(LoginFailed, match=r"no authenticated request was seen within 3s"):
        run(browser, tmp_path, timeout_s=3)


def test_a_person_logging_in_at_a_visible_window_is_only_watched(tmp_path: Path) -> None:
    browser = FakeBrowser(SITE)

    def person_logs_in(b: FakeBrowser) -> None:
        b.url = APP
        b.send(API, authed(NEW))

    browser.script = [lambda b: setattr(b, "url", LOGIN), lambda b: None, person_logs_in]

    headers = run(browser, tmp_path, account=None, headless=False, timeout_s=60)

    assert headers["authorization"] == NEW
    assert not [c for c in browser.calls if c[0] in ("fill", "click")], "never types for a person"
    assert browser.launches == [(tmp_path / "profile", False)]


def test_the_profile_directory_holding_the_session_is_private(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o755)
    browser = FakeBrowser(SITE)
    browser.on_goto = lambda b: b.send(API, authed(NEW))
    run(browser, tmp_path)
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700


def test_a_missing_playwright_installation_gives_an_install_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(ConfigError) as failure, launch_playwright(SITE, tmp_path, True):
        pytest.fail("a browser must not start")
    assert str(failure.value) == INSTALL_HINT


# --- the provider ---------------------------------------------------------------------------------


class FakeGrab:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers
        self.calls: list[dict[str, object]] = []

    def __call__(self, site: BrowserSite, **kwargs: object) -> dict[str, str]:
        self.calls.append({"site": site, **kwargs})
        return dict(self.headers)


def provider(
    tmp_path: Path,
    grab: FakeGrab,
    *,
    verify: list[Credential] | Exception | None = None,
    account: LoginAccount | None = ACCOUNT,
    headless: bool = True,
) -> BrowserLoginProvider:
    def check(credential: Credential) -> None:
        if isinstance(verify, Exception):
            raise verify
        if verify is not None:
            verify.append(credential)

    return BrowserLoginProvider(
        path=tmp_path / "creds" / "demo.cred",
        layout=LAYOUT,
        site=SITE,
        profile_dir=tmp_path / "profile",
        account=account,
        headless=headless,
        timeout_s=45,
        verify=check,
        grab=grab,
    )


def captured(token: str) -> dict[str, str]:
    return {
        "authorization": token,
        "x-demo-referer": "https://app.example.invalid/p",
        "cookie": "session=canary",
        "user-agent": "Generic/1.0",
        "region": "r1",
    }


def test_a_first_login_stores_only_the_layouts_keys(tmp_path: Path) -> None:
    grab = FakeGrab(captured(NEW))
    seen: list[Credential] = []
    subject = provider(tmp_path, grab, verify=seen)

    credential = subject.refresh(force=False)

    stored = read_values(tmp_path / "creds" / "demo.cred")
    assert stored == {
        "AUTHORIZATION": NEW,
        "REFERER": "https://app.example.invalid/p",
        "REGION": "r1",
    }
    assert credential.token_id == "new"
    assert seen == [credential], "the credential that was verified is the one that was stored"
    assert grab.calls[0]["force_login"] is False
    assert grab.calls[0]["account"] == ACCOUNT
    assert grab.calls[0]["timeout_s"] == 45


def test_a_credential_the_service_rejects_is_not_saved(tmp_path: Path) -> None:
    path = tmp_path / "creds" / "demo.cred"
    write_values(path, {"AUTHORIZATION": OLD})
    subject = provider(tmp_path, FakeGrab(captured(NEW)), verify=LoginFailed("rejected"))

    with pytest.raises(LoginFailed, match="rejected"):
        subject.refresh(force=False)

    assert read_values(path) == {"AUTHORIZATION": OLD}


def test_a_forced_login_that_returns_the_same_token_fails_and_keeps_the_stored_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / "creds" / "demo.cred"
    write_values(path, {"AUTHORIZATION": OLD, "REGION": "kept"})
    grab = FakeGrab(captured(OLD))
    subject = provider(tmp_path, grab, verify=[])

    with pytest.raises(LoginFailed, match="nothing was renewed"):
        subject.refresh(force=True)

    assert grab.calls[0]["force_login"] is True
    assert read_values(path)["REGION"] == "kept"
    assert read_values(path)["AUTHORIZATION"] == OLD


def test_a_forced_login_with_a_new_token_replaces_the_old_and_keeps_other_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "creds" / "demo.cred"
    write_values(path, {"AUTHORIZATION": OLD, "REGION": "kept"})
    headers = captured(NEW)
    del headers["region"]  # this capture lacks it; the stored value must survive
    subject = provider(tmp_path, FakeGrab(headers), verify=[])

    credential = subject.refresh(force=True)

    assert credential.token_id == "new"
    assert read_values(path)["REGION"] == "kept"


def test_an_unforced_refresh_may_return_the_stored_token_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "creds" / "demo.cred"
    write_values(path, {"AUTHORIZATION": NEW})
    provider(tmp_path, FakeGrab(captured(NEW)), verify=[]).refresh(force=False)
    assert read_values(path)["AUTHORIZATION"] == NEW


def test_a_visible_window_is_started_without_an_account(tmp_path: Path) -> None:
    grab = FakeGrab(captured(NEW))
    provider(tmp_path, grab, verify=[], account=None, headless=False).refresh(force=False)
    assert grab.calls[0]["account"] is None
    assert grab.calls[0]["headless"] is False


# --- the Playwright launcher, against a stand-in module ----------------------------------


class _PlaywrightStandIn:
    """Enough of ``playwright.sync_api`` to see how the launcher uses it."""

    class Error(Exception):
        pass

    def __init__(self) -> None:
        self.launch_kwargs: dict[str, object] = {}
        self.closed = False
        stand_in = self

        class Context:
            def close(self) -> None:
                stand_in.closed = True

        class Chromium:
            def launch_persistent_context(self, **kwargs: object) -> Context:
                stand_in.launch_kwargs = kwargs
                return Context()

        class Playwright:
            chromium = Chromium()

        @contextmanager
        def sync_playwright() -> Iterator[Playwright]:
            yield Playwright()

        module = types.ModuleType("playwright.sync_api")
        module.Error = self.Error  # type: ignore[attr-defined]
        module.sync_playwright = sync_playwright  # type: ignore[attr-defined]
        package = types.ModuleType("playwright")
        package.sync_api = module  # type: ignore[attr-defined]
        self.modules = {"playwright": package, "playwright.sync_api": module}


@pytest.fixture
def playwright_stand_in(monkeypatch: pytest.MonkeyPatch) -> _PlaywrightStandIn:
    stand_in = _PlaywrightStandIn()
    for name, module in stand_in.modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return stand_in


def test_the_browser_is_started_plainly_with_a_persistent_profile(
    playwright_stand_in: _PlaywrightStandIn, tmp_path: Path
) -> None:
    with launch_playwright(SITE, tmp_path / "profile", False):
        pass

    kwargs = playwright_stand_in.launch_kwargs
    assert kwargs == {
        "user_data_dir": str(tmp_path / "profile"),
        "headless": False,
        "viewport": {"width": 1440, "height": 900},
        "locale": "en-US",
    }
    assert "AutomationControlled" not in repr(kwargs), "no anti-detection flags are passed"
    assert "args" not in kwargs
    assert playwright_stand_in.closed


@pytest.mark.parametrize("headless", [True, False])
def test_the_browser_announces_itself_as_it_is_and_no_user_agent_is_applied(
    playwright_stand_in: _PlaywrightStandIn, tmp_path: Path, headless: bool
) -> None:
    """Overriding the user agent of a headless browser hides HeadlessChrome from the service:
    that is detection evasion. If the service refuses a headless login, the answer is a visible
    window (``login --browser --headed``), not a disguise."""
    with launch_playwright(SITE, tmp_path / "profile", headless):
        pass
    assert "user_agent" not in playwright_stand_in.launch_kwargs
    assert "user-agent" not in repr(playwright_stand_in.launch_kwargs).lower()


def test_a_browser_site_has_no_user_agent_field_to_apply() -> None:
    """The field was kept after the override it fed was removed; a field nothing reads invites
    someone to wire it back in."""
    assert "user_agent" not in {field.name for field in dataclasses.fields(BrowserSite)}


def test_the_browser_is_closed_even_when_the_login_step_fails(
    playwright_stand_in: _PlaywrightStandIn, tmp_path: Path
) -> None:
    with pytest.raises(KeyError), launch_playwright(SITE, tmp_path, True):
        raise KeyError("not a browser problem")
    assert playwright_stand_in.closed


def test_a_browser_error_becomes_a_login_failure_with_its_first_line_only(
    playwright_stand_in: _PlaywrightStandIn, tmp_path: Path
) -> None:
    message = "Timeout 60000ms exceeded.\nCall log:\n  navigating to https://x.invalid/?t=SECRET"
    with pytest.raises(LoginFailed) as failure, launch_playwright(SITE, tmp_path, True):
        raise playwright_stand_in.Error(message)
    assert "the browser failed (Error): Timeout 60000ms exceeded." in str(failure.value)
    assert "SECRET" not in str(failure.value)
    assert playwright_stand_in.closed
