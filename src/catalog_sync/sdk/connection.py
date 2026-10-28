"""The HTTP side of a source: an API client, an anonymous asset client, one rate window.

Pictures are fetched *without* the credential's headers: they usually come from a storage or CDN
host that must never see the bearer token. The two clients still share one ``RateLimiter``,
because the ceiling the operator declared is for the whole source, not per connection.

Nothing is created until it is used, so a source can be built (and its options checked) without a
credential being read or a socket opened.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from catalog_sync import __version__
from catalog_sync.credentials.base import Credential, CredentialHandle
from catalog_sync.http import HttpClient, HttpSeams, HttpSettings, RateLimiter

# Where the tool lives, in the "(+URL)" form a server operator expects of an automated client, so
# that a log line can be traced to the software. PLACEHOLDER until the repository has a public
# address: change it here and nowhere else.
PROJECT_URL = "https://example.invalid/catalog-sync"
# What a connector says about itself by default: the tool and its version, never a person or an
# account. A connector may send a browser string instead only as an exception its compliance notes
# declare and give a reason for (docs/compliance.md states the policy; a connector package lists
# its own exceptions in its README).
USER_AGENT = f"catalog-sync/{__version__} (+{PROJECT_URL})"


class Connection:
    def __init__(
        self,
        *,
        credential: CredentialHandle,
        api_headers: Callable[[Credential], Mapping[str, str]],
        asset_headers: Mapping[str, str],
        gap_s: float,
        max_requests_per_minute: int | None,
        seams: HttpSeams | None = None,
    ) -> None:
        self._handle = credential
        self._api_headers = api_headers
        self._asset_headers = asset_headers
        self._gap_s = gap_s
        self._seams = seams
        self._limiter = _limiter(max_requests_per_minute, seams)
        self._credential: Credential | None = None
        self._api: HttpClient | None = None
        self._assets: HttpClient | None = None

    def credential(self) -> Credential:
        """The stored credential, read on first use."""
        if self._credential is None:
            self._credential = self._handle.load()
        return self._credential

    def api(self) -> HttpClient:
        """The client that carries the credential. It does not follow redirects, so the
        credential's headers cannot be replayed to another host."""
        if self._api is None:
            self._api = HttpClient(
                self._api_headers(self.credential()),
                credentialed=True,
                settings=HttpSettings(gap_s=self._gap_s),
                renewal_hint=self._handle.renewal_hint,
                seams=self._seams,
                limiter=self._limiter,
            )
        return self._api

    def assets(self) -> HttpClient:
        """The client for pictures and other downloads: no credential headers."""
        if self._assets is None:
            self._assets = HttpClient(
                self._asset_headers,
                credentialed=False,
                settings=HttpSettings(gap_s=self._gap_s),
                seams=self._seams,
                limiter=self._limiter,
            )
        return self._assets


def _limiter(max_requests_per_minute: int | None, seams: HttpSeams | None) -> RateLimiter | None:
    if max_requests_per_minute is None:
        return None
    clock = seams or HttpSeams()
    return RateLimiter(max_requests_per_minute, monotonic=clock.monotonic, sleep=clock.sleep)
