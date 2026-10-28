"""Token claims, freshness states and the credential file."""

from __future__ import annotations

import base64
import json
import logging
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from catalog_sync.credentials.base import Credential, CredentialLayout
from catalog_sync.credentials.freshness import Freshness, assess, renewal_window_warning
from catalog_sync.credentials.jwt import decode_claims, describe_token_id
from catalog_sync.credentials.store import (
    credential_path,
    load_credential,
    read_values,
    write_values,
)
from catalog_sync.errors import ConfigError, CredentialMissing
from tests.fixtures.tokens import b64url, bearer, make_jwt

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
LAYOUT = CredentialLayout(
    headers={
        "AUTHORIZATION": "authorization",
        "REFERER": "x-demo-referer",
        "REGION": "region",
    },
    token_key="AUTHORIZATION",
    required=frozenset({"AUTHORIZATION"}),
    host_suffixes=("example.invalid",),
)


def token_with_payload(payload: bytes) -> str:
    return f"{b64url(b'{}')}.{b64url(payload)}.{b64url(b'sig')}"


# --- jwt ----------------------------------------------------------------------------------------


def test_expiry_and_id_are_read_from_the_payload() -> None:
    claims = decode_claims(bearer(make_jwt(exp=1_800_000_000, jti="abc-123")))
    assert claims.expires_at == datetime.fromtimestamp(1_800_000_000, tz=UTC)
    assert claims.token_id == "abc-123"


def test_a_bare_token_without_the_scheme_word_works_too() -> None:
    assert decode_claims(make_jwt(exp=1_800_000_000)).expires_at is not None


@pytest.mark.parametrize("jti", ["a", "ab", "abc", "abcd", "x" * 33, "y" * 34])
def test_every_base64_padding_length_decodes(jti: str) -> None:
    assert decode_claims(make_jwt(exp=1_800_000_000, jti=jti)).token_id == jti


@pytest.mark.parametrize(
    "authorization",
    ["", "   ", "Bearer", "Bearer abc", "Bearer a.b", "Bearer a.b.c.d", "not a token at all"],
)
def test_text_that_is_not_a_three_part_token_has_no_claims(authorization: str) -> None:
    claims = decode_claims(authorization)
    assert (claims.expires_at, claims.token_id) == (None, None)


@pytest.mark.parametrize(
    "payload",
    [b"%%%not base64 json%%%", b"[1, 2, 3]", b'"text"', b"null", b"{broken", b"\xff\xfe"],
)
def test_a_payload_that_is_not_a_json_object_has_no_claims(payload: bytes) -> None:
    claims = decode_claims(token_with_payload(payload))
    assert (claims.expires_at, claims.token_id) == (None, None)


def test_a_payload_that_is_not_base64_at_all_has_no_claims() -> None:
    claims = decode_claims("aaa.!!!!.ccc")
    assert (claims.expires_at, claims.token_id) == (None, None)


@pytest.mark.parametrize(
    "raw_payload",
    [
        b'{"exp": "1800000000"}',
        b'{"exp": true}',
        b'{"exp": null}',
        b'{"exp": [1]}',
        b'{"exp": 1e300}',
        b'{"exp": NaN}',
        b'{"exp": Infinity}',
        b'{"exp": 99999999999999999999999}',
    ],
)
def test_an_expiry_that_is_not_a_usable_number_is_unknown_not_guessed(raw_payload: bytes) -> None:
    assert decode_claims(token_with_payload(raw_payload)).expires_at is None


def test_a_float_expiry_and_a_negative_expiry_are_numbers_like_any_other() -> None:
    assert decode_claims(make_jwt(exp=1_800_000_000.5)).expires_at is not None
    assert decode_claims(make_jwt(exp=-100)).expires_at == datetime.fromtimestamp(-100, tz=UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b'{"jti": 42}', "42"),
        (b'{"jti": true}', None),
        (b'{"jti": ""}', None),
        (b'{"jti": ["x"]}', None),
        (b'{"jti": 1.5}', None),
        (b"{}", None),
    ],
)
def test_token_id_accepts_strings_and_integers_only(raw: bytes, expected: str | None) -> None:
    assert decode_claims(token_with_payload(raw)).token_id == expected


def test_a_token_id_is_never_printed_in_full() -> None:
    token_id = "jti-" + "0123456789abcdef0123456789ab"
    shown = describe_token_id(token_id)
    assert token_id not in shown
    assert shown.endswith("(32 chars)")
    assert describe_token_id(None) == "unknown"
    assert describe_token_id("ab") == "...(2 chars)", "a short id reveals nothing"


