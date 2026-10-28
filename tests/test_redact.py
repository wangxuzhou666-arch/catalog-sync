"""Masking of secrets in logs and on the terminal, and the canary that proves nothing gets past it.

Secret-looking strings are built at run time (tests/fixtures/tokens.py) so that no token-shaped
literal sits in this file for the repository's own scanners to find.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from catalog_sync import cli, cli_sources
from catalog_sync.errors import ConfigError, ItemFailed
from catalog_sync.http import HttpClient, HttpSeams, HttpSettings
from catalog_sync.models import JsonObject
from catalog_sync.redact import MASK, RedactingFilter, redact_text, redacted_logging
from catalog_sync.sources.base import AssetRef, ListedItem
from catalog_sync.state import Checkpoint, StateStore
from catalog_sync.testing.fake_transport import FakeResponse, FakeTransport, RecordedRequest
from tests.fixtures import compliance
from tests.fixtures.tokens import make_jwt

JWT = make_jwt(exp=4_000_000_000, jti="redact-test-jti")
OPAQUE = "k9Zq2Xv7Lm4Rt8Bw1Ns6Hd3Fy5Gc0Jp"  # a token with no recognisable shape of its own
AT = "@"  # kept out of the literals so the repository's own email scan has nothing to find

# --- what gets masked, and what must be left alone ------------------------------------------------

MASKED = [
    ("a bare JWT", f"token was {JWT} today", f"token was {MASK} today"),
    ("a bearer credential", f"sent Bearer {OPAQUE}", f"sent Bearer {MASK}"),
    ("a lower-case basic credential", f"basic {OPAQUE}", f"basic {MASK}"),
    ("an authorization header", f"Authorization: Bearer {OPAQUE}", f"Authorization: {MASK}"),
    ("a bare authorization value", f"authorization: {OPAQUE}", f"authorization: {MASK}"),
    (
        "a proxy authorization header",
        f"Proxy-Authorization: {OPAQUE}",
        f"Proxy-Authorization: {MASK}",
    ),
    ("a cookie header", "Cookie: sid=abc123; theme=dark", f"Cookie: {MASK}"),
    ("a set-cookie header", "set-cookie: sid=abc123; Path=/", f"set-cookie: {MASK}"),
    (
        "a header in a dict rendering",
        f"{{'authorization': '{OPAQUE}', 'x': 1}}",
        f"{{'authorization': '{MASK}', 'x': 1}}",
    ),
    ("a header in a JSON rendering", '{"Cookie": "sid=abc"}', f'{{"Cookie": "{MASK}"}}'),
    ("an api-key header", f"x-api-key: {OPAQUE}", f"x-api-key: {MASK}"),
    (
        "the query of a signed URL",
        "GET https://cdn.example.invalid/a/b.png?X-Amz-Signature=abc&Expires=9",
        f"GET https://cdn.example.invalid/a/b.png?{MASK}",
    ),
    ("a token pair", f"retrying with token={OPAQUE} now", f"retrying with token={MASK} now"),
    ("a password pair", "password=hunter2&user=me", f"password={MASK}&user=me"),
    ("a signature pair", "X-Amz-Signature=deadbeef", f"X-Amz-Signature={MASK}"),
    (
        "the password of a connection string",
        "postgresql://loader:hunter2" + AT + "db.example.invalid:5432/catalog",
        f"postgresql://loader:{MASK}" + AT + "db.example.invalid:5432/catalog",
    ),
    (
        "a connection string password with a slash in it",
        "postgresql://loader:hunter2/pa/ss" + AT + "db.example.invalid/catalog",
        f"postgresql://loader:{MASK}" + AT + "db.example.invalid/catalog",
    ),
    (
        "a connection string password with a colon and a question mark",
        "postgresql://loader:hunter2:p?q" + AT + "db.example.invalid/catalog",
        f"postgresql://loader:{MASK}" + AT + "db.example.invalid/catalog",
    ),
    ("a JSON token", f'{{"token": "{OPAQUE}", "n": 1}}', f'{{"token": {MASK}, "n": 1}}'),
    ("a compact JSON token", f'{{"token":"{OPAQUE}"}}', f'{{"token":{MASK}}}'),
    (
        "a dict rendering of a password",
        "{'password': 'hunter2', 'user': 'me'}",
        f"{{'password': {MASK}, 'user': 'me'}}",
    ),
    (
        "a quoted password with spaces",
        '{"password": "hunter2 and more"}',
        f'{{"password": {MASK}}}',
    ),
    (
        "a quoted password with an escaped quote",
        '{"password": "hunter2\\"tail"}',
        f'{{"password": {MASK}}}',
    ),
    ("a message cut off inside a quoted secret", '{"token": "hunter2', f'{{"token": {MASK}'),
    ("a key written with a colon", f"api_key: {OPAQUE}", f"api_key: {MASK}"),
    ("an upper-case key pair", f"API_KEY={OPAQUE}", f"API_KEY={MASK}"),
    (
        "a colon pair in a sentence",
        "password: hunter2 was refused",
        f"password: {MASK} was refused",
    ),
    (
        "an id token in JSON",
        '{"id_token": "hunter2", "expires_in": 3600}',
        f'{{"id_token": {MASK}, "expires_in": 3600}}',
    ),
]

UNTOUCHED = [
    "two parts only: eyJhbGciOiJIUzI1NiJ9.short",
    "version v1.2.3 of the 1.2.3 release",
    "bearer tokens go in a header",
    "Basic setup is done",
    "the authorization step passed and cookies are discarded while parsing",
    "https://cdn.example.invalid/a/b.png",
    "what? nothing to see",
    "tokens=3 retries=2",
    "mailto:someone" + AT + "example.invalid and https://example.invalid/" + AT + "handle",
    "run 20261026T120000Z-abc123 complete, 12 items",
    "https://example.invalid:8443/health and postgresql://db.example.invalid:5432/catalog",
    "tokens: 3, secrets rotated, passwords checked",
    '{"expires_in": 3600, "token_type": "bearer"}',
]


@pytest.mark.parametrize(("what", "before", "after"), MASKED, ids=[case[0] for case in MASKED])
def test_what_looks_like_a_secret_is_masked(what: str, before: str, after: str) -> None:
    assert redact_text(before) == after


@pytest.mark.parametrize("text", UNTOUCHED)
def test_ordinary_text_is_left_alone(text: str) -> None:
    assert redact_text(text) == text


@pytest.mark.parametrize(("what", "before", "after"), MASKED, ids=[case[0] for case in MASKED])
def test_masking_twice_changes_nothing(what: str, before: str, after: str) -> None:
    assert redact_text(after) == after


def test_the_secret_itself_is_gone_from_every_masked_case() -> None:
    for _, before, after in MASKED:
        for secret in (JWT, OPAQUE, "hunter2", "abc123", "deadbeef"):
            if secret in before:
                assert secret not in after


# --- the logging filter ---------------------------------------------------------------------------


def record(message: str, *args: object, exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = None
    if exc is not None:
        try:
            raise exc
        except type(exc):
            exc_info = sys.exc_info()
    return logging.LogRecord(
        "catalog_sync.test", logging.WARNING, __file__, 1, message, args, exc_info
    )


def test_the_filter_masks_the_formatted_message_and_the_arguments() -> None:
    entry = record("failed with %s for %s", f"Bearer {OPAQUE}", "item 7")
    assert RedactingFilter().filter(entry) is True
    assert entry.getMessage() == f"failed with Bearer {MASK} for item 7"
    assert OPAQUE not in str(entry.__dict__)


def test_the_filter_masks_a_traceback_and_leaves_the_exception_usable() -> None:
    entry = record("it broke", exc=RuntimeError(f"upstream said {JWT}"))
    RedactingFilter().filter(entry)
    text = logging.Formatter("%(message)s").format(entry)
    assert "RuntimeError" in text and "upstream said" in text
    assert JWT not in text


def test_a_clean_record_keeps_its_format_arguments() -> None:
    entry = record("run %s has %d items", "r1", 3)
    RedactingFilter().filter(entry)
    assert (entry.msg, entry.args) == ("run %s has %d items", ("r1", 3))


def test_a_record_whose_arguments_do_not_fit_does_not_make_the_logging_call_raise() -> None:
    entry = record("one placeholder %s", "a", "b")
    assert RedactingFilter().filter(entry) is True


# --- installing it --------------------------------------------------------------------------------


@pytest.fixture
def clean_logging() -> Iterator[None]:
    factory, handlers = logging.getLogRecordFactory(), list(logging.getLogger().handlers)
    yield
    assert logging.getLogRecordFactory() is factory, "redacted_logging must put the factory back"
    assert logging.getLogger().handlers == handlers, "...and remove its handler"


def test_inside_the_block_every_package_logger_is_masked_and_outside_nothing_is(
    clean_logging: None, caplog: pytest.LogCaptureFixture
) -> None:
    child = logging.getLogger("catalog_sync.some.deep.module")
    foreign = logging.getLogger("a_library")
    with caplog.at_level(logging.DEBUG), redacted_logging():
        child.warning("header was Authorization: %s", OPAQUE)
        foreign.warning("header was Authorization: %s", OPAQUE)
    child.warning("header was Authorization: %s", OPAQUE)
    # The text each handler formatted at emit time: a record is one shared object, and the
    # stderr handler's filter masks it in place after the capture handler has already seen it.
    inside, foreign_inside, outside = caplog.text.strip().splitlines()
    assert OPAQUE not in inside, (
        "a child logger of the package is covered (a filter on the parent would miss it)"
    )
    assert OPAQUE in foreign_inside, "other libraries' records are masked only where printed"
    assert OPAQUE in outside, "the block must end the masking"


def test_what_reaches_stderr_is_masked_whoever_logged_it(
    clean_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with redacted_logging():
        logging.getLogger("a_library").error("GET https://h.example.invalid/x?sig=%s", OPAQUE)
        logging.getLogger("catalog_sync.x").warning("Authorization: %s", OPAQUE)
        logging.getLogger("catalog_sync.x").info("below the handler's level")
    err = capsys.readouterr().err
    assert OPAQUE not in err
    assert "https://h.example.invalid/x?[redacted]" in err
    assert "below the handler" not in err


def test_the_stderr_handler_follows_the_current_stderr(
    clean_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with redacted_logging():
        capsys.readouterr()
        logging.getLogger("catalog_sync.x").warning("first")
        assert "first" in capsys.readouterr().err
        logging.getLogger("catalog_sync.x").warning("second")
        assert "second" in capsys.readouterr().err


# --- the canary: a token pushed through every path that prints or stores text ---------------------

CANARIES = (JWT, OPAQUE)
API = "https://api.example.invalid"


class LeakySource:
    """A source whose requests, failures and crashes all carry the canary in the worst places.

    Item 1 needs two retries; item 2 fails with an item-scoped error whose message holds the
    canary; item 3 is rejected as unauthorised, which stops the run with the URL in the message.
    """

    name = "leaky.source"

    def __init__(self, client: HttpClient, *, crash: bool = False) -> None:
        self._client = client
        self._crash = crash

    def enumerate(self, state: Checkpoint) -> Iterator[ListedItem]:
        for number in (1, 2, 3):
            yield ListedItem(f"item-{number}", "v1", {"n": number}, {})

    def fetch_detail(self, item: ListedItem) -> JsonObject | None:
        if self._crash:
            raise RuntimeError(f"unexpected state; Authorization: Bearer {OPAQUE} and {JWT}")
        if item.source_id == "item-2":
            raise ItemFailed(item.source_id, f"the service said {JWT} is not valid here")
        url = f"{API}/items/{JWT}/{item.source_id}?sig={OPAQUE}"
        return self._client.get_json(url)

    def asset_refs(self, item: ListedItem, detail: JsonObject | None) -> list[AssetRef]:
        return []

    def fetch_asset(self, ref: AssetRef) -> bytes:
        raise AssertionError("no assets")


def respond(request: RecordedRequest) -> FakeResponse:
    attempts = sum(1 for r in respond.seen if r == request.url)  # type: ignore[attr-defined]
    respond.seen.append(request.url)  # type: ignore[attr-defined]
    if "item-3" in request.url:
        return FakeResponse.json({"state": False}, status=401)
    if attempts < 2:
        return FakeResponse.json({}, status=503)
    return FakeResponse.json({"ok": True})


respond.seen = []  # type: ignore[attr-defined]


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, crash: bool = False):
        respond.seen = []  # type: ignore[attr-defined]
        FakeTransport(respond).install(monkeypatch)
        self.home = tmp_path / "ws"
        self.config = tmp_path / "catalog-sync.toml"
        self.config.write_text(
            textwrap.dedent(
                """
                [sources.leaky]
                plugin = "leaky"
                request_gap_s = 0.001
                """
            )
            + textwrap.dedent(compliance.block("leaky")),
            encoding="utf-8",
        )
        client = HttpClient(
            {"authorization": f"Bearer {JWT}", "cookie": f"sid={OPAQUE}"},
            credentialed=True,
            settings=HttpSettings(gap_s=0, max_attempts=4, backoff_base_s=0.001),
            seams=HttpSeams(sleep=lambda _seconds: None),
        )
        source = LeakySource(client, crash=crash)
        monkeypatch.setattr(
            cli_sources.registry,
            "source_factory",
            lambda _name: lambda _options, *, base_dir: source,
        )

    def run(self, *args: str) -> int:
        return cli.main(["--config", str(self.config), "--workspace", str(self.home), *args])

    def everything(self, out: str, err: str, log: str) -> str:
        """All the text the run left behind: terminal, log, audit log and stored item errors."""
        audit = self.home / "audit.jsonl"
        return "\n".join(
            [
                out,
                err,
                log,
                self.stored_errors(),
                audit.read_text("utf-8") if audit.exists() else "",
            ]
        )

    def stored_errors(self) -> str:
        path = self.home / "state.sqlite"
        if not path.exists():
            return ""
        conn = sqlite3.connect(path)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
            if "item_stages" not in tables:
                return ""
            return " ".join(str(row[0]) for row in conn.execute("SELECT error FROM item_stages"))
        finally:
            conn.close()


def assert_no_canary(text: str) -> None:
    for secret in CANARIES:
        assert secret not in text, f"{secret[:12]}... leaked"


def test_retries_item_failures_auth_errors_and_the_audit_log_never_show_the_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        code = rig.run("pull", "leaky")
        assert rig.run("runs", "list") == 0
    shown = capsys.readouterr()

    assert code == 2, "item-3 is rejected with HTTP 401, which stops the run"
    assert "was rejected with HTTP 401" in shown.err
    assert "retrying in" in caplog.text and "attempt 1 of 4" in caplog.text, (
        "the log still says what happened"
    )
    assert "api.example.invalid" in caplog.text
    assert_no_canary(rig.everything(shown.out, shown.err, caplog.text))


def test_a_stored_item_failure_is_masked_in_the_database_and_in_runs_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rig.run("pull", "leaky")
    capsys.readouterr()
    with StateStore(rig.home / "state.sqlite") as store:
        (run,) = store.list_runs()
    assert rig.run("runs", "show", run.run_id) == 0
    shown = capsys.readouterr()
    assert "the service said" in shown.out + shown.err, "the failure itself is still reported"
    assert_no_canary(rig.everything(shown.out, shown.err, ""))


def test_an_unexpected_crash_prints_a_masked_traceback_and_exits_with_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig(tmp_path, monkeypatch, crash=True)
    code = rig.run("pull", "leaky")
    shown = capsys.readouterr()
    assert code == 1
    assert "Traceback" in shown.err and "RuntimeError: unexpected state" in shown.err
    assert_no_canary(rig.everything(shown.out, shown.err, caplog.text))


def test_an_error_message_that_carries_the_token_is_masked_on_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = Rig(tmp_path, monkeypatch)

    def refuse(_name: str) -> None:
        raise ConfigError(f"plugin rejected the option Authorization: Bearer {OPAQUE} / {JWT}")

    monkeypatch.setattr(cli_sources.registry, "source_factory", refuse)
    assert rig.run("pull", "leaky") == 1
    err = capsys.readouterr().err
    assert "plugin rejected the option" in err
    assert_no_canary(err)


def test_a_login_and_a_status_check_keep_the_pasted_token_off_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    import io

    rig = Rig(tmp_path, monkeypatch)
    pasted = (
        f"curl 'http://127.0.0.1:8765/api/v1/suppliers' "
        f"-H 'authorization: Bearer {JWT}' -H 'x-demo-referer: https://app.example.invalid/grid' "
        f"-H 'x-demo-context: web-context-demo' -H 'cookie: sid={OPAQUE}' --data-raw '{{}}'"
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(pasted))
    with caplog.at_level(logging.DEBUG):
        assert rig.run("login", "mock", "--paste-curl", "-") == 0
        rig.run("auth", "status", "mock")
        rig.run("audit", "show")
    shown = capsys.readouterr()
    assert "stored" in shown.out
    assert_no_canary(rig.everything(shown.out, shown.err, caplog.text))


# --- the canary test must be able to fail (CODING-STYLE R.7) --------------------------------------


@pytest.fixture
def without_masking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "redacted_logging", contextlib.nullcontext)
    monkeypatch.setattr(cli, "redact_text", lambda text: text)
    import catalog_sync.puller as puller

    monkeypatch.setattr(puller, "redact_text", lambda text: text)


def test_without_the_masking_the_canary_does_reach_the_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    without_masking: None,
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        rig.run("pull", "leaky")
    shown = capsys.readouterr()
    assert JWT in caplog.text, "the retry log carries the token-shaped URL segment until masked"
    assert JWT in shown.err, "so does the auth error on the terminal"
    assert JWT in rig.everything(shown.out, shown.err, caplog.text), "and the stored item failure"


def test_without_the_masking_a_crash_puts_the_token_in_the_logged_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    without_masking: None,
) -> None:
    rig = Rig(tmp_path, monkeypatch, crash=True)
    assert rig.run("pull", "leaky") == 1
    assert OPAQUE in caplog.text and JWT in caplog.text
