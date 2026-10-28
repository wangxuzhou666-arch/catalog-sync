"""Check a built wheel, a built sdist and, optionally, an image's file list for what must not ship.

    uv run python tools/check_dist.py --wheel FILE --sdist FILE [--image-files LIST]
        [--image-root /app] [--pyproject FILE] [--private-terms FILE]

The archives are read, never extracted. Five rules, each one a way a release has leaked or could:

* ``forbidden-term``: a private term (``--private-terms`` or $CATALOG_SYNC_PRIVATE_TERMS_FILE, read
  by the code tools/scrub_scan.py uses) or one of the public patterns below is in a file's name or
  content;
* ``host-path``: ``/Users/<name>`` or ``/home/<name>`` is in a name or content;
* ``entry-points``: the artifact registers a program, source, sink, connector or credential kind
  the root pyproject does not, or holds a plugin package no root entry point leads to;
* ``never-ship``: a file or directory that is local state or a credential (NEVER_SHIP_* below);
* ``required-file``: LICENSE, README.md (sdist) or the licence metadata (wheel) is missing.

Like scrub_scan, it never prints a private term or the text around a match: a finding names a term
by its line number in the terms file, and the place by path and line.

Exit codes: 0 nothing found, 1 findings, 2 the check could not be done. Nothing is reported clean
after a failure to look.
"""

from __future__ import annotations

import argparse
import configparser
import fnmatch
import os
import re
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from scrub_scan import Matcher, ScanError, load_terms

REPO_ROOT = Path(__file__).resolve().parent.parent
TERMS_ENV = "CATALOG_SYNC_PRIVATE_TERMS_FILE"
RULES = ("forbidden-term", "host-path", "entry-points", "never-ship", "required-file")
CORE_PACKAGE = "catalog_sync"
ENV_TEMPLATE = ".env.example"

# The home-directory prefix of a macOS or Linux user. No trailing slash is required: a path that
# ends at the user's name still names the user.
HOST_PATH = re.compile(r"(?<![A-Za-z0-9_.~-])/(?:Users|home)/[A-Za-z0-9._-]+")

# The public rules of .gitleaks.toml that apply to a built file, in Python's regex dialect.
# Confidential names are not here and must not be: they come in through the private terms file.
# The CJK ranges are code points, not characters, so this file holds no CJK itself.
_CJK_RANGES = ((0x3000, 0x303F), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xFF00, 0xFFEF))
PUBLIC_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "personal-email": re.compile(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
    ),
    "cjk-text": re.compile("[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _CJK_RANGES) + "]+"),
    "bearer-token": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "private-key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
# A match of the pattern that is safe by construction, as .gitleaks.toml allows it.
_SAFE_MATCH: Mapping[str, re.Pattern[str]] = {
    "personal-email": re.compile(
        r"(?:@users\.noreply\.github\.com|@example\.(?:com|org|net)|^noreply@anthropic\.com)$"
    ),
}
# Files a public pattern does not apply to (the Chinese README is the one sanctioned CJK file).
_PATTERN_EXEMPT: Mapping[str, frozenset[str]] = {"cjk-text": frozenset({"README.zh-CN.md"})}

# Directory names that must not appear at any depth.
NEVER_SHIP_DIRS: Mapping[str, str] = {
    "connectors": "a connector package (released on its own, or not at all)",
    ".claude": "agent tool state",
    ".git": "version control state",
    "runs": "workspace run output",
    "out": "sink output",
    "browser-profile": "a browser profile",
    ".browser_profile": "a browser profile",
}
# Directory names that must not be a top-level entry (`catalog_sync/credentials/` is a package).
NEVER_SHIP_ROOT_DIRS: Mapping[str, str] = {
    "cache": "raw API snapshots",
    "secrets": "credentials",
    "credentials": "credentials",
    "creds": "credentials",
    "browser": "a browser profile",
}
# File name globs, matched case-insensitively on the last path component. `.env*` is handled apart.
NEVER_SHIP_FILES: Mapping[str, str] = {
    "*curl*.txt": "a pasted request capture",
    "*.cred": "a credential file",
    "*.token": "a credential file",
    "*.db": "a database",
    "*.db-journal": "a database",
    "*.sqlite*": "a database",
    ".pre-commit-config.yaml": "the maintainer's hook configuration",
}


