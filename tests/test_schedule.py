"""`schedule` and `health`: the loop's decisions, with no real sleeping, and the signals for real.

The loop is run with a FakeClock for its sleep and a scripted cycle, so a day of backoff takes no
time. The CLI-level tests use the replay source of the pipeline tests (no network); the signal
tests deliver real signals to this very process, always from inside the loop's own cycle, after
the handlers are installed, so that a regression cannot kill the test run.
"""

from __future__ import annotations

import fcntl
import json
import signal
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from catalog_sync import cli, cli_schedule
from catalog_sync.audit import AuditEvent, AuditLog
from catalog_sync.cli_schedule import MaxAge, Plan, Scheduler
from catalog_sync.config import Workspace, load_config
from catalog_sync.errors import (
    EXIT_COMPLETED_WITH_FAILURES,
    AuthExpired,
    ComplianceError,
    CredentialMissing,
    FuseTripped,
    NetworkError,
)
from catalog_sync.testing.fake_transport import FakeClock
from tests.test_cli_pipeline import Bench

EVERY = 6 * 3600.0


def plan(**overrides: object) -> Plan:
    fields: dict[str, object] = {
        "profile": "demo",
        "source": "demo-source",
        "every_s": EVERY,
        "jitter_s": 0.0,
        "max_consecutive_failures": 5,
        "is_one_shot": False,
    }
    return Plan(**{**fields, **overrides})  # type: ignore[arg-type]


class Rig:
    """A Scheduler whose cycles are scripted: each entry is an exit code or an exception to raise.
    After the script is used up the stop flag is set, which ends the loop like a SIGTERM would."""

    def __init__(self, tmp_path: Path, script: list[int | BaseException], **plan_overrides: object):
        self.clock = FakeClock()
        self.messages: list[str] = []
        self.stop = threading.Event()
        self.log = AuditLog(tmp_path / "audit.jsonl")
        self._script = list(script)
        self.cycles = 0
        self.scheduler = Scheduler(
            plan(**plan_overrides),
            run_cycle=self._cycle,
            log=self.log,
            stop=self.stop,
            say=self.messages.append,
            sleep=self.clock.sleep,
            jitter=lambda limit: limit,
        )

    def _cycle(self) -> int:
        self.cycles += 1
        step = self._script.pop(0)
        if not self._script:
            self.stop.set()
        if isinstance(step, BaseException):
            raise step
        return step

    def run(self) -> int:
        return self.scheduler.run()

    @property
    def schedule_lines(self) -> list[dict[str, object]]:
        return [e for e in self.log.read() if e["command"] == "schedule"]


# --- durations ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"), [("45s", 45), ("30m", 1800), ("6h", 21_600), ("1d", 86_400), ("0s", 0)]
)
def test_a_duration_is_a_number_and_a_unit(text: str, seconds: int) -> None:
    assert cli_schedule.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "6", "h", "6 hours", "-5m", "1.5h", "6H"])
def test_a_duration_without_a_unit_or_with_anything_else_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="expected a duration"):
        cli_schedule.parse_duration(text)


def test_durations_print_in_the_same_units_they_are_typed_in() -> None:
    assert cli_schedule.format_duration(21_600) == "6h"
    assert cli_schedule.format_duration(5_400) == "1h30m"
    assert cli_schedule.format_duration(90_061) == "1d1h1m1s"
    assert cli_schedule.format_duration(0) == "0s"


def test_the_max_age_is_a_duration_or_a_multiple_of_the_interval() -> None:
    assert MaxAge.parse("2x-interval").limit(3600) == 7200
    assert MaxAge.parse("1.5x-interval").limit(3600) == 5400
    assert MaxAge.parse("90m").limit(3600) == 5400


def test_a_multiple_of_the_interval_gets_the_jitter_once_and_a_fixed_age_gets_none() -> None:
    assert MaxAge.parse("2x-interval").limit(3600, 600) == 7800
    assert MaxAge.parse("90m").limit(3600, 600) == 5400


# --- the loop: success --------------------------------------------------------------------------


def test_after_a_good_cycle_the_loop_waits_the_interval_and_goes_again(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [0, 0, 0])
    assert rig.run() == 0
    assert rig.cycles == 3
    assert rig.clock.sleeps == [EVERY, EVERY]  # none after the last: the stop flag was set
    assert rig.messages[-1].startswith("stop requested")


