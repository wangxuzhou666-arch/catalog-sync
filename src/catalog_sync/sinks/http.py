"""HTTP/webhook sink: hands the rows to your own platform, which stores them.

The other sinks write a file or a database this tool owns; this one posts JSON to an endpoint of a
platform somebody else runs, and the platform applies it. What the platform must do with a request
(partial upsert of the owned fields only, never delete, idempotency) is the contract in
docs/platform-contract.md; ``catalog-sync mock platform serve`` is a reference receiver that
implements it, and the same contract suite as for the other sinks runs against it.

Delivery, in short:

* Rows go out in batches of ``batch_size``, one POST each, in order. Every request carries the
  fields the mapping owns (and nothing else), the list of those that keep an existing value when
  empty, the schema's name and version and the run id. The sensitive-field policy has been applied
  before the sink sees the batch, so a dropped field is not in the request at all.
* Each record has an idempotency key, sha256 of (source, source_id, a hash of its owned values).
  Each request has an ``Idempotency-Key`` header, the sha256 of its body: the same request sent
  again (a retry after a timeout, a load re-run after a crash) is the same key, and a platform that
  already applied it answers with the stored result instead of applying it twice.
* 5xx, 429 (honouring ``Retry-After``) and network errors are retried with backoff by the shared
  ``HttpClient``; 401/403 stop the run with exit code 2; any other 4xx is refused with the start of
  the platform's own explanation (secrets masked).
* The token is never in the config: ``auth_header_env`` names the environment variable that holds
  it, and a config that tries to carry one inline is refused, as the Postgres sink refuses a
  connection string.
* Plain ``http://`` is accepted for a machine's own loopback address only, unless
  ``allow_plain_http`` says otherwise (a private network between containers, say), because the
  token would otherwise cross the network in the clear.

``backup()`` takes no copy: the rows are not ours. It returns a receipt that says so, and recovery
is the platform's job. A dry run builds every request and reports how many would be sent; it sends
nothing, not even to ask the platform what would change.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from catalog_sync import __version__
from catalog_sync.connector_info import SettingKind, SettingSpec
from catalog_sync.errors import (
    ConfigError,
    HttpStatusError,
    NetworkError,
    SinkError,
    SourceShapeError,
)
from catalog_sync.http import (
    HttpClient,
    HttpSeams,
    HttpSettings,
    RateLimiter,
    is_loopback_host,
    strip_query,
)
from catalog_sync.redact import redact_text
from catalog_sync.sdk.options import OptionReader, declared_keys
from catalog_sync.sinks.base import BackupReceipt, Batch, WriteReport
from catalog_sync.sinks.http_wire import (
    IDEMPOTENCY_HEADER,
    Answer,
    Request,
    build_requests,
    combine_answers,
    parse_answer,
)
from catalog_sync.sinks.lifecycle import SinkState, utc_now

NO_SCHEME = "none"
_MAX_BATCH_SIZE = 1000
_HTTP_REDIRECT_MIN, _HTTP_REDIRECT_MAX = 300, 399
_EXCERPT_CHARS = 300
_INLINE_SECRET_OPTIONS = (
    "token",
    "api_key",
    "apikey",
    "secret",
    "password",
    "authorization",
    "bearer",
    "headers",
)
# A query parameter with one of these names would put a secret into the config file and the logs.
_SECRET_QUERY_NAMES = ("token", "key", "secret", "password", "auth", "sig", "signature")

URL_SETTING = SettingSpec(
    "url",
    SettingKind.ENDPOINT,
    "The platform's endpoint that receives the batches (POST), for example "
    "https://platform.example/api/ingest.",
    required=True,
)
AUTH_HEADER_ENV = SettingSpec(
    "auth_header_env",
    SettingKind.ENV_NAME,
    "The NAME of the environment variable that holds the platform's token (never the token). "
    "Leave empty for a platform that needs none.",
    essential=True,
)
AUTH_HEADER = SettingSpec(
    "auth_header",
    SettingKind.TEXT,
    "The request header that carries the token.",
    default="Authorization",
)
AUTH_SCHEME = SettingSpec(
    "auth_scheme",
    SettingKind.TEXT,
    f"The word before the token in that header; {NO_SCHEME} sends the token alone "
    "(for a header such as X-Api-Key).",
    default="Bearer",
)
BATCH_SIZE = SettingSpec(
    "batch_size",
    SettingKind.INTEGER,
    "Records per request.",
    default=100,
    minimum=1,
    maximum=_MAX_BATCH_SIZE,
)
MAX_REQUESTS_PER_MINUTE = SettingSpec(
    "max_requests_per_minute",
    SettingKind.INTEGER,
    "A ceiling on requests per minute to the platform.",
    default=600,
    minimum=1,
)
TIMEOUT_S = SettingSpec(
    "timeout_s",
    SettingKind.NUMBER,
    "Seconds to wait for one answer.",
    default=30.0,
)
ALLOW_PLAIN_HTTP = SettingSpec(
    "allow_plain_http",
    SettingKind.BOOLEAN,
    "Accept an http:// address that is not this machine's own. The token then crosses the "
    "network unencrypted; only for a private network you control.",
    default=False,
)
SETTINGS = (
    URL_SETTING,
    AUTH_HEADER_ENV,
    AUTH_HEADER,
    AUTH_SCHEME,
    BATCH_SIZE,
    MAX_REQUESTS_PER_MINUTE,
    TIMEOUT_S,
    ALLOW_PLAIN_HTTP,
)
_LABEL = "http sink"


@dataclass(frozen=True)
class HttpSinkSettings:
    url: str
    auth_header_env: str | None = None
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    batch_size: int = 100
    max_requests_per_minute: int = 600
    timeout_s: float = 30.0
    allow_plain_http: bool = False

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)
        is_exposed = parts.scheme == "http" and not is_loopback_host(parts.hostname or "")
        if is_exposed and not self.allow_plain_http:
            raise ConfigError(
                f"{_LABEL}: {strip_query(self.url)} is plain http, which would send the token "
                "across the network unencrypted. Use https, or set allow_plain_http = true "
                "for a private network you control"
            )
        leaked = [
            name
            for name, _ in parse_qsl(parts.query, keep_blank_values=True)
            if any(word in name.lower() for word in _SECRET_QUERY_NAMES)
        ]
        if leaked:
            raise ConfigError(
                f"{_LABEL}: the url has a query parameter that looks like a secret ({leaked[0]}). "
                "A config file and a log are no place for one; send it in a header, from an "
                "environment variable named in 'auth_header_env'"
            )

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> HttpSinkSettings:
        for name in _INLINE_SECRET_OPTIONS:
            if name in options:
                raise ConfigError(
                    f"{_LABEL}: there is no '{name}' option, because a token in a config file "
                    "leaks from it. Put the token in an environment variable and name that "
                    "variable in 'auth_header_env'"
                )
        reader = OptionReader(options, label=_LABEL)
        reader.reject_unknown(declared_keys(SETTINGS) - {"credential"})
        env_name = reader.read(AUTH_HEADER_ENV)
        return cls(
            url=str(reader.read(URL_SETTING)),  # validated: required, an http(s) address
            auth_header_env=env_name if isinstance(env_name, str) else None,
            auth_header=reader.text_of(AUTH_HEADER),
            auth_scheme=reader.text_of(AUTH_SCHEME),
            batch_size=reader.integer_of(BATCH_SIZE),
            max_requests_per_minute=reader.integer_of(MAX_REQUESTS_PER_MINUTE),
            timeout_s=reader.number_of(TIMEOUT_S),
            allow_plain_http=reader.flag_of(ALLOW_PLAIN_HTTP),
        )


class HttpSink(SinkState):
    backup_supported = False
    dry_run_predicts_counts = False

    def __init__(
        self,
        settings: HttpSinkSettings,
        *,
        seams: HttpSeams | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        super().__init__(clock)
        self.settings = settings
        self._seams = seams or HttpSeams()
        self._environ = os.environ if environ is None else environ
        self._client: HttpClient | None = None

    @classmethod
    def from_options(cls, options: Mapping[str, object], *, base_dir: Path) -> HttpSink:
        del base_dir  # nothing here is a path
        return cls(HttpSinkSettings.from_options(options))

    # --- lifecycle --------------------------------------------------------------------------

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        super().close()

    def backup(self) -> BackupReceipt:
        self._mark_backed_up()
        return BackupReceipt(
            path=None,
            rows_before=0,
            note=(
                "none taken: the rows live on the receiving platform, whose own backups and "
                "history are the way back (docs/platform-contract.md)"
            ),
        )

    def upsert(self, batch: Batch, *, dry_run: bool = False) -> WriteReport:
        self._require_backup_unless(is_dry_run=dry_run)
        origin = batch.origin
        if origin is None:
            raise SinkError(
                "the http sink needs to know where a batch came from (schema, run id); build it "
                "with record_batch(..., origin=...) as `load` does"
            )
        requests_to_send = list(build_requests(batch, origin, self.settings.batch_size))
        if dry_run:
            return self._dry_run(batch, requests_to_send)
        answers = [
            self._deliver(number, len(requests_to_send), request)
            for number, request in enumerate(requests_to_send, start=1)
        ]
        return combine_answers(answers)

    # --- sending ----------------------------------------------------------------------------

    def _dry_run(self, batch: Batch, requests_to_send: Sequence[Request]) -> WriteReport:
        note = (
            f"dry run: {len(batch.rows)} record(s) in {len(requests_to_send)} request(s) would be "
            f"POSTed to {strip_query(self.settings.url)}. Nothing was sent, so the platform's "
            "inserted/updated/unchanged counts are not known."
        )
        return WriteReport(0, 0, 0, notes=(note,))

    def _deliver(self, number: int, total: int, request: Request) -> Answer:
        where = f"request {number} of {total}"
        try:
            reply = self._http().post_json(
                self.settings.url, body=request.payload, headers={IDEMPOTENCY_HEADER: request.key}
            )
        except HttpStatusError as exc:
            raise SinkError(_refusal(where, number, exc)) from exc
        except SourceShapeError as exc:
            raise SinkError(
                f"the platform answered {where} with a success status but not with the JSON "
                f"object the contract defines ({exc})"
            ) from exc
        except NetworkError as exc:
            raise SinkError(f"{where} did not get through: {exc}. {_applied(number)}") from exc
        return parse_answer(reply, sent=request.record_count, where=where)

    def _http(self) -> HttpClient:
        if self._client is None:
            settings = self.settings
            clock = self._seams
            self._client = HttpClient(
                self._headers(),
                credentialed=True,  # no redirects: the token must not follow one to another host
                settings=HttpSettings(gap_s=0.0, timeout_s=settings.timeout_s),
                renewal_hint=self._renewal_hint(),
                seams=self._seams,
                limiter=RateLimiter(
                    settings.max_requests_per_minute, monotonic=clock.monotonic, sleep=clock.sleep
                ),
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {
            "user-agent": f"catalog-sync/{__version__}",
            "accept": "application/json",
            "content-type": "application/json",
        }
        name = self.settings.auth_header_env
        if name is None:
            return headers
        token = self._environ.get(name, "")
        if not token:
            raise ConfigError(
                f"the environment variable {name} is not set (or is empty); it must hold the "
                "platform's token for the http sink"
            )
        scheme = self.settings.auth_scheme
        headers[self.settings.auth_header.lower()] = (
            token if scheme.lower() == NO_SCHEME else f"{scheme} {token}"
        )
        return headers

    def _renewal_hint(self) -> str:
        name = self.settings.auth_header_env
        if name is None:
            return "The platform wants a token: set auth_header_env in the sink's options."
        return f"Check that ${name} holds a token the platform accepts."


# --- errors -------------------------------------------------------------------------------------


def _refusal(where: str, number: int, exc: HttpStatusError) -> str:
    excerpt = redact_text(exc.body_excerpt)[:_EXCERPT_CHARS]
    detail = f": {excerpt}" if excerpt else ""
    if _HTTP_REDIRECT_MIN <= exc.status <= _HTTP_REDIRECT_MAX:
        return (
            f"the platform answered {where} with a redirect (HTTP {exc.status}). Redirects are "
            "not followed, because the token would travel to the new address: use the final "
            f"address as 'url'. {_applied(number)}"
        )
    if exc.is_item_scoped:
        return f"the platform refused {where} (HTTP {exc.status}){detail}. {_applied(number)}"
    return f"{exc} ({where}). {_applied(number)}"


def _applied(number: int) -> str:
    """What the failure leaves behind, and that trying again is safe."""
    before = f"the {number - 1} request(s) before it were applied" if number > 1 else "none applied"
    return f"{before}. Loading again is safe: a request that was applied is not applied twice"
