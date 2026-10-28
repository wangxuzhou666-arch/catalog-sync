"""The release checks of the acceptance harness: dist-check, licenses and image.

The parts that decide pass or fail are plain functions, fed hand-made input here: a rule that never
went red proves nothing (CODING-STYLE R.7). The real wheel, environment and image are exercised by
the harness run itself.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

import acceptance
import acceptance_lab
import acceptance_release
import check_dist
from acceptance_lab import Harness, Installed
from acceptance_release import REVIEWED_COPYLEFT, ImageFacts, Licence
from acceptance_report import (
    Masker,
    Outcome,
    Recorder,
    Report,
    Status,
    report_json,
    report_markdown,
)
from tests.conftest import REPO_ROOT
from tests.dist_builders import GOOD_SDIST, GOOD_WHEEL, PYPROJECT, write_sdist, write_wheel

HOME_PATH = "/" + "Users" + "/" + "alice"


def harness_for(tmp_path: Path, *, terms: Path | None = None) -> Harness:
    """A harness whose `dist` directory holds the good wheel and sdist, and whose repo is a
    directory with the pyproject those artifacts were made from."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    out = tmp_path / "out"
    (out / "dist").mkdir(parents=True)
    for name in ("work", "tmp", "logs"):
        (out / name).mkdir()
    write_wheel(out / "dist" / "demo.whl", GOOD_WHEEL)
    write_sdist(out / "dist" / "demo.tar.gz", GOOD_SDIST)
    return Harness(
        out=out,
        repo=repo,
        installed=Installed(out / "venv", out / "venv" / "site-packages"),
        secrets=acceptance_lab.generate_secrets(),
        masker=Masker(),
        today="2026-10-27",
        python="3.12",
        private_terms=terms,
    )


def run(harness: Harness, function: acceptance_lab.ScenarioFunction) -> Outcome:
    return acceptance.run_guarded(Outcome("x", "x"), lambda record: function(harness, record))


def failed_checks(found: Outcome) -> list[str]:
    return [check.name.split(":")[0] for check in found.checks if not check.passed]


# --- dist-check ---------------------------------------------------------------------------------


def test_dist_check_passes_on_clean_artifacts_and_says_what_it_read(tmp_path: Path) -> None:
    found = run(harness_for(tmp_path), acceptance_release.dist_check)
    assert found.status is Status.PASS, found.reason
    assert {c.name.split(":")[0] for c in found.checks} >= {
        "forbidden-term", "host-path", "entry-points", "never-ship", "required-file",
    }  # fmt: skip
    assert found.evidence["entries scanned"] == {"wheel": len(GOOD_WHEEL), "sdist": len(GOOD_SDIST)}
    assert found.evidence["private terms applied"] == "none: the public rules only"


@pytest.mark.parametrize(
    ("rule", "wheel", "sdist"),
    [
        ("host-path", {"catalog_sync/m.py": f'P = "{HOME_PATH}"\n'}, {}),
        ("never-ship", {}, {".env": "A=1\n"}),
        ("entry-points", {"catalog_sync/plugins/hidden/__init__.py": ""}, {}),
        ("required-file", {}, {"LICENSE": None}),
        ("forbidden-term", {}, {"docs/n.txt": "a@" + "corp.net\n"}),
    ],
)
def test_dist_check_fails_the_check_of_the_rule_that_was_broken_and_no_other(
    tmp_path: Path, rule: str, wheel: dict[str, str], sdist: dict[str, str | None]
) -> None:
    harness = harness_for(tmp_path)
    write_wheel(harness.out / "dist" / "demo.whl", {**GOOD_WHEEL, **wheel})
    broken = {**GOOD_SDIST, **sdist}
    write_sdist(harness.out / "dist" / "demo.tar.gz", {k: v for k, v in broken.items() if v})
    found = run(harness, acceptance_release.dist_check)
    assert found.status is Status.FAIL
    assert failed_checks(found) == [rule]


