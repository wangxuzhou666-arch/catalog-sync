"""One typed reader for the ``[sources.NAME]`` options of a connector.

Values arrive from TOML (typed) or from ``--option KEY=VALUE`` (always strings), so every scalar is
accepted in both forms. A wrong value is a ``ConfigError`` that names the source and the key, and
``reject_unknown`` makes a misspelt option an error instead of a silent fallback to the default.

The request-pacing pair lives here because every networked connector has it: the CLI passes the
compliance block's ``max_requests_per_minute`` into the options, and ``request_gap_s`` may never be
shorter than that rate allows (the shared ``HttpClient`` enforces the rate itself; refusing a
contradicting gap at configuration time just tells the operator sooner).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final, cast
from urllib.parse import SplitResult, urlsplit

from catalog_sync.connector_info import ENV_NAME, ENV_NAME_RULE, SettingKind, SettingSpec
from catalog_sync.credentials.base import CredentialHandle
from catalog_sync.errors import ComplianceError, ConfigError

_SECONDS_PER_MINUTE = 60
CREDENTIAL_KEY: Final = "credential"
_FLAGS: Final = {"true": True, "false": False}

# Set by the CLI from the compliance block on every networked source; never typed in by hand.
MAX_REQUESTS_PER_MINUTE = SettingSpec(
    "max_requests_per_minute",
    SettingKind.INTEGER,
    "The request ceiling per minute; taken from the compliance block, not set here.",
    minimum=1,
)


def request_gap_setting(default_s: float) -> SettingSpec:
    """The pause between two requests, which a connector may default differently."""
    return SettingSpec(
        "request_gap_s",
        SettingKind.NUMBER,
        "Seconds to wait between two requests; never shorter than the compliance ceiling allows.",
        default=default_s,
    )


def declared_keys(settings: Iterable[SettingSpec]) -> frozenset[str]:
    """Every option a source accepts: its declared settings and the credential's name."""
    return frozenset({CREDENTIAL_KEY, *(spec.key for spec in settings)})


