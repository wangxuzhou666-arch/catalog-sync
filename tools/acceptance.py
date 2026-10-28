"""Clean-install acceptance: run the built package, not the source tree, and record what held.

    uv run python tools/acceptance.py --out DIR [--postgres-dsn DSN] [--only SCENARIO...]

What it does, in order (the numbers are the steps of the W4 spec):

1. records the provenance: version, commit, dirty flag, Python, OS, uv, UTC time;
2. builds the wheel and the sdist (``uv build``) and records their sha256;
3. installs the wheel into a fresh virtual environment, and a wheel built *from the sdist* into a
   second one, and proves for both that ``catalog_sync`` is imported from site-packages;
4. starts and stops the installed Mock source and reference platform on loopback;
5. writes a config in an empty working directory, with no mapping key (the connectors' packaged
   mappings apply);
6. runs the scenarios, each yielding PASS, FAIL or NOT_RUN with its evidence;
7. writes ``DIR/acceptance-report.json`` and ``DIR/acceptance-report.md`` and exits non-zero when
   anything FAILed. A NOT_RUN is listed with its reason and is never a pass.

The release checks (``dist-check``, ``licenses``, ``image``; acceptance_release.py) are registered
with the scenarios and run after them: they look at what the steps above built and installed, and
at an image built from a ``git archive`` export of HEAD.

Every command under test is ``DIR/venv/bin/catalog-sync`` run in an empty directory under
``DIR/work``, with ``CATALOG_SYNC_HOME`` under ``DIR/home``, no ``PYTHONPATH`` and a ``HOME`` that
is not yours. The harness process itself imports repository code only for the expected values
(``tests.fixtures.mock_saas``, the seeded Mock catalogue). Its only network traffic is to
127.0.0.1 (``uv`` may fetch packages while building and installing).

Structure, for adding a scenario
--------------------------------

* ``acceptance_report.py``: ``Outcome``, ``Recorder``, ``Report``, masking and rendering. Pure.
* ``acceptance_lab.py``: ``Harness`` (what the run shares), ``Lab`` (one scenario's servers, config,
  workspace and ``lab.cli(...)``), the request counters, and the **scenario registry**.
* ``acceptance_compare.py``: reading SQLite / JSONL / platform rows, field comparison, the oracle.
* ``acceptance_scenarios.py``: the scenario functions. **This is the only file a new scenario
  touches.** Write ``def s7_sensitive(harness, record): ...``, decorate it with
  ``@scenario("S7", "Sensitive values", order=7)``, and make assertions with ``record.check(...)``
  and observations with ``record.evidence(...)``. To say "cannot run here", raise
  ``NotRun("reason")``. A placeholder registered with ``pending(...)`` is replaced by a real
  ``@scenario`` of the same id.
* ``acceptance.py`` (this file): the steps above, ``--only``, the report, the exit code. It does
  not change when a scenario is added.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import acceptance_release  # noqa: F401 — importing it registers the release checks
import acceptance_scenarios  # noqa: F401 — importing it registers the scenarios
from acceptance_lab import (
    Harness,
    HarnessError,
    Installed,
    Scenario,
    generate_secrets,
    real_workspace_locations,
    registered,
)
from acceptance_report import (
    Masker,
    NotRun,
    Outcome,
    Recorder,
    Report,
    Status,
    report_json,
    report_markdown,
)
from catalog_sync.testing.workspace_guard import fingerprint
from check_dist import TERMS_ENV, CheckError, load_matcher

REPO_ROOT = Path(__file__).resolve().parent.parent
MARKER = ".acceptance-dir"  # written into a directory this tool created, so --force may remove it
UV_TIMEOUT_S = 900.0
_VERSION_LINE = re.compile(r'^__version__ = "(?P<version>[^"]+)"', re.MULTILINE)

StepFunction = Callable[[Harness, Recorder], None]


# --- the command line ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install the built package into a fresh environment and run the acceptance "
        "scenarios against the installed Mock source and reference platform.",
    )
    parser.add_argument("--out", required=True, type=Path, metavar="DIR", help="a new directory")
    parser.add_argument(
        "--postgres-dsn",
        default=os.environ.get("ACCEPTANCE_POSTGRES_DSN"),
        metavar="DSN",
        help="a throw-away Postgres to load into as well (default: $ACCEPTANCE_POSTGRES_DSN; "
        "its value is never printed or recorded)",
    )
    parser.add_argument(
        "--private-terms",
        type=Path,
        default=Path(os.environ[TERMS_ENV]) if os.environ.get(TERMS_ENV) else None,
        metavar="FILE",
        help="the maintainer's private terms file, one per line, kept outside the repository: "
        f"dist-check looks for them too (default: ${TERMS_ENV}; neither its path nor a term is "
        "recorded)",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="SCENARIO",
        help="run only these scenario ids; the others are listed as NOT_RUN",
    )
    parser.add_argument(
        "--python",
        default=f"{sys.version_info.major}.{sys.version_info.minor}",
        help="the Python of the fresh environments (default: this interpreter's minor version)",
    )
    parser.add_argument(
        "--force", action="store_true", help="remove DIR first, if this tool created it"
    )
    args = parser.parse_args(argv)
    unknown = sorted(set(args.only or []) - {s.id for s in registered()})
    if unknown:
        parser.error(f"unknown scenario(s) {unknown}; known: {[s.id for s in registered()]}")
    return args


def prepare_out(out: Path, *, force: bool) -> None:
    """Make DIR a new, empty directory. Only a directory this tool made may be replaced."""
    if out.exists() and force:
        if not (out / MARKER).exists():
            raise HarnessError(f"{out} was not created by this tool; refusing to remove it")
        shutil.rmtree(out)
    if out.exists() and any(out.iterdir()):
        raise HarnessError(f"{out} is not empty: the run needs a fresh directory (or --force)")
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("created by tools/acceptance.py\n", encoding="utf-8")
    for name in ("tmp", "work", "home", "fake-home", "logs", "state"):
        (out / name).mkdir()


# --- uv and git ---------------------------------------------------------------------------------


def tool_environment(out: Path) -> dict[str, str]:
    """The environment of ``uv`` and ``git``: the caller's, minus what could redirect an install."""
    kept = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH")}
    return {**kept, "TMPDIR": str(out / "tmp"), "PYTHON_DOTENV_DISABLED": "1"}


