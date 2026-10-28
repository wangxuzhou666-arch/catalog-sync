"""The release checks of the acceptance run: what the built package may and may not hold.

They are registered like the scenarios (see acceptance_scenarios) and run after them, on what the
steps built and installed:

* ``dist-check``: tools/check_dist.py over the built wheel and sdist;
* ``licenses``: the licence of every distribution installed in the clean wheel environment, as a
  table in the report; copyleft (outside a short reviewed list) and unknown licences fail;
* ``image``: the container image, built from a ``git archive`` export of HEAD; see below.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from acceptance_lab import Harness, HarnessError, scenario
from acceptance_report import NotRun, Recorder
from check_dist import (
    RULES,
    EntryPoints,
    Finding,
    check_listing,
    check_release,
    entry_point_findings,
    load_matcher,
    read_file_list,
    registered_entry_points,
)

# --- dist-check ---------------------------------------------------------------------------------

_RULE_TITLES: Mapping[str, str] = {
    "forbidden-term": "no forbidden term in a name or in content",
    "host-path": "no home-directory path of a macOS or Linux user",
    "entry-points": "only what the root pyproject registers, and no other package",
    "never-ship": "no file that must never ship",
    "required-file": "LICENSE, README.md and the licence metadata are there",
}
_SHOWN_FINDINGS = 6


@scenario("dist-check", "The built wheel and sdist hold nothing that must not ship", order=11)
def dist_check(harness: Harness, record: Recorder) -> None:
    dist = harness.out / "dist"
    wheel, sdist = next(dist.glob("*.whl")), next(dist.glob("*.tar.gz"))
    matcher, terms = load_matcher(harness.private_terms, repo=harness.repo)
    checked = check_release(
        wheel,
        sdist,
        pyproject=harness.repo / "pyproject.toml",
        matcher=matcher,
        term_count=terms,
    )
    by_rule = checked.by_rule()
    for rule in RULES:
        found = by_rule[rule]
        shown = [f"{f.artifact} {f.location}: {f.detail}" for f in found[:_SHOWN_FINDINGS]]
        more = f" (and {len(found) - _SHOWN_FINDINGS} more)" if len(found) > _SHOWN_FINDINGS else ""
        record.check(f"{rule}: {_RULE_TITLES[rule]}", not found, "; ".join(shown) + more)
    record.check(
        "both archives were really read (a rule that looked at nothing proves nothing)",
        all(count > 0 for count in checked.scanned.values()) and len(checked.scanned) == 2,
        str(checked.scanned),
    )
    record.evidence("entries scanned", checked.scanned)
    record.evidence(
        "private terms applied",
        terms if terms else "none: the public rules only",
    )


# --- licenses -----------------------------------------------------------------------------------

# Run inside the environment under test, with -I: it reads that environment's metadata only.
_LICENCE_LISTING = """
import importlib.metadata as metadata, json
rows = []
for dist in metadata.distributions():
    meta = dist.metadata
    rows.append({
        "name": meta["Name"],
        "version": meta["Version"],
        "expression": meta.get("License-Expression") or "",
        "license": meta.get("License") or "",
        "classifiers": [
            c for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")
        ],
    })