def test_each_cycle_is_audited_with_the_interval_the_health_check_will_read(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, [0, 1, AuthExpired("expired")])
    rig.run()
    lines = rig.schedule_lines
    assert [line["outcome"] for line in lines] == ["ok", "failed", "error:AuthExpired"]
    assert [line["counts"] for line in lines] == [
        {"cycle": 1, "exit_code": 0, "interval_seconds": 21_600, "jitter_seconds": 0},
        {"cycle": 2, "exit_code": 1, "interval_seconds": 21_600, "jitter_seconds": 0},
        {"cycle": 3, "exit_code": 2, "interval_seconds": 21_600, "jitter_seconds": 0},
    ]
    assert {line["profile"] for line in lines} == {"demo"}
    assert {line["source"] for line in lines} == {"demo-source"}


def test_jitter_is_added_to_every_pause(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [0, 0], jitter_s=600.0)
    rig.run()
    assert rig.clock.sleeps == [EVERY + 600.0]


def test_the_audit_line_records_the_jitter_so_health_can_allow_for_it(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [0], jitter_s=600.0)
    rig.run()
    assert rig.schedule_lines[0]["counts"]["jitter_seconds"] == 600  # type: ignore[index]


# --- the loop: a cycle that finished with some failed items (exit 5) ----------------------------


def test_a_cycle_that_finished_with_some_failed_items_is_a_success_marked_degraded(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, [0, EXIT_COMPLETED_WITH_FAILURES, 0])
    assert rig.run() == 0
    assert rig.cycles == 3
    assert rig.clock.sleeps == [EVERY, EVERY], "no backoff: it waits the interval like after a 0"
    assert [line["outcome"] for line in rig.schedule_lines] == ["ok", "ok_with_failures", "ok"]
    assert [line["counts"]["exit_code"] for line in rig.schedule_lines] == [0, 5, 0]  # type: ignore[index]
    assert any("cycle 2 finished degraded" in message for message in rig.messages)


def test_degraded_cycles_never_count_towards_the_failure_limit(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [EXIT_COMPLETED_WITH_FAILURES] * 6, max_consecutive_failures=2)
    assert rig.run() == 0
    assert rig.cycles == 6


def test_a_degraded_cycle_starts_the_failure_count_again(tmp_path: Path) -> None:
    script = [1, 1, 1, 1, EXIT_COMPLETED_WITH_FAILURES, 1, 1, 1, 1, 0]
    rig = Rig(tmp_path, script, max_consecutive_failures=5)
    assert rig.run() == 0
    assert rig.cycles == 10


def test_a_run_that_exceeded_its_limit_exits_one_and_is_a_failed_cycle(tmp_path: Path) -> None:
    """`run` turns 'too many failed items' into exit 1 itself; the loop then backs off."""
    rig = Rig(tmp_path, [1, 1], max_consecutive_failures=2)
    assert rig.run() == 1
    assert [line["outcome"] for line in rig.schedule_lines] == ["failed", "failed"]


def test_a_one_shot_cycle_that_finished_degraded_exits_with_five(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [EXIT_COMPLETED_WITH_FAILURES, 0], is_one_shot=True)
    assert rig.run() == EXIT_COMPLETED_WITH_FAILURES
    assert rig.clock.sleeps == []


