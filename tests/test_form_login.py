"""Logging in with a plain HTTP request: the provider, and the `login --form` command around it."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from catalog_sync.credentials.base import Credential, CredentialLayout
from catalog_sync.credentials.form import FormLoginProvider
from catalog_sync.credentials.store import read_values
from catalog_sync.errors import CredentialMissing, LoginFailed
from tests.fixtures.tokens import bearer, make_jwt

LAYOUT = CredentialLayout(
    headers={"AUTHORIZATION": "authorization"},
    token_key="AUTHORIZATION",
    required=frozenset({"AUTHORIZATION"}),
    host_suffixes=("service.invalid",),
)
FAR_FUTURE = 2_000_000_000


def token(jti: str) -> str:
    return bearer(make_jwt(exp=FAR_FUTURE, jti=jti))


def provider(
    path: Path,
    values: Mapping[str, str],
    verify: object = None,
) -> FormLoginProvider:
    return FormLoginProvider(path=path, layout=LAYOUT, login=lambda: values, verify=verify)  # type: ignore[arg-type]


def test_the_login_result_is_stored_and_read_back(tmp_path: Path) -> None:
    path = tmp_path / "creds" / "svc.cred"
    credential = provider(path, {"AUTHORIZATION": token("one")}).refresh(force=False)
    assert credential.token_id == "one"
    assert read_values(path)["AUTHORIZATION"] == token("one")
    assert path.stat().st_mode & 0o077 == 0
    assert provider(path, {}).load().token_id == "one"


def test_force_makes_no_difference_a_form_login_always_asks_the_service(tmp_path: Path) -> None:
    path = tmp_path / "svc.cred"
    provider(path, {"AUTHORIZATION": token("one")}).refresh(force=False)
    provider(path, {"AUTHORIZATION": token("two")}).refresh(force=True)
    assert provider(path, {}).load().token_id == "two"


def test_a_credential_the_check_rejects_does_not_replace_the_stored_one(tmp_path: Path) -> None:
    path = tmp_path / "svc.cred"
    provider(path, {"AUTHORIZATION": token("good")}).refresh(force=False)
    seen: list[Credential] = []

    def reject(candidate: Credential) -> None:
        seen.append(candidate)
        raise LoginFailed("the service said no")

    with pytest.raises(LoginFailed, match="said no"):
        provider(path, {"AUTHORIZATION": token("bad")}, reject).refresh(force=False)
    assert seen[0].token_id == "bad", "the check saw the candidate"
    assert read_values(path)["AUTHORIZATION"] == token("good"), "and the old one survived"


def test_a_login_answer_missing_the_token_key_is_a_credential_error(tmp_path: Path) -> None:
    with pytest.raises(CredentialMissing, match="lacks AUTHORIZATION"):
        provider(tmp_path / "svc.cred", {"OTHER": "x"}).refresh(force=False)
    assert not (tmp_path / "svc.cred").exists()
