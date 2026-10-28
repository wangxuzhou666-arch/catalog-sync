"""The ``schedule`` and ``health`` commands: run a profile on a timer, and say whether that works.

``schedule`` is a foreground loop around the same code path as ``run`` (cli_pipeline.run_profile),
so a scheduled cycle leaves the same audit trail as the command typed by hand, plus one
``schedule`` line per cycle. It is meant to be PID 1 of a container, so it never daemonises and
what it does with each kind of failure is spelled out:

* exit 0 from ``run``: wait ``--every`` (plus jitter), start again;
* exit 5 from ``run`` (it finished, but some items failed, no more than ``--max-failed-ratio`` of
  those it fetched): a successful cycle that is marked degraded in the output and the audit line
  (outcome ``ok_with_failures``); the loop goes on as after exit 0. More failed items than that
  make ``run`` exit 1, which is the last case below;
* the credential is expired or missing (exit 2): keep going and say so every cycle, because a
  restart cannot fix it and only a login on the host can; wait ``--every``, never spin;
* the compliance declaration is missing or invalid (exit 4): stop at once with exit 4, retrying
  cannot make it valid;
* anything else (a failed run, a tripped fuse, a network error): exponential backoff capped at
  ``--every``; after ``--max-consecutive-failures`` in a row, exit 1 so a supervisor restarts the
  process visibly instead of leaving a loop that has been failing for days.

The pause is measured from the end of a cycle, not from its start: a run that takes longer than
``--every`` then delays the next one instead of being followed by another at once. The price is a
cadence that drifts by the run time and by the jitter, which ``health`` allows for: by default it
accepts twice the interval plus the longest jitter the scheduler ran with.

``health`` answers one question for a Docker HEALTHCHECK: did a scheduled cycle finish
successfully recently? The interval and the jitter are read from the audit line itself, so it
needs no arguments. A cycle that finished degraded (exit 5) counts as successful and is reported
as such.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import signal
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from catalog_sync.audit import AuditLog, AuditScope, audited
from catalog_sync.cli_credentials import resolve_credential
from catalog_sync.cli_pipeline import (
    add_max_failed_ratio_argument,
    lookup_profile,
    run_profile,
)
from catalog_sync.cli_sources import describe_source
from catalog_sync.credentials.freshness import renewal_window_warning
from catalog_sync.errors import (
    EXIT_COMPLETED_WITH_FAILURES,
    AuthExpired,
    CatalogSyncError,
    ComplianceError,
    ConfigError,
    CredentialMissing,
    RecordFormatError,
)
from catalog_sync.redact import redact_text

if TYPE_CHECKING:
    from catalog_sync.cli import Context

DEFAULT_MAX_CONSECUTIVE_FAILURES = 5
DEFAULT_MAX_AGE = "2x-interval"
_BACKOFF_BASE_S = 60.0
_SECONDS_PER_UNIT = {"s": 1, "m": 60, "h": 3_600, "d": 86_400}
_DURATION = re.compile(r"(\d+)([smhd])")
_INTERVAL_MULTIPLE = re.compile(r"(\d+(?:\.\d+)?)x-interval")
_SCHEDULE_COMMAND = "schedule"
_OUTCOME_OK = "ok"
_OUTCOME_DEGRADED = "ok_with_failures"  # the cycle finished with failed items, within the limit


# --- durations ----------------------------------------------------------------------------------


def parse_duration(text: str) -> float:
    """``30m``, ``6h``, ``1d`` or ``45s`` as seconds."""
    matched = _DURATION.fullmatch(text.strip())
    if matched is None:
        raise ValueError(f"expected a duration like 30m, 6h or 1d, got {text!r}")
    return float(int(matched.group(1)) * _SECONDS_PER_UNIT[matched.group(2)])


def _duration_argument(text: str) -> float:
    try:
        return parse_duration(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def format_duration(seconds: float) -> str:
    remaining = round(seconds)
    parts: list[str] = []
    for unit, size in sorted(_SECONDS_PER_UNIT.items(), key=lambda item: -item[1]):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts) or "0s"


@dataclass(frozen=True)
class MaxAge:
    """How old the last successful cycle may be: a fixed time, or a multiple of the interval."""

    value: float
    is_interval_multiple: bool

    @classmethod
    def parse(cls, text: str) -> MaxAge:
        multiple = _INTERVAL_MULTIPLE.fullmatch(text.strip())
        if multiple is not None:
            return cls(float(multiple.group(1)), is_interval_multiple=True)
        return cls(parse_duration(text), is_interval_multiple=False)

    def limit(self, interval_s: float, jitter_s: float = 0.0) -> float:
        """A multiple counts the interval that many times and then adds the jitter once, because
        jitter lengthens a single pause by at most its own size. A fixed time is taken as given."""
        if self.is_interval_multiple:
            return self.value * interval_s + jitter_s
        return self.value


def _max_age_argument(text: str) -> MaxAge:
    try:
        return MaxAge.parse(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected a duration like 90m or 12h, or a multiple like 2x-interval; got {text!r}"
        ) from exc


# --- the loop -----------------------------------------------------------------------------------


class Verdict(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"  # finished, some items failed, within --max-failed-ratio: a success
    CREDENTIAL = "credential"
    FAILED = "failed"


@dataclass(frozen=True)
class Plan:
    profile: str
    source: str
    every_s: float
    jitter_s: float
    max_consecutive_failures: int
    is_one_shot: bool
    max_failed_ratio: float | None = None  # None: the profile's own limit


@dataclass
class Scheduler:
    """The loop, with everything that touches the outside world handed in: the cycle itself,
    the audit log, the stop flag, the sleep and the jitter. Tests run it in zero time."""

    plan: Plan
    run_cycle: Callable[[], int]
    log: AuditLog
    stop: threading.Event
    say: Callable[[str], None]
    sleep: Callable[[float], object]
    jitter: Callable[[float], float]

    def run(self) -> int:
        failures = 0
        cycle = 0
        while not self.stop.is_set():
            cycle += 1
            verdict, exit_code = self._cycle(cycle)
            if self.plan.is_one_shot:
                return exit_code
            if verdict is Verdict.FAILED:
                failures += 1
            elif verdict in (Verdict.OK, Verdict.DEGRADED):
                failures = 0
            if failures >= self.plan.max_consecutive_failures:
                self.say(
                    f"giving up after {failures} consecutive failed cycles; exiting with 1 so "
                    "that whatever supervises this process restarts it"
                )
                return 1
            if self.stop.is_set():
                break
            wait = self._wait_after(verdict, failures)
            self.say(f"next cycle in {format_duration(wait)}")
            self.sleep(wait)
        self.say("stop requested; no further cycle will start")
        return 0

    def _cycle(self, number: int) -> tuple[Verdict, int]:
        plan = self.plan
        with audited(
            self.log, _SCHEDULE_COMMAND, source=plan.source, profile=plan.profile
        ) as scope:
            scope.counts = {
                "cycle": number,
                "interval_seconds": int(plan.every_s),
                "jitter_seconds": int(plan.jitter_s),  # health needs it to know the longest pause
            }
            try:
                exit_code = self.run_cycle()
            except (AuthExpired, CredentialMissing) as exc:
                verdict, exit_code = Verdict.CREDENTIAL, exc.exit_code
                scope.outcome = f"error:{type(exc).__name__}"
                self.say(
                    f"cycle {number}: the credential is expired or missing; re-login on the host "
                    f"(this process cannot log in for you). {redact_text(str(exc))}"
                )
            except ComplianceError:
                raise  # not retryable: the audit scope records it, the CLI exits with 4
            except CatalogSyncError as exc:
                verdict, exit_code = Verdict.FAILED, exc.exit_code
                scope.outcome = f"error:{type(exc).__name__}"
                self.say(f"cycle {number} failed: {redact_text(str(exc))}")
            else:
                verdict = self._judge(number, exit_code, scope)
            scope.counts["exit_code"] = exit_code
        return verdict, exit_code

    def _judge(self, number: int, exit_code: int, scope: AuditScope) -> Verdict:
        if exit_code == 0:
            self.say(f"cycle {number} finished")
            return Verdict.OK
        if exit_code == EXIT_COMPLETED_WITH_FAILURES:
            scope.outcome = _OUTCOME_DEGRADED
            self.say(
                f"cycle {number} finished degraded: some items failed (exit {exit_code}), "
                "within the allowed share; counted as a success"
            )
            return Verdict.DEGRADED
        scope.outcome = "failed"
        self.say(f"cycle {number} finished with failed items (exit {exit_code})")
        return Verdict.FAILED

    def _wait_after(self, verdict: Verdict, failures: int) -> float:
        every = self.plan.every_s
        if verdict is Verdict.FAILED:
            base = min(every, _BACKOFF_BASE_S * 2.0 ** (failures - 1))
        else:
            base = every
        return base + self.jitter(self.plan.jitter_s)


# --- schedule -----------------------------------------------------------------------------------


def cmd_schedule(args: argparse.Namespace, context: Context) -> int:
    plan = _plan(args, context)
    context.workspace.home.mkdir(parents=True, exist_ok=True)  # the lock file lives in it
    lock_path = context.workspace.home / f"schedule-{plan.profile}.lock"
    with _exclusive_lock(lock_path, plan.profile), _stop_on_signals() as stop:
        scheduler = Scheduler(
            plan,
            run_cycle=lambda: _run_one_cycle(context, plan),
            log=AuditLog(context.workspace.audit_path, clock=context.clock),
            stop=stop,
            say=lambda message: _say(context.clock(), plan.profile, message),
            sleep=stop.wait,  # a signal sets the flag, which ends the wait at once
            jitter=_random_jitter,
        )
        return scheduler.run()


def _plan(args: argparse.Namespace, context: Context) -> Plan:
    profile = lookup_profile(context, args.profile, command=_SCHEDULE_COMMAND)
    if args.every <= 0:
        raise ConfigError("--every must be longer than zero")
    if args.jitter > args.every:
        raise ConfigError("--jitter must not be longer than --every")
    if args.max_consecutive_failures < 1:
        raise ConfigError("--max-consecutive-failures must be at least 1")
    return Plan(
        profile=profile.name,
        source=profile.source,
        every_s=args.every,
        jitter_s=args.jitter,
        max_consecutive_failures=args.max_consecutive_failures,
        is_one_shot=args.once,
        max_failed_ratio=args.max_failed_ratio,
    )


def _run_one_cycle(context: Context, plan: Plan) -> int:
    warning = _window_warning(context, plan)
    if warning is not None:
        print(f"warning: {warning}", file=sys.stderr)
    return run_profile(context, plan.profile, max_failed_ratio=plan.max_failed_ratio)


def _window_warning(context: Context, plan: Plan) -> str | None:
    """The renewal window of the source's credential against the real interval of this loop."""
    name = describe_source(plan.source, {}, context).options.get("credential")
    if not isinstance(name, str):
        return None
    return renewal_window_warning(
        renew_before_days=resolve_credential(name, context).renew_before_days,
        schedule_interval_days=plan.every_s / _SECONDS_PER_UNIT["d"],
    )


