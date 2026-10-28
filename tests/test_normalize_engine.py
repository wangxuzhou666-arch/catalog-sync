"""The mapping engine on synthetic Mock-shaped items: what it reads, what it will not invent."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from catalog_sync.models import ImageRef, Money
from catalog_sync.normalize.engine import normalize
from tests.fixtures import sample_items as items
from tests.normalize_helpers import (
    NOW,
    PRICE_MAPPING,
    PROVENANCE,
    bundle_for,
    spec_from,
    stored_thumb,
)

FIELDS = """
    version: 1
    source: mock.materials
    fields:
      {body}
"""


def spec_with(tmp_path: Path, body: str):  # type: ignore[no-untyped-def]
    indented = "\n      ".join(line for line in body.strip("\n").splitlines())
    return spec_from(tmp_path, FIELDS.format(body=indented))


# --- list and detail are not supersets of each other --------------------------------------------


def test_price_comes_from_the_detail_and_the_picture_from_the_listing(tmp_path: Path) -> None:
    record = normalize(bundle_for(1), spec_from(tmp_path, PRICE_MAPPING), PROVENANCE)
    assert record.price == Money(Decimal("11.50"), "USD"), "the detail has the price"
    assert [(i.role, i.source_url) for i in record.images] == [("thumb", items.thumb_url(1))], (
        "the listing has the picture; the detail's empty thumbs must not wipe it"
    )
    assert record.issues == ()


def test_the_listing_zero_price_is_not_a_price_when_the_detail_has_none(tmp_path: Path) -> None:
    detail = items.detail_record(1)
    del detail["price"]
    record = normalize(bundle_for(1, detail=detail), spec_from(tmp_path, PRICE_MAPPING), PROVENANCE)
    assert record.price is None
    assert "price:source_zero" in record.issues


def test_an_empty_detail_value_never_hides_a_listing_value(tmp_path: Path) -> None:
    detail = {**items.detail_record(1), "name": "   "}
    spec = spec_with(tmp_path, "name:\n  from: [detail.name, list.name]")
    assert normalize(bundle_for(1, detail=detail), spec, PROVENANCE).name == items.name(1)


def test_a_whitespace_only_value_is_nothing_not_an_unparseable_one(tmp_path: Path) -> None:
    spec = spec_with(tmp_path, "weight_gsm:\n  from: [detail.weight]")
    detail = {**items.detail_record(1), "weight": "   "}
    record = normalize(bundle_for(1, detail=detail), spec, PROVENANCE)
    assert record.weight_gsm is None
    assert record.issues == (), "the source left it empty; that is not a parse problem"


def test_the_first_non_empty_source_wins_even_if_a_later_one_differs(tmp_path: Path) -> None:
    spec = spec_with(tmp_path, "name:\n  from: [detail.name, list.name]")
    row = {**items.list_row(1), "name": "From the listing"}
    detail = {**items.detail_record(1), "name": "From the detail"}
    assert (
        normalize(bundle_for(1, row=row, detail=detail), spec, PROVENANCE).name == "From the detail"
    )


def test_a_source_id_that_is_a_number_in_the_row_still_keys_the_record(tmp_path: Path) -> None:
    row = {**items.list_row(2), "id": 1002}
    record = normalize(bundle_for(2, row=row), spec_from(tmp_path, PRICE_MAPPING), PROVENANCE)
    assert (record.source, record.source_id) == ("mock.materials", "1002")


# --- never fabricate ----------------------------------------------------------------------------


def test_zero_weight_is_none_with_an_issue(tmp_path: Path) -> None:
    spec = spec_with(tmp_path, "weight_gsm:\n  from: [detail.weight]\n  zero_is_missing: true")
    detail = {**items.detail_record(1), "weight": "0GSM"}
    record = normalize(bundle_for(1, detail=detail), spec, PROVENANCE)
    assert record.weight_gsm is None
    assert record.issues == ("weight_gsm:source_zero",)


def test_without_zero_is_missing_a_zero_stays_a_zero(tmp_path: Path) -> None:
    spec = spec_with(tmp_path, "weight_gsm:\n  from: [detail.weight]")
    detail = {**items.detail_record(1), "weight": "0"}
    assert normalize(bundle_for(1, detail=detail), spec, PROVENANCE).weight_gsm == 0.0


def test_an_unparseable_weight_is_none_with_an_issue_and_no_value_in_it(tmp_path: Path) -> None:
    spec = spec_with(tmp_path, "weight_gsm:\n  from: [detail.weight]")
    detail = {**items.detail_record(1), "weight": "heavy-ish"}
    record = normalize(bundle_for(1, detail=detail), spec, PROVENANCE)
    assert record.weight_gsm is None
    assert record.issues == ("weight_gsm:unparseable",)


def test_a_later_source_rescues_a_zero_or_unparseable_earlier_one(tmp_path: Path) -> None:
    spec = spec_with(
        tmp_path, "weight_gsm:\n  from: [detail.weight, list.weight]\n  zero_is_missing: true"
    )
    row = {**items.list_row(1), "weight": "210"}
    detail = {**items.detail_record(1), "weight": "0"}
    record = normalize(bundle_for(1, row=row, detail=detail), spec, PROVENANCE)
    assert record.weight_gsm == 210.0
    assert record.issues == ()


def test_a_field_the_source_does_not_have_is_none_without_an_issue(tmp_path: Path) -> None:
    spec = spec_with(tmp_path, "supplier:\n  from: [detail.no_such_field]")
    record = normalize(bundle_for(1), spec, PROVENANCE)
    assert record.supplier is None and record.issues == ()


def test_a_missing_detail_is_one_issue_and_listing_fields_still_fill(tmp_path: Path) -> None:
    record = normalize(bundle_for(1, detail=None), spec_from(tmp_path, PRICE_MAPPING), PROVENANCE)
    assert record.code == items.code(1)
    assert record.price is None
    assert record.issues.count("detail:missing") == 1


def test_fields_the_mapping_does_not_mention_keep_their_defaults(tmp_path: Path) -> None:
    record = normalize(bundle_for(1), spec_from(tmp_path, PRICE_MAPPING), PROVENANCE)
    assert (record.name, record.category, record.tags, record.composition) == (None, None, (), ())


# --- composition --------------------------------------------------------------------------------

COMPOSITION = """
composition:
  from: [detail.composition]
  parse: composition
  scope: mock.saas/materials
