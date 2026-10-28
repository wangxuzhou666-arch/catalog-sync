"""The compliance declaration: required, validated, and consistent with the declared pace."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from catalog_sync.compliance import (
    AUTHORIZATIONS,
    Compliance,
    check_field,
    check_request_pacing,
    gate_source,
    parse_compliance,
    stale_runs,
)
from catalog_sync.config import SourceConfig, load_config
from catalog_sync.errors import ComplianceError
from catalog_sync.state import RunRow, RunStatus
from tests.conftest import REPO_ROOT

TODAY = date(2026, 10, 26)
VALID: dict[str, object] = {
    "data_owner": "Example Co",
    "authorization": "own_account",
    "authorization_ref": "TICKET-1",
    "tos_url": "https://vendor.example.invalid/terms",
    "tos_reviewed_on": date(2026, 10, 1),
    "max_requests_per_minute": 120,
    "retention_days": 90,
}


def parse(**changes: object) -> Compliance:
    table = {**VALID, **changes}
    return parse_compliance(
        {k: v for k, v in table.items() if v is not ...}, label="s", today=TODAY
    )


def test_a_complete_declaration_parses() -> None:
    declared = parse()
    assert declared.data_owner == "Example Co"
    assert declared.tos_reviewed_on == date(2026, 10, 1)
    assert declared.tos_review_max_age_days == 180
    assert declared.minimum_gap_s == 0.5


def test_a_missing_table_names_the_table_to_write() -> None:
    with pytest.raises(ComplianceError, match=r"no compliance declaration.*\[s\.compliance\]"):
        parse_compliance(None, label="s", today=TODAY)


@pytest.mark.parametrize("key", sorted(VALID))
def test_each_required_key_is_required(key: str) -> None:
    with pytest.raises(ComplianceError, match=rf"missing required key.*{key}"):
        parse(**{key: ...})


def test_an_unknown_key_is_refused_so_a_typo_cannot_disable_a_limit() -> None:
    with pytest.raises(ComplianceError, match="unknown key"):
        parse(max_requests_per_minut=5)


@pytest.mark.parametrize("authorization", AUTHORIZATIONS)
def test_every_documented_authorization_is_accepted(authorization: str) -> None:
    assert parse(authorization=authorization).authorization == authorization


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"authorization": "i_just_do"}, "'authorization' must be one of"),
        ({"authorization": 3}, "'authorization' must be a real"),
        ({"data_owner": ""}, "'data_owner' must be a real"),
        ({"data_owner": "   "}, "'data_owner' must be a real"),
        ({"data_owner": "<fill me in>"}, "'data_owner' must be a real"),
        ({"authorization_ref": "TODO"}, "'authorization_ref' must be a real"),
        ({"tos_url": "vendor.example/terms"}, "'tos_url' must be an http"),
        ({"tos_url": "ftp://vendor.example/terms"}, "'tos_url' must be an http"),
        ({"tos_url": "http://[::1/terms"}, "'tos_url' must be an http"),
        ({"tos_url": "https://ops:s3cret" + "@vendor.example/terms"}, "'tos_url' must not carry"),
        ({"tos_reviewed_on": "last spring"}, "'tos_reviewed_on' must be a date"),
        ({"tos_reviewed_on": 20261001}, "'tos_reviewed_on' must be a date"),
        ({"tos_reviewed_on": datetime(2026, 10, 1, 9, 0, tzinfo=UTC)}, "must be a date"),
        ({"tos_reviewed_on": date(2026, 10, 27)}, "is in the future"),
        ({"max_requests_per_minute": 0}, "whole number >= 1"),
        ({"max_requests_per_minute": -5}, "whole number >= 1"),
        ({"max_requests_per_minute": 1.5}, "whole number >= 1"),
        ({"max_requests_per_minute": True}, "whole number >= 1"),
        ({"max_requests_per_minute": "60"}, "whole number >= 1"),
        ({"retention_days": 0}, "whole number >= 1"),
        ({"tos_review_max_age_days": 0}, "whole number >= 1"),
    ],
)
def test_invalid_values_are_refused_with_the_key_named(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ComplianceError, match=message):
        parse(**changes)


def test_the_review_date_may_be_written_as_an_iso_string() -> None:
    assert parse(tos_reviewed_on="2026-10-01").tos_reviewed_on == date(2026, 10, 1)


def test_the_digest_is_stable_and_changes_with_any_value() -> None:
    assert parse().digest == parse().digest
    assert len(parse().digest) == 64
    assert parse(retention_days=91).digest != parse().digest
    assert parse(data_owner="Other Co").digest != parse().digest
    assert parse(tos_review_max_age_days=181).digest != parse().digest


def test_an_old_terms_review_warns_and_a_recent_one_does_not() -> None:
    assert parse().tos_review_warning(TODAY) is None
    stale = parse(tos_reviewed_on=date(2026, 4, 1))
    warning = stale.tos_review_warning(TODAY)
    assert warning is not None and "208 days" in warning and "limit 180" in warning
    boundary = parse(tos_reviewed_on=date(2026, 4, 29))  # exactly 180 days
    assert boundary.tos_review_warning(TODAY) is None
    assert (
        parse(tos_reviewed_on=date(2026, 4, 1), tos_review_max_age_days=365).tos_review_warning(
            TODAY
        )
        is None
    )


# --- pacing -------------------------------------------------------------------------------------


@pytest.mark.parametrize("gap", [0.5, 0.51, 3, "1", "0.5"])
def test_a_gap_at_or_above_the_minimum_passes(gap: object) -> None:
    check_request_pacing(parse(), {"request_gap_s": gap}, label="s")


@pytest.mark.parametrize("gap", [0, 0.3, 0.49, "0.1", -1, float("nan")])
def test_a_gap_below_the_minimum_is_refused(gap: object) -> None:
    with pytest.raises(ComplianceError, match=r"request_gap_s = .* shorter than 60 / max_requests"):
        check_request_pacing(parse(), {"request_gap_s": gap}, label="s")


def test_a_source_that_sets_no_gap_is_left_to_the_runtime_ceiling() -> None:
    check_request_pacing(parse(), {}, label="s")


@pytest.mark.parametrize("gap", ["fast", True, [1], {"a": 1}])
def test_a_gap_that_is_not_a_number_is_refused(gap: object) -> None:
    with pytest.raises(ComplianceError, match="must be a number"):
        check_request_pacing(parse(), {"request_gap_s": gap}, label="s")


# --- the gate -----------------------------------------------------------------------------------


def entry(compliance: dict[str, object] | None, **options: object) -> SourceConfig:
    return SourceConfig("s", "mock.materials", options, compliance)


def test_a_local_source_needs_no_declaration() -> None:
    gate = gate_source("demo", None, plugin="replay", options={}, today=TODAY)
    assert gate.is_local and gate.compliance is None


def test_a_networked_source_without_a_declaration_is_refused() -> None:
    with pytest.raises(ComplianceError, match="no compliance declaration"):
        gate_source("s", entry(None), plugin="mock.materials", options={}, today=TODAY)


def test_a_networked_source_named_only_on_the_command_line_is_refused() -> None:
    with pytest.raises(ComplianceError, match=r"not defined in the config.*\[sources\.mock"):
        gate_source("mock.materials", None, plugin="mock.materials", options={}, today=TODAY)


def test_an_unknown_plugin_is_gated_by_default() -> None:
    with pytest.raises(ComplianceError):
        gate_source("s", entry(None), plugin="some-new-plugin", options={}, today=TODAY)


def test_the_gate_passes_a_valid_declaration_and_checks_the_merged_options() -> None:
    declared = dict(VALID)
    gate = gate_source("s", entry(declared), plugin="mock.materials", options={}, today=TODAY)
    assert gate.compliance is not None and not gate.is_local
    with pytest.raises(ComplianceError, match="request_gap_s"):
        gate_source(
            "s",
            entry(declared, request_gap_s=1),
            plugin="mock.materials",
            options={"request_gap_s": "0"},  # a --option override of a safe configured value
            today=TODAY,
        )


# --- the shipped example ------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["mock-materials", "mock-suppliers"])
def test_the_example_config_carries_a_valid_declaration_with_a_consistent_pace(name: str) -> None:
    config = load_config(REPO_ROOT / "catalog-sync.example.toml")
    source = config.sources[name]
    gate = gate_source(
        name, source, plugin=source.plugin, options=source.options, today=date(2026, 11, 1)
    )
    assert gate.compliance is not None
    assert gate.compliance.authorization in AUTHORIZATIONS
    assert "demo" in config.sources and config.sources["demo"].compliance is None


def fields_without_a_comment(text: str, header: str) -> list[str]:
    """Keys of VALID that the table under `header` lacks or writes without a `#` comment."""
    block = text[text.index(header) :].split("\n\n")[0]
    lines = {line.split("=")[0].strip(): line for line in block.splitlines() if "=" in line}
    return [key for key in VALID if key not in lines or "#" not in lines[key].partition("=")[2]]


EXAMPLE_HEADER = "[sources.mock-materials.compliance]"


def test_the_example_declaration_is_commented_field_by_field() -> None:
    text = (REPO_ROOT / "catalog-sync.example.toml").read_text(encoding="utf-8")
    assert fields_without_a_comment(text, EXAMPLE_HEADER) == []


def test_a_declaration_with_a_bare_or_missing_field_is_found() -> None:
    commented = "\n".join(f"{key} = 1  # what to put here" for key in VALID)
    assert fields_without_a_comment(f"{EXAMPLE_HEADER}\n{commented}\n", EXAMPLE_HEADER) == []
    bare = commented.replace("  # what to put here", "", 1)
    assert fields_without_a_comment(f"{EXAMPLE_HEADER}\n{bare}\n", EXAMPLE_HEADER) == ["data_owner"]
    gone = "\n".join(commented.splitlines()[1:])
    assert fields_without_a_comment(f"{EXAMPLE_HEADER}\n{gone}\n", EXAMPLE_HEADER) == ["data_owner"]
    # A comment on the line above does not count: the field's own line says what to put there.
    above = commented.replace(
        "retention_days = 1  # what to put here", "# note\nretention_days = 1"
    )
    assert fields_without_a_comment(f"{EXAMPLE_HEADER}\n{above}\n", EXAMPLE_HEADER) == [
        "retention_days"
    ]


# --- retention ----------------------------------------------------------------------------------


def run_row(run_id: str, started: str) -> RunRow:
    return RunRow(run_id, "s", RunStatus.COMPLETE, started, None, True)


def test_runs_older_than_the_retention_are_listed() -> None:
    now = datetime(2026, 10, 26, tzinfo=UTC)
    runs = [
        run_row("new", "2026-10-20T00:00:00+00:00"),
        run_row("edge", "2026-07-28T00:00:00+00:00"),
        run_row("old", "2026-07-01T00:00:00+00:00"),
    ]
    assert [r.run_id for r in stale_runs(runs, 90, now=now)] == ["old"]
    assert [r.run_id for r in stale_runs(runs, 30, now=now)] == ["edge", "old"]


def test_nothing_is_stale_in_an_empty_cache() -> None:
    assert stale_runs([], 1, now=datetime.now(UTC)) == []


# --- one field at a time (what `init` asks) -------------------------------------------------------

BAD_VALUES: dict[str, list[object]] = {
    "data_owner": ["", "   ", "<the organisation>", "TODO", 5],
    "authorization": ["owner", "", "Own_Account"],
    "authorization_ref": ["", "<where>", "tbd"],
    "tos_url": ["ftp://x.example", "vendor.example/terms", "", "<url>"],
    "tos_reviewed_on": ["2026-10-27", "yesterday", 20261001, ""],
    "max_requests_per_minute": [0, -1, True, "60", 1.5],
    "retention_days": [0, -3, False, "90"],
}


def test_one_field_checked_alone_gives_the_value_as_it_is_stored() -> None:
    kwargs = {"label": "s", "today": TODAY}
    assert check_field("data_owner", "  Example Co ", **kwargs) == "Example Co"
    assert check_field("tos_reviewed_on", "2026-10-01", **kwargs) == date(2026, 10, 1)
    assert check_field("tos_reviewed_on", date(2026, 10, 1), **kwargs) == date(2026, 10, 1)
    assert check_field("max_requests_per_minute", 120, **kwargs) == 120
    assert check_field("authorization", "contract", **kwargs) == "contract"
    assert check_field("tos_review_max_age_days", 30, **kwargs) == 30


@pytest.mark.parametrize(
    ("name", "value"), [(n, v) for n, values in BAD_VALUES.items() for v in values]
)
def test_one_field_is_refused_for_exactly_the_reason_the_whole_declaration_would_be(
    name: str, value: object
) -> None:
    """`init` checks answers with `check_field` and the gate with `parse_compliance`; both must
    say the same thing about the same value, or the wizard would accept what the gate refuses."""
    with pytest.raises(ComplianceError) as alone:
        check_field(name, value, label="s", today=TODAY)
    with pytest.raises(ComplianceError) as whole:
        parse(**{name: value})
    assert str(alone.value) == str(whole.value)


def test_a_field_that_does_not_exist_is_refused_by_name() -> None:
    with pytest.raises(ComplianceError, match="unknown key 'owner'"):
        check_field("owner", "x", label="s", today=TODAY)
