"""The pre-publication scanner, proven on throw-away repositories (CODING-STYLE R.7).

Every term here is generated at run time, so no confidential-looking string sits in this file. Each
place the scanner is meant to look gets a repository where the term is in that place and nowhere
else (it must fail), a repository with near misses (it must pass), and a run with the responsible
scanner switched off (it must pass, which shows that scanner is what found it).
"""

from __future__ import annotations

import os
import secrets
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import scrub_scan
from scrub_scan import Matcher, ScanError, load_terms, main

TERM = "vq" + secrets.token_hex(4) + "zk"  # one confidential name
OTHER = "tl" + secrets.token_hex(4) + "mw"  # another, which never appears anywhere
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "Test Committer",
    "GIT_COMMITTER_EMAIL": "committer@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo: Path, *args: str, **env: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        check=True,
        capture_output=True,
        env={**os.environ, **GIT_ENV, **env},
    )


def new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "README.md", "an ordinary file\nwith ordinary lines\n")
    commit(repo, "initial commit")
    return repo


def write(repo: Path, name: str, text: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str, **env: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, **env)


def terms_file(tmp_path: Path, *terms: str) -> Path:
    path = tmp_path / "private" / "terms.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(terms) + "\n", encoding="utf-8")
    return path


def scan(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *terms: str,
    extra: tuple[str, ...] = (),
) -> tuple[int, str]:
    listed = terms or (TERM,)
    code = main(["--terms", str(terms_file(tmp_path, *listed)), "--repo", str(repo), *extra])
    shown = capsys.readouterr()
    return code, shown.out + shown.err


# --- one scenario per place ---------------------------------------------------------------------


def untracked_file(repo: Path) -> None:
    write(repo, "notes/draft.txt", f"line one\nsee {TERM} here\n")


def tracked_file(repo: Path) -> None:
    write(repo, "src/module.py", f"x = 1\n# {TERM}\n")
    commit(repo, "add module")


def file_name_only(repo: Path) -> None:
    write(repo, f"{TERM}-notes.md", "nothing sensitive inside\n")


def committed_file_name(repo: Path) -> None:
    write(repo, f"{TERM}-notes.md", "nothing sensitive inside\n")
    commit(repo, "add notes")
    git(repo, "rm", "-q", f"{TERM}-notes.md")
    commit(repo, "remove notes")


def deleted_file(repo: Path) -> None:
    write(repo, "secret.txt", f"{TERM}\n")
    commit(repo, "add file")
    git(repo, "rm", "-q", "secret.txt")
    commit(repo, "remove file")


def scrubbed_line(repo: Path) -> None:
    write(repo, "data.txt", f"keep\n{TERM}\nkeep\n")
    commit(repo, "add data")
    write(repo, "data.txt", "keep\nkeep\n")
    commit(repo, "scrub data")


def other_branch(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "wip")
    write(repo, "wip.txt", f"{TERM}\n")
    commit(repo, "work in progress")
    git(repo, "checkout", "-q", "main")


def message(repo: Path) -> None:
    write(repo, "a.txt", "a\n")
    commit(repo, f"fix the import for {TERM}")


def author_name(repo: Path) -> None:
    write(repo, "a.txt", "a\n")
    commit(repo, "change", GIT_AUTHOR_NAME=f"Dev {TERM}")


def author_email(repo: Path) -> None:
    write(repo, "a.txt", "a\n")
    commit(repo, "change", GIT_AUTHOR_EMAIL=f"someone@{TERM}.example.com")


def committer_email(repo: Path) -> None:
    write(repo, "a.txt", "a\n")
    commit(repo, "change", GIT_COMMITTER_EMAIL=f"someone@{TERM}.example.com")


def tag_message(repo: Path) -> None:
    git(repo, "tag", "-a", "v1", "-m", f"release for {TERM}")


def branch_name(repo: Path) -> None:
    git(repo, "branch", f"feature/{TERM}")


