"""Building blocks for writing a connector (a source plugin). Public, typed, small.

A connector is a few hundred lines once these are used; ``catalog-sync new-connector NAME``
generates one that already does. What each module is for:

``options``      ``OptionReader``: typed ``[sources.NAME]`` options read against their
                 ``SettingSpec`` declarations (which ``init`` asks about); the pacing pair.
``connection``   ``Connection``: API client + anonymous asset client + one shared rate window.
``paging``       Page-number, next-page-token and offset/limit pagination, resumable.
``folders``      ``FolderWalker``: breadth-first folder tree with fuses and a saved position.
``responses``    Response checks with call context, the double-envelope detail, item-scoped errors.
``assets``       ``PictureAssets``: picture URLs from a row and its detail, fetched to a budget.
``credentials``  ``header_layout``, ``EnvAccount``, ``FormLogin``, ``verify_with_probe``.

Everything else a connector needs (``HttpClient``, ``peel_data``, the Source contract, the errors)
stays where it always was in ``catalog_sync``; this package only holds what is shared by the
shipped connectors and the generated scaffold (a piece used by one connector plus the scaffold stays
here: the scaffold is a tested second consumer). Test helpers live in ``catalog_sync.testing``.
"""

from catalog_sync.connector_info import ConnectorInfo, SettingKind, SettingSpec
from catalog_sync.sdk.assets import PictureAssets, UrlRule, https_only, same_host
from catalog_sync.sdk.connection import USER_AGENT, Connection
from catalog_sync.sdk.credentials import EnvAccount, FormLogin, header_layout, verify_with_probe
from catalog_sync.sdk.folders import (
    FolderWalker,
    Sighting,
    WalkSettings,
    folder_label,
)
from catalog_sync.sdk.options import (
    MAX_REQUESTS_PER_MINUTE,
    OptionReader,
    declared_keys,
    request_gap_setting,
)
from catalog_sync.sdk.paging import (
    OffsetPage,
    PageCursor,
    RowPage,
    TokenPage,
    offset_page,
    rows_page,
    token_page,
    walk_number_pages,
    walk_offset_pages,
    walk_token_pages,
)
from catalog_sync.sdk.responses import item_scoped, parse_detail, shape_context

__all__ = [
    "MAX_REQUESTS_PER_MINUTE",
    "USER_AGENT",
    "Connection",
    "ConnectorInfo",
    "EnvAccount",
    "FolderWalker",
    "FormLogin",
    "OffsetPage",
    "OptionReader",
    "PageCursor",
    "PictureAssets",
    "RowPage",
    "SettingKind",
    "SettingSpec",
    "Sighting",
    "TokenPage",
    "UrlRule",
    "WalkSettings",
    "declared_keys",
    "folder_label",
    "header_layout",
    "https_only",
    "item_scoped",
    "offset_page",
    "parse_detail",
    "request_gap_setting",
    "rows_page",
    "same_host",
    "shape_context",
    "token_page",
    "verify_with_probe",
    "walk_number_pages",
    "walk_offset_pages",
    "walk_token_pages",
]