def test_dist_check_applies_the_private_terms_and_never_prints_one(tmp_path: Path) -> None:
    term = "qz" + secrets.token_hex(4) + "kj"
    terms = tmp_path / "terms.txt"
    terms.write_text(f"{term}\n", encoding="utf-8")
    harness = harness_for(tmp_path, terms=terms)
    clean = run(harness, acceptance_release.dist_check)
    assert clean.status is Status.PASS and clean.evidence["private terms applied"] == 1
    write_sdist(harness.out / "dist" / "demo.tar.gz", {**GOOD_SDIST, "docs/n.md": f"for {term}\n"})
    found = run(harness, acceptance_release.dist_check)
    assert failed_checks(found) == ["forbidden-term"]
    assert term not in " ".join(c.detail for c in found.checks)


def test_a_terms_file_inside_the_repo_ends_the_run_before_it_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inside = REPO_ROOT / "pyproject.toml"
    assert acceptance.main(["--out", str(tmp_path / "o"), "--private-terms", str(inside)]) == 2
    assert "inside the repository" in capsys.readouterr().err
    assert not (tmp_path / "o").exists(), "nothing was created for a run that could not start"


def test_the_terms_file_default_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CATALOG_SYNC_PRIVATE_TERMS_FILE", str(tmp_path / "t.txt"))
    assert acceptance.parse_args(["--out", str(tmp_path / "o")]).private_terms == tmp_path / "t.txt"
    monkeypatch.delenv("CATALOG_SYNC_PRIVATE_TERMS_FILE")
    assert acceptance.parse_args(["--out", str(tmp_path / "o")]).private_terms is None


# --- licenses -----------------------------------------------------------------------------------


def licence(
    name: str, expression: str = "", text: str = "", *classifiers: str, version: str = "1.0"
) -> Licence:
    return Licence(name, version, expression, text, classifiers)


@pytest.mark.parametrize(
    "stated",
    [
        licence("a", "MIT"),
        licence("a", "MIT-CMU"),
        licence("a", "Apache-2.0"),
        licence("a", "BSD-3-Clause"),
        licence("a", "", "MIT"),
        licence("a", "", "", "License :: OSI Approved :: MIT License"),
        licence("a", "", "Simple permissive licence (a sample of the sort)"),
        licence("a", "", "Copyright (c) the authors\n\nPermission is hereby granted"),
    ],
)
def test_a_permissive_licence_is_ok(stated: Licence) -> None:
    assert stated.verdict == "ok", stated


@pytest.mark.parametrize(
    ("stated", "family"),
    [
        (licence("a", "GPL-2.0-only"), "GPL"),
        (licence("a", "GPL-3.0-or-later"), "GPL"),
        (licence("a", "AGPL-3.0-or-later"), "AGPL"),
        (licence("a", "LGPL-3.0-only"), "LGPL"),
        (licence("a", "", "GNU Lesser General Public License v3 (LGPLv3)"), "LGPL"),
        (
            licence(
                "a", "", "", "License :: OSI Approved :: GNU General Public License v3 (GPLv3)"
            ),
            "GPL",
        ),
        (licence("a", "SSPL-1.0"), "SSPL"),
        (licence("a", "EUPL-1.2"), "EUPL"),
        (licence("a", "MPL-2.0"), "MPL"),
        (licence("a", "", "Mozilla Public License 2.0"), "MPL"),
        (licence("a", "MIT OR GPL-2.0-only"), "GPL"),
    ],
)
def test_every_copyleft_family_the_spec_names_is_found_and_not_confused_with_another(
    stated: Licence, family: str
) -> None:
    assert stated.families == {family}
    assert stated.verdict == "COPYLEFT"


@pytest.mark.parametrize(
    "stated",
    [licence("a"), licence("a", "", "UNKNOWN"), licence("a", "", "  "), licence("a", " unknown ")],
)
def test_a_distribution_that_states_no_licence_is_unknown(stated: Licence) -> None:
    assert stated.verdict == "UNKNOWN"


def test_a_reviewed_exception_covers_its_own_family_only() -> None:
    assert licence("certifi", "MPL-2.0").verdict == "reviewed exception"
    assert licence("psycopg", "", "GNU Lesser General Public License v3 (LGPLv3)").verdict == (
        "reviewed exception"
    )
    assert licence("psycopg_binary", "LGPL-3.0-only").verdict == "reviewed exception"
    assert licence("certifi", "GPL-3.0-only").verdict == "COPYLEFT", "another family is not covered"
    assert licence("certifi", "MPL-2.0 OR GPL-3.0-only").verdict == "COPYLEFT"
    assert licence("requests-like", "MPL-2.0").verdict == "COPYLEFT", "the list is by name"