print(json.dumps(sorted(rows, key=lambda row: row["name"].lower())))
"""

# The copyleft families the spec names. Acronyms are matched with a letter guard before them (so
# "GPL" is not found inside "LGPL", nor "MPL" inside "simple") and at most one letter after them
# ("LGPLv3"); the long names cover metadata that spells the licence out.
_FAMILIES: Mapping[str, re.Pattern[str]] = {
    name: re.compile(rf"(?<![A-Za-z]){acronym}(?![A-Za-z]{{2}})|{words}", re.IGNORECASE)
    for name, acronym, words in (
        ("AGPL", "AGPL", "Affero"),
        ("LGPL", "LGPL", "Lesser General Public"),
        ("GPL", "GPL", "GNU General Public"),
        ("SSPL", "SSPL", "Server Side Public"),
        ("EUPL", "EUPL", "European Union Public"),
        ("MPL", "MPL", "Mozilla Public"),
    )
}

# Copyleft licences a person looked at and accepted, with the family each may carry and why. A new
# distribution, or one whose licence changes to another family, is not covered: it fails until a
# person adds it here (and says why in the commit). Nothing else relaxes the check.
REVIEWED_COPYLEFT: Mapping[str, tuple[frozenset[str], str]] = {
    "certifi": (
        frozenset({"MPL"}),
        "file-level copyleft on a bundle of CA certificates, imported unmodified; "
        "requests needs it",
    ),
    "psycopg": (
        frozenset({"LGPL"}),
        "LGPL, imported unmodified and linked dynamically; the optional [postgres] extra only",
    ),
    "psycopg-binary": (
        frozenset({"LGPL"}),
        "LGPL, imported unmodified and linked dynamically; the optional [postgres] extra only",
    ),
}
_UNSTATED = {"", "unknown"}


@dataclass(frozen=True)
class Licence:
    name: str
    version: str
    expression: str
    text: str
    classifiers: tuple[str, ...]

    @property
    def is_stated(self) -> bool:
        return any(
            part.strip().lower() not in _UNSTATED for part in (self.expression, self.text)
        ) or bool(self.classifiers)

    @property
    def families(self) -> frozenset[str]:
        """Which copyleft families any of the three places names (the strictest reading)."""
        everything = " ".join([self.expression, self.text, *self.classifiers])
        return frozenset(name for name, found in _FAMILIES.items() if found.search(everything))

    @property
    def shown(self) -> str:
        first_line = self.text.strip().splitlines()[0] if self.text.strip() else ""
        classifier = "; ".join(c.rsplit(" :: ", 1)[-1] for c in self.classifiers)
        return (self.expression or first_line or classifier or "(none stated)")[:70]

    @property
    def verdict(self) -> str:
        if not self.is_stated:
            return "UNKNOWN"
        if not self.families:
            return "ok"
        reviewed = REVIEWED_COPYLEFT.get(self.name.lower().replace("_", "-"))
        if reviewed is not None and self.families <= reviewed[0]:
            return "reviewed exception"
        return "COPYLEFT"


def licences_of(venv_python: Path, harness: Harness) -> list[Licence]:
    done = subprocess.run(
        [str(venv_python), "-I", "-c", _LICENCE_LISTING],
        cwd=harness.out / "work",
        env=harness.environment(harness.out / "home" / "licenses"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"listing the installed distributions failed: {done.stderr[-300:]}")
    return [
        Licence(
            row["name"],
            row["version"],
            row["expression"],
            row["license"],
            tuple(row["classifiers"]),
        )
        for row in json.loads(done.stdout)
    ]


def licence_checks(rows: Sequence[Licence], record: Recorder) -> None:
    """The checks and the table; separate from the listing so tests can feed it any rows."""
    record.check(
        "the listing holds catalog-sync itself and at least one dependency (it looked)",
        any(r.name == "catalog-sync" for r in rows) and len(rows) > 1,
        f"{len(rows)} distribution(s)",
    )
    unknown = [r.name for r in rows if r.verdict == "UNKNOWN"]
    record.check("every installed distribution states a licence", not unknown, ", ".join(unknown))
    copyleft = [f"{r.name} ({r.shown})" for r in rows if r.verdict == "COPYLEFT"]
    record.check(
        "no copyleft licence (GPL, AGPL, LGPL, SSPL, EUPL, MPL) outside the reviewed exceptions",
        not copyleft,
        ", ".join(copyleft),
    )
    record.table(
        "Licences of the distributions installed in the clean wheel environment",
        ("Distribution", "Version", "Licence", "Verdict"),
        [(r.name, r.version, r.shown, r.verdict) for r in rows],
    )
    record.evidence("distributions", len(rows))
    record.evidence(
        "reviewed copyleft exceptions in use",
        {
            r.name: REVIEWED_COPYLEFT[r.name.lower().replace("_", "-")][1]
            for r in rows
            if r.verdict == "reviewed exception"
        },
    )


@scenario(
    "licenses", "The licence of every installed distribution is known and not copyleft", order=12
)
def licenses(harness: Harness, record: Recorder) -> None:
    licence_checks(licences_of(harness.installed.python, harness), record)
    extras = "the postgres extra" if harness.postgres_dsn else "no extra (the base install only)"
    record.evidence("environment", f"the clean wheel environment, with {extras}")


# --- image --------------------------------------------------------------------------------------

DOCKER_TIMEOUT_S = 1800.0  # a build that downloads dependencies
APP_ROOT = "/app"  # the Dockerfile's working directory, and where its virtual environment is
IMAGE_TAG_PREFIX = "catalog-sync-accept-"
_VERSION_LINE = re.compile(r'^__version__ = "(?P<version>[^"]+)"', re.MULTILINE)
# Run inside the image with its own interpreter: the distributions it installed, and every entry
# point each of them registers.
_IMAGE_LISTING = """
import importlib.metadata as metadata, json
distributions, points = [], []
for dist in metadata.distributions():
    name = dist.metadata["Name"]
    distributions.append([name, dist.version])
    points += [[e.group, e.name, e.value, name] for e in dist.entry_points]
