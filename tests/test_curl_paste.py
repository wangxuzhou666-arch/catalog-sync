from __future__ import annotations

import stat
from pathlib import Path

import pytest

from catalog_sync.credentials.curl_paste import (
    CurlPasteProvider,
    ParsedCurl,
    credential_values,
    parse_curl,
)
from catalog_sync.credentials.store import read_values, write_values
from catalog_sync.errors import LoginFailed
from tests.fixtures.tokens import bearer, make_jwt
from tests.test_credentials_basics import LAYOUT

TOKEN = make_jwt(exp=1_800_000_000, jti="paste-1")
COOKIE_SECRET = "canary-cookie-" + "9f8e7d"


def chrome_curl(
    *, url: str = "https://api.example.invalid/v1/items:list", extra: str = "", token: str = TOKEN
) -> str:
    """The shape of a browser's 'Copy as cURL (bash)': line continuations, single quotes."""
    return (
        f"curl '{url}' \\\n"
        f"  -H 'accept: */*' \\\n"
        f"  -H 'authorization: {bearer(token)}' \\\n"
        f"  -H 'x-demo-referer: https://app.example.invalid/items?folder=7' \\\n"
        f"  -H 'cookie: session={COOKIE_SECRET}; theme=dark' \\\n"
        f"  -H 'user-agent: Mozilla/5.0 (X11)' \\\n"
        f"  -b 'extra={COOKIE_SECRET}' \\\n"
        f"  {extra} \\\n"
        f'  --data-raw \'{{"page":1,"note":"https://decoy.example.invalid/"}}\' \\\n'
        f"  --compressed"
    )


def test_only_the_layouts_headers_are_taken_from_a_browser_command() -> None:
    values = credential_values(parse_curl(chrome_curl()), LAYOUT)
    assert values == {
        "AUTHORIZATION": bearer(TOKEN),
        "REFERER": "https://app.example.invalid/items?folder=7",
    }


def test_cookies_never_leave_the_parser() -> None:
    parsed = parse_curl(chrome_curl())
    assert "cookie" not in parsed.headers
    assert COOKIE_SECRET not in repr(parsed)
    assert COOKIE_SECRET not in repr(credential_values(parsed, LAYOUT))


def test_the_url_is_the_command_target_not_a_value_inside_the_body() -> None:
    parsed = parse_curl(chrome_curl())
    assert parsed.url == "https://api.example.invalid/v1/items:list"


def test_bash_ansi_c_quoting_is_decoded() -> None:
    # Chrome switches to $'...' when a value holds characters such as quotes or non-ASCII.
    text = (
        "curl 'https://api.example.invalid/x' "
        f"-H $'authorization: {bearer(TOKEN)}' "
        "-H $'x-demo-referer: https://app.example.invalid/caf\\u00e9?q=it\\'s' "
        f"-H $'cookie: a={COOKIE_SECRET}\\n'"
    )
    parsed = parse_curl(text)
    assert parsed.headers["x-demo-referer"] == "https://app.example.invalid/café?q=it's"
    assert parsed.headers["authorization"] == bearer(TOKEN)
    assert "cookie" not in parsed.headers


def test_header_names_are_case_insensitive_and_the_last_duplicate_wins() -> None:
    text = (
        "curl 'https://api.example.invalid/x' -H 'Authorization: first' "
        "-H 'AUTHORIZATION: second' -H 'REGION: r9'"
    )
    assert credential_values(parse_curl(text), LAYOUT) == {
        "AUTHORIZATION": "second",
        "REGION": "r9",
    }


def test_options_that_take_a_value_do_not_leak_it_into_the_url() -> None:
    text = (
        "curl -X POST -b 'a=b' --data-raw 'https://decoy.example.invalid/' "
        "'https://api.example.invalid/real' -H 'authorization: t'"
    )
    assert parse_curl(text).url == "https://api.example.invalid/real"


def test_an_explicit_url_option_is_understood() -> None:
    parsed = parse_curl("curl --url https://api.example.invalid/u -H 'authorization: t'")
    assert parsed.url == "https://api.example.invalid/u"