def test_a_credential_prints_without_any_header_value() -> None:
    secret = "canary-" + "value-123"
    credential = Credential({"authorization": secret}, {}, NOW, secret)
    assert secret not in repr(credential)
    assert secret not in str(credential)
    assert "authorization" in repr(credential)


# --- freshness ----------------------------------------------------------------------------------


def credential_expiring_in(delta: timedelta | None) -> Credential:
    return Credential({}, {}, None if delta is None else NOW + delta, None)


def test_a_credential_outside_the_renewal_window_is_fresh() -> None:
    report = assess(credential_expiring_in(timedelta(days=5)), now=NOW, renew_before_days=2)
    assert report.state is Freshness.FRESH
    assert report.days_left == pytest.approx(5)


def test_exactly_at_the_window_edge_is_still_fresh_and_just_inside_is_renew_soon() -> None:
    edge = assess(credential_expiring_in(timedelta(days=2)), now=NOW, renew_before_days=2)
    inside = assess(
        credential_expiring_in(timedelta(days=2) - timedelta(seconds=1)),
        now=NOW,
        renew_before_days=2,
    )
    assert edge.state is Freshness.FRESH
    assert inside.state is Freshness.RENEW_SOON


def test_the_expiry_instant_itself_is_expired() -> None:
    at_expiry = assess(credential_expiring_in(timedelta(0)), now=NOW, renew_before_days=2)
    just_before = assess(credential_expiring_in(timedelta(seconds=1)), now=NOW, renew_before_days=2)
    assert at_expiry.state is Freshness.EXPIRED
    assert just_before.state is Freshness.RENEW_SOON


def test_a_past_expiry_reports_negative_days() -> None:
    report = assess(credential_expiring_in(-timedelta(days=3)), now=NOW, renew_before_days=2)
    assert report.state is Freshness.EXPIRED
    assert report.days_left == pytest.approx(-3)


def test_an_unreadable_expiry_is_unknown_never_fresh() -> None:
    report = assess(credential_expiring_in(None), now=NOW, renew_before_days=2)
    assert (report.state, report.days_left) == (Freshness.UNKNOWN, None)


def test_a_zero_window_never_asks_for_renewal_early() -> None:
    soon = assess(credential_expiring_in(timedelta(minutes=1)), now=NOW, renew_before_days=0)
    assert soon.state is Freshness.FRESH


def test_a_negative_window_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        assess(credential_expiring_in(timedelta(days=1)), now=NOW, renew_before_days=-1)


def test_the_window_warning_fires_only_when_runs_are_further_apart_than_the_window() -> None:
    warning = renewal_window_warning(renew_before_days=2, schedule_interval_days=7)
    assert warning is not None
    assert "2" in warning
    assert "7" in warning
    assert renewal_window_warning(renew_before_days=7, schedule_interval_days=7) is None
    assert renewal_window_warning(renew_before_days=8, schedule_interval_days=7) is None
    assert renewal_window_warning(renew_before_days=2, schedule_interval_days=None) is None


# --- credential file ----------------------------------------------------------------------------


def test_a_line_is_split_on_the_first_equals_sign_only(tmp_path: Path) -> None:
    path = tmp_path / "demo.cred"
    path.write_text("AUTHORIZATION=Bearer abc==\r\nREFERER = https://x.example.invalid/?a=b \r\n")
    assert read_values(path) == {
        "AUTHORIZATION": "Bearer abc==",
        "REFERER": "https://x.example.invalid/?a=b",
    }


def test_blank_lines_and_comments_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "demo.cred"
    path.write_text("# captured yesterday\n\nREGION=r1\n   \n")
    assert read_values(path) == {"REGION": "r1"}


def test_a_missing_file_says_how_to_create_the_credential(tmp_path: Path) -> None:
    with pytest.raises(CredentialMissing, match=r"no credential stored for 'demo'.*login demo"):
        read_values(tmp_path / "demo.cred")


def test_an_empty_file_is_not_a_credential(tmp_path: Path) -> None:
    path = tmp_path / "demo.cred"
    path.write_text("\n# nothing\n")
    with pytest.raises(CredentialMissing, match="empty"):
        read_values(path)


