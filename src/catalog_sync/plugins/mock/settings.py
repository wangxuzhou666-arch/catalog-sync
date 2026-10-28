"""The ``[sources.NAME]`` options of the two Mock SaaS sources, validated.

Values arrive from TOML (typed) or from ``--option KEY=VALUE`` (always strings), so scalars are
accepted in both forms. Anything unknown is an error: a typo in an option name must not silently
fall back to a default.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from catalog_sync.credentials.base import CredentialHandle
from catalog_sync.images import ImagePolicy
from catalog_sync.sdk import (
    MAX_REQUESTS_PER_MINUTE,
    OptionReader,
    SettingKind,
    SettingSpec,
    declared_keys,
    request_gap_setting,
)

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
# Small on purpose, so that even the demo's few dozen items span several pages.
DEFAULT_PAGE_SIZE = 10

BASE_URL = SettingSpec(
    "base_url",
    SettingKind.URL,
    "Where the Mock SaaS listens (catalog-sync mock serve).",
    default=DEFAULT_BASE_URL,
    essential=True,
)
PAGE_SIZE = SettingSpec(
    "page_size",
    SettingKind.INTEGER,
    "Rows per request.",
    default=DEFAULT_PAGE_SIZE,
    minimum=1,
    maximum=50,
)
MAX_PAGES = SettingSpec(
    "max_pages",
    SettingKind.INTEGER,
    "Most pages of one listing (one folder, in the folder walk) a single run fetches before it "
    "stops with a fuse (exit code 3); a resumed run counts again from zero.",
    default=1000,
    minimum=1,
)
REQUEST_GAP = request_gap_setting(0.3)
ROOT_FOLDER_ID = SettingSpec(
    "root_folder_id", SettingKind.TEXT, "The folder the walk starts in.", default="0"
)
MAX_DEPTH = SettingSpec(
    "max_depth", SettingKind.INTEGER, "How deep the folder tree may go.", default=6
)
IMAGES = SettingSpec(
    "images",
    SettingKind.TABLE,
    "Picture size limits: max_edge, edge_fallbacks, qualities, max_bytes.",
)

_COMMON = (BASE_URL, PAGE_SIZE, MAX_PAGES, REQUEST_GAP, MAX_REQUESTS_PER_MINUTE)
MATERIALS_SETTINGS = (*_COMMON, ROOT_FOLDER_ID, MAX_DEPTH, IMAGES)
SUPPLIERS_SETTINGS = _COMMON


@dataclass(frozen=True)
class MockSettings:
    credential: CredentialHandle
    base_url: str = DEFAULT_BASE_URL
    page_size: int = DEFAULT_PAGE_SIZE
    max_pages: int = 1000
    request_gap_s: float = 0.3
    # The compliance ceiling the CLI hands every networked source; see RateLimiter in http.py.
    max_requests_per_minute: int | None = None
    root_folder_id: str = "0"
    max_depth: int = 6
    images: ImagePolicy = field(default_factory=ImagePolicy)

    @classmethod
    def from_options(cls, options: Mapping[str, object], *, source: str) -> MockSettings:
        """`source` is the plugin name ("mock.materials" or "mock.suppliers"): the folder and
        picture options exist only for the first."""
        declared = MATERIALS_SETTINGS if source == "mock.materials" else SUPPLIERS_SETTINGS
        reader = OptionReader(options, label=source)
        reader.reject_unknown(declared_keys(declared))
        request_gap_s, ceiling = reader.pacing(REQUEST_GAP)
        return cls(
            credential=reader.credential(),
            base_url=reader.url_of(BASE_URL),
            page_size=reader.integer_of(PAGE_SIZE),
            max_pages=reader.integer_of(MAX_PAGES),
            request_gap_s=request_gap_s,
            max_requests_per_minute=ceiling,
            root_folder_id=reader.text_of(ROOT_FOLDER_ID),
            max_depth=reader.integer_of(MAX_DEPTH),
            images=ImagePolicy.from_options(reader.table(IMAGES.key)),
        )