TREE, BLOBS, COMMITS, REFS = "scan_tree", "_scan_blobs", "_scan_commits", "_scan_refs"
# scenario -> (how to build it, the scanner(s) that find it, the area reported first)
SCENARIOS: dict[str, tuple[Callable[[Path], None], tuple[str, ...], str]] = {
    "an untracked file": (untracked_file, (TREE,), "tree"),
    "a file name that was never committed": (file_name_only, (TREE,), "tree"),
    "a committed file": (tracked_file, (TREE, BLOBS), "tree"),
    "the name of a file that was later deleted": (committed_file_name, (BLOBS,), "history"),
    "a file that was later deleted": (deleted_file, (BLOBS,), "history"),
    "a line that was later scrubbed": (scrubbed_line, (BLOBS,), "history"),
    "a file on another branch": (other_branch, (BLOBS,), "history"),
    "a commit message": (message, (COMMITS,), "message"),
    "an author name": (author_name, (COMMITS,), "identity"),
    "an author email": (author_email, (COMMITS,), "identity"),
    "a committer email": (committer_email, (COMMITS,), "identity"),
    "a tag message": (tag_message, (REFS,), "ref"),
    "a branch name": (branch_name, (REFS,), "ref"),
}
ALL_SCANNERS = (TREE, BLOBS, COMMITS, REFS)


