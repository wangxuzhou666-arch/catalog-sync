from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from catalog_sync.models import Money
from catalog_sync.normalize.codetables import CodeTable
from catalog_sync.normalize.parsers import (
    PARSERS,
    ParseFailure,
    parse_composition,
    parse_gsm,
    parse_integer,
    parse_list,
    parse_money,
    parse_number,
    parse_split,
    parse_text,
    parse_timestamp,
    parse_width_cm,
)

TABLE = CodeTable(
    scope="demo/endpoint",
    kind="composition",
    codes={"P": "Polyester", "C": "Cotton", "SP": "Elastane", "Ten": "Lyocell"},
)
OPTIONS = {"table": TABLE}


def composition(text: str) -> tuple[list[tuple[str | None, float | None, str]], tuple[str, ...]]:
    parsed = parse_composition(text, OPTIONS)
    assert isinstance(parsed.value, tuple)
    return [(part.fiber, part.percent, part.raw) for part in parsed.value], parsed.issues


# --- weight -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("280", 280.0),
        ("280G/M2", 280.0),
        ("280g/m²", 280.0),
        (" 180 g / m2 ", 180.0),
        ("180gsm", 180.0),
        ("180 GSM", 180.0),
        ("120.5g", 120.5),
        ("0GSM", 0.0),
        (150, 150.0),
        (95.5, 95.5),
    ],
)
def test_gsm_reads_the_number_and_keeps_zero_as_zero(raw: object, expected: float) -> None:
    assert parse_gsm(raw, {}).value == expected


@pytest.mark.parametrize(
    "raw", ["", "abc", "5 oz/yd2", "180 kg", "1,5", "-3", "g180", True, None, [1]]
)
def test_gsm_refuses_what_it_cannot_read_instead_of_guessing(raw: object) -> None:
    with pytest.raises(ParseFailure):
        parse_gsm(raw, {})


# --- width --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("150", 150.0),
        ("150CM", 150.0),
        ("150 cm", 150.0),
        ('56"', 142.2),
        ("56 in", 142.2),
        ('57/58"', 146.1),
        ("57 / 58 inch", 146.1),
        (148, 148.0),
        (148.04, 148.0),
    ],
)
def test_width_understands_centimetres_inches_and_inch_ranges(raw: object, expected: float) -> None:
    assert parse_width_cm(raw, {}).value == expected


@pytest.mark.parametrize("raw", ["", "wide", "1.5m", "150-152cm", "57/58", True])
def test_width_refuses_other_forms(raw: object) -> None:
    with pytest.raises(ParseFailure):
        parse_width_cm(raw, {})


# --- money --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "amount"),
    [("12.50", "12.50"), (" 7 ", "7"), (3, "3"), (0.1, "0.1"), ("0", "0")],
)
def test_money_is_exact_decimal_in_the_currency_the_mapping_states(
    raw: object, amount: str
) -> None:
    parsed = parse_money(raw, {"currency": "EUR"})
    assert parsed.value == Money(Decimal(amount), "EUR")


@pytest.mark.parametrize("raw", ["", "1,234.50", "$5", "-1", "1e3", "NaN", True, None])
def test_money_refuses_ambiguous_or_negative_amounts(raw: object) -> None:
    with pytest.raises(ParseFailure):
        parse_money(raw, {"currency": "EUR"})


# --- text and lists -----------------------------------------------------------------------------


def test_text_strips_and_turns_blank_into_nothing() -> None:
    assert parse_text("  Twill ", {}).value == "Twill"
    assert parse_text("   ", {}).value is None
    assert parse_text(1001, {}).value == "1001"
    with pytest.raises(ParseFailure):
        parse_text({"a": 1}, {})
    with pytest.raises(ParseFailure):
        parse_text(True, {})


def test_split_uses_the_separators_the_rule_names_and_drops_blanks_and_repeats() -> None:
    assert parse_split("a, b;;a ,c", {}).value == ("a", "b", "c")
    assert parse_split("shirt/dress,coat", {"separators": "/,"}).value == ("shirt", "dress", "coat")
    assert parse_split(["x", " y ", "x"], {}).value == ("x", "y")