def _random_jitter(limit_s: float) -> float:
    return random.uniform(0.0, limit_s)  # noqa: S311 — spreads load over time, not a secret


def _say(now: datetime, profile: str, message: str) -> None:
    print(f"{now:%Y-%m-%dT%H:%M:%SZ} schedule {profile}: {message}", flush=True)


@contextmanager
def _exclusive_lock(path: Path, profile: str) -> Iterator[None]:
    """Refuse a second scheduler for the same profile in the same workspace.

    Two loops would run the same profile at the same time against one state database and one set
    of sink files. The kernel drops an flock when its process dies, so a crash leaves no stale lock.
    """
    if sys.platform == "win32":
        print(
            "warning: this platform has no fcntl, so a second scheduler for the same profile "
            "would not be refused",
            file=sys.stderr,
        )
        yield
        return
    import fcntl  # POSIX only, hence not imported at module level

    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise ConfigError(
            f"another scheduler already runs profile {profile!r} in this workspace "
            f"(lock file {path.name} is held)"
        ) from exc
    except OSError as exc:
        os.close(fd)
        # A filesystem without lock support: say so rather than run unprotected.
        raise ConfigError(f"cannot lock {path.name}: {exc.strerror}") from exc
    try:
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the flock


@contextmanager
def _stop_on_signals() -> Iterator[threading.Event]:
    """SIGTERM and SIGINT ask the loop to stop once the current cycle is over.

    The puller has no cancellation point, so "the current cycle" is the whole run: the process
    exits after it, which a container needs to be given time for (`stop_grace_period`). A second
    signal gives up on that and interrupts the run where it is; the puller then marks the run
    interrupted and `pull --resume` can continue it.
    """
    stop = threading.Event()

    def on_signal(_signal: int, _frame: object) -> None:
        if stop.is_set():
            raise KeyboardInterrupt
        stop.set()

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield stop
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


