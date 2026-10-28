"""The acceptance harness (tools/acceptance.py): its rules for an honest record, without a run.

What matters most is what the record may never say: a scenario that checked nothing is not a pass,
a NOT_RUN is not a pass, a secret or a host path does not reach the report, and the request counts
read the very lines the Mock source prints. The full run (build, two installs, every scenario) is
one slow test, off unless CATALOG_SYNC_RUN_ACCEPTANCE=1.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import acceptance
import acceptance_compare
import acceptance_lab
import acceptance_scenarios
from acceptance_lab import (
    ENTITIES,
    Harness,
    Installed,
    Request,
    config_text,
    count_kinds,
    parse_platform_lines,
    parse_request_lines,
    per_item,
    scenario,
)
from acceptance_report import (
    Check,
    Masker,
    NotRun,
    Outcome,
    Recorder,
    Report,
    Status,
    report_json,
    report_markdown,
)
from catalog_sync.mock.platform import PlatformEvent as PlatformLine
from catalog_sync.mock.service import LoggedRequest
from tests.conftest import REPO_ROOT


def outcome(
    checks: list[Check] | None = None, *, not_run: str | None = None, error: str | None = None
) -> Outcome:
    return Outcome("S0", "A scenario", checks or [], not_run_reason=not_run, error=error)


# --- status: what may be called a pass ----------------------------------------------------------


def test_an_outcome_with_all_checks_passing_is_a_pass() -> None:
    assert outcome([Check("a", True), Check("b", True)]).status is Status.PASS


def test_an_outcome_that_checked_nothing_is_a_fail_not_a_pass() -> None:
    found = outcome([])
    assert found.status is Status.FAIL
    assert "no check" in found.reason


def test_one_failed_check_fails_the_outcome_and_names_it() -> None:
    found = outcome([Check("rows equal", True), Check("zero refetch", False, "24 requests")])
    assert found.status is Status.FAIL
    assert "zero refetch" in found.reason


def test_a_not_run_keeps_its_reason_whatever_else_was_recorded() -> None:
    found = outcome([Check("a", True)], not_run="no DSN")
    assert found.status is Status.NOT_RUN
    assert found.reason == "no DSN"


def test_an_error_is_a_fail_even_when_every_recorded_check_passed() -> None:
    assert outcome([Check("a", True)], error="KeyError: 'x'").status is Status.FAIL


# --- the report as a whole ----------------------------------------------------------------------


def report_of(*outcomes: Outcome) -> Report:
    return Report({"version": "0.1.0rc1", "dirty": False}, [], outcomes)


def test_not_run_is_partial_never_pass_and_does_not_fail_the_exit_code() -> None:
    report = report_of(outcome([Check("a", True)]), outcome(not_run="not implemented yet (W4b)"))
    assert report.overall == "PARTIAL"
    assert report.exit_code == 0
    assert report.counts() == {"PASS": 1, "FAIL": 0, "NOT_RUN": 1}


def test_any_fail_is_a_non_zero_exit() -> None:
    report = report_of(outcome([Check("a", True)]), outcome([Check("b", False)]))
    assert report.overall == "FAIL"
    assert report.exit_code == 1


def test_only_passes_is_pass() -> None:
    assert report_of(outcome([Check("a", True)])).overall == "PASS"


def test_the_markdown_lists_a_not_run_with_its_reason_and_no_pass_mark() -> None:
    text = report_markdown(report_of(outcome(not_run="no DSN")), Masker())
    assert "| S0 | A scenario | NOT_RUN | no DSN |" in text
    assert "Not run: no DSN" in text
    assert "PASS" not in text.replace("0 PASS", "").replace("never a pass", "")


def test_the_markdown_shows_checks_evidence_and_provenance() -> None:
    found = outcome([Check("rows equal", True, "24 vs 24"), Check("no refetch", False, "5")])
    found.evidence["row counts"] = {"sqlite": 24}
    text = report_markdown(report_of(found), Masker())
    assert "- version: `0.1.0rc1`" in text
    assert "| rows equal | pass | 24 vs 24 |" in text
    assert "| no refetch | FAIL | 5 |" in text
    assert '- row counts: `{"sqlite": 24}`' in text


def test_the_json_report_round_trips_with_statuses_and_counts() -> None:
    report = report_of(outcome([Check("a", True)]), outcome(not_run="no DSN"))
    document = json.loads(report_json(report, Masker()))
    assert document["overall"] == "PARTIAL"
    assert document["counts"] == {"PASS": 1, "FAIL": 0, "NOT_RUN": 1}
    assert [s["status"] for s in document["scenarios"]] == ["PASS", "NOT_RUN"]
    assert document["scenarios"][1]["reason"] == "no DSN"


# --- masking ------------------------------------------------------------------------------------


def test_host_paths_become_placeholders_longest_first(tmp_path: Path) -> None:
    out, repo = tmp_path / "out", tmp_path / "repo"
    masker = Masker([(tmp_path, "<tmp>"), (out, "<out>"), (repo, "<repo>")])
    assert masker.text(f"{out}/venv and {repo}/src and {tmp_path}/x") == (
        "<out>/venv and <repo>/src and <tmp>/x"
    )


def test_a_user_directory_that_no_rule_named_is_still_masked() -> None:
    users, home = "/" + "Users", "/" + "home"  # spelled in pieces: no path literal in the repo
    text = Masker().text(f"see {users}/someone/code/x and {home}/ci/work/y")
    assert "someone" not in text and "/ci/" not in text
    assert text.count("<home>") == 2


def test_throw_away_secrets_are_masked_in_text_and_nested_values() -> None:
    masker = Masker(secrets=["pw-0123456789abcdef"])
    assert masker.text("token pw-0123456789abcdef used") == "token <secret> used"
    nested = {"pw-0123456789abcdef": ["a pw-0123456789abcdef b", 3]}
    assert masker.value(nested) == {"<secret>": ["a <secret> b", 3]}


def test_a_too_short_secret_is_ignored_rather_than_masking_ordinary_text() -> None:
    assert Masker(secrets=["", "abc"]).text("abc def") == "abc def"


def test_the_rendered_report_holds_neither_a_secret_nor_a_host_path(tmp_path: Path) -> None:
    secret = "tok-feedfacefeedface"
    found = outcome([Check("c", True, f"{tmp_path}/x used {secret}")])
    found.evidence["where"] = str(tmp_path / "venv")
    masker = Masker([(tmp_path, "<out>")], [secret])
    report = report_of(found)
    for rendered in (report_json(report, masker), report_markdown(report, masker)):
        assert secret not in rendered and str(tmp_path) not in rendered
        assert "<out>" in rendered


# --- running one scenario safely ----------------------------------------------------------------


def test_run_guarded_turns_not_run_into_a_reason() -> None:
    def refuse(record: Recorder) -> None:
        del record
        raise NotRun("no DSN")

    found = acceptance.run_guarded(outcome(), refuse)
    assert (found.status, found.reason) == (Status.NOT_RUN, "no DSN")


def test_run_guarded_turns_a_crash_into_a_fail_that_names_it() -> None:
    def crash(record: Recorder) -> None:
        record.check("before the crash", True)
        raise KeyError("price")

    found = acceptance.run_guarded(outcome(), crash)
    assert found.status is Status.FAIL
    assert "KeyError" in found.reason and "price" in found.reason


# --- the registry: how W4b adds a scenario ------------------------------------------------------


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acceptance_lab, "_REGISTRY", {})


def test_scenarios_run_in_their_declared_order(empty_registry: None) -> None:
    del empty_registry
    scenario("B", "second", order=2)(lambda harness, record: None)
    scenario("A", "first", order=1)(lambda harness, record: None)
    assert [s.id for s in acceptance_lab.registered()] == ["A", "B"]


def test_a_real_scenario_replaces_its_placeholder(empty_registry: None) -> None:
    del empty_registry
    acceptance_lab.pending("S1", "Resume", order=1, reason="not implemented yet (W4b)")
    assert acceptance_lab.registered()[0].run is None
    scenario("S1", "Resume", order=1)(lambda harness, record: None)
    assert acceptance_lab.registered()[0].run is not None


def test_registering_a_scenario_twice_is_a_bug(empty_registry: None) -> None:
    del empty_registry
    scenario("S1", "Resume", order=1)(lambda harness, record: None)
    with pytest.raises(ValueError, match="twice"):
        scenario("S1", "Resume again", order=1)(lambda harness, record: None)


def test_every_scenario_of_the_spec_is_registered_with_a_real_function() -> None:
    by_id = {s.id: s for s in acceptance_lab.registered()}
    scenarios = {"S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "PG"}
    assert set(by_id) == scenarios | {"dist-check", "licenses", "image"}
    assert [s.id for s in by_id.values() if s.run is None] == [], "no placeholder is left"
    assert [s.id for s in acceptance_lab.registered()] == [
        "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "PG", "S9",
        "dist-check", "licenses", "image",
    ]  # fmt: skip


def test_postgres_is_not_run_without_a_dsn(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    found = acceptance.run_guarded(
        Outcome("PG", "pg"), lambda record: acceptance_scenarios.postgres(harness, record)
    )
    assert (found.status, found.reason) == (Status.NOT_RUN, "no DSN")


def test_only_selects_scenarios_and_lists_the_rest_as_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []

    def probe(harness: Harness, record: Recorder) -> None:
        del harness
        ran.append("S2")
        record.check("ran", True)

    monkeypatch.setitem(
        acceptance_lab._REGISTRY, "S2", acceptance_lab.Scenario("S2", "probe", 2, probe)
    )
    by_id = {o.id: o for o in acceptance.run_scenarios(make_harness(tmp_path), ["S2"])}
    assert ran == ["S2"] and by_id["S2"].status is Status.PASS
    assert by_id["S3"].reason == "not selected (--only)"
    assert by_id["S1"].reason == "not selected (--only)"
    assert by_id["PG"].reason == "not selected (--only)"


def test_an_unknown_scenario_id_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as stopped:
        acceptance.parse_args(["--out", str(tmp_path / "x"), "--only", "S99"])
    assert stopped.value.code == 2


# --- the output directory -----------------------------------------------------------------------


def test_the_output_directory_must_be_new_or_empty(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("mine")
    with pytest.raises(acceptance_lab.HarnessError, match="not empty"):
        acceptance.prepare_out(tmp_path, force=False)


def test_force_removes_only_a_directory_the_tool_made(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("mine")
    with pytest.raises(acceptance_lab.HarnessError, match="was not created by this tool"):
        acceptance.prepare_out(tmp_path, force=True)
    assert (tmp_path / "keep.txt").exists()
    fresh = tmp_path / "fresh"
    acceptance.prepare_out(fresh, force=False)
    (fresh / "stale.txt").write_text("old run")
    acceptance.prepare_out(fresh, force=True)
    assert not (fresh / "stale.txt").exists() and (fresh / acceptance.MARKER).exists()


# --- request counting: the Mock source's own lines ----------------------------------------------


def test_the_harness_reads_exactly_what_mock_serve_verbose_prints(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from catalog_sync.cli_mock import _print_request

    for entry in (
        LoggedRequest("POST", "/api/login", 200, sent_authorization=False, sent_cookie=False),
        LoggedRequest("GET", "/api/v1/materials", 200, sent_authorization=True, sent_cookie=False),
        LoggedRequest("GET", "/api/v1/materials/mat-0003", 503, True, False),
        LoggedRequest("GET", "/media/swatch/mat-0003.jpg", 200, False, False),
    ):
        _print_request(entry)
    parsed = parse_request_lines(capsys.readouterr().out)
    assert parsed == [
        Request("POST", "/api/login", 200),
        Request("GET", "/api/v1/materials", 200),
        Request("GET", "/api/v1/materials/mat-0003", 503),
        Request("GET", "/media/swatch/mat-0003.jpg", 200),
    ]


def test_requests_are_told_apart_by_what_they_fetch() -> None:
    lines = "\n".join(
        [
            "mock SaaS listening on http://127.0.0.1:1 (pid 7)",
            "POST /api/login -> 200",
            "GET /api/v1/materials -> 200",
            "GET /api/v1/materials -> 200",
            "GET /api/v1/materials/mat-0001 -> 200",
            "GET /api/v1/materials/mat-0001 -> 503",
            "GET /media/thumb/mat-0001.png -> 200",
            "GET /media/swatch/mat-0001.jpg -> 200",
            "GET /api/v1/suppliers -> 200",
            "GET /terms -> 200",
        ]
    )
    requests = parse_request_lines(lines)
    assert count_kinds(requests) == {"login": 1, "list": 3, "detail": 2, "asset": 2, "other": 1}
    assert per_item(requests, "detail") == {"mat-0001": 2}
    assert per_item(requests, "asset") == {"mat-0001": 2}


def test_the_harness_reads_what_the_platform_prints() -> None:
    lines = "\n".join(
        [
            PlatformLine("ingested", 200, "ab" * 32, "20261027T000000Z-aaaaaa", "1/2", 24).line(),
            PlatformLine("replayed", 200, "ab" * 32, "20261027T000000Z-aaaaaa", "1/2", 24).line(),
            PlatformLine("rejected", 422, None, None, None, 0, "bad key").line(),
        ]
    )
    events = parse_platform_lines(lines)
    assert [(e.kind, e.key_prefix, e.status) for e in events] == [
        ("ingested", "abababababab", 200),
        ("replayed", "abababababab", 200),
        ("rejected", None, 422),
    ]


def test_load_counts_are_read_per_sink() -> None:
    output = (
        "load -> sqlite-materials (sqlite), written:\n  inserted 0, updated 0, unchanged 24\n"
        "load -> platform-materials (http), written:\n  inserted 1, updated 2, unchanged 21\n"
    )
    assert acceptance_scenarios.load_counts(output) == {
        "sqlite-materials": (0, 0, 24),
        "platform-materials": (1, 2, 21),
    }


# --- the config the harness writes --------------------------------------------------------------


def test_the_config_names_no_mapping_and_carries_a_compliance_block_per_source() -> None:
    text = config_text(
        source_url="http://127.0.0.1:1", platform_url="http://127.0.0.1:2/x", today="2026-10-27"
    )
    assert "mapping" not in text
    assert text.count("[sources.") == 2 * len(ENTITIES)  # each source and its compliance block
    assert "tos_reviewed_on = 2026-10-27" in text
    assert 'auth_header_env = "ACCEPTANCE_PLATFORM_TOKEN"' in text


def test_the_config_without_compliance_has_none() -> None:
    text = config_text(
        source_url="http://127.0.0.1:1", platform_url=None, today="x", with_compliance=False,
        sinks=("sqlite",),
    )  # fmt: skip
    assert "compliance" not in text and 'kind = "http"' not in text


def test_the_config_never_holds_a_credential(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    text = config_text(
        source_url="http://127.0.0.1:1", platform_url="http://127.0.0.1:2/x", today="x"
    )
    for value in harness.secrets.values():
        assert value not in text


# --- comparing stores ---------------------------------------------------------------------------


Table = dict[str, dict[str, object]]


def stores_of(**tables: Table) -> dict[str, dict[tuple[str, str], dict[str, object]]]:
    return {
        name: {("mock.materials", sid): row for sid, row in rows.items()}
        for name, rows in tables.items()
    }


def test_equal_stores_have_no_mismatch_and_the_fields_are_counted() -> None:
    row: dict[str, object] = {"name": "a", "price": {"amount": "1", "currency": "USD"}}
    stores = stores_of(ref={"m1": row, "m2": row}, sqlite={"m1": dict(row), "m2": dict(row)})
    comparison = acceptance_compare.compare_stores(stores, "ref", ["name", "price"])
    assert comparison.is_equal and comparison.records == 2 and comparison.fields == 4


def test_a_different_value_a_missing_row_and_an_extra_row_are_each_a_mismatch() -> None:
    stores = stores_of(
        ref={"m1": {"name": "a"}, "m2": {"name": "b"}},
        sqlite={"m1": {"name": "CHANGED"}, "m3": {"name": "c"}},
    )
    comparison = acceptance_compare.compare_stores(stores, "ref", ["name"])
    problems = {(m["id"], m.get("problem") or m["field"]) for m in comparison.mismatches}
    assert problems == {("m1", "name"), ("m2", "missing"), ("m3", "extra")}
    assert not comparison.is_equal


def test_key_order_does_not_make_two_json_values_differ() -> None:
    stores = stores_of(ref={"m1": {"a": {"x": 1, "y": 2}}}, sqlite={"m1": {"a": {"y": 2, "x": 1}}})
    assert acceptance_compare.compare_stores(stores, "ref", ["a"]).is_equal


def test_the_owned_fields_exclude_what_a_drop_policy_withholds() -> None:
    sidecar = {"owned": ["name", "contact_email", "rating"], "sensitive": ["contact_email"]}
    assert acceptance_compare.owned_fields(sidecar) == ["name", "rating"]


def test_the_sample_is_fixed_and_always_ends_with_the_last_id() -> None:
    ids = [f"m{n:02d}" for n in range(1, 25)]
    sample = acceptance_compare.sample_ids(ids)
    assert sample == acceptance_compare.sample_ids(list(reversed(ids)))
    assert sample[0] == "m01" and sample[-1] == "m24" and len(sample) == 9


def test_the_oracle_gives_the_seeded_ids_and_the_expected_record() -> None:
    oracle = acceptance_compare.Oracle(REPO_ROOT)
    materials, suppliers = ENTITIES
    assert len(oracle.ids(materials)) == 24 and len(oracle.ids(suppliers)) == 9
    expected = oracle.expected(materials, "mat-0001", "http://127.0.0.1:1")
    assert expected["source_id"] == "mat-0001" and expected["source"] == "mock.materials"


# --- S9 -----------------------------------------------------------------------------------------


def make_harness(tmp_path: Path) -> Harness:
    return Harness(
        out=tmp_path,
        repo=REPO_ROOT,
        installed=Installed(tmp_path / "venv", tmp_path / "venv" / "site-packages"),
        secrets=acceptance_lab.generate_secrets(),
        masker=Masker(),
        today="2026-10-27",
        python="3.12",
    )


def test_s9_passes_when_the_real_workspace_is_as_it_was_and_fails_when_it_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalog_sync.testing.workspace_guard import fingerprint

    real = tmp_path / "real-workspace"
    real.mkdir()
    (real / "state.sqlite").write_text("x")
    monkeypatch.setattr(acceptance_scenarios, "real_workspace_locations", lambda: [real])
    harness = make_harness(tmp_path / "run")
    harness.fake_home.mkdir(parents=True)
    harness.real_workspaces_before = {real: fingerprint(real)}

    untouched = acceptance.run_guarded(
        Outcome("S9", "s9"), lambda record: acceptance_scenarios.real_workspace(harness, record)
    )
    assert untouched.status is Status.PASS

    (real / "leaked.txt").write_text("a demo run wrote this")
    touched = acceptance.run_guarded(
        Outcome("S9", "s9"), lambda record: acceptance_scenarios.real_workspace(harness, record)
    )
    assert touched.status is Status.FAIL and "is unchanged" in touched.reason


def test_s9_notices_a_command_that_fell_back_to_the_default_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acceptance_scenarios, "real_workspace_locations", lambda: [])
    harness = make_harness(tmp_path)
    (harness.fake_home / ".local/share/catalog-sync").mkdir(parents=True)
    found = acceptance.run_guarded(
        Outcome("S9", "s9"), lambda record: acceptance_scenarios.real_workspace(harness, record)
    )
    assert found.status is Status.FAIL and "fell back" in found.reason


# --- W4b: the background command, the waiting, the scans ---------------------------------------


def fake_cli(harness: Harness, script: str) -> None:
    """An installed `catalog-sync` that is a shell script: a `Lab` can be tested without a build."""
    cli = harness.installed.cli
    cli.parent.mkdir(parents=True)
    cli.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    cli.chmod(0o755)


def test_a_background_command_can_be_killed_and_comes_back_as_an_ordinary_run(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path)
    fake_cli(harness, 'echo "begun $1"; echo "oops" >&2; exec sleep 60')
    for name in ("work", "home", "logs", "state", "tmp", "fake-home"):
        (tmp_path / name).mkdir()
    lab = harness.lab("t")
    first, second = lab.start_command("pull", "a"), lab.start_command("pull", "b")
    assert first.is_running and second.is_running
    deadline = time.monotonic() + 10
    while (
        time.monotonic() < deadline and "begun" not in (lab.state / "background-1.out").read_text()
    ):
        time.sleep(0.02)
    first.kill()
    second.kill()
    killed = first.finish()
    assert killed.returncode == -signal.SIGKILL
    assert killed.stdout.strip() == "begun pull" and killed.stderr.strip() == "oops"
    assert second.finish().args == ("pull", "b")
    assert [run.args for run in lab.commands] == [("pull", "a"), ("pull", "b")]
    assert "exit -9" in lab.log_path.read_text(encoding="utf-8")


def source_with_log(tmp_path: Path, text: str) -> acceptance_lab.SourceProcess:
    log = tmp_path / "source.log"
    log.write_text(text, encoding="utf-8")
    return acceptance_lab.SourceProcess(["x", "--pid-file", str(tmp_path / "p")], {}, tmp_path, log)


DETAIL_LINES = "".join(f"GET /api/v1/materials/mat-{n:04d} -> 200\n" for n in range(1, 6))


def test_waiting_for_a_request_count_returns_as_soon_as_it_is_reached(tmp_path: Path) -> None:
    source = source_with_log(tmp_path, "POST /api/login -> 200\n" + DETAIL_LINES)
    seen = source.wait_for("detail", 3, since=1, is_alive=lambda: True, timeout_s=5)
    assert [r.kind for r in seen] == ["detail"] * 5 and source.mark() == 6


def test_waiting_gives_up_loudly_when_the_command_has_ended_or_nothing_comes(
    tmp_path: Path,
) -> None:
    source = source_with_log(tmp_path, DETAIL_LINES)
    with pytest.raises(acceptance_lab.HarnessError, match="before 9 detail request"):
        source.wait_for("detail", 9, since=0, is_alive=lambda: False, timeout_s=5)
    with pytest.raises(acceptance_lab.HarnessError, match=r"did not arrive in 0\.2 s"):
        source.wait_for("detail", 9, since=0, is_alive=lambda: True, timeout_s=0.2)


def test_settle_waits_for_a_log_that_is_still_growing(tmp_path: Path) -> None:
    source = source_with_log(tmp_path, DETAIL_LINES)
    late = threading.Timer(
        0.2, lambda: source.log.write_text(DETAIL_LINES + "GET /api/v1/materials/mat-0006 -> 200\n")
    )
    late.start()
    assert source.settle() == 6, "the request logged 0.2 s later is part of what was seen"
    late.join()


def test_run_ids_and_stage_counts_are_read_from_the_programs_output() -> None:
    listing = (
        "RUN                      SOURCE           STATUS    ITEMS FAILED USED BY  STARTED\n"
        "20261027T210402Z-3d817b  mock.materials   running      24      0       0  2026-10-27T21\n"
        "USED BY: items whose current raw data lives in that run\n"
    )
    assert [m["id"] for m in acceptance_scenarios._RUN_ID.finditer(listing)] == [
        "20261027T210402Z-3d817b"
    ]
    shown = (
        "items:    24\n  list   : 24 done\n  detail : 16 pending, 8 done\n  assets : 24 pending\n"
    )
    assert acceptance_scenarios.stage_counts(shown) == {"list": 24, "detail": 8}


def test_only_data_requests_after_the_last_login_count_against_it() -> None:
    requests = [
        Request("GET", "/api/v1/materials", 200),
        Request("POST", "/api/login", 200),
        Request("GET", "/api/v1/materials", 200),
        Request("GET", "/media/thumb/mat-0001.png", 200),
        Request("GET", "/api/v1/materials/mat-0001", 200),
    ]
    assert acceptance_scenarios._data_requests_since_last_login(requests) == 2
    assert acceptance_scenarios._data_requests_since_last_login([]) == 0


def test_the_failed_item_lines_and_the_share_are_read_as_the_program_prints_them() -> None:
    shown = "  failed detail mat-0012: detail answers HTTP 404\n  failed detail mat-0024: x\n"
    assert [m["id"] for m in acceptance_scenarios._FAILED_LINE.finditer(shown)] == [
        "mat-0012",
        "mat-0024",
    ]
    said = (
        "warning: 2 of 24 fetched item(s) failed (8.3%), within the limit of 10.0% "
        "(--max-failed-ratio); completed with item failures (exit 5)"
    )
    found = acceptance_scenarios._FAILED_SHARE.search(said)
    assert found is not None and (found["failed"], found["tried"], found["verdict"]) == (
        "2", "24", "within",
    )  # fmt: skip


def phase(*paths: str) -> list[Request]:
    return [Request("GET", path, 200) for path in paths]


def not_fetched_twice(*phases: list[Request], ids: tuple[str, ...] = ("mat-1", "mat-2")) -> Outcome:
    result = Outcome("S1", "resume")
    acceptance_scenarios._check_not_fetched_twice(Recorder(result), ids, phases)
    return result


def detail(n: int) -> str:
    return f"/api/v1/materials/mat-{n}"


def picture(n: int, role: str) -> str:
    return f"/media/{role}/mat-{n}.png"


def test_a_pull_that_resumed_cleanly_passes_the_not_fetched_twice_check() -> None:
    killed_in_details = phase(detail(1))
    killed_in_assets = phase(detail(2), picture(1, "thumb"))
    finished = phase(picture(1, "swatch"), picture(2, "thumb"), picture(2, "swatch"))
    assert not_fetched_twice(killed_in_details, killed_in_assets, finished).status is Status.PASS


def test_the_one_request_in_flight_at_a_kill_may_be_made_again() -> None:
    killed_in_details = phase(detail(1))
    killed_in_assets = phase(detail(1), detail(2), picture(1, "thumb"))
    finished = phase(
        picture(1, "thumb"), picture(1, "swatch"), picture(2, "thumb"), picture(2, "swatch")
    )
    assert not_fetched_twice(killed_in_details, killed_in_assets, finished).status is Status.PASS


def test_a_detail_fetched_again_that_was_not_in_flight_fails_the_check() -> None:
    first = phase(detail(1), detail(2))  # detail 1 was long done when the kill came
    second = phase(detail(1), picture(1, "thumb"))
    third = phase(picture(1, "swatch"), picture(2, "thumb"), picture(2, "swatch"))
    found = not_fetched_twice(first, second, third)
    assert found.status is Status.FAIL and "made twice" in found.reason


def test_an_item_that_was_never_fetched_fails_the_check() -> None:
    found = not_fetched_twice(
        phase(detail(1)), phase(picture(1, "thumb")), phase(picture(1, "swatch"))
    )
    assert found.status is Status.FAIL


def test_values_are_found_in_the_place_that_holds_them_and_counted_once_each(
    tmp_path: Path,
) -> None:
    (tmp_path / "cache" / "runs").mkdir(parents=True)
    (tmp_path / "cache" / "runs" / "a.json").write_text('{"mail": "ann@example.com"}')
    (tmp_path / "cache" / "runs" / "b.json").write_text("ann@example.com bob@example.com")
    (tmp_path / "out.db").write_bytes(b"\x00\x01bob@example.com\x02")
    found = acceptance_compare.find_values(
        [("mail", "ann@example.com"), ("mail", "bob@example.com"), ("key", "s3cret-value")],
        [
            *acceptance_compare.tree_blobs(tmp_path, "workspace"),
            ("output", "ünï bob@example.com".encode()),
        ],
    )
    assert found == {
        ("mail", "workspace/cache"): 2,
        ("mail", "workspace/out.db"): 1,
        ("mail", "output"): 1,
    }


def test_a_value_in_a_place_the_contract_does_not_allow_fails_the_scan() -> None:
    kind = acceptance_scenarios.KIND_SENSITIVE
    needles = [(kind, "ann@example.com")]
    allowed = {(kind, "workspace/cache"): 1, (kind, "normalized JSONL"): 1}
    good = Outcome("S7", "x")
    acceptance_scenarios._check_places(Recorder(good), needles, allowed)
    # the documented-behaviour checks want all of a kind; the only value is in both places
    assert good.status is Status.PASS
    leaked = Outcome("S7", "x")
    acceptance_scenarios._check_places(
        Recorder(leaked), needles, {**allowed, (kind, "workspace/out"): 1}
    )
    assert leaked.status is Status.FAIL and "workspace/out" in str(leaked.checks[0].detail)


def test_a_credential_found_anywhere_fails_the_scan() -> None:
    kind = acceptance_scenarios.KIND_LOGIN_KEY
    leaked = Outcome("S7", "x")
    acceptance_scenarios._check_places(
        Recorder(leaked), [(kind, "pw-xyz")], {(kind, "command output"): 1}
    )
    assert leaked.status is Status.FAIL


def test_the_oracle_gives_the_source_s_own_value_of_a_field() -> None:
    oracle = acceptance_compare.Oracle(REPO_ROOT)
    supplier = oracle.dataset.suppliers[0]
    suppliers = ENTITIES[1]
    assert (
        oracle.source_attribute(suppliers, supplier.id, "contact_email") == supplier.contact_email
    )
    assert oracle.source_attribute(suppliers, supplier.id, "no_such_field") is None


def test_the_config_can_name_a_postgres_sink_by_variable_never_by_value(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    harness.postgres_dsn = "postgresql://127.0.0.1:5432/database-named-in-the-dsn"
    text = config_text(
        source_url="http://127.0.0.1:1", platform_url=None, today="x",
        sinks=("postgres",), postgres_schema="acc_ab12",
    )  # fmt: skip
    assert 'kind = "postgres"' in text and 'schema = "acc_ab12"' in text
    assert f'dsn_env = "{acceptance_lab.POSTGRES_DSN_ENV}"' in text
    assert "database-named-in-the-dsn" not in text
    env = harness.environment(tmp_path / "ws")
    assert env[acceptance_lab.POSTGRES_DSN_ENV] == harness.postgres_dsn
    assert acceptance_lab.POSTGRES_DSN_ENV not in make_harness(tmp_path).environment(tmp_path)


def test_a_second_source_in_a_lab_has_its_own_log_and_can_take_over_the_port(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path)
    log = tmp_path / "recorded-argv"
    fake_cli(harness, f'echo "$@" >> {log}; exec sleep 0.1')
    for name in ("work", "home", "logs", "state", "tmp", "fake-home"):
        (tmp_path / name).mkdir()
    lab = harness.lab("t")
    with pytest.raises(acceptance_lab.HarnessError):  # the fake never writes a pid file: it exits
        lab.start_source(gone_every=12, expire_after=2, materials=23, port=4321, label="second")
    argv = log.read_text(encoding="utf-8").split()
    assert argv[argv.index("--port") + 1] == "4321" and argv[argv.index("--materials") + 1] == "23"
    assert argv[argv.index("--gone-every") + 1] == "12"
    assert argv[argv.index("--expire-after") + 1] == "2"
    assert argv[argv.index("--pid-file") + 1].endswith("second.pid")


def test_postgres_schema_exists_during_the_block_and_is_gone_after(
    postgres_dsn: str,
) -> None:
    from catalog_sync.sinks.postgres import connect

    def exists(name: str) -> bool:
        conn = connect(postgres_dsn)
        try:
            row = conn.execute(
                "SELECT count(*) FROM information_schema.schemata WHERE schema_name = %s", (name,)
            ).fetchone()
        finally:
            conn.close()
        return bool(row and row[0])

    with acceptance_compare.postgres_schema(postgres_dsn, "acc_unit_test"):
        assert exists("acc_unit_test")
        assert acceptance_compare.read_postgres(postgres_dsn, "materials", "acc_unit_test") == {}
    assert not exists("acc_unit_test")


# --- the full run -------------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("CATALOG_SYNC_RUN_ACCEPTANCE") != "1",
    reason="builds and installs the package, needs uv and a network: set "
    "CATALOG_SYNC_RUN_ACCEPTANCE=1",
)
def test_the_harness_runs_end_to_end_from_the_built_package(tmp_path: Path) -> None:
    out = tmp_path / "acceptance"
    done = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "acceptance.py"), "--out", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-1000:]
    document = json.loads((out / "acceptance-report.json").read_text(encoding="utf-8"))
    statuses = {s["id"]: s["status"] for s in [*document["steps"], *document["scenarios"]]}
    assert document["counts"]["FAIL"] == 0
    assert all(statuses[i] == "PASS" for i in ("build", "install-wheel", "install-sdist"))
    assert all(
        statuses[i] == "PASS" for i in ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9")
    )
    assert statuses["PG"] == "NOT_RUN", "without a DSN the Postgres scenario is listed, not passed"
    assert statuses["dist-check"] == "PASS" and statuses["licenses"] == "PASS"
    assert statuses["image"] in ("PASS", "NOT_RUN"), "NOT_RUN only where docker is absent"
    assert (out / "acceptance-report.md").read_text(encoding="utf-8").startswith("# Acceptance")
    assert str(out) not in (out / "acceptance-report.md").read_text(encoding="utf-8")
