"""The credential file: ``KEY=VALUE`` lines, owner-only, replaced atomically.

One file per credential name inside the workspace's credentials directory (outside any
repository). Tokens are base64 and often contain ``=``, so a line is split on the *first* ``=``
only. Saving merges: keys the new capture does not carry keep their old values, because not every
captured request carries every header, and overwriting would turn a good file into a broken one.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path

from catalog_sync.credentials.base import Credential, CredentialLayout, renewal_hint
from catalog_sync.credentials.jwt import decode_claims
from catalog_sync.errors import ConfigError, CredentialMissing
from catalog_sync.fsutil import atomic_write_bytes, ensure_private_dir

logger = logging.getLogger(__name__)

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
_GROUP_OR_WORLD_ACCESS = 0o077


def credential_path(directory: Path, name: str) -> Path:
    """The file for credential `name`. The name becomes a file name, so it is restricted."""
    if not _NAME.fullmatch(name):
        raise ConfigError(
            f"invalid credential name {name!r}: use letters, digits, '.', '_' and '-' only"
        )
    return directory / f"{name}.cred"


def read_values(path: Path) -> dict[str, str]:
    """The stored KEY=VALUE pairs. Raises CredentialMissing when absent, empty or malformed."""
    name = path.stem
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CredentialMissing(f"no credential stored for {name!r}. {renewal_hint(name)}") from exc
    if path.stat().st_mode & _GROUP_OR_WORLD_ACCESS:
        logger.warning("credential file %s is accessible to other users; run: chmod 600", path)
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator or not key.strip():
            # The line itself is never quoted: it may be most of a token.
            raise CredentialMissing(
                f"credential {name!r} is malformed at line {number}. {renewal_hint(name)}"
            )
        values[key.strip()] = value.strip()
    if not values:
        raise CredentialMissing(f"credential {name!r} is empty. {renewal_hint(name)}")
    return values


def write_values(path: Path, updates: Mapping[str, str]) -> None:
    """Merge `updates` into the file (old keys survive) and write it 0600, atomically."""
    for key, value in updates.items():
        if not _KEY.fullmatch(key):
            raise ConfigError(f"invalid credential key {key!r}: expected UPPER_SNAKE_CASE")
        if "\n" in value or "\r" in value:
            raise ConfigError(f"the value for {key} spans several lines; a credential cannot")
    try:
        merged = read_values(path)
    except CredentialMissing:
        merged = {}  # no file yet, or one too broken to keep anything from
    merged.update(updates)
    ensure_private_dir(path.parent)
    atomic_write_bytes(path, "".join(f"{k}={v}\n" for k, v in merged.items()).encode("utf-8"))


def load_credential(path: Path, layout: CredentialLayout) -> Credential:
    """The stored values as a Credential: headers by the layout, expiry from the token."""
    return credential_from_values(read_values(path), layout, name=path.stem)


def credential_from_values(
    values: Mapping[str, str], layout: CredentialLayout, *, name: str
) -> Credential:
    missing = sorted(layout.required - values.keys())
    if missing:
        raise CredentialMissing(
            f"credential {name!r} lacks {', '.join(missing)}. {renewal_hint(name)}"
        )
    claims = decode_claims(values.get(layout.token_key, ""))
    return Credential(
        headers={layout.headers[key]: values[key] for key in layout.headers if key in values},
        params={},
        expires_at=claims.expires_at,
        token_id=claims.token_id,
    )
