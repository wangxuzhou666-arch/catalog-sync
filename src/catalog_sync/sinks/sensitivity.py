"""What a load does with fields a mapping marked ``sensitive: true``.

The policy is a sink option, ``sensitive_fields``, so one mapping can feed an internal store that
keeps everything and a shared one that must not:

* ``drop`` (the default): the field is not written at all. It leaves the batch's owned set, so a
  value a store already holds is not touched either.
* ``hash``: the value is replaced by ``sha256:<hex>`` of the workspace salt plus the value. The
  same value hashes the same way within one workspace, so rows can still be matched and counted,
  but the value cannot be read back, and without the salt (a 0600 file in the workspace) it cannot
  be looked up in a table of guesses either. A null stays null.
* ``keep``: written as it is. The operator must ask for this explicitly.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import replace
from enum import StrEnum
from pathlib import Path

from catalog_sync.errors import ConfigError
from catalog_sync.fsutil import ensure_private_dir
from catalog_sync.models import is_empty_json
from catalog_sync.schema import FieldKind
from catalog_sync.sinks.base import Batch
from catalog_sync.snapshot import canonical_json, sha256_hex

OPTION = "sensitive_fields"
_SALT_BYTES = 32


class SensitivePolicy(StrEnum):
    DROP = "drop"
    HASH = "hash"
    KEEP = "keep"


def split_policy(options: Mapping[str, object]) -> tuple[SensitivePolicy, dict[str, object]]:
    """Take `sensitive_fields` out of a sink's options; the sink never sees it."""
    remaining = {key: value for key, value in options.items() if key != OPTION}
    raw = options.get(OPTION, SensitivePolicy.DROP.value)
    policies = {policy.value: policy for policy in SensitivePolicy}
    if isinstance(raw, str) and raw in policies:
        return policies[raw], remaining
    raise ConfigError(f"sink option {OPTION} must be one of {', '.join(policies)}, got {raw!r}")


def workspace_salt(path: Path) -> bytes:
    """The workspace's hashing salt, created on first use and then never changed.

    The salt is written to a private temp file and hard-linked into place, which fails if the
    name already exists. Two runs starting together therefore cannot each keep a different salt
    (the same value would then hash differently depending on which run saw it first), and a
    reader never sees a half-written file.
    """
    ensure_private_dir(path.parent)
    if not path.exists():
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".salt.")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(secrets.token_bytes(_SALT_BYTES).hex() + "\n")
            with contextlib.suppress(FileExistsError):
                os.link(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    try:
        return bytes.fromhex(path.read_text(encoding="ascii").strip())
    except ValueError as exc:
        raise ConfigError(f"the hash salt file {path.name} is corrupt; do not edit it") from exc


def _hashed(value: object, salt: bytes) -> str | None:
    """Nothing (null, an empty list or object) stays null; there is no value to protect."""
    return None if is_empty_json(value) else hash_value(value, salt)


def hash_value(value: object, salt: bytes) -> str:
    return "sha256:" + sha256_hex(salt + canonical_json(value))


def apply_policy(
    batch: Batch,
    sensitive: frozenset[str],
    policy: SensitivePolicy,
    salt: Callable[[], bytes],
) -> Batch:
    """The batch as the sink may see it. `salt` is only called when something is hashed."""
    affected = sensitive & batch.owned
    if policy is SensitivePolicy.KEEP or not affected:
        return batch
    if policy is SensitivePolicy.DROP:
        return replace(
            batch,
            rows=[{k: v for k, v in row.items() if k not in affected} for row in batch.rows],
            owned=batch.owned - affected,
            kinds={k: v for k, v in batch.kinds.items() if k not in affected},
            keep_existing_if_null=batch.keep_existing_if_null - affected,
        )
    secret = salt()
    return replace(
        batch,
        rows=[
            {k: (_hashed(v, secret) if k in affected else v) for k, v in row.items()}
            for row in batch.rows
        ],
        kinds={k: (FieldKind.TEXT if k in affected else v) for k, v in batch.kinds.items()},
    )