def test_every_reviewed_exception_has_a_reason_and_a_family() -> None:
    assert REVIEWED_COPYLEFT
    for name, (families, reason) in REVIEWED_COPYLEFT.items():
        assert name == name.lower().replace("_", "-") and families and len(reason) > 20


def checks_of(rows: list[Licence]) -> Outcome:
    outcome = Outcome("licenses", "licenses")
    acceptance_release.licence_checks(rows, Recorder(outcome))
    return outcome


GOOD_ROWS = [licence("catalog-sync", "MIT"), licence("requests", "Apache-2.0")]


def test_licenses_passes_for_known_permissive_licences_and_keeps_the_table() -> None:
    found = checks_of(GOOD_ROWS)
    assert found.status is Status.PASS
    [table] = found.tables
    assert table.rows == (
        ("catalog-sync", "1.0", "MIT", "ok"),
        ("requests", "1.0", "Apache-2.0", "ok"),
    )
    assert found.evidence["distributions"] == 2


def test_licenses_fails_on_a_copyleft_distribution_and_names_it() -> None:
    found = checks_of([*GOOD_ROWS, licence("somelib", "GPL-3.0-only")])
    assert found.status is Status.FAIL
    assert [c.detail for c in found.checks if not c.passed] == ["somelib (GPL-3.0-only)"]


def test_licenses_fails_on_an_unknown_licence_and_passes_a_reviewed_exception() -> None:
    assert checks_of([*GOOD_ROWS, licence("mystery")]).status is Status.FAIL
    reviewed = checks_of([*GOOD_ROWS, licence("certifi", "MPL-2.0")])
    assert reviewed.status is Status.PASS
    in_use = reviewed.evidence["reviewed copyleft exceptions in use"]
    assert isinstance(in_use, dict) and list(in_use) == ["certifi"]


def test_licenses_fails_when_the_listing_is_empty_or_holds_only_the_package_itself() -> None:
    assert checks_of([]).status is Status.FAIL
    assert checks_of([licence("catalog-sync", "MIT")]).status is Status.FAIL


def test_the_listing_script_runs_in_an_environment_and_reads_its_metadata(tmp_path: Path) -> None:
    harness = harness_for(tmp_path)
    rows = acceptance_release.licences_of(Path(sys.executable), harness)
    names = {row.name for row in rows}
    assert "catalog-sync" in names and "requests" in names
    assert {row.name: row for row in rows}["requests"].is_stated


def test_the_table_reaches_the_markdown_and_the_json_report() -> None:
    found = checks_of(GOOD_ROWS)
    report = Report({}, [], [found])
    text = report_markdown(report, Masker())
    assert "| Distribution | Version | Licence | Verdict |" in text
    assert "| requests | 1.0 | Apache-2.0 | ok |" in text
    document = json.loads(report_json(report, Masker()))
    assert document["scenarios"][0]["tables"][0]["columns"] == [
        "Distribution", "Version", "Licence", "Verdict",
    ]  # fmt: skip


def test_the_release_checks_are_registered_after_the_scenarios() -> None:
    by_id = {s.id: s for s in acceptance_lab.registered()}
    assert [by_id[i].order for i in ("dist-check", "licenses", "image")] == [11, 12, 13]
    assert all(by_id[i].run is not None for i in ("dist-check", "licenses", "image"))


# --- image: what must hold of the facts ---------------------------------------------------------


def demo_registered(tmp_path: Path) -> check_dist.EntryPoints:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT, encoding="utf-8")
    return check_dist.registered_entry_points(pyproject)


ALPHA_TARGET = "catalog_sync.plugins.alpha.source:Alpha.from_options"
OWN_POINTS = (
    ("console_scripts", "demo", "catalog_sync.cli:main", "catalog-sync"),
    ("catalog_sync.sources", "alpha", ALPHA_TARGET, "catalog-sync"),
    ("catalog_sync.sources", "alpha.two", "catalog_sync.plugins.alpha:two", "catalog-sync"),
    ("console_scripts", "normalizer", "charset_normalizer.cli:cli_detect", "charset-normalizer"),
)  # fmt: skip