composition_raw:
  from: [detail.composition]
"""


def with_composition(text: str) -> object:
    return {**items.detail_record(1), "composition": text}


def test_known_codes_resolve_and_unknown_ones_become_issues(tmp_path: Path) -> None:
    spec = spec_with(tmp_path, COMPOSITION)
    detail = {**items.detail_record(1), "composition": "60%CO/25%PES/15%ZZ"}
    record = normalize(bundle_for(1, detail=detail), spec, PROVENANCE)  # type: ignore[arg-type]
    assert [(c.fiber, c.percent) for c in record.composition] == [
        ("Cotton", 60.0),
        ("Polyester", 25.0),
        (None, 15.0),
    ]
    assert record.composition_raw == "60%CO/25%PES/15%ZZ"
    assert record.issues == ("composition:unknown_code:ZZ",)


def test_a_yarn_count_is_never_read_as_a_fibre(tmp_path: Path) -> None:
    spec = spec_with(tmp_path, COMPOSITION)
    detail = {**items.detail_record(1), "composition": "30S 68CO,2EA"}
    record = normalize(bundle_for(1, detail=detail), spec, PROVENANCE)  # type: ignore[arg-type]
    assert [c.fiber for c in record.composition] == ["Cotton", "Elastane"]
    assert record.composition_raw == "30S 68CO,2EA", "the raw text is kept whole"
    assert "composition:yarn_count_ignored:30S" in record.issues
    assert not any("unknown_code:S" in issue for issue in record.issues)


def test_a_denier_over_filament_count_is_not_two_shares_that_sum_past_100(tmp_path: Path) -> None:
    spec = spec_with(tmp_path, COMPOSITION)
    detail = {**items.detail_record(1), "composition": "100%PES 75D/72F"}
    record = normalize(bundle_for(1, detail=detail), spec, PROVENANCE)  # type: ignore[arg-type]
    assert [(c.fiber, c.percent) for c in record.composition] == [("Polyester", 100.0)]
    assert record.issues == ("composition:yarn_count_ignored:75D/72F",)


def test_shares_over_100_leave_no_composition_but_keep_the_raw_text(tmp_path: Path) -> None:
    spec = spec_with(tmp_path, COMPOSITION)
    detail = {**items.detail_record(1), "composition": "70%CO 60%PES"}
    record = normalize(bundle_for(1, detail=detail), spec, PROVENANCE)  # type: ignore[arg-type]
    assert record.composition == ()
    assert record.composition_raw == "70%CO 60%PES"
    assert record.issues == ("composition:sum_over_100",)


def test_an_unparseable_composition_keeps_the_raw_text_and_flags_it(tmp_path: Path) -> None:
    spec = spec_with(tmp_path, COMPOSITION)
    detail = {**items.detail_record(1), "composition": "see label"}
    record = normalize(bundle_for(1, detail=detail), spec, PROVENANCE)  # type: ignore[arg-type]
    assert record.composition == ()
    assert record.composition_raw == "see label"
    assert record.issues == ("composition:unparseable",)


# --- pictures -----------------------------------------------------------------------------------


def test_a_stored_picture_adds_its_cache_path_hash_and_size(tmp_path: Path) -> None:
    bundle = bundle_for(1, assets=(stored_thumb(),))
    record = normalize(bundle, spec_from(tmp_path, PRICE_MAPPING), PROVENANCE)
    assert record.images == (
        ImageRef(
            role="thumb",
            source_url=items.thumb_url(1),
            local_path=f"assets/{items.item_id(1)}+thumb.bin",
            sha256="ab" * 32,
            size_bytes=321,
        ),
    )


def test_a_stored_picture_brings_its_width_and_height(tmp_path: Path) -> None:
    sized = replace(stored_thumb(), width=1200, height=800)
    record = normalize(
        bundle_for(1, assets=(sized,)), spec_from(tmp_path, PRICE_MAPPING), PROVENANCE
    )
    (image,) = record.images
    assert (image.width, image.height) == (1200, 800)
    assert not [issue for issue in record.issues if issue.startswith("images:")]


def test_a_stored_file_that_is_not_a_picture_leaves_the_size_empty_and_says_so(
    tmp_path: Path,
) -> None:
    unreadable = replace(stored_thumb(), is_unreadable=True)
    record = normalize(
        bundle_for(1, assets=(unreadable,)), spec_from(tmp_path, PRICE_MAPPING), PROVENANCE
    )
    (image,) = record.images
    assert (image.width, image.height) == (None, None), "no size is invented"
    assert image.local_path == f"assets/{items.item_id(1)}+thumb.bin", "the file is still recorded"
    assert "images:thumb:unreadable" in record.issues


def test_a_picture_that_was_never_downloaded_has_no_size_and_no_issue(tmp_path: Path) -> None:
    record = normalize(bundle_for(1), spec_from(tmp_path, PRICE_MAPPING), PROVENANCE)
    (image,) = record.images
    assert (image.local_path, image.width, image.height) == (None, None, None)
    assert not [issue for issue in record.issues if "unreadable" in issue]


def test_picture_urls_lose_their_signature_before_they_are_stored(tmp_path: Path) -> None:
    row = items.list_row(1, query="?Expires=1&Signature=SECRET&Key-Pair-Id=K")
    record = normalize(bundle_for(1, row=row), spec_from(tmp_path, PRICE_MAPPING), PROVENANCE)
    (image,) = record.images
    assert image.source_url == items.thumb_url(1)
    assert "SECRET" not in repr(record.to_dict())


def test_a_picture_that_is_not_a_url_is_refused_with_an_issue(tmp_path: Path) -> None:
    row = {**items.list_row(1), "thumbnail_url": "not a url"}
    record = normalize(bundle_for(1, row=row), spec_from(tmp_path, PRICE_MAPPING), PROVENANCE)
    assert record.images == ()
    assert "images:thumb:not_a_url" in record.issues


def test_a_downloaded_picture_without_a_mapped_url_still_appears(tmp_path: Path) -> None:
    row = {key: value for key, value in items.list_row(1).items() if key != "thumbnail_url"}
    record = normalize(
        bundle_for(1, row=row, assets=(stored_thumb(),)),
        spec_from(tmp_path, PRICE_MAPPING),
        PROVENANCE,
    )
    (image,) = record.images
    assert image.local_path == f"assets/{items.item_id(1)}+thumb.bin"
    assert image.source_url == items.thumb_url(1)


def test_several_picture_roles(tmp_path: Path) -> None:
    spec = spec_with(
        tmp_path,
        "images:\n  roles:\n    thumb: [list.thumbnail_url]\n    swatch: [detail.swatch_url]",
    )
    detail = {
        **items.detail_record(1),
        "swatch_url": "https://assets.example.invalid/s/1.png?sig=x",
    }
    record = normalize(bundle_for(1, detail=detail), spec, PROVENANCE)
    assert [(i.role, i.source_url) for i in record.images] == [
        ("thumb", items.thumb_url(1)),
        ("swatch", "https://assets.example.invalid/s/1.png"),
    ]


# --- lists, context, extra ----------------------------------------------------------------------


def test_folder_paths_come_from_the_walk_context_including_both_folders(tmp_path: Path) -> None:
    spec = spec_with(tmp_path, "folder_paths:\n  from: [context.folder_paths]")
    both = normalize(bundle_for(2, folder_paths=["Alpha", "Beta"]), spec, PROVENANCE)
    assert both.folder_paths == ("Alpha", "Beta")
    root = normalize(bundle_for(0, folder_paths=[]), spec, PROVENANCE)
    assert root.folder_paths == ()


def test_split_turns_a_delimited_text_field_into_tags(tmp_path: Path) -> None:
    spec = spec_with(tmp_path, "tags:\n  from: [detail.keywords]\n  parse: split")
    detail = {**items.detail_record(1), "keywords": "linen, summer;linen,  "}
    assert normalize(bundle_for(1, detail=detail), spec, PROVENANCE).tags == ("linen", "summer")


def test_extra_values_are_namespaced_and_passed_through_untouched(tmp_path: Path) -> None:
    spec = spec_with(
        tmp_path,
        "extra.mock.price_text:\n  from: [detail.price]\n"
        "extra.mock.missing:\n  from: [detail.nothing_here]",
    )
    record = normalize(bundle_for(1), spec, PROVENANCE)
    assert record.extra == {"mock": {"price_text": "11.50"}}


def test_provenance_is_copied_into_the_record(tmp_path: Path) -> None:
    record = normalize(bundle_for(1), spec_from(tmp_path, PRICE_MAPPING), PROVENANCE)
    assert (record.source_version, record.raw_ref) == ("1700000000", "run-1:1000")
    assert (record.fetched_at, record.normalized_at) == (NOW, NOW)


def test_records_validate_as_the_models_expect(tmp_path: Path) -> None:
    record = normalize(bundle_for(1), spec_from(tmp_path, PRICE_MAPPING), PROVENANCE)
    assert type(record).from_dict(record.to_dict()) == record


@pytest.mark.parametrize("n", range(5))
def test_the_whole_synthetic_library_normalises_without_exceptions(tmp_path: Path, n: int) -> None:
    normalize(bundle_for(n), spec_from(tmp_path, PRICE_MAPPING), PROVENANCE)