# --- health -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LastSuccess:
    finished_at: datetime
    interval_s: float
    jitter_s: float
    profile: str | None
    is_degraded: bool


def last_success(
    entries: Sequence[Mapping[str, object]], profile: str | None
) -> LastSuccess | None:
    """The newest ``schedule`` audit line whose cycle finished with exit 0, or degraded (5)."""
    for entry in reversed(entries):
        outcome = entry.get("outcome")
        if entry.get("command") != _SCHEDULE_COMMAND or outcome not in (
            _OUTCOME_OK,
            _OUTCOME_DEGRADED,
        ):
            continue
        if profile is not None and entry.get("profile") != profile:
            continue
        timestamp, counts = entry.get("ts"), entry.get("counts")
        if not isinstance(timestamp, str) or not isinstance(counts, dict):
            raise RecordFormatError("a schedule line of the audit log has no 'ts' or 'counts'")
        interval = counts.get("interval_seconds")
        if not isinstance(interval, int):
            raise RecordFormatError("a schedule line of the audit log has no 'interval_seconds'")
        jitter = counts.get("jitter_seconds", 0)  # lines written before jitter was recorded
        if not isinstance(jitter, int):
            raise RecordFormatError("a schedule line of the audit log has a bad 'jitter_seconds'")
        listed_profile = entry.get("profile")
        return LastSuccess(
            datetime.fromisoformat(timestamp),
            float(interval),
            float(jitter),
            listed_profile if isinstance(listed_profile, str) else None,
            is_degraded=outcome == _OUTCOME_DEGRADED,
        )
    return None


