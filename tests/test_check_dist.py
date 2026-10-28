"""tools/check_dist.py, proven on throw-away wheels and sdists (CODING-STYLE R.7).

Each rule gets an artifact that breaks only that rule (it must fail, and with that rule's id), and
near misses (they must pass). Everything that looks confidential is assembled at run time, so none
of it sits in this file.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from pathlib import Path

import pytest

import check_dist
from check_dist import (
    Checked,
    CheckError,
    check_release,
    load_matcher,
    main,
    read_file_list,
    read_sdist,
    read_wheel,
    report,
)
from tests.conftest import REPO_ROOT
from tests.dist_builders import (
    CJK,
    ENTRY_POINTS,
    GOOD_SDIST,
    GOOD_WHEEL,
    PYPROJECT,
    write_sdist,
    write_wheel,
)

TERM = "xq" + secrets.token_hex(4) + "wv"  # a private term, which no good artifact holds
HOME_PATH = "/" + "Users" + "/" + "alice"
LINUX_HOME = "/" + "home" + "/" + "bob"
SHADOW_EMAIL = "jane.doe" + "@" + "corp.net"
TOKEN = "Bearer " + "A1b2C3d4" * 4
KEY_BLOCK = "-----BEGIN " + "PRIVATE KEY-----"


class Dists:
    """A wheel and an sdist in a temporary directory; given files replace or add to good ones."""

    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path / "dists"
        self.dir.mkdir()
        self.pyproject = self.dir / "pyproject.toml"
        self.pyproject.write_text(PYPROJECT, encoding="utf-8")

    def check(
        self,
        wheel: Mapping[str, str | bytes | None] | None = None,
        sdist: Mapping[str, str | bytes | None] | None = None,
        *,
        terms: Path | None = None,
        image_files: str | None = None,
    ) -> Checked:
        """`None` as a value removes the file."""
        wheel_files = {**GOOD_WHEEL, **(wheel or {})}
        sdist_files = {**GOOD_SDIST, **(sdist or {})}
        built_wheel = write_wheel(
            self.dir / "demo.whl", {k: v for k, v in wheel_files.items() if v is not None}
        )
        built_sdist = write_sdist(
            self.dir / "demo.tar.gz", {k: v for k, v in sdist_files.items() if v is not None}
        )
        listing = None
        if image_files is not None:
            listing = self.dir / "image.txt"
            listing.write_text(image_files, encoding="utf-8")
        matcher, count = load_matcher(terms, repo=REPO_ROOT)
        return check_release(
            built_wheel,
            built_sdist,
            pyproject=self.pyproject,
            matcher=matcher,
            term_count=count,
            image_files=listing,
        )


@pytest.fixture
def dists(tmp_path: Path) -> Dists:
    return Dists(tmp_path)


def terms_file(tmp_path: Path, *terms: str) -> Path:
    path = tmp_path / "terms.txt"
    path.write_text("\n".join(terms) + "\n", encoding="utf-8")
    return path


def rules_of(checked: Checked) -> set[str]:
    return {finding.rule for finding in checked.findings}


# --- the good artifacts -------------------------------------------------------------------------


def test_a_clean_wheel_and_sdist_pass_every_rule(dists: Dists) -> None:
    checked = dists.check()
    assert checked.findings == [], report(checked)
    assert checked.scanned == {"wheel": len(GOOD_WHEEL), "sdist": len(GOOD_SDIST)}


def test_the_real_pyproject_is_readable_by_the_entry_point_rule() -> None:
    registered = check_dist.registered_entry_points(REPO_ROOT / "pyproject.toml")
    assert "catalog-sync" in registered["console_scripts"]
    assert registered["catalog_sync.sources"], "the rule must really see the root entry points"


# --- forbidden-term -----------------------------------------------------------------------------


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_a_private_term_in_content_fails_and_is_named_by_its_line_number(
    dists: Dists, tmp_path: Path, artifact: str
) -> None:
    files = {"docs/notes.txt": f"fine\nthe customer is {TERM}\n"}
    wheel = {"catalog_sync/notes.txt": files["docs/notes.txt"]} if artifact == "wheel" else None
    sdist = files if artifact == "sdist" else None
    checked = dists.check(wheel, sdist, terms=terms_file(tmp_path, "other-name", TERM))
    [finding] = checked.findings
    assert (finding.rule, finding.artifact, finding.detail) == (
        "forbidden-term",
        artifact,
        "private term 2",
    )
    assert finding.location.endswith("notes.txt:2")
    assert TERM not in report(checked)


def test_a_private_term_in_a_file_name_fails_and_the_name_is_masked(
    dists: Dists, tmp_path: Path
) -> None:
    checked = dists.check(
        sdist={f"docs/{TERM}-plan.md": "ordinary\n"}, terms=terms_file(tmp_path, TERM)
    )
    [finding] = checked.findings
    assert finding.rule == "forbidden-term" and "<term 1>" in finding.location
    assert TERM not in report(checked)


def test_a_term_with_a_space_also_finds_its_joined_and_underscored_forms(
    dists: Dists, tmp_path: Path
) -> None:
    words = f"{TERM} textile"
    checked = dists.check(
        sdist={"docs/a.md": f"see {TERM}_textile here\n"}, terms=terms_file(tmp_path, words)
    )
    assert rules_of(checked) == {"forbidden-term"}


def test_text_that_is_not_a_private_term_passes_even_with_a_terms_file(
    dists: Dists, tmp_path: Path
) -> None:
    near = {"docs/a.md": f"{TERM[:-1]} and {TERM[1:]}\n"}
    checked = dists.check(sdist=near, terms=terms_file(tmp_path, TERM))
    assert checked.findings == [] and checked.private_terms == 1


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        (f"mail {SHADOW_EMAIL}\n", "personal-email"),
        (f"title {CJK}\n", "cjk-text"),
        (f"Authorization: {TOKEN}\n", "bearer-token"),
        (f"{KEY_BLOCK}\nabc\n", "private-key"),
        ("t=eyJ" + "a" * 10 + ".eyJ" + "b" * 10 + "." + "c" * 10 + "\n", "jwt"),
    ],
)
def test_each_public_pattern_fails_an_artifact_that_holds_it(
    dists: Dists, text: str, pattern: str
) -> None:
    for wheel, sdist in (({"catalog_sync/n.txt": text}, None), (None, {"docs/n.txt": text})):
        checked = dists.check(wheel, sdist)
        assert [f.detail for f in checked.findings] == [pattern], (wheel, sdist)


@pytest.mark.parametrize(
    "text",
    [
        "mail someone@example.com, a@users.noreply.github.com, noreply@anthropic.com\n",
        "Authorization: Bearer <token>\n",
        "a short Bearer abc\n",
        "-----BEGIN CERTIFICATE-----\n",
        "plain ascii text\n",
    ],
)
def test_near_misses_of_the_public_patterns_pass(dists: Dists, text: str) -> None:
    assert dists.check(sdist={"docs/n.txt": text}).findings == []


def test_only_the_two_reviewed_chinese_documents_may_hold_cjk(dists: Dists) -> None:
    assert dists.check().findings == []  # README.zh-CN.md is in the good sdist
    assert dists.check(sdist={"docs/README.zh-CN.md": CJK}).findings == []
    checked = dists.check(sdist={"docs/private-notes.md": CJK})
    assert [f.rule for f in checked.findings] == ["forbidden-term"]


def test_chinese_document_allowlist_does_not_exempt_private_terms(
    dists: Dists, tmp_path: Path
) -> None:
    checked = dists.check(
        sdist={"docs/README.zh-CN.md": f"{CJK} {TERM}"},
        terms=terms_file(tmp_path, TERM),
    )
    assert [(f.rule, f.detail) for f in checked.findings] == [("forbidden-term", "private term 1")]
    assert TERM not in report(checked)


# --- host-path ----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [HOME_PATH, LINUX_HOME, HOME_PATH + "/work/x.py"])
def test_a_home_directory_path_in_content_fails_without_being_printed(
    dists: Dists, path: str
) -> None:
    checked = dists.check(wheel={"catalog_sync/mod.py": f'ROOT = "{path}"\n'})
    assert [(f.rule, f.artifact) for f in checked.findings] == [("host-path", "wheel")]
    assert checked.findings[0].location.endswith("mod.py:1")
    assert path not in report(checked)


def test_a_home_directory_path_in_a_file_name_fails(dists: Dists) -> None:
    checked = dists.check(sdist={f"{HOME_PATH.lstrip('/')}/notes.md": "x\n"})
    assert "host-path" in rules_of(checked)


@pytest.mark.parametrize(
    "text",
    [
        "~/work and $HOME/x\n",
        "/opt/home/x and /homes/y and /Users\n",
        "a/Us" + "ers/b is a relative path\n",
    ],
)
def test_text_near_a_home_directory_path_passes(dists: Dists, text: str) -> None:
    assert dists.check(sdist={"docs/n.md": text}).findings == []


# --- entry-points -------------------------------------------------------------------------------


def test_a_wheel_entry_point_the_root_pyproject_lacks_fails(dists: Dists) -> None:
    extra = ENTRY_POINTS.replace(
        "alpha.two = ", "beta = catalog_sync.plugins.alpha:other\nalpha.two = "
    )
    checked = dists.check(wheel={"catalog_sync-1.0.dist-info/entry_points.txt": extra})
    [finding] = checked.findings
    assert finding.rule == "entry-points" and "[catalog_sync.sources] beta" in finding.location
    assert finding.detail == "not registered by the root pyproject"


def test_a_wheel_entry_point_in_a_group_the_root_pyproject_lacks_fails(dists: Dists) -> None:
    extra = ENTRY_POINTS + "\n[catalog_sync.credentials]\nhidden = catalog_sync.plugins.alpha:x\n"
    checked = dists.check(wheel={"catalog_sync-1.0.dist-info/entry_points.txt": extra})
    assert [f.rule for f in checked.findings] == ["entry-points"]


def test_an_entry_point_with_another_target_than_the_pyproject_fails(dists: Dists) -> None:
    changed = ENTRY_POINTS.replace("alpha:two", "alpha:other")
    checked = dists.check(wheel={"catalog_sync-1.0.dist-info/entry_points.txt": changed})
    assert [f.rule for f in checked.findings] == ["entry-points"]


def test_an_entry_point_the_wheel_lost_fails(dists: Dists) -> None:
    lost = ENTRY_POINTS.replace("alpha.two = catalog_sync.plugins.alpha:two\n", "")
    checked = dists.check(wheel={"catalog_sync-1.0.dist-info/entry_points.txt": lost})
    [finding] = checked.findings
    assert finding.detail == "registered by the root pyproject but absent here"


def test_an_sdist_pyproject_that_registers_more_than_the_root_one_fails(dists: Dists) -> None:
    more = PYPROJECT + '\n[project.entry-points."catalog_sync.sinks"]\nx = "catalog_sync.sinks:x"\n'
    checked = dists.check(sdist={"pyproject.toml": more})
    assert [(f.rule, f.artifact) for f in checked.findings] == [("entry-points", "sdist")]


@pytest.mark.parametrize(
    ("wheel", "sdist"),
    [
        ({"catalog_sync/plugins/hidden/__init__.py": ""}, None),
        ({"other_pkg/__init__.py": ""}, None),
        ({"stray.py": "x = 1\n"}, None),
        (None, {"src/catalog_sync/plugins/hidden/source.py": "x = 1\n"}),
        (None, {"src/other_pkg/__init__.py": ""}),
        (None, {"src/stray.py": "x = 1\n"}),
    ],
)
def test_code_no_root_entry_point_leads_to_fails(
    dists: Dists, wheel: dict[str, str] | None, sdist: dict[str, str] | None
) -> None:
    assert rules_of(dists.check(wheel, sdist)) == {"entry-points"}


def test_a_package_next_to_the_core_with_a_dist_info_directory_is_not_a_second_package(
    dists: Dists,
) -> None:
    assert dists.check(wheel={"catalog_sync-1.0.data/scripts/demo": "#!python\n"}).findings == []


# --- never-ship ---------------------------------------------------------------------------------

NEVER = [
    "connectors/demo/pyproject.toml",
    "src/connectors/demo.py",
    ".claude/settings.json",
    ".env",
    "deploy/.env",
    ".env.local",
    "notes/session-curl.txt",
    "notes/Style-CURL-capture.TXT",
    "x/token.cred",
    "x/session.token",
    "state.db",
    "x/state.sqlite",
    "x/state.sqlite-wal",
    "x/data.sqlite3",
    "x/data.db-journal",
    "runs/run-1/raw.json",
    "docs/runs/run-1.json",
    "out/materials.csv",
    "browser-profile/Default/Cookies",
    "x/.browser_profile/Default",
    ".pre-commit-config.yaml",
    "credentials/mock.json",
    "secrets/key.txt",
    "cache/snapshot.json",
    ".git/config",
]
FINE = [
    "src/catalog_sync/credentials/login.py",
    "docs/runs.md",
    "docs/outline.md",
    "tests/test_cache.py",
    "tests/fixtures/curl_paste.py",
    "docs/curl-notes.md",
    "deploy/.env.example",
    ".gitignore",
    ".gitleaks.toml",
    "tests/browser_helpers.py",
    "src/catalog_sync/state.py",
]


@pytest.mark.parametrize("name", NEVER)
def test_a_file_that_must_never_ship_fails_in_the_sdist_and_in_the_wheel(
    dists: Dists, name: str
) -> None:
    for wheel, sdist in ((None, {name: "x\n"}), ({name: "x\n"}, None)):
        assert "never-ship" in rules_of(dists.check(wheel, sdist)), (name, wheel, sdist)


@pytest.mark.parametrize("name", FINE)
def test_a_file_that_merely_looks_similar_passes(dists: Dists, name: str) -> None:
    assert dists.check(sdist={name: "A=\n"}).findings == []


def test_the_env_template_passes_only_while_it_gives_no_variable_a_value(dists: Dists) -> None:
    assert dists.check(sdist={"deploy/.env.example": "A=\nB=\n"}).findings == []
    filled = dists.check(sdist={"deploy/.env.example": "A=\nB=hunter2\n"})
    assert [f.rule for f in filled.findings] == ["never-ship"]
    assert "hunter2" not in report(filled)


def test_a_credentials_package_is_fine_but_a_top_level_credentials_directory_is_not(
    dists: Dists,
) -> None:
    assert dists.check(sdist={"src/catalog_sync/credentials/a.py": "x = 1\n"}).findings == []
    assert "never-ship" in rules_of(dists.check(sdist={"credentials/a.py": "x = 1\n"}))


# --- required-file ------------------------------------------------------------------------------


def test_a_wheel_without_a_licence_file_fails(dists: Dists) -> None:
    checked = dists.check(wheel={"catalog_sync-1.0.dist-info/licenses/LICENSE": None})
    assert [(f.rule, f.artifact) for f in checked.findings] == [("required-file", "wheel")]


def test_a_wheel_whose_metadata_names_no_licence_fails(dists: Dists) -> None:
    bare = "Metadata-Version: 2.5\nName: catalog-sync\nVersion: 1.0\n"
    checked = dists.check(wheel={"catalog_sync-1.0.dist-info/METADATA": bare})
    assert [(f.rule, f.artifact) for f in checked.findings] == [("required-file", "wheel")]
    old_style = "Name: x\nLicense: MIT\n"
    assert dists.check(wheel={"catalog_sync-1.0.dist-info/METADATA": old_style}).findings == []


@pytest.mark.parametrize("name", ["LICENSE", "README.md", "pyproject.toml"])
def test_an_sdist_missing_a_required_file_fails(dists: Dists, name: str) -> None:
    checked = dists.check(sdist={name: None})
    assert ("required-file", "sdist", name) in {
        (f.rule, f.artifact, f.location) for f in checked.findings
    }


# --- the image's file list ----------------------------------------------------------------------

IMAGE_FILES = (
    "/app/.venv/lib/python3.12/site-packages/catalog_sync/cli.py\n/app/catalog-sync.toml\n"
)


def test_an_image_file_list_of_ordinary_files_passes(dists: Dists) -> None:
    checked = dists.check(image_files=IMAGE_FILES)
    assert checked.findings == [] and checked.scanned["image"] == 2


@pytest.mark.parametrize(
    "extra", ["/app/.env", "/app/connectors", "/app/runs/a.json", "/app/state.sqlite"]
)
def test_a_never_ship_file_in_the_image_fails(dists: Dists, extra: str) -> None:
    checked = dists.check(image_files=IMAGE_FILES + extra + "\n")
    assert [(f.rule, f.artifact) for f in checked.findings] == [("never-ship", "image")]


def test_a_private_term_or_a_home_path_in_an_image_file_name_fails(
    dists: Dists, tmp_path: Path
) -> None:
    listing = IMAGE_FILES + f"/app/{TERM}.txt\n/app{HOME_PATH}/x\n"
    checked = dists.check(image_files=listing, terms=terms_file(tmp_path, TERM))
    assert {"forbidden-term", "host-path"} <= rules_of(checked)
    assert TERM not in report(checked)


def test_a_path_outside_the_app_root_is_not_part_of_the_listing(tmp_path: Path) -> None:
    listing = tmp_path / "files.txt"
    listing.write_text("/etc/passwd\n/app/a.txt\n", encoding="utf-8")
    assert [m.path for m in read_file_list(listing, "/app")] == ["a.txt"]


# --- reading and the command line ---------------------------------------------------------------


def test_the_archive_readers_strip_the_sdist_top_directory_and_keep_wheel_paths(
    tmp_path: Path,
) -> None:
    wheel = write_wheel(tmp_path / "a.whl", {"pkg/a.py": "x\n"})
    sdist = write_sdist(tmp_path / "a.tar.gz", {"src/pkg/a.py": "x\n"})
    assert [m.path for m in read_wheel(wheel)] == ["pkg/a.py"]
    assert [m.path for m in read_sdist(sdist)] == ["src/pkg/a.py"]


def cli(dists: Dists, *extra: str) -> int:
    return main(
        [
            "--wheel", str(dists.dir / "demo.whl"),
            "--sdist", str(dists.dir / "demo.tar.gz"),
            "--pyproject", str(dists.pyproject),
            *extra,
        ]
    )  # fmt: skip


def test_the_exit_code_is_0_for_a_clean_release_and_1_for_a_finding(
    dists: Dists, capsys: pytest.CaptureFixture[str]
) -> None:
    dists.check()
    assert cli(dists) == 0
    assert "nothing found" in capsys.readouterr().out
    dists.check(sdist={".env": "A=1\n"})
    assert cli(dists) == 1
    assert "never-ship" in capsys.readouterr().err


def test_the_terms_file_comes_from_the_flag_or_from_the_environment(
    dists: Dists,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dists.check(sdist={"docs/a.md": f"{TERM}\n"})
    terms = terms_file(tmp_path, TERM)
    assert cli(dists, "--private-terms", str(terms)) == 1
    assert cli(dists) == 0, "without a terms file only the public rules apply"
    monkeypatch.setenv(check_dist.TERMS_ENV, str(terms))
    assert cli(dists) == 1
    assert TERM not in capsys.readouterr().err


def test_a_check_that_could_not_be_done_is_exit_2_never_a_pass(
    dists: Dists, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dists.check()
    inside = REPO_ROOT / "pyproject.toml"
    assert cli(dists, "--pyproject", str(inside), "--private-terms", str(inside)) == 2
    assert "inside the repository" in capsys.readouterr().err
    (dists.dir / "demo.whl").write_bytes(b"not a zip")
    assert cli(dists) == 2
    missing = tmp_path / "missing.txt"
    assert cli(dists, "--private-terms", str(missing)) == 2
    with pytest.raises(CheckError):
        read_wheel(tmp_path / "nope.whl")
