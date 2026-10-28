"""Exception hierarchy. The CLI maps each class to a process exit code."""

from __future__ import annotations

from typing import ClassVar

# Not an exception: a pull or run that finished with some items failed (cli_pipeline.run_profile).
EXIT_COMPLETED_WITH_FAILURES = 5


class CatalogSyncError(Exception):
    """Base class: anything the tool raises on purpose. Exit code 1."""

    exit_code: ClassVar[int] = 1


class AuthExpired(CatalogSyncError):
    """The service rejected our credential (HTTP 401/403). Renewing it is the operator's job."""

    exit_code: ClassVar[int] = 2


class CredentialMissing(CatalogSyncError):
    """No credential is stored (or exported) for the requested source."""

    exit_code: ClassVar[int] = 2


class SourceShapeError(CatalogSyncError):
    """A response did not have the structure the plugin was written against."""


class ItemFailed(CatalogSyncError):
    """One item could not be fetched; the run records it and moves on to the next item."""

    def __init__(self, source_id: str, reason: str) -> None:
        super().__init__(f"{source_id}: {reason}")
        self.source_id = source_id
        self.reason = reason


class ConfigError(CatalogSyncError):
    """The configuration file, a CLI option or a plugin registration is invalid."""


class ComplianceError(CatalogSyncError):
    """A source's compliance declaration is missing or invalid, or its pacing contradicts it.

    The tool refuses to contact the service until the operator has stated who owns the data, on
    what authority it is read, and how fast it may be read.
    """

    exit_code: ClassVar[int] = 4


class FuseTripped(CatalogSyncError):
    """Too many consecutive item failures: the service or the credential is probably broken."""

    exit_code: ClassVar[int] = 3


class RecordFormatError(CatalogSyncError):
    """A stored record (JSON object or JSONL row) does not match the schema it claims to follow."""


class SnapshotError(CatalogSyncError):
    """A run snapshot is missing, unsafe to write, or fails its integrity check."""


class SinkError(CatalogSyncError):
    """A sink was used out of order or could not read its existing contents."""


class HttpStatusError(CatalogSyncError):
    """The service answered with an HTTP status this client does not treat as success."""

    def __init__(self, status: int, message: str, *, body_excerpt: str = "") -> None:
        super().__init__(message)
        self.status = status
        # The start of the answer's body, for a caller that talks to a service of the operator's
        # own and wants its explanation. Never part of the message: bodies are not logged.
        self.body_excerpt = body_excerpt

    @property
    def is_item_scoped(self) -> bool:
        """A 4xx other than 429 says something about the one thing asked for (gone, malformed),
        not about the service or the credential, so a run can record it and move on."""
        return 400 <= self.status < 500 and self.status != 429


class NetworkError(CatalogSyncError):
    """The connection failed (or kept failing) after all retries."""


class ResponseTooLarge(CatalogSyncError):
    """A download exceeded the size limit the caller set before it was complete."""


class LoginFailed(CatalogSyncError):
    """A login did not produce a usable credential. Renewing it is the operator's job."""

    exit_code: ClassVar[int] = 2