def test_list_wraps_a_single_value_and_cleans_a_list() -> None:
    assert parse_list("solo", {}).value == ("solo",)
    assert parse_list(["a", "", "a", 7], {}).value == ("a", "7")
    assert parse_list([], {}).value == ()


# --- composition --------------------------------------------------------------------------------


def test_composition_resolves_codes_through_the_table() -> None:
    shares, issues = composition("60%C/25%P/15%SP")
    assert shares == [
        ("Cotton", 60.0, "60%C"),
        ("Polyester", 25.0, "25%P"),
        ("Elastane", 15.0, "15%SP"),
    ]
    assert issues == ()


def test_an_unknown_code_is_null_and_an_issue_never_a_guess() -> None:
    shares, issues = composition("60%C/40%V")
    assert shares[1] == (None, 40.0, "40%V")
    assert issues == ("composition:unknown_code:V",)


def test_names_the_table_does_not_list_are_unknown_codes_too() -> None:
    shares, issues = composition("70% Demo Cotton 30% Demo Linen")
    assert [fiber for fiber, _, _ in shares] == [None, None]
    assert [percent for _, percent, _ in shares] == [70.0, 30.0]
    assert issues == (
        "composition:unknown_code:Demo Cotton",
        "composition:unknown_code:Demo Linen",
    )


def test_a_yarn_count_is_not_a_fibre_share() -> None:
    shares, issues = composition("30S 68C,2SP")
    assert shares == [("Cotton", 68.0, "68C"), ("Elastane", 2.0, "2SP")]
    assert issues == ("composition:yarn_count_ignored:30S",)


def test_a_lone_yarn_count_yields_no_shares_at_all() -> None:
    assert composition("40S") == ([], ("composition:yarn_count_ignored:40S",))


def test_with_a_percent_sign_s_is_an_ordinary_code() -> None:
    table = CodeTable("demo/other", "composition", {"S": "Silk"})
    parsed = parse_composition("15%S", {"table": table})
    assert parsed.value[0].fiber == "Silk"  # type: ignore[index]


def test_codes_match_without_regard_to_case_or_spacing() -> None:
    shares, issues = composition("50% ten /50%p")
    assert [fiber for fiber, _, _ in shares] == ["Lyocell", "Polyester"]
    assert issues == ()


@pytest.mark.parametrize("raw", ["Cotton 60%", "Shell: 60%C", "60%", "60% 40%C", "see label"])
def test_a_string_that_is_not_a_list_of_shares_is_unparseable(raw: str) -> None:
    with pytest.raises(ParseFailure):
        parse_composition(raw, OPTIONS)


def test_fullwidth_and_ideographic_commas_separate_shares() -> None:
    comma, ideographic = chr(0xFF0C), chr(0x3001)
    shares, _ = composition(f"60%C{comma}30%P{ideographic}10%SP")
    assert [fiber for fiber, _, _ in shares] == ["Cotton", "Polyester", "Elastane"]


TIMES_SIGN = chr(0xD7)


@pytest.mark.parametrize(
    ("raw", "shares", "yarn"),
    [
        ("100%P 75D/72F", [("Polyester", 100.0, "100%P")], "75D/72F"),
        ("100%C 40S*40S", [("Cotton", 100.0, "100%C")], "40S*40S"),
        ("100%C 21Sx21S", [("Cotton", 100.0, "100%C")], "21Sx21S"),
        ("21Sx21S", [], "21Sx21S"),
        ("30S", [], "30S"),
        ("75D", [], "75D"),
        ("100%C 40S/2", [("Cotton", 100.0, "100%C")], "40S/2"),
        (f"100%C 40s{TIMES_SIGN}40s", [("Cotton", 100.0, "100%C")], f"40s{TIMES_SIGN}40s"),
    ],
)
def test_every_percentless_yarn_count_form_is_left_out_of_the_shares(
    raw: str, shares: list[tuple[str, float, str]], yarn: str
) -> None:
    """``75D/72F`` is denier over filaments and ``40S*40S`` warp by weft, never 75% D and 72% F."""
    got_shares, issues = composition(raw)
    assert got_shares == shares
    assert issues == (f"composition:yarn_count_ignored:{yarn}",)