@pytest.mark.parametrize("name", SCENARIOS)
def test_a_term_in_each_place_is_found_and_never_printed(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build, _, area = SCENARIOS[name]
    repo = new_repo(tmp_path)
    build(repo)
    code, output = scan(repo, tmp_path, capsys)
    assert code == 1, output
    assert "term 1 " in output and area in output
    assert TERM.lower() not in output.lower(), "the term must not be echoed, not even in a path"


@pytest.mark.parametrize("name", SCENARIOS)
def test_the_responsible_scanner_is_what_finds_it(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    build, responsible, _ = SCENARIOS[name]
    repo = new_repo(tmp_path)
    build(repo)
    for other in ALL_SCANNERS:
        if other not in responsible:
            monkeypatch.setattr(scrub_scan, other, lambda *_a, **_k: [])
    assert scan(repo, tmp_path, capsys)[0] == 1, "left alone, it still finds it"
    monkeypatch.undo()

    for scanner in responsible:
        monkeypatch.setattr(scrub_scan, scanner, lambda *_a, **_k: [])
    code, output = scan(repo, tmp_path, capsys)
    assert code == 0, f"nothing else covers this place, so switching it off must pass: {output}"


def test_a_path_is_printed_with_the_term_masked_and_the_rest_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = new_repo(tmp_path)
    file_name_only(repo)
    _, output = scan(repo, tmp_path, capsys)
    assert "<term 1>-notes.md" in output


def test_the_report_names_the_line_not_its_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = new_repo(tmp_path)
    untracked_file(repo)
    _, output = scan(repo, tmp_path, capsys)
    assert "notes/draft.txt line 2" in output
    assert "see " not in output.replace("scrub_scan", "")


def test_a_history_finding_says_which_commits_touched_the_file_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = new_repo(tmp_path)
    deleted_file(repo)
    _, output = scan(repo, tmp_path, capsys)
    assert "secret.txt" in output and "blob " in output and "commits " in output


def test_terms_are_told_apart_by_their_line_in_the_terms_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = new_repo(tmp_path)
    untracked_file(repo)
    code, output = scan(repo, tmp_path, capsys, "# a comment", "", OTHER, TERM)
    assert code == 1
    assert "term 4 " in output and "term 3 " not in output, "comments and blanks keep their numbers"


# --- what must pass -------------------------------------------------------------------------------


def test_a_clean_repository_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = new_repo(tmp_path)
    tracked = repo / "src"
    tracked.mkdir()
    write(repo, "src/clean.py", "print('hello')\n")
    commit(repo, "add a clean module")
    code, output = scan(repo, tmp_path, capsys, TERM, OTHER)
    assert code == 0 and "no term found" in output


@pytest.mark.parametrize(
    "variant",
    [TERM[:-1], TERM[1:], TERM[::-1], TERM[: len(TERM) // 2] + " " + TERM[len(TERM) // 2 :]],
    ids=["missing last character", "missing first character", "reversed", "split by a space"],
)
def test_near_misses_pass(variant: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = new_repo(tmp_path)
    write(repo, "notes.txt", f"{variant}\n")
    commit(repo, f"mention {variant}")
    assert scan(repo, tmp_path, capsys)[0] == 0


def test_an_ignored_file_is_not_part_of_what_would_be_published(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = new_repo(tmp_path)
    write(repo, ".gitignore", "local/\n")
    commit(repo, "ignore local files")
    write(repo, "local/scratch.txt", f"{TERM}\n")
    assert scan(repo, tmp_path, capsys)[0] == 0


# --- how terms match ------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [TERM.upper(), TERM.title(), f"x{TERM}y", f"<<{TERM}>>"])
def test_matching_ignores_case_and_surroundings(text: str, tmp_path: Path) -> None:
    matcher = Matcher(load_terms(terms_file(tmp_path, TERM), repo=tmp_path / "elsewhere"))
    assert matcher.terms_in(text) == [1]


@pytest.mark.parametrize("separator", [" ", "_", "-", ".", "", "  ", " - "])
def test_a_space_inside_a_term_matches_any_separator_or_none(
    separator: str, tmp_path: Path
) -> None:
    matcher = Matcher(load_terms(terms_file(tmp_path, f"{TERM} {OTHER}"), repo=tmp_path / "x"))
    assert matcher.terms_in(f"{TERM}{separator}{OTHER}") == [1]
    assert matcher.terms_in(f"{TERM}/{OTHER}") == [], "a slash is a different thing"


def test_full_width_forms_are_found_through_normalisation(tmp_path: Path) -> None:
    matcher = Matcher(load_terms(terms_file(tmp_path, "abc" + TERM), repo=tmp_path / "x"))
    wide = "".join(chr(ord(c) + 0xFEE0) if "!" < c <= "~" else c for c in "abc" + TERM)
    assert matcher.terms_in(wide) == [1]


def test_non_ascii_terms_are_found_in_content_and_in_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    term = "".join(chr(point) for point in (0x4E09, 0x6DA6))
    repo = new_repo(tmp_path)
    write(repo, f"{term}.txt", f"{term}\n")
    code, output = scan(repo, tmp_path, capsys, term)
    assert code == 1 and term not in output and "<term 1>" in output


# --- when the scan itself cannot be done ---------------------------------------------------------


def test_a_missing_or_empty_terms_file_is_an_error_not_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = new_repo(tmp_path)
    assert main(["--terms", str(tmp_path / "nothing.txt"), "--repo", str(repo)]) == 2
    assert "cannot read the terms file" in capsys.readouterr().err
    empty = terms_file(tmp_path, "# only a comment", "", "   ")
    assert main(["--terms", str(empty), "--repo", str(repo)]) == 2
    assert "no terms" in capsys.readouterr().err


def test_a_terms_file_inside_the_repository_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = new_repo(tmp_path)
    write(repo, "terms.txt", f"{TERM}\n")
    assert main(["--terms", str(repo / "terms.txt"), "--repo", str(repo)]) == 2
    assert "inside the repository" in capsys.readouterr().err


def test_a_one_character_term_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ScanError, match="line 2"):
        load_terms(terms_file(tmp_path, TERM, "x"), repo=tmp_path / "elsewhere")


def test_a_directory_that_is_not_a_repository_cannot_have_its_history_scanned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    write(plain, "a.txt", f"{TERM}\n")
    assert scan(plain, tmp_path, capsys)[0] == 2
    code, output = scan(plain, tmp_path, capsys, extra=("--no-history",))
    assert code == 1 and "a.txt line 1" in output


def test_a_repository_without_commits_has_only_a_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "fresh"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    assert scan(repo, tmp_path, capsys)[0] == 0
    write(repo, "a.txt", f"{TERM}\n")
    assert scan(repo, tmp_path, capsys)[0] == 1


def test_a_failing_git_is_an_error_not_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = new_repo(tmp_path)
    real_run = subprocess.run

    def broken(command: list[str], *args: object, **kwargs: object) -> object:
        if "cat-file" in command:
            return subprocess.CompletedProcess(command, 128, b"", b"fatal: broken")
        return real_run(command, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "run", broken)
    code, output = scan(repo, tmp_path, capsys)
    assert code == 2 and "history was not scanned" in output


def test_the_scanner_has_the_exit_codes_it_documents(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    terms = terms_file(tmp_path, TERM)
    script = Path(scrub_scan.__file__)
    clean = subprocess.run(
        [os_python(), str(script), "--terms", str(terms), "--repo", str(repo)],
        capture_output=True,
        check=False,
    )
    assert clean.returncode == 0
    untracked_file(repo)
    dirty = subprocess.run(
        [os_python(), str(script), "--terms", str(terms), "--repo", str(repo)],
        capture_output=True,
        check=False,
    )
    assert dirty.returncode == 1 and TERM.encode() not in dirty.stdout + dirty.stderr


def os_python() -> str:
    import sys

    return sys.executable
