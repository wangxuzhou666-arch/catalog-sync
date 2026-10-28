"""What a connector or a sink says about itself, so that people and tools can ask it.

The options of a source used to be discoverable only by reading its settings class. A
``SettingSpec`` is the public declaration of one option (its key, type, default, whether it must
be given, and a line of help), and a connector lists them next to the code that reads them: the
reader (``catalog_sync.sdk.OptionReader.read``) validates against the same declaration, so the
list cannot say one thing while the code does another. ``catalog-sync init`` asks the questions
this declares; ``catalog-sync new-connector`` generates a connector that declares its own.

A connector also registers one ``ConnectorInfo`` under the ``catalog_sync.connectors`` entry-point
group (``"<source name>" = "pkg.module:function"``, the function returning the info).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

# The NAME of an environment variable, in the usual UPPER_SNAKE form and short. A pasted secret is
# not one: a token is mixed-case or has dashes, and an all-caps key (an AWS key id) is one long
# unbroken run, which no variable name is. Both lookaheads stay in the pattern, so every place
# that matches against it (the wizard, the option reader) refuses the same things.
ENV_NAME: Final = re.compile(
    r"(?!(?:GH[POUSR]|SK_LIVE|SK_TEST|PK_LIVE|RK_LIVE)_)"  # a token's own prefix
    r"(?![A-Z0-9]{16,}\Z)"  # sixteen or more letters and digits without an underscore
    r"[A-Z_][A-Z0-9_]{0,63}"
)
ENV_NAME_RULE: Final = "capital letters, digits and underscores, at most 64, like MY_SERVICE_TOKEN"


class SettingKind(StrEnum):
    TEXT = "text"
    URL = "url"  # http(s)://host[:port], no path: the address of a web app or an API
    ENDPOINT = "endpoint"  # a full http(s) URL, path allowed, no credentials in it
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    CHOICE = "choice"  # one of `choices`
    ENV_NAME = "env_name"  # the NAME of a variable that holds a secret, never the secret itself
    TABLE = "table"  # structured; edited in the config file, never asked for


@dataclass(frozen=True)
class SettingSpec:
    """One option of a source, a credential kind or a sink.

    ``required`` means there is no usable default: the option has to be given. ``essential`` means
    it has a default but nearly everybody sets it (an address), so ``init`` asks about it. Every
    other option keeps its default unless the config file says otherwise.
    """

    key: str
    kind: SettingKind
    help: str
    default: object = None
    required: bool = False
    essential: bool = False
    choices: tuple[str, ...] = ()
    minimum: int = 0
    maximum: int | None = None

    def __post_init__(self) -> None:
        if (self.kind is SettingKind.CHOICE) != bool(self.choices):
            raise ValueError(
                f"setting {self.key!r}: 'choices' go with the choice kind, and only it"
            )
        if self.required and self.default is not None:
            raise ValueError(f"setting {self.key!r}: a required setting has no default")
        if self.kind is SettingKind.TABLE and (self.required or self.essential):
            raise ValueError(f"setting {self.key!r}: a table is edited in the file, not asked for")
        if self.default is not None and not _fits(self.kind, self.default):
            raise ValueError(
                f"setting {self.key!r}: the default {self.default!r} is not a {self.kind} value"
            )

    @property
    def is_asked(self) -> bool:
        """Whether ``init`` asks for it."""
        return self.required or self.essential


@dataclass(frozen=True)
class ConnectorInfo:
    """What ``catalog-sync init`` needs to set up a source plugin without knowing its code.

    ``credential`` is the name of the credential kind the source's ``credential`` option points
    at. ``credential_settings`` are the keys of ``settings`` that the credential kind takes as
    well (the address of the service, usually), so they are asked once and written to both
    tables. ``mapping`` is the mapping file that fits the source, and ``terms_url`` where its
    terms of service are, when that follows from the settings (``{base_url}/terms``): a hint
    shown while the compliance block is filled in, never written without being confirmed.
    """

    summary: str
    credential: str
    settings: tuple[SettingSpec, ...]
    mapping: Path
    credential_settings: tuple[str, ...] = ()
    terms_url: str | None = None

    def __post_init__(self) -> None:
        keys = [spec.key for spec in self.settings]
        if len(set(keys)) != len(keys):
            raise ValueError("a setting is declared twice")
        stray = sorted(set(self.credential_settings) - set(keys))
        if stray:
            raise ValueError(f"credential_settings {stray} are not declared settings")


def _fits(kind: SettingKind, value: object) -> bool:
    """Whether `value` is the Python type the kind is read as (a bool is never a number)."""
    if kind is SettingKind.BOOLEAN:
        return isinstance(value, bool)
    if kind in (SettingKind.INTEGER, SettingKind.NUMBER):
        wanted = int if kind is SettingKind.INTEGER else (int, float)
        return isinstance(value, wanted) and not isinstance(value, bool)
    if kind is SettingKind.TABLE:
        return False
    return isinstance(value, str)
