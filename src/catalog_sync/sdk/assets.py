"""Pictures: which URL a record's picture comes from, and fetching it into the byte budget.

Where a picture comes from is written as ``list.<path>`` (the listing row) or ``detail.<path>``
(the detail record) plus a path like ``thumbs[0].url``; the first expression that yields a
non-empty string wins. The two are read side by side because neither record is a superset of the
other (the row may have the picture and the detail not, or the reverse).

Two things are deliberate here:

* The URL the *stored* ``AssetRef`` carries has no query string: a signed link keeps its signature
  there, and a signature must not travel into a record or a database. The download needs it back,
  so the full URL is remembered in memory, per item and role. Another process (a resumed run) has
  only the stripped one, and a signed link then fails loudly instead of fetching something else.
* Which hosts a picture may come from is a policy the connector states (``UrlRule``): a listing
  that points somewhere unexpected is a reason to stop, not to fetch.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

from catalog_sync.errors import SourceShapeError
from catalog_sync.http import is_private_host, strip_query
from catalog_sync.images import MAX_INPUT_BYTES, ImagePolicy, fit_jpeg
from catalog_sync.models import JsonObject
from catalog_sync.normalize.mapping import pick
from catalog_sync.sdk.connection import Connection
from catalog_sync.sdk.responses import item_scoped
from catalog_sync.sources.base import AssetRef, ListedItem

_HTTP_FORBIDDEN = 403
_PICTURE_HINTS = {_HTTP_FORBIDDEN: "a signed link has expired; pull again"}


@dataclass(frozen=True)
class UrlRule:
    """Which picture URLs may be fetched. ``requirement`` completes "... is not <requirement>"."""

    requirement: str
    accepts: Callable[[SplitResult], bool]


def https_only(*, allow_private_hosts: bool = False) -> UrlRule:
    """Any absolute https URL on a public host: pictures that live on a CDN or a storage host of
    their own.

    An address on this machine or on a private network (``localhost``, ``127.0.0.1``,
    ``169.254.169.254``, ``10.0.0.5``) is refused unless `allow_private_hosts` is set: a listing
    that points a picture there is trying to make this machine fetch something from inside the
    operator's network. Set it only for a service that really lives there.
    """

    def accepts(parts: SplitResult) -> bool:
        host = parts.hostname
        if parts.scheme != "https" or not host:
            return False
        return allow_private_hosts or not is_private_host(host)

    return UrlRule(
        "an absolute https URL"
        if allow_private_hosts
        else "an absolute https URL on a public host",
        accepts,
    )


def same_host(base_url: str) -> UrlRule:
    """Only the host the API itself lives on."""
    host = urlsplit(base_url).netloc
    return UrlRule("on the service's own host", lambda parts: parts.netloc == host)


class PictureAssets:
    """``roles`` maps a picture role (``thumb``) to the expressions it may come from."""

    def __init__(
        self,
        connection: Connection,
        *,
        roles: Mapping[str, tuple[str, ...]],
        url_rule: UrlRule,
        policy: ImagePolicy,
    ) -> None:
        self._connection = connection
        self._roles = roles
        self._url_rule = url_rule
        self._policy = policy
        self._download_urls: dict[tuple[str, str], str] = {}

    def refs(self, item: ListedItem, detail: JsonObject | None) -> list[AssetRef]:
        """One ref per role that has a URL. Pure: no network."""
        refs: list[AssetRef] = []
        for role, expressions in self._roles.items():
            url = self._first_url(expressions, item, detail, role)
            if url is None:
                continue
            self._download_urls[(item.source_id, role)] = url
            refs.append(AssetRef(item.source_id, role, strip_query(url)))
        return refs

    def fetch(self, ref: AssetRef) -> bytes:
        """The picture as a JPEG within the policy's byte budget."""
        url = self._download_urls.get((ref.source_id, ref.role), ref.url)
        with item_scoped(ref.source_id, f"picture {ref.role!r}", hints=_PICTURE_HINTS):
            # The rule that admitted the first URL is asked again about every redirect hop.
            raw = self._connection.assets().get_bytes(
                url, max_bytes=MAX_INPUT_BYTES, redirect_rule=self._url_rule.accepts
            )
        return fit_jpeg(raw, self._policy, source_id=ref.source_id).jpeg

    def _first_url(
        self,
        expressions: tuple[str, ...],
        item: ListedItem,
        detail: JsonObject | None,
        role: str,
    ) -> str | None:
        for expression in expressions:
            scope, _, path = expression.partition(".")
            found = pick(item.payload if scope == "list" else detail, path)
            if found is None or found == "":
                continue
            if not isinstance(found, str):
                raise SourceShapeError(f"{item.source_id}: {expression} is not a string")
            if not self._url_rule.accepts(urlsplit(found)):
                # The URL itself is not quoted: it may be a signed link.
                raise SourceShapeError(
                    f"{item.source_id}: picture {role!r} is not {self._url_rule.requirement}"
                )
            return found
        return None