def test_a_malformed_line_is_reported_by_number_and_never_quoted(tmp_path: Path) -> None:
    secret = "part-of-a-token-" + "xyz"
    path = tmp_path / "demo.cred"
    path.write_text(f"REGION=r1\n{secret}\n")
    with pytest.raises(CredentialMissing, match="line 2") as failure:
        read_values(path)
    assert secret not in str(failure.value)


def test_saving_creates_an_owner_only_file_in_an_owner_only_directory(tmp_path: Path) -> None:
    path = credential_path(tmp_path / "creds", "demo")
    write_values(path, {"AUTHORIZATION": "Bearer x"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["demo.cred"], "no temp file left behind"


def test_saving_merges_new_keys_over_old_and_keeps_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "demo.cred"
    write_values(path, {"AUTHORIZATION": "old", "REFERER": "r-old", "REGION": "region-1"})
    write_values(path, {"AUTHORIZATION": "new"})
    assert read_values(path) == {"AUTHORIZATION": "new", "REFERER": "r-old", "REGION": "region-1"}


def test_saving_tightens_a_file_that_was_world_readable(tmp_path: Path) -> None:
    path = tmp_path / "demo.cred"
    path.write_text("REGION=r1\n")
    path.chmod(0o644)
    write_values(path, {"AUTHORIZATION": "x"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_saving_over_a_broken_file_replaces_it(tmp_path: Path) -> None:
    path = tmp_path / "demo.cred"
    path.write_text("this is not a credential\n")
    write_values(path, {"AUTHORIZATION": "x"})
    assert read_values(path) == {"AUTHORIZATION": "x"}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("AUTHORIZATION", "line one\nREGION=injected"),
        ("AUTHORIZATION", "carriage\rreturn"),
        ("authorization", "x"),
        ("BAD KEY", "x"),
        ("1ST", "x"),
    ],
)
def test_values_that_could_forge_other_lines_or_keys_are_refused(
    tmp_path: Path, key: str, value: str
) -> None:
    path = tmp_path / "demo.cred"
    with pytest.raises(ConfigError):
        write_values(path, {key: value})
    assert not path.exists()


@pytest.mark.parametrize("name", ["", "..", "../x", "a/b", ".hidden", "x" * 80, "has space"])
def test_a_credential_name_cannot_escape_the_directory(tmp_path: Path, name: str) -> None:
    with pytest.raises(ConfigError, match="invalid credential name"):
        credential_path(tmp_path, name)


def test_loading_builds_headers_from_the_layout_and_the_expiry_from_the_token(
    tmp_path: Path,
) -> None:
    token = make_jwt(exp=1_800_000_000, jti="jti-1")
    path = tmp_path / "demo.cred"
    write_values(path, {"AUTHORIZATION": bearer(token), "REGION": "r1", "FOLDER_ID": "ignored"})

    credential = load_credential(path, LAYOUT)

    assert credential.headers == {"authorization": bearer(token), "region": "r1"}
    assert credential.expires_at == datetime.fromtimestamp(1_800_000_000, tz=UTC)
    assert credential.token_id == "jti-1"
    assert credential.params == {}


def test_a_file_without_the_required_key_is_missing_that_credential(tmp_path: Path) -> None:
    path = tmp_path / "demo.cred"
    write_values(path, {"REGION": "r1"})
    with pytest.raises(CredentialMissing, match="lacks AUTHORIZATION"):
        load_credential(path, LAYOUT)


def test_a_group_readable_credential_file_triggers_a_warning_without_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "demo.cred"
    path.write_text("AUTHORIZATION=canary-secret-value\n")
    path.chmod(0o640)
    with caplog.at_level(logging.WARNING):
        read_values(path)
    assert "accessible to other users" in caplog.text
    assert "canary-secret-value" not in caplog.text


def test_layout_rejects_a_token_key_that_is_not_a_required_header() -> None:
    with pytest.raises(ValueError, match="token key"):
        CredentialLayout(
            headers={"A": "a"},
            token_key="B",
            required=frozenset({"A"}),
            host_suffixes=("x.invalid",),
        )


def test_the_raw_base64_helper_round_trips_for_the_fixtures() -> None:
    # The fixtures' unpadded encoding must be what decode_claims expects of real tokens.
    assert base64.urlsafe_b64decode(b64url(b"hello") + "=" * 3).startswith(b"hello")
    assert json.loads(base64.urlsafe_b64decode(b64url(b"{}") + "==")) == {}