class OptionReader:
    """``label`` is the source's plugin name; it prefixes every error."""

    def __init__(self, options: Mapping[str, object], *, label: str) -> None:
        self._options = options
        self.label = label

    def get(self, key: str, default: object = None) -> object:
        return self._options.get(key, default)

    def reject_unknown(self, allowed: Iterable[str]) -> None:
        unknown = sorted(set(self._options) - set(allowed))
        if unknown:
            raise ConfigError(f"{self.label}: unknown option(s) {unknown}")

    # --- reading by declaration -------------------------------------------------------------

    def read(self, spec: SettingSpec) -> object:
        """The option `spec` declares, validated against it; its default when absent.

        What the config file holds (typed) and what ``--option KEY=VALUE`` gives (a string) are
        both accepted. A required option that is absent is an error naming it.
        """
        if spec.key not in self._options:
            if spec.required:
                raise ConfigError(f"{self.label}: '{spec.key}' is required ({spec.help})")
            return spec.default
        readers = {
            SettingKind.TEXT: self.text_of,
            SettingKind.URL: self.url_of,
            SettingKind.ENDPOINT: self.endpoint_of,
            SettingKind.INTEGER: self.integer_of,
            SettingKind.NUMBER: self.number_of,
            SettingKind.BOOLEAN: self.flag_of,
            SettingKind.CHOICE: self.choice_of,
            SettingKind.ENV_NAME: self.env_name_of,
            SettingKind.TABLE: self.table_of,
        }
        return readers[spec.kind](spec)

    def text_of(self, spec: SettingSpec) -> str:
        return self.text(spec.key, _default_text(spec))

    def url_of(self, spec: SettingSpec) -> str:
        """An ``http(s)://host[:port]`` address with no path; a trailing slash is dropped."""
        default = _default_text(spec)
        value = self.text(spec.key, default).rstrip("/")
        parts = self._split_address(spec.key, value)
        # Checked first, and the value not repeated: it holds a password.
        self._refuse_userinfo(spec.key, parts)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.path or parts.query:
            raise ConfigError(f"{self.label}: '{spec.key}' must look like {default}, got {value!r}")
        return value

    def endpoint_of(self, spec: SettingSpec) -> str:
        """A full http(s) URL. Credentials in it (``user:password@host``) are refused unechoed:
        a URL ends up in logs and in the config file, which are not places for a secret."""
        value = self.text(spec.key, _default_text(spec) if spec.default else "-")
        parts = self._split_address(spec.key, value)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.fragment:
            raise ConfigError(
                f"{self.label}: '{spec.key}' must be an http(s) address such as "
                "https://platform.example/api/ingest"
            )
        self._refuse_userinfo(spec.key, parts)
        return value

    def _split_address(self, key: str, value: str) -> SplitResult:
        try:
            return urlsplit(value)
        except ValueError as exc:  # e.g. an unclosed '[' of an IPv6 host
            raise ConfigError(f"{self.label}: '{key}' is not a valid http(s) address") from exc

    def _refuse_userinfo(self, key: str, parts: SplitResult) -> None:
        if parts.username is not None or parts.password is not None:
            raise ConfigError(
                f"{self.label}: '{key}' must not carry a user name or password; "
                "put the token in an environment variable instead"
            )

    def integer_of(self, spec: SettingSpec) -> int:
        # SettingSpec has checked that a default is an int; a required setting has none, and is
        # then given or refused by `read` before it gets here.
        default = cast("int", spec.minimum if spec.default is None else spec.default)
        return self.integer(spec.key, default, minimum=spec.minimum, maximum=spec.maximum)

    def number_of(self, spec: SettingSpec) -> float:
        return self.number(spec.key, float(cast("float", spec.default)))

    def flag_of(self, spec: SettingSpec) -> bool:
        value = self._options.get(spec.key, spec.default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in _FLAGS:
            return _FLAGS[value.strip().lower()]
        raise ConfigError(f"{self.label}: '{spec.key}' must be true or false, got {value!r}")

    def choice_of(self, spec: SettingSpec) -> str:
        value = self._options.get(spec.key, spec.default)
        if value not in spec.choices:
            raise ConfigError(
                f"{self.label}: '{spec.key}' must be one of {', '.join(spec.choices)}, "
                f"got {value!r}"
            )
        return str(value)

    def env_name_of(self, spec: SettingSpec) -> str:
        """The name of an environment variable. Anything that does not look like a name is
        refused without being echoed: it may well be the secret itself, pasted by mistake."""
        value = self._options.get(spec.key, spec.default)
        if not isinstance(value, str) or ENV_NAME.fullmatch(value) is None:
            raise ConfigError(
                f"{self.label}: '{spec.key}' must be the NAME of an environment variable "
                f"({ENV_NAME_RULE}), not the secret itself. Put the secret in that variable"
            )
        return value

    def table_of(self, spec: SettingSpec) -> Mapping[str, object]:
        return self.table(spec.key) or {}

    def pacing(self, gap: SettingSpec) -> tuple[float, int | None]:
        """The pause between requests and the per-minute ceiling, which constrains the pause."""
        ceiling = self.request_ceiling()
        return self.request_gap(float(cast("float", gap.default)), ceiling=ceiling), ceiling

    def credential(self) -> CredentialHandle:
        """The handle the CLI put in place of the ``credential = "<name>"`` option."""
        value = self._options.get("credential")
        if not isinstance(value, CredentialHandle):
            raise ConfigError(
                f'{self.label} needs `credential = "<name>"` naming a credential '
                "(see `catalog-sync login --help`)"
            )
        return value

    def text(self, key: str, default: str) -> str:
        value = self._options.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (str, int)) or str(value) == "":
            raise ConfigError(f"{self.label}: '{key}' must be a non-empty string, got {value!r}")
        return str(value)

    def integer(self, key: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
        value = self._options.get(key, default)
        try:
            number = (
                int(value)
                if isinstance(value, (str, int)) and not isinstance(value, bool)
                else None
            )
        except ValueError:
            number = None
        if number is None or number < minimum or (maximum is not None and number > maximum):
            bound = f"at least {minimum}" if maximum is None else f"between {minimum} and {maximum}"
            raise ConfigError(f"{self.label}: '{key}' must be an integer {bound}, got {value!r}")
        return number

    def number(self, key: str, default: float) -> float:
        value = self._options.get(key, default)
        try:
            number = (
                float(value)
                if isinstance(value, (str, int, float)) and not isinstance(value, bool)
                else -1
            )
        except ValueError:
            number = -1
        if not number >= 0:  # also rejects NaN
            raise ConfigError(f"{self.label}: '{key}' must be a number >= 0, got {value!r}")
        return number

    def table(self, key: str) -> Mapping[str, object] | None:
        value = self._options.get(key)
        if value is not None and not isinstance(value, dict):
            raise ConfigError(f"{self.label}: '{key}' must be a table")
        return value

    def base_url(self, default: str) -> str:
        """The ``base_url`` option (see ``url_of``), for callers that declare no setting."""
        return self.url_of(SettingSpec("base_url", SettingKind.URL, "", default=default))

    def request_ceiling(self) -> int | None:
        """``max_requests_per_minute`` as the CLI passes it down, or None when not set."""
        if self._options.get("max_requests_per_minute") is None:
            return None
        return self.integer("max_requests_per_minute", 1, minimum=1)

    def request_gap(self, default: float, *, ceiling: int | None) -> float:
        """The pause between requests. Under a declared ceiling it can never be shorter than the
        ceiling allows: an unset gap is raised to it, an explicit shorter one is refused."""
        if ceiling is None:
            return self.number("request_gap_s", default)
        minimum = _SECONDS_PER_MINUTE / ceiling
        if "request_gap_s" not in self._options:
            return max(default, minimum)
        gap = self.number("request_gap_s", default)
        if gap < minimum:
            raise ComplianceError(
                f"{self.label}: request_gap_s = {gap:g} is shorter than 60 / "
                f"max_requests_per_minute = {minimum:g} s"
            )
        return gap


def _default_text(spec: SettingSpec) -> str:
    return cast("str", spec.default)
