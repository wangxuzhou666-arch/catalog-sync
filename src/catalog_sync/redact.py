"""Masking secrets in anything the tool prints or logs.

The code is written not to put secrets in messages (URLs are logged without their query, header
values never, exceptions name the thing that failed and not its content). This module is the
second line: whatever still slips into a message, because a remote service echoed it or a plugin
formatted an object carelessly, is masked on the way out.

What counts as a secret is a shape, not a list of known values:

* a JSON Web Token (three base64url parts starting ``eyJ``) and a ``Bearer`` / ``Basic`` credential;
* the value of an ``Authorization``, ``Cookie``, ``Set-Cookie``, ``X-Api-Key`` or ``X-Auth-Token``
  header, in ``Name: value`` text or in a dict/JSON rendering;
* the query string of any URL (signed download links keep their signature there);
* ``token=``, ``password=``, ``signature=`` and similar pairs outside a URL, written ``k=v``,
  ``k: v``, ``"k": "v"`` (JSON) or ``'k': 'v'`` (a dict's repr);
* the password in ``scheme://user:password@host``, whatever characters it has but ``@``.

Masking is lossy on purpose and idempotent. It cannot find a secret that has no recognisable
shape, so it complements, and does not replace, keeping secrets out of messages in the first place.

Where it is applied: `redacted_logging` (used by the CLI) masks every log record of the package at
the moment it is created, and every record that reaches the CLI's stderr handler, whatever its
origin. A filter on the package's own logger would not do: Python does not apply a logger's filter
to records of its child loggers, and every module logs under its own name.
"""

from __future__ import annotations

import logging
import re
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final, TextIO

PACKAGE: Final = "catalog_sync"
MASK: Final = "[redacted]"

_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
# 16 characters: long enough that the word after "bearer" in prose is not taken for a token.
_SCHEME_CREDENTIAL = re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{16,}")
_SENSITIVE_HEADER = re.compile(
    r"""(?ix)
    (\b(?:proxy-)?authorization | \b(?:set-)?cookie | \bx-api-key | \bx-auth-token)
    (["']?\s*[:=]\s*["']?)
    [^\r\n"']+
    """
)
_URL_QUERY = re.compile(r"(?i)(\bhttps?://[^\s?#'\"<>]+)\?[^\s'\"<>]*")
# The value is a quoted string (escapes honoured, closing quote optional so a truncated message is
# still masked to its end) or a run up to the next space, '&' or quote. A value that is already the
# mask is skipped, which keeps masking idempotent.
_SECRET_PAIR = re.compile(
    r"(?i)\b(access[_-]?token|id[_-]?token|refresh[_-]?token|token|signature|sig|api[_-]?key"
    r"|apikey|password|passwd|secret|x-amz-signature|x-amz-credential|x-amz-security-token)"
    r"""(["']?\s*[:=]\s*)(?!\[redacted\])"""
    r"""(?:"(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?|[^&\s'"]+)"""
)
# A password may hold '/', ':' and '?': it ends at the '@' that starts the host.
_URL_PASSWORD = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]+):[^\s@]+@")


def redact_text(text: str) -> str:
    """`text` with everything that looks like a secret replaced by a mask."""
    text = _SENSITIVE_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    text = _SCHEME_CREDENTIAL.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    text = _JWT.sub(MASK, text)
    text = _URL_QUERY.sub(lambda m: f"{m.group(1)}?{MASK}", text)
    text = _SECRET_PAIR.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    return _URL_PASSWORD.sub(lambda m: f"{m.group(1)}:{MASK}@", text)


class RedactingFilter(logging.Filter):
    """Masks a record's message, its traceback and its stack, in place.

    A record that holds nothing secret is left exactly as it was (the message keeps its format
    arguments, which other handlers may group by).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            # Arguments that do not fit the format string: logging reports that itself when a
            # handler formats the record, and a logging call must never raise because of it.
            return True
        masked = redact_text(message)
        if masked != message:
            record.msg, record.args = masked, None
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        return True


class _StderrHandler(logging.StreamHandler[TextIO]):
    """Writes to whatever ``sys.stderr`` is at the moment of the record, not when it was built
    (the standard library's own last-resort handler does the same)."""

    def __init__(self) -> None:
        logging.Handler.__init__(self, logging.WARNING)

    @property
    def stream(self) -> TextIO:  # type: ignore[override]
        return sys.stderr


def _is_package_logger(name: str) -> bool:
    return name == PACKAGE or name.startswith(PACKAGE + ".")


@contextmanager
def redacted_logging() -> Iterator[None]:
    """For the duration of the block, nothing the package logs or stderr shows is unmasked.

    Two layers, because they cover different records: a record factory masks the package's own
    records as they are created, so even a test's log capture or a library user's handler sees the
    masked text; a stderr handler with the same filter prints warnings and errors from any logger,
    including libraries, and masks them too. Both are removed again on exit.
    """
    redactor = RedactingFilter()
    previous = logging.getLogRecordFactory()

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if _is_package_logger(record.name):
            redactor.filter(record)
        return record

    handler = _StderrHandler()
    handler.addFilter(redactor)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    logging.setLogRecordFactory(factory)
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        logging.setLogRecordFactory(previous)