class CheckError(Exception):
    """The check could not be carried out; the message is safe to print."""


@dataclass(frozen=True)
class Member:
    """One entry of an archive or of a file list. `content` is None when it has none to read."""

    path: str  # POSIX, relative to the artifact's root
    content: bytes | None = None

    @property
    def text(self) -> str:
        return (self.content or b"").decode("utf-8", errors="replace")

    @property
    def parts(self) -> tuple[str, ...]:
        return PurePosixPath(self.path).parts


@dataclass(frozen=True)
class Finding:
    rule: str
    artifact: str  # wheel, sdist or image
    location: str  # never contains a private term
    detail: str  # never contains a private term or the text that matched


@dataclass(frozen=True)
class Checked:
    findings: list[Finding]
    scanned: dict[str, int]  # artifact -> number of entries looked at
    private_terms: int  # how many private terms were applied (0 = public rules only)

    def by_rule(self) -> dict[str, list[Finding]]:
        return {rule: [f for f in self.findings if f.rule == rule] for rule in RULES}


# --- reading ------------------------------------------------------------------------------------


def read_wheel(path: Path) -> list[Member]:
    try:
        with zipfile.ZipFile(path) as archive:
            return [
                Member(info.filename.rstrip("/"), None if info.is_dir() else archive.read(info))
                for info in archive.infolist()
            ]
    except (OSError, zipfile.BadZipFile) as exc:
        raise CheckError(f"cannot read the wheel {path.name}: {exc}") from exc


def read_sdist(path: Path) -> list[Member]:
    """The members of the source distribution, each path relative to its single top directory."""
    members: list[Member] = []
    try:
        with tarfile.open(path, "r:gz") as archive:
            for info in archive:
                relative = "/".join(PurePosixPath(info.name).parts[1:])
                if not relative:
                    continue
                reader = archive.extractfile(info) if info.isfile() else None
                members.append(Member(relative, reader.read() if reader else None))
    except (OSError, tarfile.TarError) as exc:
        raise CheckError(f"cannot read the sdist {path.name}: {exc}") from exc
    return members


