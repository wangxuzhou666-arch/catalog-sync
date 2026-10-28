"""Filesystem helpers shared by the snapshot store, sinks and the credential store."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write `content` so that readers see either the old file or the whole new one.

    The temp file lives next to the target (same filesystem, so the rename is atomic) and is
    created by mkstemp, i.e. mode 0600: cached third-party data and credentials stay private.
    Durability of the rename itself across power loss is out of scope; a killed process is not.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    finally:
        # After a successful replace the temp name no longer exists, so this is a no-op.
        tmp_path.unlink(missing_ok=True)


def ensure_private_dir(path: Path) -> None:
    """Create `path` readable by the owner only (0700), tightening an existing directory too.

    For anything that holds a live login: credential files and a browser profile.
    """
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