def good_facts(**changes: object) -> ImageFacts:
    fields: dict[str, object] = {
        "size_bytes": 123_000_000,
        "configured_user": "10001:10001",
        "uid": 10001,
        "program_version": "catalog-sync 1.0",
        "distributions": (("catalog-sync", "1.0"), ("requests", "2.34")),
        "entry_points": OWN_POINTS,
    }
    return ImageFacts(**{**fields, **changes})  # type: ignore[arg-type]


def image_outcome(
    tmp_path: Path, facts: ImageFacts, listing: list[object] | None = None
) -> Outcome:
    outcome = Outcome("image", "image")
    acceptance_release.image_checks(
        Recorder(outcome),
        facts,
        registered=demo_registered(tmp_path),
        listing=listing or [],  # type: ignore[arg-type]
        expected_version="1.0",
    )
    return outcome


def test_an_image_with_the_core_only_a_non_root_user_and_clean_files_passes(tmp_path: Path) -> None:
    found = image_outcome(tmp_path, good_facts())
    assert found.status is Status.PASS, found.reason
    assert len(found.checks) == 5


@pytest.mark.parametrize(
    ("changes", "failing"),
    [
        (
            {"distributions": (("catalog-sync", "1.0"), ("catalog-sync-demo", "0.1"))},
            "no connector",
        ),
        (
            {"entry_points": (*OWN_POINTS, ("catalog_sync.sources", "hidden", "x.y:z", "other"))},
            "only the root",
        ),
        (
            {
                "entry_points": (
                    *OWN_POINTS,
                    ("catalog_sync.credentials", "k", "x:y", "catalog-sync"),
                )
            },
            "only the root",
        ),
        ({"entry_points": OWN_POINTS[:2]}, "only the root"),
        ({"uid": 0}, "non-root"),
        ({"configured_user": "root"}, "non-root"),
        ({"configured_user": ""}, "non-root"),
        ({"program_version": "catalog-sync 0.9"}, "version"),
    ],
)
def test_each_way_an_image_can_be_wrong_fails_its_own_check(
    tmp_path: Path, changes: dict[str, object], failing: str
) -> None:
    found = image_outcome(tmp_path, good_facts(**changes))
    assert found.status is Status.FAIL
    assert [c.name for c in found.checks if not c.passed] == [
        c.name for c in found.checks if not c.passed and failing in c.name
    ] != []  # fmt: skip


def test_a_never_ship_file_in_the_image_fails_the_file_check(tmp_path: Path) -> None:
    listing = check_dist.check_listing("image", check_dist.read_file_list(
        write_text(tmp_path / "files.txt", "/app/.env\n/app/ok.py\n"), "/app"
    ), None)  # fmt: skip
    found = image_outcome(tmp_path, good_facts(), listing)  # type: ignore[arg-type]
    assert [c.name for c in found.checks if not c.passed] == [
        "no file under /app (the virtual environment included) is on the never-ship list"
    ]