def run_tool(
    command: Sequence[str | Path], *, cwd: Path, env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        env=dict(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=UV_TIMEOUT_S,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_version(repo: Path) -> str:
    found = _VERSION_LINE.search(
        (repo / "src/catalog_sync/__init__.py").read_text(encoding="utf-8")
    )
    if found is None:
        raise HarnessError("src/catalog_sync/__init__.py has no __version__")
    return found["version"]


def provenance(args: argparse.Namespace, env: Mapping[str, str]) -> dict[str, str | bool]:
    head = run_tool(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, env=env)
    status = run_tool(["git", "status", "--porcelain"], cwd=REPO_ROOT, env=env)
    uv_version = run_tool(["uv", "--version"], cwd=REPO_ROOT, env=env)
    return {
        "version": source_version(REPO_ROOT),
        "commit": head.stdout.strip(),
        "dirty": bool(status.stdout.strip()),
        "python (harness)": platform.python_version(),
        "python (fresh environments)": args.python,
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "uv": uv_version.stdout.strip(),
        "time (UTC)": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "postgres": "a DSN was given (its value is not recorded)"
        if args.postgres_dsn
        else "no DSN",
        "private terms": "a terms file was given (neither its path nor a term is recorded)"
        if args.private_terms
        else "none: dist-check applied the public rules only",
        "command": "uv run python tools/acceptance.py --out <out>"
        + (" --postgres-dsn <dsn>" if args.postgres_dsn else "")
        + (" --private-terms <file>" if args.private_terms else "")
        + (f" --only {' '.join(args.only)}" if args.only else ""),
    }


# --- steps 2 and 3: build and install -----------------------------------------------------------


def build_step(harness: Harness, record: Recorder) -> None:
    env = tool_environment(harness.out)
    dist = harness.out / "dist"
    built = run_tool(["uv", "build", "--out-dir", dist], cwd=harness.repo, env=env)
    record.check("uv build exits 0", built.returncode == 0, _tail(built))
    wheels, sdists = sorted(dist.glob("*.whl")), sorted(dist.glob("*.tar.gz"))
    record.check("one wheel and one sdist were built", len(wheels) == 1 and len(sdists) == 1)
    wheel, sdist = wheels[0], sdists[0]
    record.evidence(
        "wheel", {"file": wheel.name, "sha256": sha256_of(wheel), "bytes": wheel.stat().st_size}
    )
    record.evidence(
        "sdist", {"file": sdist.name, "sha256": sha256_of(sdist), "bytes": sdist.stat().st_size}
    )


def _built_files(harness: Harness) -> tuple[Path, Path]:
    dist = harness.out / "dist"
    return next(dist.glob("*.whl")), next(dist.glob("*.tar.gz"))


def _tail(done: subprocess.CompletedProcess[str]) -> str:
    return f"exit {done.returncode}: {(done.stdout + done.stderr).strip()[-400:]}"


def make_environment(harness: Harness, name: str, artifact: Path, python: str) -> Installed:
    """A fresh virtual environment with `artifact` (a wheel) installed, plus [postgres] if asked."""
    env = tool_environment(harness.out)
    venv = harness.out / name
    created = run_tool(["uv", "venv", venv, "--python", python], cwd=harness.out, env=env)
    if created.returncode != 0:
        raise HarnessError(f"uv venv {name} failed: {_tail(created)}")
    requirement = f"{artifact}[postgres]" if harness.postgres_dsn else str(artifact)
    install = run_tool(
        ["uv", "pip", "install", "--python", venv / "bin" / "python", requirement],
        cwd=harness.out,
        env=env,
    )
    if install.returncode != 0:
        raise HarnessError(f"uv pip install into {name} failed: {_tail(install)}")
    purelib = run_tool(
        [
            venv / "bin" / "python",
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        cwd=harness.out,
        env=env,
    )
    return Installed(venv, Path(purelib.stdout.strip()))


def install_wheel_step(harness: Harness, record: Recorder) -> None:
    wheel, _ = _built_files(harness)
    installed = make_environment(harness, "venv", wheel, harness.python)
    harness.installed = installed
    _check_version(harness, installed, record, "wheel")


def install_sdist_step(harness: Harness, record: Recorder) -> None:
    _, sdist = _built_files(harness)
    env = tool_environment(harness.out)
    rebuilt = harness.out / "dist-from-sdist"
    built = run_tool(
        ["uv", "build", "--wheel", "--out-dir", rebuilt, sdist], cwd=harness.out, env=env
    )
    record.check("a wheel builds from the sdist", built.returncode == 0, _tail(built))
    wheel_from_sdist = next(rebuilt.glob("*.whl"))
    original, _ = _built_files(harness)
    record.evidence(
        "wheel built from the sdist",
        {"file": wheel_from_sdist.name, "sha256": sha256_of(wheel_from_sdist)},
    )
    record.evidence(
        "same bytes as the wheel built from the checkout",
        sha256_of(wheel_from_sdist) == sha256_of(original),
    )
    installed = make_environment(harness, "venv-sdist", wheel_from_sdist, harness.python)
    _check_version(harness, installed, record, "sdist")
    harness.sdist_installed = installed


def _check_version(harness: Harness, installed: Installed, record: Recorder, label: str) -> None:
    done = run_tool(
        [installed.cli, "--version"],
        cwd=harness.out / "work",
        env=harness.environment(harness.out / "home" / label, installed=installed),
    )
    expected = f"catalog-sync {source_version(harness.repo)}"
    record.check(
        f"{label}: `catalog-sync --version` is the version of the source",
        done.stdout.strip() == expected,
        done.stdout.strip(),
    )


def installed_copy_step(harness: Harness, record: Recorder) -> None:
    """The package the commands import is the installed one, not the source tree."""
    assert harness.sdist_installed is not None
    for label, installed in (("wheel", harness.installed), ("sdist", harness.sdist_installed)):
        done = run_tool(
            [installed.python, "-I", "-c", "import catalog_sync; print(catalog_sync.__file__)"],
            cwd=harness.out / "work",
            env=harness.environment(harness.out / "home" / label, installed=installed),
        )
        location = Path(done.stdout.strip()).resolve()
        inside = location.is_relative_to(installed.site_packages.resolve())
        outside = not location.is_relative_to(harness.repo.resolve())
        record.check(
            f"{label}: catalog_sync is imported from the environment's site-packages",
            inside and outside,
            str(location),
        )
        record.evidence(f"{label}: catalog_sync.__file__", str(location))
    record.evidence("PYTHONPATH in the commands' environment", "absent")


def servers_step(harness: Harness, record: Recorder) -> None:
    """Both servers start from the installed copy, listen on loopback, and stop cleanly."""
    with harness.lab("servers") as lab:
        source, platform_server = lab.start_source(), lab.start_platform()
        record.check(
            "the Mock source listens on loopback",
            source.is_listening() and "127.0.0.1" in source.base_url,
        )
        record.check(
            "the reference platform listens on loopback",
            platform_server.is_listening() and "127.0.0.1" in platform_server.base_url,
        )
        config = lab.write_config()
        record.check(
            "the config has no mapping key", "mapping" not in config.read_text(encoding="utf-8")
        )
        parsed = lab.cli("runs", "list")
        record.check(
            "the installed program reads that config (runs list exits 0)",
            parsed.returncode == 0,
            parsed.stderr.strip()[:200],
        )
        record.check("no request reached the source meanwhile", source.mark() == 0)
        source.stop()
        platform_server.stop()
        record.check(
            "both servers exit 0 on SIGTERM",
            source.returncode == 0 and platform_server.returncode == 0,
        )
        record.check(
            "their ports are closed afterwards",
            not source.is_listening() and not platform_server.is_listening(),
        )


# --- running ------------------------------------------------------------------------------------


def run_guarded(outcome: Outcome, function: Callable[[Recorder], None]) -> Outcome:
    """Run one step or scenario; whatever it raises becomes the outcome's error."""
    record = Recorder(outcome)
    try:
        function(record)
    except NotRun as skipped:
        outcome.not_run_reason = str(skipped)
    except Exception as crash:  # noqa: BLE001 — batch-unit isolation: one crashing scenario is recorded under its id and the others still run
        outcome.error = f"{type(crash).__name__}: {crash}"
    return outcome


def run_scenarios(harness: Harness, only: Sequence[str] | None) -> list[Outcome]:
    outcomes = []
    for scenario in registered():
        outcome = Outcome(scenario.id, scenario.title)
        if only and scenario.id not in only:
            outcome.not_run_reason = "not selected (--only)"
        elif scenario.run is None:
            outcome.not_run_reason = scenario.pending or "not implemented"
        else:
            run_guarded(outcome, _bind(scenario, harness))
        outcomes.append(outcome)
    return outcomes


def _bind(scenario: Scenario, harness: Harness) -> Callable[[Recorder], None]:
    function = scenario.run
    assert function is not None
    return lambda record: function(harness, record)


STEPS: tuple[tuple[str, str, StepFunction], ...] = (
    ("build", "Build the wheel and the sdist", build_step),
    ("install-wheel", "Install the wheel into a fresh environment", install_wheel_step),
    (
        "install-sdist",
        "Build a wheel from the sdist and install it into a second environment",
        install_sdist_step,
    ),
    ("installed-copy", "The installed copy is the one under test", installed_copy_step),
    ("servers", "Start and stop the installed Mock source and reference platform", servers_step),
)


def run_steps(harness: Harness) -> list[Outcome]:
    """Steps in order; after the first FAIL the rest are NOT_RUN, because they depend on it."""
    outcomes: list[Outcome] = []
    failed: str | None = None
    for step_id, title, function in STEPS:
        outcome = Outcome(step_id, title)
        if failed is not None:
            outcome.not_run_reason = f"step {failed} failed"
        else:
            run_guarded(outcome, partial(function, harness))
            failed = step_id if outcome.status is Status.FAIL else None
        outcomes.append(outcome)
    return outcomes


# --- main ---------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    out = args.out.resolve()
    try:
        load_matcher(args.private_terms, repo=REPO_ROOT)  # a bad terms file ends the run early
        prepare_out(out, force=args.force)
    except (HarnessError, CheckError) as refusal:
        print(f"acceptance: {refusal}", file=sys.stderr)
        return 2
    secrets = generate_secrets()
    hidden = [*secrets.values(), *([args.postgres_dsn] if args.postgres_dsn else [])]
    masker = Masker([(out, "<out>"), (REPO_ROOT, "<repo>"), (Path.home(), "<home>")], hidden)
    harness = Harness(
        out=out,
        repo=REPO_ROOT,
        installed=Installed(out / "venv", out / "venv"),  # replaced by the install step
        secrets=secrets,
        masker=masker,
        today=datetime.now(UTC).date().isoformat(),
        python=args.python,
        postgres_dsn=args.postgres_dsn,
        private_terms=args.private_terms,
    )
    harness.real_workspaces_before = {
        path: fingerprint(path) for path in real_workspace_locations()
    }
    meta = provenance(args, tool_environment(out))
    steps = run_steps(harness)
    scenarios = (
        run_scenarios(harness, args.only)
        if all(step.status is Status.PASS for step in steps)
        else _not_run_scenarios()
    )
    report = Report(meta, steps, scenarios)
    (out / "acceptance-report.json").write_text(report_json(report, masker), encoding="utf-8")
    (out / "acceptance-report.md").write_text(report_markdown(report, masker), encoding="utf-8")
    print(masker.text(_summary(report)))
    return report.exit_code


def _not_run_scenarios() -> list[Outcome]:
    outcomes = []
    for scenario in registered():
        outcome = Outcome(scenario.id, scenario.title)
        outcome.not_run_reason = "a setup step failed"
        outcomes.append(outcome)
    return outcomes


def _summary(report: Report) -> str:
    counts = report.counts()
    lines = [
        f"acceptance: {report.overall} ({counts['PASS']} PASS, {counts['FAIL']} FAIL, "
        f"{counts['NOT_RUN']} NOT_RUN)"
    ]
    for outcome in [*report.steps, *report.scenarios]:
        reason = f"  {outcome.reason}" if outcome.status is not Status.PASS else ""
        lines.append(f"  {outcome.id:<16}{outcome.status.value:<8}{outcome.title}{reason}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
