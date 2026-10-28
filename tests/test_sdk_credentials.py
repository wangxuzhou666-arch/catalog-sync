"""Credential building blocks: the layout, the account, the HTTP login and the live check."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import pytest

from catalog_sync.credentials.base import Credential
from catalog_sync.credentials.browser import LoginAccount
from catalog_sync.credentials.curl_paste import credential_values, parse_curl
from catalog_sync.errors import ConfigError, HttpStatusError, LoginFailed, SourceShapeError
from catalog_sync.http import HttpClient, HttpSeams, HttpSettings
from catalog_sync.sdk import USER_AGENT, EnvAccount, FormLogin, header_layout, verify_with_probe
from catalog_sync.testing.fake_transport import FakeResponse, FakeTransport

LOGIN_URL = "https://app.example.invalid/api/login"
ACCOUNT = EnvAccount("ACME_USER", "ACME_PASSWORD")
ENV = {"ACME_USER": "ops", "ACME_PASSWORD": "pw-1234"}


# --- the layout -----------------------------------------------------------------------------------


def test_a_single_header_layout_stores_one_key_and_checks_the_host_of_a_pasted_command() -> None:
    layout = header_layout("app.example.invalid", key="API_KEY", header="X-Api-Key")
    assert layout.headers == {"API_KEY": "x-api-key"}
    assert (layout.token_key, layout.required) == ("API_KEY", frozenset({"API_KEY"}))
    good = "curl 'https://app.example.invalid/api/items' -H 'X-Api-Key: k-123'"
    assert credential_values(parse_curl(good), layout) == {"API_KEY": "k-123"}
    with pytest.raises(LoginFailed, match=r"not for app\.example\.invalid"):
        credential_values(parse_curl(good.replace("app.example", "other.example")), layout)
    with pytest.raises(LoginFailed, match="carries no API_KEY header"):
        credential_values(parse_curl("curl 'https://app.example.invalid/x'"), layout)


# --- the account ----------------------------------------------------------------------------------


def test_the_account_is_both_variables_or_neither() -> None:
    account = ACCOUNT(ENV)
    assert account == LoginAccount("ops", "pw-1234") and "pw-1234" not in repr(account)
    assert ACCOUNT({}) is None
    assert ACCOUNT({"HOME": "/x"}) is None


@pytest.mark.parametrize("env", [{"ACME_USER": "ops"}, {"ACME_PASSWORD": "pw"}])
def test_half_an_account_is_an_error_not_a_silent_default(env: dict[str, str]) -> None:
    demo = EnvAccount("ACME_USER", "ACME_PASSWORD", default=LoginAccount("demo", "demo"))
    for account in (ACCOUNT, demo):
        with pytest.raises(ConfigError, match="ACME_USER and ACME_PASSWORD must both be set"):
            account(env)


def test_a_default_account_applies_only_when_nothing_is_set() -> None:
    demo = EnvAccount("ACME_USER", "ACME_PASSWORD", default=LoginAccount("demo", "demo"))
    assert demo({}) == LoginAccount("demo", "demo")
    assert demo(ENV) == LoginAccount("ops", "pw-1234")


# --- the HTTP login -------------------------------------------------------------------------------


def login(**options: object) -> FormLogin:
    return FormLogin(
        service="the Acme API",
        url=LOGIN_URL,
        account=ACCOUNT,
        credential_key="AUTHORIZATION",
        **options,  # type: ignore[arg-type]
    )


def run_login(
    form: FormLogin, answer: FakeResponse, env: dict[str, str] | None = None
) -> tuple[dict[str, str], FakeTransport]:
    transport = FakeTransport()
    transport.queue(answer)
    values = form(ENV if env is None else env, seams=HttpSeams(adapter=transport))
    return dict(values), transport


def token_answer(value: str = "tok-123") -> FakeResponse:
    return FakeResponse.json({"state": True, "data": {"token": value}})


def test_a_json_login_posts_the_account_and_keeps_the_token_with_its_prefix() -> None:
    values, transport = run_login(login(), token_answer())
    assert values == {"AUTHORIZATION": "Bearer tok-123"}
    (sent,) = transport.requests
    assert sent.method == "POST" and sent.url == LOGIN_URL
    assert sent.json_body() == {"username": "ops", "password": "pw-1234"}
    assert sent.header("content-type") == "application/json"
    assert sent.header("authorization") is None
    assert sent.header("user-agent") == USER_AGENT


def test_the_field_names_the_token_path_and_the_prefix_are_the_connectors_to_choose() -> None:
    form = login(
        user_field="email",
        password_field="secret",
        token_path="session.key",
        token_prefix="",
        expects_state=False,
    )
    values, transport = run_login(form, FakeResponse.json({"session": {"key": "abc"}}))
    assert values == {"AUTHORIZATION": "abc"}
    assert transport.requests[0].json_body() == {"email": "ops", "secret": "pw-1234"}


def test_a_form_encoded_login_sends_urlencoded_fields() -> None:
    _, transport = run_login(login(encoding="form"), token_answer())
    (sent,) = transport.requests
    assert sent.header("content-type") == "application/x-www-form-urlencoded"
    assert parse_qs((sent.body or b"").decode()) == {"username": ["ops"], "password": ["pw-1234"]}


def test_a_session_cookie_is_kept_when_the_service_answers_with_one() -> None:
    answer = FakeResponse.json(
        {"state": True}, headers={"Set-Cookie": "acme_session=s3cr3t; Path=/; HttpOnly"}
    )
    values, _ = run_login(login(session_cookie="acme_session"), answer)
    assert values == {"AUTHORIZATION": "acme_session=s3cr3t"}


def test_a_cookie_the_service_did_not_set_is_a_login_failure() -> None:
    with pytest.raises(LoginFailed, match="sets no acme_session cookie"):
        run_login(login(session_cookie="acme_session"), FakeResponse.json({"state": True}))


@pytest.mark.parametrize(
    ("answer", "complaint"),
    [
        (FakeResponse.json({"state": True, "data": {}}), "carries no token"),
        (FakeResponse.json({"state": True, "data": {"token": ""}}), "carries no token"),
        (FakeResponse.json({"state": True, "data": {"token": 5}}), "carries no token"),
        (FakeResponse.json({"state": False, "msg": "x"}), "not what was expected"),
        (FakeResponse.json({"data": {"token": "t"}}), "not what was expected"),
    ],
)
def test_an_answer_that_is_not_a_token_is_a_login_failure(
    answer: FakeResponse, complaint: str
) -> None:
    with pytest.raises(LoginFailed, match=f"the Acme API's login answer .*{complaint}"):
        run_login(login(), answer)


def test_a_refused_password_is_one_request_and_never_echoed() -> None:
    transport = FakeTransport()
    transport.queue(FakeResponse(401), token_answer())
    with pytest.raises(
        LoginFailed, match="the Acme API rejected the user name or password"
    ) as caught:
        login()(ENV, seams=HttpSeams(adapter=transport))
    assert "pw-1234" not in str(caught.value)
    assert len(transport.requests) == 1, "a refusal is not retried"


def test_other_http_failures_are_not_mistaken_for_a_wrong_password() -> None:
    transport = FakeTransport()
    transport.queue(FakeResponse(404))
    with pytest.raises(HttpStatusError) as caught:
        login()(ENV, seams=HttpSeams(adapter=transport))
    assert caught.value.status == 404


def test_without_an_account_it_says_which_variables_to_set() -> None:
    transport = FakeTransport()
    with pytest.raises(ConfigError, match="set ACME_USER and ACME_PASSWORD to log in"):
        login()({}, seams=HttpSeams(adapter=transport))
    assert transport.requests == [], "no request without an account"


def test_the_demo_account_logs_in_when_nothing_is_set() -> None:
    form = FormLogin(
        service="the Acme API",
        url=LOGIN_URL,
        account=EnvAccount("ACME_USER", "ACME_PASSWORD", default=LoginAccount("demo", "demo-pw")),
        credential_key="AUTHORIZATION",
    )
    _, transport = run_login(form, token_answer(), env={})
    assert transport.requests[0].json_body() == {"username": "demo", "password": "demo-pw"}


# --- the live check -------------------------------------------------------------------------------


def probe_with(answer: FakeResponse) -> tuple[FakeTransport, Exception | None]:
    transport = FakeTransport()
    transport.queue(answer, FakeResponse.json({"state": True, "data": {"ok": 1}}))

    def probe(client: HttpClient) -> object:
        payload = client.get_json("https://app.example.invalid/api/ping")
        if payload.get("state") is not True:
            raise SourceShapeError("state is not true")
        return payload

    try:
        verify_with_probe({"authorization": "Bearer t"}, probe, seams=HttpSeams(adapter=transport))
    except LoginFailed as exc:
        return transport, exc
    return transport, None


def test_a_credential_the_service_accepts_passes_with_the_headers_it_was_given() -> None:
    transport, failure = probe_with(FakeResponse.json({"state": True}))
    assert failure is None
    assert transport.requests[0].header("authorization") == "Bearer t"


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_credential_fails_the_login_after_one_request(status: int) -> None:
    transport, failure = probe_with(FakeResponse(status))
    assert isinstance(failure, LoginFailed) and "rejected it" in str(failure)
    assert len(transport.requests) == 1


def test_an_answer_of_the_wrong_shape_fails_the_login_even_with_http_200() -> None:
    _, failure = probe_with(FakeResponse.json({"state": False}))
    assert isinstance(failure, LoginFailed)
    assert "service's answer is wrong: state is not true" in str(failure)


def test_a_service_that_keeps_failing_is_reported_not_hidden() -> None:
    transport = FakeTransport(lambda _request: FakeResponse(503))
    with pytest.raises(HttpStatusError) as caught:
        verify_with_probe(
            {"authorization": "Bearer t"},
            lambda client: client.get_json("https://app.example.invalid/api/ping"),
            seams=HttpSeams(adapter=transport, sleep=lambda _s: None),
        )
    assert caught.value.status == 503
    assert len(transport.requests) == 3, "three attempts, then it gives up"


# --- the HTTP client pieces the login relies on ---------------------------------------------------


def test_post_form_sends_urlencoded_bytes_and_reads_json() -> None:
    transport = FakeTransport()
    transport.queue(FakeResponse.json({"ok": True}))
    client = HttpClient(
        {"accept": "application/json"},
        credentialed=False,
        settings=HttpSettings(gap_s=0.0),
        seams=HttpSeams(adapter=transport),
    )
    assert client.post_form("https://app.example.invalid/f", fields={"a": "1 2", "b": "x&y"}) == {
        "ok": True
    }
    (sent,) = transport.requests
    assert sent.body == b"a=1+2&b=x%26y"
    assert sent.header("content-type") == "application/x-www-form-urlencoded"


def test_the_client_keeps_the_cookies_the_service_sets_and_only_those() -> None:
    transport = FakeTransport()
    transport.queue(
        FakeResponse.json({}, headers={"Set-Cookie": "a=1; Path=/"}), FakeResponse.json({})
    )
    client = HttpClient(
        {}, credentialed=False, settings=HttpSettings(gap_s=0.0), seams=HttpSeams(adapter=transport)
    )
    assert client.cookie("a") is None
    client.get_json("https://app.example.invalid/x")
    assert client.cookie("a") == "1"
    assert client.cookie("b") is None
    client.get_json("https://app.example.invalid/x")
    assert transport.requests[1].header("cookie") == "a=1", "the session sends it back"
    json.dumps(transport.requests[1].headers)  # the recorded headers stay plain strings


def test_a_credential_object_prints_no_secret() -> None:
    credential = Credential({"cookie": "acme_session=s3cr3t"}, {}, None, None)
    assert "s3cr3t" not in repr(credential)