print(json.dumps({"distributions": sorted(distributions), "points": sorted(points)}))
"""


@dataclass(frozen=True)
class ImageFacts:
    size_bytes: int
    configured_user: str
    uid: int
    program_version: str
    distributions: tuple[tuple[str, str], ...]
    entry_points: tuple[tuple[str, str, str, str], ...]  # group, name, target, distribution


def _normalised(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_entry_points(facts: ImageFacts) -> EntryPoints:
    """What the image registers that concerns this tool: every `catalog_sync.*` group from any
    distribution (that is where a connector would show), and the program of catalog-sync itself."""
    found: EntryPoints = {}
    for group, name, target, distribution in facts.entry_points:
        own_program = group == "console_scripts" and _normalised(distribution) == "catalog-sync"
        if group.startswith("catalog_sync.") or own_program:
            found.setdefault(group, {})[name] = target
    return found


def image_checks(
    record: Recorder,
    facts: ImageFacts,
    *,
    registered: EntryPoints,
    listing: Sequence[Finding],
    expected_version: str,
) -> None:
    """What must hold of the image; separate from docker so tests can feed it any facts."""
    ours = [n for n, _ in facts.distributions if _normalised(n).startswith("catalog-sync")]
    record.check(
        "no connector distribution: catalog-sync is the only distribution of that name",
        [_normalised(n) for n in ours] == ["catalog-sync"],
        ", ".join(ours),
    )
    extra = entry_point_findings(
        "image", "installed entry points", installed_entry_points(facts), registered
    )
    record.check(
        "only the root pyproject's entry points are registered",
        not extra,
        "; ".join(f"{f.location}: {f.detail}" for f in extra[:_SHOWN_FINDINGS]),
    )
    record.check(
        "the image runs as a non-root user",
        facts.uid != 0 and facts.configured_user.split(":")[0] not in ("", "0", "root"),
        f"uid {facts.uid}, configured user {facts.configured_user!r}",
    )
    record.check(
        f"no file under {APP_ROOT} (the virtual environment included) is on the never-ship list",
        not listing,
        "; ".join(f"{f.location}: {f.detail}" for f in listing[:_SHOWN_FINDINGS]),
    )
    record.check(
        "its program reports the version of the source",
        facts.program_version == f"catalog-sync {expected_version}",
        facts.program_version,
    )


def _docker(
    harness: Harness, docker: str, *args: str, timeout: float = 300.0
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH")}
    return subprocess.run(
        [docker, *args],
        cwd=harness.out,
        env={**env, "TMPDIR": str(harness.out / "tmp"), "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def export_head(harness: Harness) -> Path:
    """The tracked files of HEAD as `git archive` writes them, so that nothing untracked or
    ignored on this machine can get into the build context."""
    archive, target = harness.out / "tmp" / "head.tar", harness.out / "image-src"
    done = subprocess.run(
        ["git", "-C", str(harness.repo), "archive", "--format=tar", "-o", str(archive), "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise HarnessError(f"git archive HEAD failed: {done.stderr.strip()[-300:]}")
    target.mkdir()
    with tarfile.open(archive) as exported:
        exported.extractall(target, filter="data")
    return target


def _docker_available(harness: Harness) -> str:
    docker = shutil.which("docker")
    if docker is None:
        raise NotRun("docker is not on PATH")
    if _docker(harness, docker, "info", timeout=60.0).returncode != 0:
        raise NotRun("the docker daemon does not answer (`docker info` failed)")
    return docker


def _in_image(
    harness: Harness, docker: str, tag: str, *args: str, entrypoint: str | None = None
) -> subprocess.CompletedProcess[str]:
    """A throw-away container of the image, with no network."""
    flags = ["--entrypoint", entrypoint] if entrypoint else []
    return _docker(harness, docker, "run", "--rm", "--network", "none", *flags, tag, *args)


def _inspect(harness: Harness, docker: str, tag: str) -> ImageFacts:
    shown = _docker(
        harness, docker, "image", "inspect", "--format", "{{.Size}} {{.Config.User}}", tag
    )
    size, _, user = shown.stdout.strip().partition(" ")
    ids = _in_image(harness, docker, tag, "-u", entrypoint="id")
    version = _in_image(harness, docker, tag, "--version")
    listed = _in_image(
        harness, docker, tag, "-I", "-c", _IMAGE_LISTING, entrypoint=f"{APP_ROOT}/.venv/bin/python"
    )
    if listed.returncode != 0:
        raise HarnessError(f"listing the image's distributions failed: {listed.stderr[-300:]}")
    document = json.loads(listed.stdout)
    return ImageFacts(
        size_bytes=int(size),
        configured_user=user,
        uid=int(ids.stdout.strip()),
        program_version=version.stdout.strip(),
        distributions=tuple((n, v) for n, v in document["distributions"]),
        entry_points=tuple((g, n, t, d) for g, n, t, d in document["points"]),
    )


def _image_files(harness: Harness, docker: str, tag: str) -> Path:
    found = _in_image(harness, docker, tag, APP_ROOT, entrypoint="find")
    if found.returncode != 0:
        raise HarnessError(f"listing the files of the image failed: {found.stderr[-300:]}")
    listing = harness.out / "logs" / "image-files.txt"
    listing.write_text(found.stdout, encoding="utf-8")
    return listing


@scenario("image", "The container image holds the core package only and runs as non-root", order=13)
def image(harness: Harness, record: Recorder) -> None:
    docker = _docker_available(harness)
    source = export_head(harness)
    tag = f"{IMAGE_TAG_PREFIX}{secrets.token_hex(4)}:rc"
    try:
        built = _docker(
            harness, docker, "build", "--tag", tag, str(source), timeout=DOCKER_TIMEOUT_S
        )
        (harness.out / "logs" / "image-build.log").write_text(
            built.stdout + built.stderr, encoding="utf-8"
        )
        record.check(
            "docker build of the HEAD export exits 0",
            built.returncode == 0,
            "" if built.returncode == 0 else built.stderr[-300:],
        )
        if built.returncode == 0:
            _check_built_image(harness, docker, tag, source, record)
    finally:
        _docker(harness, docker, "rmi", "--force", tag)
    gone = _docker(harness, docker, "image", "inspect", tag).returncode != 0
    record.check("the image tag this run built is removed afterwards", gone, tag)


def _check_built_image(
    harness: Harness, docker: str, tag: str, source: Path, record: Recorder
) -> None:
    facts = _inspect(harness, docker, tag)
    matcher, _ = load_matcher(harness.private_terms, repo=harness.repo)
    files = read_file_list(_image_files(harness, docker, tag), APP_ROOT)
    version = _VERSION_LINE.search(
        (source / "src" / "catalog_sync" / "__init__.py").read_text(encoding="utf-8")
    )
    assert version is not None, "the export has no __version__"
    image_checks(
        record,
        facts,
        registered=registered_entry_points(source / "pyproject.toml"),
        listing=check_listing("image", files, matcher),
        expected_version=version["version"],
    )
    record.check(
        "the listing of the image's files was not empty", len(files) > 0, f"{len(files)} entries"
    )
    record.evidence("image size (bytes)", facts.size_bytes)
    record.evidence("user", {"configured": facts.configured_user, "uid": facts.uid})
    record.evidence("distributions", [f"{name} {version}" for name, version in facts.distributions])
    record.evidence("entries under /app", len(files))
    record.evidence("exported commit", _head(harness))


def _head(harness: Harness) -> str:
    done = subprocess.run(
        ["git", "-C", str(harness.repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip()