def write_text(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- image: the docker side, with a docker that is a script -------------------------------------

FAKE_DOCKER = """#!{python}
import json, os, sys
args = sys.argv[1:]
state = os.environ["FAKE_DOCKER_DIR"]
with open(os.path.join(state, "calls.jsonl"), "a") as log:
    log.write(json.dumps(args) + "\\n")
plan = json.load(open(os.path.join(state, "plan.json")))
removed = os.path.exists(os.path.join(state, "removed"))
def reply(code=0, out="", err=""):
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(code)
command = args[0]
if command == "info":
    reply(plan["info"])
if command == "build":
    reply(plan["build"], "", "build output")
if command == "rmi":
    open(os.path.join(state, "removed"), "w").close()
    reply()
if command == "image":
    reply(1 if removed else 0, plan["inspect"])
if command == "run":
    entry = args[args.index("--entrypoint") + 1] if "--entrypoint" in args else "catalog-sync"
    reply(0, plan["run"][os.path.basename(entry)])
reply(2, "", "unexpected " + command)
"""


def listing_json(points: tuple[tuple[str, str, str, str], ...] = OWN_POINTS) -> str:
    return json.dumps(
        {
            "distributions": [["catalog-sync", "1.0"], ["requests", "2.34"]],
            "points": [list(p) for p in points],
        }
    )


class FakeDocker:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **plan: object) -> None:
        self.state = tmp_path / "docker-state"
        self.state.mkdir()
        binary = tmp_path / "bin" / "docker"
        binary.parent.mkdir()
        binary.write_text(FAKE_DOCKER.replace("{python}", sys.executable), encoding="utf-8")
        binary.chmod(0o755)
        self.plan: dict[str, object] = {
            "info": 0,
            "build": 0,
            "inspect": "123456789 10001:10001\n",
            "run": {
                "id": "10001\n",
                "catalog-sync": "catalog-sync 1.0\n",
                "python": listing_json(),
                "find": "/app/.venv/lib/a.py\n/app/catalog-sync.toml\n",
            },
            **plan,
        }
        (self.state / "plan.json").write_text(json.dumps(self.plan), encoding="utf-8")
        monkeypatch.setenv("FAKE_DOCKER_DIR", str(self.state))
        monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ['PATH']}")

    @property
    def calls(self) -> list[list[str]]:
        text = (self.state / "calls.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]


def committed_harness(tmp_path: Path) -> Harness:
    """A harness whose repo is a git repository holding one commit of the demo project."""
    harness = harness_for(tmp_path)
    package = harness.repo / "src" / "catalog_sync"
    package.mkdir(parents=True)
    write_text(package / "__init__.py", '__version__ = "1.0"\n')
    write_text(harness.repo / "Dockerfile", "FROM scratch\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    }  # fmt: skip
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "demo"]):
        subprocess.run(["git", "-C", str(harness.repo), *args], check=True, env=env)
    return harness


def test_the_image_is_not_run_without_docker_or_without_a_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = harness_for(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    found = run(harness, acceptance_release.image)
    assert (found.status, found.reason) == (Status.NOT_RUN, "docker is not on PATH")
    FakeDocker(tmp_path, monkeypatch, info=1)
    found = run(harness, acceptance_release.image)
    assert found.status is Status.NOT_RUN and "daemon" in found.reason


def test_the_image_is_built_from_the_head_export_checked_and_its_tag_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker(tmp_path, monkeypatch)
    harness = committed_harness(tmp_path)
    found = run(harness, acceptance_release.image)
    assert found.status is Status.PASS, found.reason
    build = next(call for call in fake.calls if call[0] == "build")
    tag = build[build.index("--tag") + 1]
    assert tag.startswith(acceptance_release.IMAGE_TAG_PREFIX)
    assert build[-1] == str(harness.out / "image-src"), "the context is the export, not the repo"
    assert (harness.out / "image-src" / "Dockerfile").exists()
    assert ["rmi", "--force", tag] in fake.calls
    runs = [call for call in fake.calls if call[0] == "run"]
    assert runs and all(call[1:4] == ["--rm", "--network", "none"] for call in runs)
    assert found.evidence["image size (bytes)"] == 123456789


def test_the_tag_is_removed_when_the_build_fails_and_when_a_check_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker(tmp_path, monkeypatch, build=1)
    found = run(committed_harness(tmp_path), acceptance_release.image)
    assert found.status is Status.FAIL and failed_checks(found) == [
        "docker build of the HEAD export exits 0"
    ]
    assert any(call[0] == "rmi" for call in fake.calls)
    assert not any(call[0] == "run" for call in fake.calls), "nothing is run from a failed build"


def test_a_crash_while_inspecting_still_removes_the_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker(tmp_path, monkeypatch, inspect="not a number\n")
    found = run(committed_harness(tmp_path), acceptance_release.image)
    assert found.status is Status.FAIL and found.error is not None
    assert any(call[0] == "rmi" for call in fake.calls)


def test_a_root_image_with_a_connector_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = (*OWN_POINTS, ("catalog_sync.sources", "hidden", "x.y:z", "catalog-sync-demo"))
    run_plan = {
        "id": "0\n",
        "catalog-sync": "catalog-sync 1.0\n",
        "python": listing_json(extra),
        "find": "/app/.env\n",
    }
    FakeDocker(tmp_path, monkeypatch, inspect="1 root\n", run=run_plan)
    found = run(committed_harness(tmp_path), acceptance_release.image)
    assert found.status is Status.FAIL
    assert len(failed_checks(found)) >= 3  # the entry points, the user, the files