def read_file_list(path: Path, root: str) -> list[Member]:
    """An image's listing, one path per line; `root` (the app directory) is taken off the front."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CheckError(f"cannot read the image file list: {exc.strerror}") from exc
    prefix = root.rstrip("/") + "/"
    members = []
    for line in lines:
        name = line.strip()
        if name.startswith(prefix):
            members.append(Member(name.removeprefix(prefix)))
    return members


# --- forbidden-term and host-path ---------------------------------------------------------------


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _pattern_lines(name: str, text: str) -> list[int]:
    """Line numbers where public pattern `name` matches, safe matches left out."""
    safe = _SAFE_MATCH.get(name)
    return [
        _line_of(text, found.start())
        for found in PUBLIC_PATTERNS[name].finditer(text)
        if safe is None or safe.search(found.group()) is None
    ]


def check_terms(artifact: str, members: Sequence[Member], matcher: Matcher | None) -> list[Finding]:
    findings: list[Finding] = []
    for member in members:
        shown = matcher.mask(member.path) if matcher else member.path
        if matcher:
            findings += [
                Finding("forbidden-term", artifact, f"file name {shown}", f"private term {n}")
                for n in matcher.terms_in(member.path)
            ]
            findings += [
                Finding("forbidden-term", artifact, f"{shown}:{line}", f"private term {n}")
                for n, line in matcher.lines_with(member.text)
            ]
        findings += _public_pattern_findings(artifact, member, shown)
    return findings


def _public_pattern_findings(artifact: str, member: Member, shown: str) -> list[Finding]:
    findings = []
    for name, pattern in PUBLIC_PATTERNS.items():
        if member.path in _PATTERN_EXEMPT.get(name, ()):
            continue
        if pattern.search(member.path):
            findings.append(Finding("forbidden-term", artifact, f"file name {shown}", name))
        findings += [
            Finding("forbidden-term", artifact, f"{shown}:{line}", name)
            for line in _pattern_lines(name, member.text)
        ]
    return findings


def check_host_paths(artifact: str, members: Sequence[Member]) -> list[Finding]:
    findings = []
    for member in members:
        if HOST_PATH.search("/" + member.path):  # the archive's root counts as a directory
            findings.append(
                Finding("host-path", artifact, f"file name {member.path}", "a home directory path")
            )
        findings += [
            Finding(
                "host-path",
                artifact,
                f"{member.path}:{_line_of(member.text, found.start())}",
                "a home directory path",
            )
            for found in HOST_PATH.finditer(member.text)
        ]
    return findings


# --- entry-points -------------------------------------------------------------------------------

EntryPoints = dict[str, dict[str, str]]  # group -> name -> "module:attribute"


def entry_points_of_pyproject(text: str, *, where: str) -> EntryPoints:
    """Everything a pyproject registers: its programs and every entry-point group."""
    try:
        project = tomllib.loads(text)["project"]
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise CheckError(f"cannot read the [project] table of {where}: {exc!r}") from exc
    groups: EntryPoints = {"console_scripts": dict(project.get("scripts", {}))}
    for group, entries in project.get("entry-points", {}).items():
        groups[group] = dict(entries)
    return groups


def registered_entry_points(pyproject: Path) -> EntryPoints:
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError as exc:
        raise CheckError(f"cannot read the root pyproject: {exc.strerror}") from exc
    return entry_points_of_pyproject(text, where="the root pyproject")


class _CaseSensitiveParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr  # entry-point names are case-sensitive and keep their dots and slashes


def parse_entry_points_txt(text: str) -> EntryPoints:
    parser = _CaseSensitiveParser(delimiters=("=",), interpolation=None)
    parser.read_string(text)
    return {group: dict(parser.items(group)) for group in parser.sections()}


def entry_point_findings(
    artifact: str, location: str, found: EntryPoints, registered: EntryPoints
) -> list[Finding]:
    findings = []
    for group in sorted({*found, *registered}):
        here, there = found.get(group, {}), registered.get(group, {})
        for name in sorted({*here, *there}):
            if here.get(name) == there.get(name):
                continue
            # Here but not equal there: an extra name, or a name with another target.
            what = (
                "not registered by the root pyproject"
                if name in here
                else "registered by the root pyproject but absent here"
            )
            findings.append(
                Finding("entry-points", artifact, f"{location}: [{group}] {name}", what)
            )
    return findings


def _registered_plugin_packages(registered: EntryPoints) -> set[str]:
    """`mock` for a target `catalog_sync.plugins.mock.source:Class`."""
    prefix = f"{CORE_PACKAGE}.plugins."
    targets = (value for entries in registered.values() for value in entries.values())
    return {
        target.removeprefix(prefix).split(".")[0].split(":")[0]
        for target in targets
        if target.startswith(prefix)
    }


def _package_roots(members: Sequence[Member], prefix: str) -> set[str]:
    """Names of the top-level packages: directories right under `prefix` ("" or "src/")."""
    depth = len(PurePosixPath(prefix).parts)
    return {
        member.parts[depth]
        for member in members
        if member.path.startswith(prefix) and len(member.parts) > depth + 1
    }


def _unregistered_packages(
    artifact: str, members: Sequence[Member], prefix: str, registered: EntryPoints
) -> list[Finding]:
    findings = []
    for package in sorted(_package_roots(members, prefix) - {CORE_PACKAGE}):
        if package.endswith((".dist-info", ".data")):
            continue
        findings.append(
            Finding(
                "entry-points", artifact, f"{prefix}{package}/", "a package other than the core"
            )
        )
    findings += [
        Finding("entry-points", artifact, member.path, "a module outside the core package")
        for member in members
        if member.path.startswith(prefix)
        and member.path.endswith(".py")
        and len(member.parts) == len(PurePosixPath(prefix).parts) + 1
    ]
    plugins_prefix = f"{prefix}{CORE_PACKAGE}/plugins/"
    shipped = _package_roots(members, plugins_prefix) - {"__pycache__"}
    for plugin in sorted(shipped - _registered_plugin_packages(registered)):
        findings.append(
            Finding(
                "entry-points",
                artifact,
                f"{plugins_prefix}{plugin}/",
                "no root entry point leads here",
            )
        )
    return findings


def check_wheel_entry_points(members: Sequence[Member], registered: EntryPoints) -> list[Finding]:
    listed = [m for m in members if m.path.endswith(".dist-info/entry_points.txt")]
    found = parse_entry_points_txt(listed[0].text) if listed else {}
    location = listed[0].path if listed else "entry_points.txt"
    return [
        *entry_point_findings("wheel", location, found, registered),
        *_unregistered_packages("wheel", members, "", registered),
    ]


def check_sdist_entry_points(members: Sequence[Member], registered: EntryPoints) -> list[Finding]:
    pyprojects = [m for m in members if m.path == "pyproject.toml"]
    if not pyprojects:
        return []  # reported by the required-file rule
    found = entry_points_of_pyproject(pyprojects[0].text, where="the sdist's pyproject.toml")
    return [
        *entry_point_findings("sdist", "pyproject.toml", found, registered),
        *_unregistered_packages("sdist", members, "src/", registered),
    ]


# --- never-ship ---------------------------------------------------------------------------------


def _env_template_has_no_value(member: Member) -> bool:
    """A template lists variables and gives none a value; whoever copies it fills them in."""
    if member.content is None:
        return False  # a listing cannot show it
    assignments = [
        line for line in member.text.splitlines() if line.strip() and not line.startswith("#")
    ]
    return all("=" in line and not line.split("=", 1)[1].strip() for line in assignments)


def _never_ship_reason(member: Member) -> str | None:
    parts = member.parts
    for depth, part in enumerate(parts):
        reason = NEVER_SHIP_DIRS.get(part) or (
            NEVER_SHIP_ROOT_DIRS.get(part) if depth == 0 else None
        )
        if reason:
            return reason
    name = parts[-1].lower() if parts else ""
    if name == ".env" or (name.startswith(".env.") and not _is_clean_template(member)):
        return "an environment file"
    for glob, reason in NEVER_SHIP_FILES.items():
        if fnmatch.fnmatchcase(name, glob):
            return reason
    return None


def _is_clean_template(member: Member) -> bool:
    return member.parts[-1] == ENV_TEMPLATE and _env_template_has_no_value(member)


def check_never_ship(artifact: str, members: Sequence[Member]) -> list[Finding]:
    findings = []
    for member in members:
        reason = _never_ship_reason(member)
        if reason:
            findings.append(Finding("never-ship", artifact, member.path, reason))
    return findings


# --- required-file ------------------------------------------------------------------------------

_LICENCE_FIELD = re.compile(r"(?m)^(?:License-Expression|License): \S")


def check_wheel_required(members: Sequence[Member]) -> list[Finding]:
    findings = []
    licences = [m for m in members if re.search(r"\.dist-info/(?:licenses/)?LICENSE$", m.path)]
    if not licences:
        findings.append(
            Finding("required-file", "wheel", "*.dist-info/licenses/LICENSE", "missing")
        )
    metadata = [m for m in members if m.path.endswith(".dist-info/METADATA")]
    if not metadata or not _LICENCE_FIELD.search(metadata[0].text):
        findings.append(
            Finding(
                "required-file", "wheel", "*.dist-info/METADATA", "no License-Expression or License"
            )
        )
    return findings


def check_sdist_required(members: Sequence[Member]) -> list[Finding]:
    present = {m.path for m in members}
    return [
        Finding("required-file", "sdist", name, "missing")
        for name in ("LICENSE", "README.md", "pyproject.toml")
        if name not in present
    ]


# --- putting it together ------------------------------------------------------------------------


def check_listing(
    artifact: str, members: Sequence[Member], matcher: Matcher | None
) -> list[Finding]:
    """The rules that need names only: what a file list can show."""
    return [
        *check_terms(artifact, members, matcher),
        *check_host_paths(artifact, members),
        *check_never_ship(artifact, members),
    ]


def check_release(
    wheel: Path,
    sdist: Path,
    *,
    pyproject: Path,
    matcher: Matcher | None = None,
    term_count: int = 0,
    image_files: Path | None = None,
    image_root: str = "/app",
) -> Checked:
    registered = registered_entry_points(pyproject)
    wheel_members, sdist_members = read_wheel(wheel), read_sdist(sdist)
    artifacts = {"wheel": wheel_members, "sdist": sdist_members}
    if image_files is not None:
        artifacts["image"] = read_file_list(image_files, image_root)
    findings: list[Finding] = []
    for artifact, members in artifacts.items():
        findings += check_listing(artifact, members, matcher)
    findings += check_wheel_entry_points(wheel_members, registered)
    findings += check_sdist_entry_points(sdist_members, registered)
    findings += check_wheel_required(wheel_members)
    findings += check_sdist_required(sdist_members)
    scanned = {artifact: len(members) for artifact, members in artifacts.items()}
    return Checked(findings, scanned, term_count)


def load_matcher(terms_file: Path | None, *, repo: Path) -> tuple[Matcher | None, int]:
    """The private terms, read as scrub_scan reads them, or none when no file was given."""
    if terms_file is None:
        return None, 0
    try:
        terms = load_terms(terms_file, repo=repo)
    except ScanError as exc:
        raise CheckError(str(exc)) from exc
    return Matcher(terms), len(terms)


def report(checked: Checked) -> str:
    lines = [f"check_dist: {len(checked.findings)} finding(s)"]
    for finding in checked.findings:
        lines.append(
            f"  {finding.rule:<15} {finding.artifact:<6} {finding.location}: {finding.detail}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check built artifacts for what must not ship.")
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--sdist", required=True, type=Path)
    parser.add_argument("--image-files", type=Path, help="a file list, one path per line")
    parser.add_argument("--image-root", default="/app", help="the prefix of the listed paths")
    parser.add_argument("--pyproject", type=Path, default=REPO_ROOT / "pyproject.toml")
    parser.add_argument(
        "--private-terms",
        type=Path,
        default=Path(os.environ[TERMS_ENV]) if os.environ.get(TERMS_ENV) else None,
        help=f"private file, one term per line (default: ${TERMS_ENV})",
    )
    args = parser.parse_args(argv)
    try:
        matcher, term_count = load_matcher(args.private_terms, repo=args.pyproject.parent)
        checked = check_release(
            args.wheel,
            args.sdist,
            pyproject=args.pyproject,
            matcher=matcher,
            term_count=term_count,
            image_files=args.image_files,
            image_root=args.image_root,
        )
    except CheckError as exc:
        print(f"check_dist: {exc}", file=sys.stderr)
        return 2
    if checked.findings:
        print(report(checked), file=sys.stderr)
        return 1
    where = ", ".join(f"{count} {artifact} entries" for artifact, count in checked.scanned.items())
    terms = (
        f"{checked.private_terms} private terms" if checked.private_terms else "public rules only"
    )
    print(f"check_dist: nothing found ({where}; {terms})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