def test_the_limit_given_to_schedule_reaches_every_cycles_run(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits: list[float | None] = []

    def fake(_context: object, _profile: str, *, max_failed_ratio: float | None = None) -> int:
        limits.append(max_failed_ratio)
        return 0

    monkeypatch.setattr(cli_schedule, "run_profile", fake)
    assert schedule(bench, "--once", "--max-failed-ratio", "0.25") == 0
    assert schedule(bench, "--once") == 0
    assert limits == [0.25, None], "None means: the profile's own limit"


# --- the loop: failures -------------------------------------------------------------------------


def test_failures_back_off_exponentially_and_the_fifth_in_a_row_ends_the_process(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, [1, NetworkError("down"), 1, FuseTripped("fuse"), 1, 0])
    assert rig.run() == 1
    assert rig.cycles == 5, "the sixth cycle must never start"
    assert rig.clock.sleeps == [60.0, 120.0, 240.0, 480.0]
    assert "giving up after 5 consecutive failed cycles" in rig.messages[-1]


def test_the_backoff_never_exceeds_the_interval(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [1, 1, 1, 1, 1], every_s=100.0, max_consecutive_failures=6)
    rig.run()
    assert rig.clock.sleeps == [60.0, 100.0, 100.0, 100.0]  # none after the last cycle


def test_the_limit_is_the_one_given(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [1, 1, 1, 0], max_consecutive_failures=2)
    assert rig.run() == 1
    assert rig.cycles == 2


def test_a_good_cycle_starts_the_count_again(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [1, 1, 1, 1, 0, 1, 1, 1, 1, 0], max_consecutive_failures=5)
    assert rig.run() == 0
    assert rig.cycles == 10


def test_a_failure_message_names_the_cause_and_hides_a_secret(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [NetworkError("could not reach api; Authorization: Bearer abcdef123456")])
    rig.run()
    assert any("cycle 1 failed: could not reach api" in m for m in rig.messages)
    assert not any("abcdef123456" in m for m in rig.messages)


# --- the loop: credential and compliance --------------------------------------------------------


def test_an_expired_credential_is_said_every_cycle_and_never_stops_the_loop(tmp_path: Path) -> None:
    script: list[int | BaseException] = [AuthExpired("token expired"), CredentialMissing("none")]
    rig = Rig(tmp_path, script * 4, max_consecutive_failures=2)
    assert rig.run() == 0
    assert rig.cycles == 8, "credential problems do not count towards the limit"
    assert rig.clock.sleeps == [EVERY] * 7, "it waits the interval; it does not spin"
    said = [m for m in rig.messages if "re-login on the host" in m]
    assert len(said) == 8


def test_a_missing_compliance_declaration_stops_the_scheduler_without_a_retry(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, [0, ComplianceError("no declaration"), 0])
    with pytest.raises(ComplianceError):
        rig.run()
    assert rig.cycles == 2
    assert rig.clock.sleeps == [EVERY]
    assert rig.schedule_lines[-1]["outcome"] == "error:ComplianceError"


@pytest.mark.parametrize(
    ("step", "expected"),
    [(0, 0), (1, 1), (AuthExpired("expired"), 2), (FuseTripped("fuse"), 3)],
)
def test_a_one_shot_exits_with_the_code_of_its_cycle_and_never_sleeps(
    tmp_path: Path, step: int | BaseException, expected: int
) -> None:
    rig = Rig(tmp_path, [step, 0], is_one_shot=True)
    assert rig.run() == expected
    assert rig.cycles == 1
    assert rig.clock.sleeps == []


# --- the loop: stopping -------------------------------------------------------------------------


def test_a_loop_asked_to_stop_before_it_starts_runs_no_cycle(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [0])
    rig.stop.set()
    assert rig.run() == 0
    assert rig.cycles == 0
    assert rig.schedule_lines == []


def test_a_stop_during_a_cycle_lets_the_cycle_finish_and_skips_the_pause(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [0, 0])  # the stop flag is set while the second cycle runs
    assert rig.run() == 0
    assert rig.schedule_lines[-1]["outcome"] == "ok", "the cycle in progress was finished"
    assert len(rig.clock.sleeps) == 1


def test_a_failed_last_cycle_still_counts_when_the_stop_arrives_with_it(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [1], max_consecutive_failures=1)
    assert rig.run() == 1


# --- the command --------------------------------------------------------------------------------


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    return Bench(tmp_path)


def schedule(bench: Bench, *args: str) -> int:
    return bench.run("schedule", "demo", "--every", "1h", *args)


def scripted_cycles(monkeypatch: pytest.MonkeyPatch, step: Callable[[int], int]) -> list[int]:
    """Replace the cycle's body; `step(n)` gets the cycle number and returns the exit code."""
    calls: list[int] = []

    def fake(_context: object, _profile: str, *, max_failed_ratio: float | None = None) -> int:
        calls.append(len(calls) + 1)
        return step(calls[-1])

    monkeypatch.setattr(cli_schedule, "run_profile", fake)
    return calls


def test_one_cycle_of_the_command_is_a_real_run_plus_a_schedule_line(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    assert schedule(bench, "--once") == 0
    trail = bench.audit
    assert [e["command"] for e in trail] == [
        "pull",
        "normalize",
        "load",
        "load",
        "load",
        "run",
        "schedule",
    ]
    assert trail[-1]["outcome"] == "ok"
    assert trail[-1]["counts"] == {
        "cycle": 1,
        "exit_code": 0,
        "interval_seconds": 3600,
        "jitter_seconds": 0,
    }
    out = capsys.readouterr().out
    assert "schedule demo: cycle 1 finished" in out
    assert bench.run("health") == 0
    assert (
        "healthy: the last successful scheduled cycle of 'demo' finished" in capsys.readouterr().out
    )


def test_a_second_scheduler_for_the_same_profile_is_refused(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    bench.home.mkdir(parents=True)
    with (bench.home / "schedule-demo.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert schedule(bench, "--once") == 1
    assert "another scheduler already runs profile 'demo'" in capsys.readouterr().err
    assert bench.audit == [], "the refused one must not have run anything"
    assert schedule(bench, "--once") == 0, "and the lock is free again once its holder is gone"


def test_the_lock_is_per_profile(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    scripted_cycles(monkeypatch, lambda _n: 0)
    bench.home.mkdir(parents=True)
    with (bench.home / "schedule-other.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert schedule(bench, "--once") == 0


def test_a_compliance_failure_exits_four_and_says_why(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(_n: int) -> int:
        raise ComplianceError("no compliance declaration")

    calls = scripted_cycles(monkeypatch, refuse)
    assert schedule(bench) == 4
    assert calls == [1]
    assert "error: no compliance declaration" in capsys.readouterr().err
    assert bench.audit[-1]["outcome"] == "error:ComplianceError"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--every", "0s"], "--every must be longer than zero"),
        (["--every", "1h", "--jitter", "2h"], "--jitter must not be longer than --every"),
        (["--every", "1h", "--max-consecutive-failures", "0"], "at least 1"),
    ],
)
def test_nonsense_options_are_refused_before_anything_runs(
    bench: Bench, capsys: pytest.CaptureFixture[str], arguments: list[str], message: str
) -> None:
    assert bench.run("schedule", "demo", *arguments) == 1
    assert message in capsys.readouterr().err
    assert bench.audit == []


def test_an_unknown_profile_is_refused_with_the_names_that_exist(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bench.run("schedule", "nope", "--every", "1h") == 1
    assert "unknown profile 'nope' (defined: demo)" in capsys.readouterr().err


@pytest.mark.parametrize("arguments", [[], ["--every", "6"], ["--every", "1h", "--jitter", "x"]])
def test_every_needs_a_duration_with_a_unit(bench: Bench, arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        bench.run("schedule", "demo", *arguments)
    assert raised.value.code == 2


def test_the_command_needs_a_config_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--workspace", str(tmp_path), "schedule", "p", "--every", "1h"]) == 1
    assert "`schedule` needs a config file" in capsys.readouterr().err


# --- signals ------------------------------------------------------------------------------------


@pytest.fixture
def handlers_restored() -> Iterator[None]:
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    yield
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before


def send_to_main_thread(sig: signal.Signals) -> None:
    signal.pthread_kill(threading.main_thread().ident or 0, sig)


@pytest.mark.usefixtures("handlers_restored")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_during_the_pause_ends_the_wait_at_once(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, sig: signal.Signals
) -> None:
    def first_cycle_then_signal(number: int) -> int:
        if number == 1:  # the handlers are installed by now; the pause comes next
            threading.Timer(0.2, send_to_main_thread, args=(sig,)).start()
        else:
            send_to_main_thread(signal.SIGTERM)  # only reached if the first signal was ignored
        return 0

    calls = scripted_cycles(monkeypatch, first_cycle_then_signal)
    started = time.monotonic()
    assert bench.run("schedule", "demo", "--every", "30s") == 0
    assert time.monotonic() - started < 10, "the 30 s pause must have been cut short"
    assert calls == [1]


@pytest.mark.usefixtures("handlers_restored")
def test_a_signal_during_a_cycle_lets_it_finish_and_then_exits_zero(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def signalled(_number: int) -> int:
        signal.raise_signal(signal.SIGTERM)
        return 0

    calls = scripted_cycles(monkeypatch, signalled)
    assert schedule(bench) == 0
    assert calls == [1]
    assert bench.audit[-1]["outcome"] == "ok", "the cycle was not cut short"
    assert "stop requested" in capsys.readouterr().out


@pytest.mark.usefixtures("handlers_restored")
def test_a_second_signal_interrupts_the_cycle_where_it_is(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def impatient(_number: int) -> int:
        signal.raise_signal(signal.SIGTERM)
        signal.raise_signal(signal.SIGTERM)
        return 0

    scripted_cycles(monkeypatch, impatient)
    assert schedule(bench) == 130
    assert bench.audit[-1]["command"] == "schedule"
    assert bench.audit[-1]["outcome"] == "interrupted"
    assert "interrupted" in capsys.readouterr().err


# --- the renewal window -------------------------------------------------------------------------


def credential_context(tmp_path: Path, renew_before_days: float) -> cli.Context:
    config = tmp_path / "catalog-sync.toml"
    config.write_text(
        f"""
[credentials.mock]
kind = "mock"
renew_before_days = {renew_before_days}

[sources.m]
plugin = "mock.materials"
credential = "mock"
base_url = "http://127.0.0.1:1"

[sinks.s]
kind = "jsonl"
path = "out.jsonl"

[profiles.p]
source = "m"
sinks = ["s"]
""",
        encoding="utf-8",
    )
    return cli.Context(load_config(config), Workspace(tmp_path / "ws"))


def test_a_renewal_window_shorter_than_the_interval_is_warned_about_each_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_schedule, "run_profile", lambda _context, _profile, **_kw: 0)
    context = credential_context(tmp_path, renew_before_days=0.5)
    every_two_days = plan(profile="p", source="m", every_s=2 * 86_400.0)
    for _ in range(2):
        assert cli_schedule._run_one_cycle(context, every_two_days) == 0
    warnings = [
        line for line in capsys.readouterr().err.splitlines() if "renew_before_days" in line
    ]
    assert len(warnings) == 2
    assert "(0.5) is shorter than the schedule interval (2 days)" in warnings[0]


def test_a_window_that_covers_the_interval_is_not_warned_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_schedule, "run_profile", lambda _context, _profile, **_kw: 0)
    context = credential_context(tmp_path, renew_before_days=2)
    cli_schedule._run_one_cycle(context, plan(profile="p", source="m", every_s=6 * 3600.0))
    assert capsys.readouterr().err == ""


def test_a_source_without_a_credential_has_nothing_to_warn_about(bench: Bench) -> None:
    context = cli.Context(load_config(bench.config), Workspace(bench.home))
    assert cli_schedule._window_warning(context, plan(source="demo")) is None


# --- health -------------------------------------------------------------------------------------


def write_schedule_line(
    home: Path,
    *,
    ago: timedelta,
    outcome: str = "ok",
    interval_s: int = 21_600,
    jitter_s: int | None = None,
    profile: str = "demo",
) -> None:
    log = AuditLog(home / "audit.jsonl", clock=lambda: datetime.now(UTC) - ago)
    counts = {"cycle": 1, "exit_code": 0, "interval_seconds": interval_s}
    if jitter_s is not None:  # lines written before the jitter was recorded have none
        counts["jitter_seconds"] = jitter_s
    log.append(AuditEvent(command="schedule", outcome=outcome, profile=profile, counts=counts))


def health(tmp_path: Path, *args: str) -> int:
    return cli.main(["--workspace", str(tmp_path), "health", *args])


def test_health_is_unhealthy_in_a_workspace_that_never_ran_a_cycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert health(tmp_path) == 1
    assert "unhealthy: no scheduled cycle has finished successfully" in capsys.readouterr().err


def test_a_recent_success_is_healthy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_schedule_line(tmp_path, ago=timedelta(hours=1))
    assert health(tmp_path) == 0
    assert "finished 1h ago (limit 12h)" in capsys.readouterr().out


def test_the_default_limit_is_twice_the_interval_the_scheduler_ran_with(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_schedule_line(tmp_path, ago=timedelta(hours=11, minutes=50))
    assert health(tmp_path) == 0
    write_schedule_line(tmp_path.joinpath("late"), ago=timedelta(hours=12, minutes=10))
    assert health(tmp_path / "late") == 1
    assert "unhealthy: the last successful scheduled cycle" in capsys.readouterr().err


def test_the_default_limit_allows_for_the_longest_jitter_the_scheduler_ran_with(
    tmp_path: Path,
) -> None:
    """Jitter may be as long as the interval, so a healthy loop can wait 2x the interval between
    two finished cycles before the run time is even added: 12h10m is then not yet late."""
    ran_with_full_jitter = tmp_path / "jittery"
    write_schedule_line(ran_with_full_jitter, ago=timedelta(hours=12, minutes=10), jitter_s=21_600)
    assert health(ran_with_full_jitter) == 0
    write_schedule_line(tmp_path / "too-late", ago=timedelta(hours=18, minutes=10), jitter_s=21_600)
    assert health(tmp_path / "too-late") == 1
    without = tmp_path / "no-jitter"
    write_schedule_line(without, ago=timedelta(hours=12, minutes=10), jitter_s=0)
    assert health(without) == 1, "no jitter, no extra allowance"


def test_a_line_written_before_jitter_was_recorded_still_reads_as_no_jitter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_schedule_line(tmp_path, ago=timedelta(hours=1))  # no jitter_seconds in its counts
    assert health(tmp_path) == 0
    assert "(limit 12h)" in capsys.readouterr().out


def test_a_degraded_cycle_keeps_the_service_healthy_and_is_reported_as_degraded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_schedule_line(tmp_path, ago=timedelta(hours=1), outcome="ok_with_failures")
    assert health(tmp_path) == 0
    out = capsys.readouterr().out
    assert out.startswith("healthy, degraded: ") and "finished with failed items" in out


def test_a_healthy_cycle_is_not_called_degraded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_schedule_line(tmp_path, ago=timedelta(hours=1))
    assert health(tmp_path) == 0
    assert capsys.readouterr().out.startswith("healthy: ")


def test_a_degraded_cycle_that_is_too_old_is_unhealthy_like_any_other(tmp_path: Path) -> None:
    write_schedule_line(tmp_path, ago=timedelta(hours=40), outcome="ok_with_failures")
    assert health(tmp_path) == 1


def test_max_age_overrides_the_default(tmp_path: Path) -> None:
    write_schedule_line(tmp_path, ago=timedelta(hours=20))
    assert health(tmp_path) == 1
    assert health(tmp_path, "--max-age", "24h") == 0
    assert health(tmp_path, "--max-age", "4x-interval") == 0
    assert health(tmp_path, "--max-age", "30m") == 1


def test_only_a_successful_cycle_counts_however_recent_the_failures_are(tmp_path: Path) -> None:
    write_schedule_line(tmp_path, ago=timedelta(hours=3))
    write_schedule_line(tmp_path, ago=timedelta(minutes=5), outcome="failed")
    write_schedule_line(tmp_path, ago=timedelta(minutes=1), outcome="error:AuthExpired")
    assert health(tmp_path) == 0, "the success of three hours ago is the one that counts"

    only_old_success = tmp_path / "old"
    write_schedule_line(only_old_success, ago=timedelta(hours=40))
    write_schedule_line(only_old_success, ago=timedelta(minutes=5), outcome="failed")
    assert health(only_old_success) == 1, "recent failures do not rescue an old success"


def test_other_commands_in_the_audit_log_do_not_count(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(AuditEvent(command="run", outcome="ok", profile="demo"))
    assert health(tmp_path) == 1


def test_health_can_be_asked_about_one_profile(tmp_path: Path) -> None:
    write_schedule_line(tmp_path, ago=timedelta(hours=1), profile="fast")
    write_schedule_line(tmp_path, ago=timedelta(hours=30), profile="slow")
    assert health(tmp_path, "--profile", "fast") == 0
    assert health(tmp_path, "--profile", "slow") == 1
    assert health(tmp_path, "--profile", "missing") == 1


def test_a_damaged_audit_log_is_unhealthy_not_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "audit.jsonl").write_text('{"command": "schedule"\n', encoding="utf-8")
    assert health(tmp_path) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_a_schedule_line_without_its_interval_is_a_damaged_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    line = {
        "ts": datetime.now(UTC).isoformat(),
        "command": "schedule",
        "outcome": "ok",
        "counts": {},
    }
    (tmp_path / "audit.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    assert health(tmp_path) == 1
    assert "no 'interval_seconds'" in capsys.readouterr().err


def test_a_max_age_that_is_neither_a_duration_nor_a_multiple_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        health(tmp_path, "--max-age", "soon")
    assert raised.value.code == 2
