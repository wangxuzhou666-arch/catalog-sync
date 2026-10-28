"""Pre-publication scan: does anything confidential appear anywhere in this repository?

The confidential terms (a customer's name, real catalogue codes, a supplier, a folder name) are
kept in a private file *outside* the repository, one per line, and passed with ``--terms``. This
tool looks for them in everything that would be published, not just in the files as they are now:

* the working tree (tracked files and untracked files that are not ignored), contents and names;
* every file version ever committed on any ref, including files that were later deleted or edited
  (the equivalent of grepping ``git log -p --all``, done once per distinct blob);
* commit messages, author and committer names and emails, and ref names and tag messages.

It never prints a term, nor the line it was found on, because its output ends up in CI logs and
terminals. A finding names the term by its **line number in the terms file**, and the place by file
and line, by commit, or by which field of which commit. Any part of a path that matches a term is
replaced by ``<term N>``.

Matching is case-insensitive, Unicode-normalised (NFKC), and treats a space inside a term as "any
run of spaces, underscores, hyphens and dots, or nothing", so ``Acme Textile`` also finds
``acme_textile`` and ``AcmeTextile``. Terms are literal text, not patterns.

Exit codes: 0 nothing found, 1 findings, 2 the scan could not be done (bad terms file, not a git
repository, git failed). Nothing is ever reported as clean after a failure to look.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

MIN_TERM_LENGTH = 2
SHOWN_COMMITS = 5
SHOWN_LINES = 8
_SEPARATORS = r"[\s_.\-]*"
_FIELD, _RECORD = "\x1f", "\x1e"


class ScanError(Exception):
    """The scan could not be carried out; the message is safe to print."""


@dataclass(frozen=True)
class Term:
    number: int  # 1-based line number in the terms file: the only name a term is given in output
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class Finding:
    term: int
    area: str  # tree, history, message, identity or ref
    location: str  # never contains a term
    line: int | None = None


def load_terms(path: Path, *, repo: Path) -> list[Term]:
    resolved = path.resolve()
    if resolved.is_relative_to(repo.resolve()):
        raise ScanError(
            "the terms file is inside the repository being scanned; keep it outside, or it "
            "would be scanned, and committed, along with everything else"
        )
    try:
        lines = resolved.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScanError(f"cannot read the terms file: {exc.strerror}") from exc
    terms: list[Term] = []
    for number, line in enumerate(lines, start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if len(text) < MIN_TERM_LENGTH:
            raise ScanError(
                f"terms file line {number}: a term needs at least {MIN_TERM_LENGTH} characters"
            )
        terms.append(Term(number, _compile(text)))
    if not terms:
        raise ScanError("the terms file has no terms (blank lines and # comments do not count)")
    return terms


def _compile(term: str) -> re.Pattern[str]:
    words = unicodedata.normalize("NFKC", term).split()
    return re.compile(_SEPARATORS.join(re.escape(word) for word in words), re.IGNORECASE)


class Matcher:
    def __init__(self, terms: Sequence[Term]) -> None:
        self._terms = terms

    def terms_in(self, text: str) -> list[int]:
        """Numbers of the terms that occur anywhere in `text`."""
        normal = _normalise(text)
        return [term.number for term in self._terms if term.pattern.search(normal)]

    def lines_with(self, text: str) -> Iterator[tuple[int, int]]:
        """``(term number, line number)`` for every line of `text` holding a term."""
        for line_number, line in enumerate(_normalise(text).splitlines(), start=1):
            for term in self._terms:
                if term.pattern.search(line):
                    yield term.number, line_number

    def mask(self, text: str) -> str:
        """`text` with every term replaced by ``<term N>``, so a path can be printed."""
        masked = _normalise(text)
        for term in self._terms:
            masked = term.pattern.sub(f"<term {term.number}>", masked)
        return masked


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


# --- the working tree -------------------------------------------------------------------------


def scan_tree(repo: Path, matcher: Matcher) -> list[Finding]:
    findings: list[Finding] = []
    for relative in _tree_files(repo):
        for number in matcher.terms_in(relative):
            findings.append(Finding(number, "tree", f"file name {matcher.mask(relative)}"))
        text = (repo / relative).read_bytes().decode("utf-8", errors="replace")
        for number, line in matcher.lines_with(text):
            findings.append(Finding(number, "tree", matcher.mask(relative), line))
    return findings


def _tree_files(repo: Path) -> list[str]:
    if (repo / ".git").exists():
        listing = _git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        names = [name for name in listing.decode("utf-8", errors="replace").split("\0") if name]
        return sorted(name for name in names if (repo / name).is_file())
    return sorted(
        str(path.relative_to(repo))
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    )


# --- history ----------------------------------------------------------------------------------


def scan_history(repo: Path, matcher: Matcher) -> list[Finding]:
    """Every file version, commit message, identity and ref name reachable from any ref."""
    return [
        *_scan_blobs(repo, matcher),
        *_scan_commits(repo, matcher),
        *_scan_refs(repo, matcher),
    ]


def _scan_blobs(repo: Path, matcher: Matcher) -> list[Finding]:
    paths_by_blob: dict[str, list[str]] = {}
    listing = _git(repo, "-c", "core.quotepath=off", "rev-list", "--all", "--objects")
    for entry in listing.decode("utf-8", errors="replace").splitlines():
        oid, _, path = entry.partition(" ")
        if path:
            paths_by_blob.setdefault(oid, []).append(path)
    findings: list[Finding] = []
    for oid, text in _blobs(repo, list(paths_by_blob)):
        paths = paths_by_blob[oid]
        named = [matcher.mask(path) for path in paths]
        for number in {n for path in paths for n in matcher.terms_in(path)}:
            findings.append(Finding(number, "history", f"file name {named[0]} (blob {oid[:7]})"))
        hits = list(matcher.lines_with(text))
        if hits:
            where = f"{named[0]} (blob {oid[:7]}; {_commits_with(repo, oid)})"
            findings += [Finding(number, "history", where, line) for number, line in hits]
    return findings


def _blobs(repo: Path, oids: Sequence[str]) -> Iterator[tuple[str, str]]:
    """``(oid, text)`` of each object in `oids` that is a blob, via one ``cat-file --batch``."""
    if not oids:
        return
    batch = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input=("\n".join(oids) + "\n").encode(),
        capture_output=True,
        check=False,
    )
    if batch.returncode != 0:
        raise ScanError("git cat-file failed; the history was not scanned")
    data, position = batch.stdout, 0
    while position < len(data):
        end = data.index(b"\n", position)
        oid, kind, size = data[position:end].decode().split()
        start = end + 1
        content = data[start : start + int(size)]
        position = start + int(size) + 1
        if kind == "blob":
            yield oid, content.decode("utf-8", errors="replace")


def _commits_with(repo: Path, oid: str) -> str:
    shown = _git(repo, "log", "--all", "--format=%h", f"--find-object={oid}").decode().split()
    more = f" and {len(shown) - SHOWN_COMMITS} more" if len(shown) > SHOWN_COMMITS else ""
    return "commits " + ", ".join(shown[:SHOWN_COMMITS]) + more if shown else "no commit found"


_COMMIT_FIELDS = ("author name", "author email", "committer name", "committer email", "message")


def _scan_commits(repo: Path, matcher: Matcher) -> list[Finding]:
    fmt = _FIELD.join(["%h", "%an", "%ae", "%cn", "%ce", "%B"]) + _RECORD
    out = _git(repo, "log", "--all", f"--format={fmt}").decode("utf-8", errors="replace")
    findings: list[Finding] = []
    for record in out.split(_RECORD):
        if not record.strip():
            continue
        short, *fields = record.strip("\n").split(_FIELD)
        for label, value in zip(_COMMIT_FIELDS, fields, strict=True):
            area = "message" if label == "message" else "identity"
            findings += [
                Finding(number, area, f"commit {short}: {label}")
                for number in matcher.terms_in(value)
            ]
    return findings


def _scan_refs(repo: Path, matcher: Matcher) -> list[Finding]:
    """Ref names, and the tagger and message of annotated tags.

    A branch's own message is its tip commit's, which the commit scan already covers.
    """
    fields = ["%(refname)", "%(objecttype)", "%(taggername)", "%(taggeremail)", "%(contents)"]
    out = _git(repo, "for-each-ref", f"--format={_FIELD.join(fields)}{_RECORD}")
    findings: list[Finding] = []
    for record in out.decode("utf-8", errors="replace").split(_RECORD):
        if not record.strip():
            continue
        name, kind, *tag_fields = record.strip("\n").split(_FIELD)
        shown = matcher.mask(name)
        findings += [Finding(n, "ref", f"ref name {shown}") for n in matcher.terms_in(name)]
        if kind != "tag":
            continue
        for label, value in zip(
            ("tagger name", "tagger email", "message"), tag_fields, strict=True
        ):
            findings += [Finding(n, "ref", f"{shown}: {label}") for n in matcher.terms_in(value)]
    return findings


# --- command line -----------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if result.returncode != 0:
        raise ScanError(
            f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()[:200]}"
        )
    return result.stdout


def run_scan(repo: Path, terms_file: Path, *, include_history: bool) -> list[Finding]:
    matcher = Matcher(load_terms(terms_file, repo=repo))
    findings = scan_tree(repo, matcher)
    if include_history:
        if not (repo / ".git").exists():
            raise ScanError("not a git repository: there is no history to scan (--no-history)")
        findings += scan_history(repo, matcher)
    return sorted(set(findings), key=lambda f: (f.term, f.area, f.location, f.line or 0))


def report(findings: Iterable[Finding]) -> str:
    items = list(findings)
    grouped: dict[tuple[int, str, str], list[int]] = {}
    for finding in items:
        lines = grouped.setdefault((finding.term, finding.area, finding.location), [])
        if finding.line is not None:
            lines.append(finding.line)
    out = [
        f"scrub_scan: {len(items)} finding(s) for {len({f.term for f in items})} term(s). "
        "Terms are named by their line number in the terms file; nothing matched is printed."
    ]
    for (term, area, location), lines in sorted(grouped.items()):
        shown = ", ".join(str(n) for n in sorted(set(lines))[:SHOWN_LINES])
        more = len(set(lines)) - SHOWN_LINES
        suffix = f" line {shown}" + (f" and {more} more" if more > 0 else "") if lines else ""
        out.append(f"  term {term:<3} {area:<9} {location}{suffix}")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan a repository for confidential terms.")
    parser.add_argument("--terms", required=True, type=Path, help="private file, one term per line")
    parser.add_argument("--repo", type=Path, default=Path(), help="repository root (default: .)")
    parser.add_argument("--no-history", action="store_true", help="scan the working tree only")
    args = parser.parse_args(argv)
    try:
        findings = run_scan(args.repo, args.terms, include_history=not args.no_history)
    except ScanError as exc:
        print(f"scrub_scan: {exc}", file=sys.stderr)
        return 2
    if findings:
        print(report(findings), file=sys.stderr)
        return 1
    print("scrub_scan: no term found in the tree, the history, the messages or the ref names")
    return 0


if __name__ == "__main__":
    sys.exit(main())