def cmd_health(args: argparse.Namespace, context: Context) -> int:
    entries = AuditLog(context.workspace.audit_path).read()
    success = last_success(entries, args.profile)
    if success is None:
        print(
            "unhealthy: no scheduled cycle has finished successfully in this workspace yet",
            file=sys.stderr,
        )
        return 1
    age_s = (context.clock() - success.finished_at).total_seconds()
    limit_s = args.max_age.limit(success.interval_s, success.jitter_s)
    summary = (
        f"the last successful scheduled cycle{_of(success.profile)} finished "
        f"{format_duration(max(age_s, 0))} ago (limit {format_duration(limit_s)})"
    )
    if age_s > limit_s:
        print(f"unhealthy: {summary}", file=sys.stderr)
        return 1
    if success.is_degraded:
        print(f"healthy, degraded: {summary}; that cycle finished with failed items")
    else:
        print(f"healthy: {summary}")
    return 0


def _of(profile: str | None) -> str:
    return f" of {profile!r}" if profile is not None else ""


# --- parser -------------------------------------------------------------------------------------


def add_schedule_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    schedule = commands.add_parser(
        _SCHEDULE_COMMAND,
        help="run a profile on a timer, in the foreground",
        description="Runs PROFILE (exactly what `run` does) every EVERY, until SIGTERM or SIGINT. "
        "An expired credential is reported every cycle and does not stop the loop (log in again "
        "on the host); a missing compliance declaration stops it with exit 4; other failures are "
        "retried with a growing pause, up to --max-consecutive-failures in a row, then exit 1. "
        f"A cycle that finishes with some failed items (run's exit {EXIT_COMPLETED_WITH_FAILURES}) "
        "counts as a success, marked degraded, unless more than --max-failed-ratio of the fetched "
        "items failed. The pause counts from the end of a cycle. Two schedulers cannot run one "
        "profile in one workspace.",
    )
    schedule.add_argument("profile")
    schedule.add_argument(
        "--every",
        type=_duration_argument,
        required=True,
        metavar="DURATION",
        help="the pause between two cycles, for example 30m, 6h or 1d",
    )
    schedule.add_argument(
        "--jitter",
        type=_duration_argument,
        default=0.0,
        metavar="DURATION",
        help="add a random 0..DURATION to every pause, so that many installs do not all call "
        "the service at the same minute (default: none; at most --every)",
    )
    schedule.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_FAILURES,
        metavar="N",
        help="exit 1 after N cycles in a row fail for a reason other than the credential "
        f"(default: {DEFAULT_MAX_CONSECUTIVE_FAILURES})",
    )
    add_max_failed_ratio_argument(schedule)
    schedule.add_argument(
        "--once", action="store_true", help="run one cycle and exit with its exit code"
    )
    schedule.set_defaults(run_command=cmd_schedule)

    health = commands.add_parser(
        "health",
        help="exit 0 if a scheduled cycle finished successfully recently (for a HEALTHCHECK)",
        description="Reads the audit log of the workspace. Exit 0 when the last successful "
        "`schedule` cycle (including one that finished degraded, with some failed items) is "
        "not older than --max-age, else 1. The default allows two intervals plus the longest "
        "jitter the scheduler ran with.",
    )
    health.add_argument(
        "--max-age",
        type=_max_age_argument,
        default=MaxAge.parse(DEFAULT_MAX_AGE),
        metavar="AGE",
        help=f"a duration (90m, 12h) or a multiple of the interval the scheduler ran with, "
        f"plus its jitter (default: {DEFAULT_MAX_AGE})",
    )
    health.add_argument("--profile", help="only the cycles of this profile")
    health.set_defaults(run_command=cmd_health)
