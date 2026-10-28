"""Turn a browser's "Copy as cURL" text into a stored credential.

The text a browser copies contains far more than the credential: session cookies, tracking
headers, the request body. This parser reads the command, throws everything away except the
headers the service's layout names, and never hands a cookie to the caller: cookie headers and
``--cookie`` arguments are dropped while parsing, so no later step can leak what it never saw.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from catalog_sync.credentials.base import Credential, CredentialLayout
from catalog_sync.credentials.store import load_credential, write_values
from catalog_sync.errors import LoginFailed

# Options that consume the next token as their value. Their values (a request body, a cookie
# jar, a user name) are none of our business and must not be mistaken for the URL.
_VALUE_OPTIONS = frozenset(
    {
        "-X", "--request", "-d", "--data", "--data-raw", "--data-binary", "--data-ascii",
        "--data-urlencode", "-b", "--cookie", "-c", "--cookie-jar", "-e", "--referer", "-A",
        "--user-agent", "-u", "--user", "-F", "--form", "-x", "--proxy", "-m", "--max-time",
        "--connect-timeout", "--resolve", "-o", "--output", "-w", "--write-out",
    }
)  # fmt: skip
# Chrome writes values with unusual characters as bash ANSI-C strings: $'...'.
_ANSI_C_STRING = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.DOTALL)
_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.DOTALL)
_SIMPLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t"}
_UNICODE_ESCAPE_LENGTH = 5  # "u" + 4 hex digits
_HEX_ESCAPE_LENGTH = 3  # "x" + 2 hex digits


@dataclass(frozen=True)
class ParsedCurl:
    url: str | None
    headers: Mapping[str, str]  # lower-case names; never contains a cookie


def parse_curl(text: str) -> ParsedCurl:
    """Parse a bash-style ``curl`` command. Windows ``cmd`` quoting (``^"``) is not supported."""
    joined = text.replace("\\\r\n", " ").replace("\\\n", " ")
    unquoted = _ANSI_C_STRING.sub(lambda m: shlex.quote(_unescape(m.group(1))), joined)
    try:
        tokens = shlex.split(unquoted)
    except ValueError as exc:
        raise LoginFailed(
            "the pasted text is not a complete shell command (unbalanced quotes). Copy the "
            "request again with 'Copy as cURL (bash)'."
        ) from exc
    if not tokens or tokens[0] != "curl":
        raise LoginFailed("the pasted text does not start with 'curl'. Use 'Copy as cURL (bash)'.")
    return _read_arguments(iter(tokens[1:]))


def credential_values(parsed: ParsedCurl, layout: CredentialLayout) -> dict[str, str]:
    """The layout's keys found in a parsed command; anything else is dropped."""
    _require_known_host(parsed.url, layout)
    values: dict[str, str] = {}
    for header, value in parsed.headers.items():
        key = layout.key_for_header(header)
        if key is not None and value:
            values[key] = value
    missing = sorted(layout.required - values.keys())
    if missing:
        raise LoginFailed(
            f"the pasted request carries no {', '.join(missing)} header. Copy a request that was "
            "made while logged in (an API call, not the page itself)."
        )
    return values


class CurlPasteProvider:
    """Stores a credential from pasted text. It cannot renew one by itself."""

    def __init__(self, *, path: Path, layout: CredentialLayout, pasted: str | None = None) -> None:
        self._path = path
        self._layout = layout
        self._pasted = pasted

    def load(self) -> Credential:
        return load_credential(self._path, self._layout)

    def refresh(self, *, force: bool) -> Credential:
        # `force` changes nothing here: pasting a command is already an explicit replacement.
        del force
        if self._pasted is None:
            raise LoginFailed("nothing to store: pass the copied cURL command (--paste-curl)")
        values = credential_values(parse_curl(self._pasted), self._layout)
        write_values(self._path, values)
        return self.load()


def _read_arguments(arguments: Iterator[str]) -> ParsedCurl:
    url: str | None = None
    headers: dict[str, str] = {}
    for argument in arguments:
        if argument in ("-H", "--header"):
            _add_header(headers, next(arguments, ""))
        elif argument == "--url":
            url = next(arguments, None)
        elif argument in _VALUE_OPTIONS:
            next(arguments, None)
        elif argument.startswith("-"):
            continue  # a flag without a value: --compressed, -s, --insecure, --cookie=..., ...
        elif url is None:
            url = argument
    return ParsedCurl(url, headers)


def _add_header(headers: dict[str, str], raw: str) -> None:
    name, separator, value = raw.partition(":")
    lowered = name.strip().lower()
    if separator and lowered and lowered != "cookie":
        headers[lowered] = value.strip()


def _unescape(body: str) -> str:
    def replace(match: re.Match[str]) -> str:
        code = match.group(1)
        if code[0] == "u" and len(code) == _UNICODE_ESCAPE_LENGTH:
            return chr(int(code[1:], 16))
        if code[0] == "x" and len(code) == _HEX_ESCAPE_LENGTH:
            return chr(int(code[1:], 16))
        return _SIMPLE_ESCAPES.get(code, code)

    return _ESCAPE.sub(replace, body)


def _require_known_host(url: str | None, layout: CredentialLayout) -> None:
    """Refuse a command for another site: its token would later be sent to this service."""
    host = urlsplit(url).hostname if url else None
    if host is None:
        raise LoginFailed("the pasted command has no URL")
    if not any(host == s or host.endswith("." + s) for s in layout.host_suffixes):
        raise LoginFailed(
            f"the pasted command is for {host}, not for {' / '.join(layout.host_suffixes)}"
        )
