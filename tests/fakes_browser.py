"""A scripted stand-in for Playwright's page and context: no browser is ever started.

Time advances only when the code under test waits (``wait_for_timeout``); each wait runs the next
step of the script, which can navigate the page or make the "site" send a request.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from catalog_sync.credentials.browser import (
    BrowserContext,
    BrowserPage,
    BrowserRequest,
    BrowserSite,
    Launcher,
)
from catalog_sync.testing.fake_transport import FakeClock

Step = Callable[["FakeBrowser"], None]


class FakeRequest:
    def __init__(self, url: str, headers: dict[str, str] | Exception) -> None:
        self.url = url
        self._headers = headers

    def all_headers(self) -> dict[str, str]:
        if isinstance(self._headers, Exception):
            raise self._headers
        return dict(self._headers)


class FakePage:
    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser

    @property
    def url(self) -> str:
        return self._browser.url

    def goto(self, url: str, *, wait_until: str, timeout: float) -> object:
        self._browser.calls.append(("goto", url))
        self._browser.url = url
        self._browser.on_goto(self._browser)
        return None

    def fill(self, selector: str, value: str) -> None:
        self._browser.calls.append(("fill", selector, value))

    def click(self, selector: str) -> None:
        self._browser.calls.append(("click", selector))
        self._browser.on_click(self._browser)

    def evaluate(self, expression: str) -> object:
        self._browser.calls.append(("evaluate", expression))
        return None

    def wait_for_timeout(self, timeout: float) -> None:
        browser = self._browser
        browser.clock.sleep(timeout / 1000)
        if browser.script:
            browser.script.pop(0)(browser)


class FakeContext:
    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser
        self.pages: list[BrowserPage] = [FakePage(browser)]

    def new_page(self) -> BrowserPage:
        return FakePage(self._browser)

    def on(self, event: str, handler: Callable[[BrowserRequest], None]) -> None:
        assert event == "request"
        self._browser.handlers.append(handler)

    def clear_cookies(self) -> None:
        self._browser.calls.append(("clear_cookies",))


class FakeBrowser:
    def __init__(self, site: BrowserSite, *, script: list[Step] | None = None) -> None:
        self.site = site
        self.url = "about:blank"
        self.clock = FakeClock()
        self.script: list[Step] = script or []
        self.calls: list[tuple[str, ...]] = []
        self.handlers: list[Callable[[BrowserRequest], None]] = []
        self.launches: list[tuple[Path, bool]] = []
        self.on_goto: Step = lambda _browser: None
        self.on_click: Step = lambda _browser: None

    def send(self, url: str, headers: dict[str, str] | Exception) -> None:
        for handler in list(self.handlers):
            handler(FakeRequest(url, headers))

    def launcher(self) -> Launcher:
        @contextmanager
        def launch(
            _site: BrowserSite, profile_dir: Path, headless: bool
        ) -> Iterator[BrowserContext]:
            self.launches.append((profile_dir, headless))
            yield FakeContext(self)

        return launch