@pytest.mark.parametrize(
    "url",
    ["https://api.example.invalid/x", "https://example.invalid/x", "http://a.b.example.invalid"],
)
def test_the_services_own_hosts_and_subdomains_are_accepted(url: str) -> None:
    credential_values(ParsedCurl(url, {"authorization": "t"}), LAYOUT)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil-example.invalid/x",
        "https://example.invalid.attacker.test/",
        "https://other.test/",
    ],
)
def test_a_command_for_another_site_is_refused_so_its_token_is_never_sent_here(url: str) -> None:
    with pytest.raises(LoginFailed, match=r"not for example\.invalid"):
        credential_values(ParsedCurl(url, {"authorization": "t"}), LAYOUT)


def test_a_command_without_a_url_is_refused() -> None:
    with pytest.raises(LoginFailed, match="no URL"):
        credential_values(ParsedCurl(None, {"authorization": "t"}), LAYOUT)


def test_a_request_that_carries_no_token_is_refused_with_advice() -> None:
    text = "curl 'https://api.example.invalid/x' -H 'accept: */*'"
    with pytest.raises(LoginFailed, match="no AUTHORIZATION header"):
        credential_values(parse_curl(text), LAYOUT)


def test_an_empty_header_value_does_not_count_as_a_token() -> None:
    with pytest.raises(LoginFailed, match="no AUTHORIZATION"):
        credential_values(ParsedCurl("https://example.invalid/", {"authorization": ""}), LAYOUT)


@pytest.mark.parametrize(
    "text", ["", "   ", "wget https://example.invalid", "https://example.invalid", "not a command"]
)
def test_text_that_is_not_a_curl_command_is_refused(text: str) -> None:
    with pytest.raises(LoginFailed, match="does not start with 'curl'"):
        parse_curl(text)


def test_unbalanced_quotes_are_refused_without_echoing_the_text() -> None:
    secret = "canary-token-" + "abc123"
    with pytest.raises(LoginFailed, match="unbalanced quotes") as failure:
        parse_curl(f"curl 'https://example.invalid/x -H authorization: {secret}")
    assert secret not in str(failure.value)


def test_windows_cmd_quoting_is_not_mistaken_for_a_valid_command() -> None:
    text = 'curl ^"https://api.example.invalid/x^" ^\n  -H ^"authorization: t^"'
    with pytest.raises(LoginFailed):
        credential_values(parse_curl(text), LAYOUT)


# --- the provider ---------------------------------------------------------------------------------


def test_pasting_stores_the_credential_owner_only_and_reads_it_back(tmp_path: Path) -> None:
    path = tmp_path / "creds" / "demo.cred"
    provider = CurlPasteProvider(path=path, layout=LAYOUT, pasted=chrome_curl())

    credential = provider.refresh(force=False)

    assert credential.token_id == "paste-1"
    assert credential.headers["authorization"] == bearer(TOKEN)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert COOKIE_SECRET not in path.read_text(), "the cookie must not reach the file"
    assert provider.load() == credential


def test_pasting_keeps_keys_the_new_command_does_not_carry(tmp_path: Path) -> None:
    path = tmp_path / "demo.cred"
    write_values(path, {"AUTHORIZATION": "old", "REGION": "kept-region"})
    CurlPasteProvider(path=path, layout=LAYOUT, pasted=chrome_curl()).refresh(force=True)
    assert read_values(path)["REGION"] == "kept-region"
    assert read_values(path)["AUTHORIZATION"] == bearer(TOKEN)


def test_a_provider_without_pasted_text_cannot_renew(tmp_path: Path) -> None:
    provider = CurlPasteProvider(path=tmp_path / "demo.cred", layout=LAYOUT)
    with pytest.raises(LoginFailed, match="nothing to store"):
        provider.refresh(force=True)


def test_a_rejected_paste_leaves_the_stored_credential_untouched(tmp_path: Path) -> None:
    path = tmp_path / "demo.cred"
    write_values(path, {"AUTHORIZATION": "keep-me"})
    bad = chrome_curl(url="https://other.test/x")
    with pytest.raises(LoginFailed):
        CurlPasteProvider(path=path, layout=LAYOUT, pasted=bad).refresh(force=True)
    assert read_values(path) == {"AUTHORIZATION": "keep-me"}
