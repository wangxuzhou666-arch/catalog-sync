"""`login` and `auth status` through the real CLI, with fakes where a browser would be."""

from __future__ import annotations

import io
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from catalog_sync import cli, cli_credentials, registry
from catalog_sync.audit import AuditLog, hash_token_id
from catalog_sync.credentials.base import Credential, CredentialLayout
from catalog_sync.credentials.kind import CredentialKind
from catalog_sync.credentials.store import credential_path, read_values, write_values
from catalog_sync.errors import LoginFailed
from catalog_sync.http import HttpClient, HttpSeams, HttpSettings, RateLimiter
from catalog_sync.plugins.mock.login import mock_credential_kind
from catalog_sync.testing.fake_transport import FakeResponse, FakeTransport
from tests.fixtures.tokens import bearer, make_jwt

COMPLIANCE_EXIT = 4

COOKIE_SECRET = "canary-cookie-" + "5c4b3a"
NOW = int(time.time())
DAY = 86_400
FRESH = bearer(make_jwt(exp=NOW + 6 * DAY, jti="fresh-token-id-0001"))
SOON = bearer(make_jwt(exp=NOW + DAY, jti="soon-token-id-0002"))
EXPIRED = bearer(make_jwt(exp=NOW - 3 * DAY, jti="old-token-id-0003"))
NEW = bearer(make_jwt(exp=NOW + 7 * DAY, jti="new-token-id-0004"))
NO_EXPIRY = bearer(make_jwt(exp=None, jti="no-expiry-id-0005"))


def curl_text(token: str, *, host: str = "127.0.0.1:8765") -> str:
    return (
        f"curl 'http://{host}/api/v1/suppliers?page=1' \\\n"
        f"  -H 'authorization: {token}' \\\n"
        f"  -H 'x-demo-context: web-context-demo' \\\n"
        f"  -H 'cookie: session={COOKIE_SECRET}' \\\n"
        f"  --data-raw '{{\"page\":1}}' --compressed"
    )


SOURCE_TEMPLATE = """
[sources.catalog]
plugin = "mock.materials"
credential = "{credential}"
{compliance}
"""
COMPLIANCE_TEMPLATE = """
[sources.catalog.compliance]
data_owner = "Example Co"
authorization = "own_account"
authorization_ref = "wiki/data-access/0000"
tos_url = "https://vendor.example/terms"
tos_reviewed_on = {reviewed}
max_requests_per_minute = {ceiling}
retention_days = 90
"""


class Workspace:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "ws"
        self.path = credential_path(self.home / "credentials", "mock")
        self.config: Path | None = None
        self._config_path = tmp_path / "catalog-sync.toml"

    def store(self, token: str) -> None:
        write_values(self.path, {"AUTHORIZATION": token})

    def declare_source(
        self,
        *,
        credential: str = "mock",
        ceiling: int | None = 120,
        reviewed: date | None = None,
    ) -> None:
        """A config with a networked source that uses `credential` and, unless `ceiling` is None,
        a valid compliance declaration: what a login that contacts the service is held to."""
        compliance = (
            ""
            if ceiling is None
            else COMPLIANCE_TEMPLATE.format(
                reviewed=(reviewed or date.today()).isoformat(), ceiling=ceiling
            )
        )
        self._config_path.write_text(
            SOURCE_TEMPLATE.format(credential=credential, compliance=compliance), encoding="utf-8"
        )
        self.config = self._config_path

    def run(self, *args: str) -> int:
        config = ["--config", str(self.config)] if self.config else []
        return cli.main(["--workspace", str(self.home), *config, *args])


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def paste_file(tmp_path: Path, text: str) -> str:
    path = tmp_path / "pasted.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def mock_kind(base_dir: Path) -> CredentialKind:
    return mock_credential_kind({}, base_dir=base_dir)


def use_kind(monkeypatch: pytest.MonkeyPatch, kind: CredentialKind) -> None:
    """Make every credential kind lookup answer with `kind`: how a test gives the CLI a kind with
    a different layout, or without a login method, than any installed plugin has."""
    monkeypatch.setattr(
        registry, "credential_kind_factory", lambda name: lambda options, *, base_dir: kind
    )


# --- paste ----------------------------------------------------------------------------------------