def test_a_percentless_share_of_a_longer_code_is_not_mistaken_for_a_yarn_count() -> None:
    shares, issues = composition("40SP 60%C")
    assert shares == [("Elastane", 40.0, "40SP"), ("Cotton", 60.0, "60%C")]
    assert issues == ()


def test_a_legitimate_composition_that_adds_up_to_100_is_untouched() -> None:
    shares, issues = composition("65%P 35%C")
    assert shares == [("Polyester", 65.0, "65%P"), ("Cotton", 35.0, "35%C")]
    assert issues == ()


def test_shares_adding_up_to_less_than_100_are_kept_as_written() -> None:
    shares, issues = composition("60%C 30%P")
    assert [percent for _, percent, _ in shares] == [60.0, 30.0]
    assert issues == ()


def test_shares_adding_up_to_more_than_100_are_dropped_not_trusted() -> None:
    parsed = parse_composition("60%C/50%P", OPTIONS)
    assert parsed.value is None
    assert parsed.issues == ("composition:sum_over_100",)


def test_the_sum_is_exact_so_decimal_percentages_do_not_trip_the_check() -> None:
    assert composition("33.3%C 33.3%P 33.4%SP")[1] == ()


def test_the_registry_has_the_documented_parsers() -> None:
    assert set(PARSERS) == {
        "text",
        "list",
        "split",
        "gsm",
        "width_cm",
        "number",
        "integer",
        "timestamp",
        "money",
        "composition",
        "raw",
    }


# --- generic scalars (added with schema-declared records) ---------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("4.5", 4.5), ("-2", -2.0), (3, 3.0), (0.25, 0.25), (" 7 ", 7.0), ("0", 0.0)],
)
def test_number_reads_plain_numbers(value: object, expected: float) -> None:
    assert parse_number(value, {}).value == expected


@pytest.mark.parametrize(
    "value", ["4,5", "4.5kg", "", "1e3", True, float("nan"), float("inf"), [1]]
)
def test_number_refuses_everything_else(value: object) -> None:
    with pytest.raises(ParseFailure):
        parse_number(value, {})


@pytest.mark.parametrize(
    ("value", "expected"), [(14, 14), ("14", 14), ("-3", -3), ("+3", 3), (14.0, 14), ("0", 0)]
)
def test_integer_reads_whole_numbers(value: object, expected: int) -> None:
    parsed = parse_integer(value, {}).value
    assert parsed == expected and isinstance(parsed, int)


@pytest.mark.parametrize("value", [14.5, "14.5", "14 days", "", True, None, [14]])
def test_integer_refuses_what_would_need_rounding_or_guessing(value: object) -> None:
    with pytest.raises(ParseFailure):
        parse_integer(value, {})


def _timestamp_text(raw: str) -> str:
    parsed = parse_timestamp(raw, {}).value
    assert isinstance(parsed, datetime)
    return parsed.isoformat()


def test_timestamp_is_converted_to_utc_keeping_the_instant() -> None:
    assert _timestamp_text("2026-03-01T12:00:00Z") == "2026-03-01T12:00:00+00:00"
    assert _timestamp_text("2026-03-01T12:00:00+02:00") == "2026-03-01T10:00:00+00:00"
    assert _timestamp_text("2026-03-01T01:30:00-05:00") == "2026-03-01T06:30:00+00:00"


def test_timestamps_from_different_offsets_sort_as_text_in_time_order() -> None:
    """The sinks store the ISO text; it is only chronological if every offset is the same."""
    in_time_order = [
        "2026-03-01T00:30:00+08:00",  # 2026-02-28T16:30Z
        "2026-02-28T17:00:00Z",
        "2026-02-28T13:00:00-05:00",  # 18:00Z
    ]
    stored = [_timestamp_text(raw) for raw in in_time_order]
    assert stored == sorted(stored)
    assert in_time_order != sorted(in_time_order)  # the raw strings do not sort that way


@pytest.mark.parametrize(
    "value", ["2026-03-01T12:00:00", "2026-03-01", "yesterday", "", 1700000000]
)
def test_timestamp_refuses_a_time_without_an_offset_or_not_a_time(value: object) -> None:
    """No offset means an unknown instant; assuming UTC would make the value depend on a guess."""
    with pytest.raises(ParseFailure):
        parse_timestamp(value, {})