def test_pasting_a_curl_command_stores_only_the_credential_and_reports_it(
    ws: Workspace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ws.run("login", "mock", "--paste-curl", paste_file(tmp_path, curl_text(NEW))) == 0

    out = capsys.readouterr()
    assert "credential 'mock' stored (pasted cURL command)" in out.out
    assert "7.0 day(s) left" in out.out
    assert "keys: AUTHORIZATION" in out.out
    assert "not captured" not in out.out, "the Mock layout is one header: nothing is left out"
    assert read_values(ws.path)["AUTHORIZATION"] == NEW
    assert stat.S_IMODE(ws.path.stat().st_mode) == 0o600
    for text in (out.out, out.err, ws.path.read_text()):
        assert COOKIE_SECRET not in text
    for text in (out.out, out.err):
        assert NEW not in text
        assert "new-token-id-0004" not in text, "the token id is shown redacted only"


def test_the_report_names_the_headers_it_captured_and_the_ones_it_did_not(
    ws: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    layout = CredentialLayout(
        headers={
            "AUTHORIZATION": "authorization",
            "CONTEXT": "x-demo-context",
            "REFERER": "x-demo-referer",
            "REGION": "x-demo-region",
        },
        token_key="AUTHORIZATION",
        required=frozenset({"AUTHORIZATION"}),
        host_suffixes=("127.0.0.1",),
    )
    use_kind(monkeypatch, replace(mock_kind(tmp_path), layout=layout))

    assert ws.run("login", "mock", "--paste-curl", paste_file(tmp_path, curl_text(NEW))) == 0

    out = capsys.readouterr().out
    assert "keys: AUTHORIZATION, CONTEXT" in out
    assert "not captured: REFERER, REGION" in out


def test_a_login_is_audited_by_credential_name_and_token_fingerprint_only(
    ws: Workspace, tmp_path: Path
) -> None:
    assert ws.run("login", "mock", "--paste-curl", paste_file(tmp_path, curl_text(NEW))) == 0
    other = paste_file(tmp_path, curl_text(NEW, host="other.example.invalid"))
    assert ws.run("login", "mock", "--paste-curl", other, "--force") == 2

    ok, refused = AuditLog(ws.home / "audit.jsonl").read()
    assert (ok["command"], ok["source"], ok["outcome"]) == ("login", "mock", "ok")
    assert ok["credential_hash"] == hash_token_id("new-token-id-0004")
    assert refused["outcome"] == "error:LoginFailed"
    logged = (ws.home / "audit.jsonl").read_text(encoding="utf-8")
    for secret in (NEW, "new-token-id-0004", COOKIE_SECRET, "web-context-demo"):
        assert secret not in logged


def test_the_command_can_be_piped_in_on_stdin(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(curl_text(NEW)))
    assert ws.run("login", "mock", "--paste-curl", "-") == 0
    assert read_values(ws.path)["AUTHORIZATION"] == NEW


def test_a_credential_that_is_still_fresh_is_left_alone_unless_forced(
    ws: Workspace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws.store(FRESH)
    pasted = paste_file(tmp_path, curl_text(NEW))

    assert ws.run("login", "mock", "--paste-curl", pasted) == 0
    assert "still valid" in capsys.readouterr().out
    assert read_values(ws.path)["AUTHORIZATION"] == FRESH

    assert ws.run("login", "mock", "--paste-curl", pasted, "--force") == 0
    assert read_values(ws.path)["AUTHORIZATION"] == NEW


@pytest.mark.parametrize("old", [SOON, EXPIRED, NO_EXPIRY])
def test_a_credential_that_needs_renewal_is_replaced_without_asking(
    ws: Workspace, tmp_path: Path, old: str
) -> None:
    ws.store(old)
    assert ws.run("login", "mock", "--paste-curl", paste_file(tmp_path, curl_text(NEW))) == 0
    assert read_values(ws.path)["AUTHORIZATION"] == NEW


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        ([], "choose how to log in"),
        (["--paste-curl", "x", "--browser"], "not both"),
        (["--form", "--browser"], "not both --form and --browser"),
        (["--form", "--paste-curl", "x"], "not both --paste-curl and --form"),
        (["--form", "--headed"], "only applies to --browser"),
        (["--headed"], "choose how to log in"),
        (["--paste-curl", "x", "--headed"], "only applies to --browser"),
    ],
)
def test_login_needs_exactly_one_way_to_log_in(
    ws: Workspace, capsys: pytest.CaptureFixture[str], flags: list[str], message: str
) -> None:
    assert ws.run("login", "mock", *flags) == 1
    assert message in capsys.readouterr().err


def test_a_command_for_another_site_is_refused_with_exit_two_and_nothing_is_stored(
    ws: Workspace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pasted = paste_file(tmp_path, curl_text(NEW, host="other.example.invalid"))
    assert ws.run("login", "mock", "--paste-curl", pasted) == 2
    assert "not for 127.0.0.1" in capsys.readouterr().err
    assert not ws.path.exists()


def test_an_unreadable_paste_file_is_a_clear_error(
    ws: Workspace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ws.run("login", "mock", "--paste-curl", str(tmp_path / "absent.txt")) == 1
    assert "cannot read" in capsys.readouterr().err


def test_an_unknown_credential_name_lists_what_is_installed(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ws.run("auth", "status", "mystery") == 1
    err = capsys.readouterr().err
    assert "unknown credential 'mystery'" in err
    assert "installed kinds: mock" in err


def test_a_kind_without_an_http_login_says_so_for_form(
    ws: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    use_kind(monkeypatch, replace(mock_kind(tmp_path), form_login=None))
    assert ws.run("login", "mock", "--form") == 1
    assert "credential kind 'mock' has no HTTP login; use --paste-curl" in capsys.readouterr().err


# --- auth status ----------------------------------------------------------------------------------


def test_status_of_a_fresh_credential(ws: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
    ws.store(FRESH)
    assert ws.run("auth", "status", "mock") == 0
    out = capsys.readouterr().out
    assert "state:      fresh" in out
    assert "expires" in out
    assert "(6.0 day(s) left)" in out or "(5.9 day(s) left)" in out
    assert "renew when: fewer than 2 day(s) are left" in out


def test_status_shows_the_token_id_redacted_and_never_the_token(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    ws.store(FRESH)
    ws.run("auth", "status", "mock")
    shown = capsys.readouterr()
    assert "token id:   fresh-...(19 chars)" in shown.out
    assert "fresh-token-id-0001" not in shown.out + shown.err
    assert FRESH not in shown.out + shown.err


def test_status_of_a_credential_about_to_lapse_says_so_but_exits_zero(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    ws.store(SOON)
    assert ws.run("auth", "status", "mock") == 0
    assert "state:      renew soon" in capsys.readouterr().out


def test_status_of_an_expired_credential_exits_two_with_the_renewal_command(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    ws.store(EXPIRED)
    assert ws.run("auth", "status", "mock") == 2
    shown = capsys.readouterr()
    assert "state:      expired" in shown.out
    assert "expired" in shown.out
    assert "catalog-sync login mock --paste-curl -" in shown.err


def test_status_of_a_token_without_a_readable_expiry_does_not_claim_it_is_fine(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    ws.store(NO_EXPIRY)
    assert ws.run("auth", "status", "mock") == 0
    out = capsys.readouterr().out
    assert "state:      unknown" in out
    assert "expiry unknown" in out


def test_status_without_a_stored_credential_exits_two_with_the_way_to_make_one(
    ws: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ws.run("auth", "status", "mock") == 2
    assert "no credential stored for 'mock'" in capsys.readouterr().err


def test_status_warns_when_the_renewal_window_is_shorter_than_the_schedule(
    ws: Workspace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws.store(FRESH)
    config = tmp_path / "catalog-sync.toml"
    config.write_text(
        "[credentials.mock]\nrenew_before_days = 2\nschedule_interval_days = 7\n",
        encoding="utf-8",
    )
    assert ws.run("--config", str(config), "auth", "status", "mock") == 0
    assert "shorter than the schedule interval" in capsys.readouterr().err


# --- browser login (the browser itself is replaced) -----------------------------------------------


class FakeBrowserProvider:
    """Stands in for BrowserLoginProvider: records how it was built and what it was asked."""

    instances: list[FakeBrowserProvider] = []  # noqa: RUF012 — reset by the fixture below
    outcomes: list[Credential | Exception] = []  # noqa: RUF012

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.forced: list[bool] = []
        FakeBrowserProvider.instances.append(self)

    def refresh(self, *, force: bool) -> Credential:
        self.forced.append(force)
        outcome = FakeBrowserProvider.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def renewed_credential() -> Credential:
    return Credential(
        {"authorization": NEW},
        {},
        None,
        "new-token-id-0004",
    )


@pytest.fixture
def fake_browser(monkeypatch: pytest.MonkeyPatch, ws: Workspace) -> type[FakeBrowserProvider]:
    ws.declare_source()  # a login through a browser contacts the service: it is gated like a pull
    FakeBrowserProvider.instances = []
    FakeBrowserProvider.outcomes = [renewed_credential()]
    monkeypatch.setattr(cli_credentials, "BrowserLoginProvider", FakeBrowserProvider)
    monkeypatch.setenv("MOCK_SAAS_USER", "demo-user")
    monkeypatch.setenv("MOCK_SAAS_PASSWORD", "demo-password")
    monkeypatch.setattr(cli_credentials, "_is_interactive", lambda: False)
    return FakeBrowserProvider


def test_a_headless_browser_login_uses_the_account_from_the_environment(
    ws: Workspace, fake_browser: type[FakeBrowserProvider], capsys: pytest.CaptureFixture[str]
) -> None:
    assert ws.run("login", "mock", "--browser") == 0

    (provider,) = fake_browser.instances
    assert provider.kwargs["headless"] is True
    assert repr(provider.kwargs["account"]) == "LoginAccount(<hidden>)"
    assert provider.kwargs["timeout_s"] == 90
    assert provider.kwargs["profile_dir"] == ws.home / "browser" / "mock"
    assert provider.kwargs["path"] == ws.path
    assert provider.forced == [False], "nothing stored: just take what the session has"
    out = capsys.readouterr().out
    assert "stored (browser login)" in out
    assert "demo-password" not in out


def test_a_visible_window_never_gets_the_account(
    ws: Workspace, fake_browser: type[FakeBrowserProvider]
) -> None:
    assert ws.run("login", "mock", "--browser", "--headed") == 0
    (provider,) = fake_browser.instances
    assert provider.kwargs["headless"] is False
    assert provider.kwargs["account"] is None
    assert provider.kwargs["timeout_s"] == 300


@pytest.mark.parametrize(
    ("stored", "flags", "forced"),
    [
        (None, [], False),
        (FRESH, ["--force"], True),
        (SOON, [], True),
        (EXPIRED, [], True),
        (NO_EXPIRY, [], False),
    ],
)
def test_the_stored_state_decides_whether_the_login_state_is_wiped(
    ws: Workspace,
    fake_browser: type[FakeBrowserProvider],
    stored: str | None,
    flags: list[str],
    forced: bool,
) -> None:
    if stored is not None:
        ws.store(stored)
    assert ws.run("login", "mock", "--browser", *flags) == 0
    assert fake_browser.instances[0].forced == [forced]


def test_a_fresh_credential_does_not_start_a_browser(
    ws: Workspace, fake_browser: type[FakeBrowserProvider]
) -> None:
    ws.store(FRESH)
    assert ws.run("login", "mock", "--browser") == 0
    assert fake_browser.instances == []


def test_a_failed_headless_login_in_a_terminal_falls_back_to_a_visible_window(
    ws: Workspace,
    fake_browser: type[FakeBrowserProvider],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli_credentials, "_is_interactive", lambda: True)
    fake_browser.outcomes = [LoginFailed("still on the login page"), renewed_credential()]

    assert ws.run("login", "mock", "--browser") == 0

    headless, headed = fake_browser.instances
    assert (headless.kwargs["headless"], headed.kwargs["headless"]) == (True, False)
    assert headed.kwargs["account"] is None
    assert "headless login failed: still on the login page" in capsys.readouterr().err


def test_without_a_terminal_a_failed_login_is_an_error_not_a_window_nobody_sees(
    ws: Workspace, fake_browser: type[FakeBrowserProvider], capsys: pytest.CaptureFixture[str]
) -> None:
    fake_browser.outcomes = [LoginFailed("still on the login page")]
    assert ws.run("login", "mock", "--browser") == 2
    assert "still on the login page" in capsys.readouterr().err
    assert len(fake_browser.instances) == 1


def test_a_failed_visible_login_is_reported_without_another_attempt(
    ws: Workspace, fake_browser: type[FakeBrowserProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_credentials, "_is_interactive", lambda: True)
    fake_browser.outcomes = [LoginFailed("closed the window")]
    assert ws.run("login", "mock", "--browser", "--headed") == 2
    assert len(fake_browser.instances) == 1


# --- a login that contacts the service is gated like a pull ---------------------------------------


def undeclared_workspaces(ws: Workspace) -> dict[str, Callable[[], None]]:
    return {
        "no config at all": lambda: setattr(ws, "config", None),
        "a source that uses another credential": lambda: ws.declare_source(credential="other"),
        "a source without a compliance block": lambda: ws.declare_source(ceiling=None),
    }


@pytest.mark.parametrize(
    "situation",
    [
        "no config at all",
        "a source that uses another credential",
        "a source without a compliance block",
    ],
)
def test_a_browser_login_without_a_declaration_it_can_be_held_to_is_refused_before_anything_starts(
    ws: Workspace,
    fake_browser: type[FakeBrowserProvider],
    capsys: pytest.CaptureFixture[str],
    situation: str,
) -> None:
    undeclared_workspaces(ws)[situation]()
    assert ws.run("login", "mock", "--browser") == COMPLIANCE_EXIT
    assert fake_browser.instances == [], "no browser, no request"
    assert not ws.path.exists()
    err = capsys.readouterr().err
    assert "compliance" in err
    assert "demo-password" not in err


def test_the_refusal_for_a_credential_nobody_uses_says_what_to_add_and_what_needs_nothing(
    ws: Workspace, fake_browser: type[FakeBrowserProvider], capsys: pytest.CaptureFixture[str]
) -> None:
    ws.config = None
    ws.run("login", "mock", "--browser")
    err = capsys.readouterr().err
    assert "no [sources.*] entry uses credential 'mock'" in err
    assert "login --paste-curl" in err


def test_pasting_a_curl_command_makes_no_request_so_it_needs_no_declaration(
    ws: Workspace, tmp_path: Path
) -> None:
    assert ws.config is None
    assert ws.run("login", "mock", "--paste-curl", paste_file(tmp_path, curl_text(NEW))) == 0


def test_a_kind_that_cannot_log_in_that_way_is_reported_before_the_declaration_is_asked_for(
    ws: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    use_kind(monkeypatch, replace(mock_kind(tmp_path), form_login=None))
    assert ws.run("login", "mock", "--form") == 1
    assert "has no HTTP login" in capsys.readouterr().err


def test_a_declared_login_is_audited_with_the_declaration_it_ran_under(
    ws: Workspace, fake_browser: type[FakeBrowserProvider]
) -> None:
    assert ws.run("login", "mock", "--browser") == 0
    (line,) = [e for e in AuditLog(ws.home / "audit.jsonl").read() if e["command"] == "login"]
    assert isinstance(line["compliance_sha256"], str) and len(line["compliance_sha256"]) == 64


class RecordingLimiter(RateLimiter):
    created: list[RecordingLimiter] = []  # noqa: RUF012 -- reset by the test using it

    def __init__(self, limit_per_minute: int) -> None:
        super().__init__(limit_per_minute)
        self.acquired = 0
        RecordingLimiter.created.append(self)

    def acquire(self) -> None:
        self.acquired += 1


def test_the_requests_of_a_login_count_against_the_lowest_ceiling_of_the_sources_using_it(
    ws: Workspace, fake_browser: type[FakeBrowserProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    class ProbingBrowser(FakeBrowserProvider):
        """Makes the one request a connector's own check would make, with a client it builds."""

        def refresh(self, *, force: bool) -> Credential:
            transport = FakeTransport()
            transport.queue(FakeResponse.json({"state": True}))
            client = HttpClient(
                {}, credentialed=True, seams=HttpSeams(adapter=transport), settings=HttpSettings()
            )
            client.get_json("https://api.example.invalid/check")
            return super().refresh(force=force)

    monkeypatch.setattr(cli_credentials, "BrowserLoginProvider", ProbingBrowser)
    monkeypatch.setattr(cli_credentials, "RateLimiter", RecordingLimiter)
    RecordingLimiter.created = []
    assert ws.config is not None, "the fake_browser fixture declared one source"
    second = SOURCE_TEMPLATE.format(
        credential="mock",
        compliance=COMPLIANCE_TEMPLATE.format(reviewed=date.today(), ceiling=30),
    ).replace("[sources.catalog", "[sources.catalog-slow")
    ws.config.write_text(ws.config.read_text() + second, encoding="utf-8")

    assert ws.run("login", "mock", "--browser") == 0

    (limiter,) = RecordingLimiter.created
    assert limiter.limit_per_minute == 30
    assert limiter.acquired == 1, "the request the provider's own client made was counted"


# --- named credentials ------------------------------------------------------------------------


def test_a_credential_can_have_any_name_and_borrow_a_kind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "ws"
    config = tmp_path / "catalog-sync.toml"
    config.write_text("[credentials.work]\nkind = 'mock'\nrenew_before_days = 5\n")
    write_values(credential_path(home / "credentials", "work"), {"AUTHORIZATION": FRESH})

    code = cli.main(["--config", str(config), "--workspace", str(home), "auth", "status", "work"])

    assert code == 0
    out = capsys.readouterr().out
    assert "credential: work (kind mock)" in out
    assert "fewer than 5 day(s)" in out


def test_a_configured_credential_with_an_uninstalled_kind_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "catalog-sync.toml"
    config.write_text("[credentials.work]\nkind = 'nokind'\n")
    args = ["--config", str(config), "--workspace", str(tmp_path), "auth", "status", "work"]
    assert cli.main(args) == 1
    assert "uses kind 'nokind', which is not installed" in capsys.readouterr().err
