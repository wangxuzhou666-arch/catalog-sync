"""``catalog-sync new-connector``: the name rules, the generator, a generated package run for real.

The strongest claim the command makes is that what it writes is green at once: it passes its own
tests, ``ruff check``, ``ruff format --check`` and ``mypy --strict``, and it plugs into the tool's
registry and compliance gate. The end-to-end tests below generate packages into a temporary
directory and run exactly those commands on them, in child processes (a generated package has
its own ``tests`` package, which must not meet this repository's).
"""

from __future__ import annotations

import ast
import itertools
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from catalog_sync import cli, registry
from catalog_sync.errors import ConfigError
from catalog_sync.scaffold import (
    AUTH_KINDS,
    PAGINATIONS,
    ConnectorSpec,
    CoreDependency,
    check_name,
    default_directory,
    find_checkout,
    generate,
    render,
    resolve_core,
)
from tests.conftest import REPO_ROOT

# Every credential kind with every pagination. Each of the 12 is generated and then run for real
# (ruff, mypy, its own tests): a template or fragment that is fine alone can still break when two
# of them meet, and a claim that every combination is tested has to mean all of them.
VARIANTS = list(itertools.product(AUTH_KINDS, PAGINATIONS))
# Built from code points so that this file holds no CJK text itself.
CJK = re.compile(
    "[" + "".join(chr(point) for point in (0x3400, 0x2D, 0x9FFF, 0xFF00, 0x2D, 0xFFEF)) + "]"
)
LONGEST_NAME = "abcdefghi_jklmnopqrs"  # 20 characters, two words: the longest every line must fit


def spec_for(name: str = "acme", auth: str = "bearer", pagination: str = "page") -> ConnectorSpec:
    return ConnectorSpec(name, auth, pagination)


def generated(tmp_path: Path, name: str, auth: str, pagination: str) -> Path:
    directory = tmp_path / f"{name}-{auth}-{pagination}"
    generate(spec_for(name, auth, pagination), directory)
    return directory


def child_env() -> dict[str, str]:
    """The environment of a child that must import this checkout of catalog_sync, whether or not
    the editable install's path file is honoured by the interpreter."""
    src = str(REPO_ROOT / "src")
    return {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": src,
        "MYPYPATH": src,
    }


def run(arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", *arguments],
        cwd=cwd,
        env=child_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def shown(result: subprocess.CompletedProcess[str]) -> str:
    return f"exit {result.returncode}\n{result.stdout}\n{result.stderr}"


# --- the name -------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["acme", "acme_crm", "a", "x2", "shop_2_go", "t" * 20])
def test_a_lowercase_identifier_that_nothing_uses_is_accepted(name: str) -> None:
    check_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Acme",
        "ACME",
        "acme-crm",
        "acme crm",
        "acme.crm",
        "1acme",
        "_acme",
        "acme_",
        "acme__crm",
        "../acme",
        "acme/crm",
        "t" * 21,
        "acmé",
    ],
)
def test_anything_else_is_refused_before_a_file_is_written(name: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid connector name"):
        generate(spec_for(name), tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("name", "existing"),
    [
        ("mock", "source plugin 'mock.materials'"),
        ("mock", "credential kind 'mock'"),
        ("replay", "source plugin 'replay'"),
    ],
)
def test_the_name_of_an_installed_connector_is_refused_and_says_which(
    name: str, existing: str, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError, match=f"already installed.*{existing}"):
        generate(spec_for(name), tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("group", "entry", "name"),
    [
        (registry.SOURCES_GROUP, "zeta.orders", "zeta"),
        (registry.CREDENTIALS_GROUP, "zeta", "zeta"),
        (registry.SCHEMAS_GROUP, "zeta_item", "zeta"),
    ],
)
def test_a_connector_installed_from_another_package_counts_too(
    group: str, entry: str, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry is asked, not a list kept in this module: any of the three names a generated
    package registers (its source prefix, its credential kind, its entity) is enough to clash."""
    real = registry.available
    monkeypatch.setattr(
        registry, "available", lambda g: [*real(g), entry] if g == group else real(g)
    )
    with pytest.raises(ConfigError, match="already installed"):
        check_name(name)


def test_a_name_that_only_looks_similar_to_an_installed_one_is_fine() -> None:
    check_name("mockery")  # not `mock`: only an exact first segment clashes
    check_name("mock_pro")


# --- the directory and the options ----------------------------------------------------------------


def test_the_default_directory_is_named_after_the_distribution() -> None:
    assert default_directory(spec_for("acme_crm"), cwd=Path("/work")) == Path(
        "/work/catalog-sync-acme-crm"
    )


def test_a_directory_that_has_anything_in_it_is_left_alone(tmp_path: Path) -> None:
    directory = tmp_path / "taken"
    directory.mkdir()
    (directory / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(ConfigError, match="already exists and is not empty; nothing was written"):
        generate(spec_for(), directory)
    assert [p.name for p in directory.iterdir()] == ["notes.txt"]
    assert (directory / "notes.txt").read_text(encoding="utf-8") == "mine"


def test_a_file_where_the_directory_should_be_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "a-file"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="not empty"):
        generate(spec_for(), target)


def test_an_existing_empty_directory_is_filled(tmp_path: Path) -> None:
    directory = tmp_path / "empty"
    directory.mkdir()
    assert generate(spec_for(), directory)
    assert (directory / "pyproject.toml").is_file()


@pytest.mark.parametrize(
    ("auth", "pagination", "complaint"),
    [
        ("oauth", "page", "--auth must be one of"),
        ("bearer", "cursor", "--pagination must be one of"),
    ],
)
def test_an_unknown_variant_is_refused(
    auth: str, pagination: str, complaint: str, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError, match=complaint):
        generate(spec_for(auth=auth, pagination=pagination), tmp_path / "out")
    assert not (tmp_path / "out").exists()


# --- the template engine --------------------------------------------------------------------------


def test_every_marker_is_replaced_and_other_braces_are_left_alone() -> None:
    template = 'path = f"/items/{item_id}"  # {{name}}\nd = {{{key}}: 1}\n'
    assert (
        render(template, {"name": "acme", "key": "k"})
        == 'path = f"/items/{item_id}"  # acme\nd = {k: 1}\n'
    )


def test_a_marker_without_a_value_is_a_bug_not_a_blank() -> None:
    with pytest.raises(ValueError, match=r"\{\{missing\}\}"):
        render("a {{name}} b {{missing}}", {"name": "x"})


def test_a_fragment_may_hold_markers_of_its_own() -> None:
    assert render("{{outer}}", {"outer": "see {{inner}}", "inner": "here"}) == "see here"


def test_a_fragment_that_refers_to_itself_is_not_followed_forever() -> None:
    with pytest.raises(ValueError, match="unresolved"):
        render("{{loop}}", {"loop": "again {{loop}}"})


def test_an_empty_whole_line_marker_takes_the_line_and_the_blank_after_it() -> None:
    template = "def a():\n    pass\n\n{{extra}}\n\ndef b():\n    pass\n"
    assert render(template, {"extra": ""}) == "def a():\n    pass\n\ndef b():\n    pass\n"
    full = render(template, {"extra": "def x():\n    pass"})
    assert full == "def a():\n    pass\n\ndef x():\n    pass\n\ndef b():\n    pass\n"


def test_a_marker_inside_a_line_with_an_empty_value_leaves_the_line() -> None:
    assert render("x = {{v}}  # c\n", {"v": ""}) == "x =   # c\n"


def test_every_fragment_piece_is_used_by_some_template() -> None:
    """The engine refuses a marker with no value; this is the other direction, so that a piece
    left behind when a template changes does not sit in the fragment files unnoticed."""
    from catalog_sync.scaffold import _TEMPLATES, _pieces

    templates = "\n".join(path.read_text("utf-8") for path in _TEMPLATES.rglob("*.tmpl"))
    pieces = {
        name
        for fragment in sorted((_TEMPLATES / "fragments").glob("*.tmpl"))
        for name in _pieces(f"fragments/{fragment.name}")
    }
    assert pieces, "the scan must really see the fragment files"
    unused = sorted(name for name in pieces if "{{" + name + "}}" not in templates)
    assert unused == []


def test_every_fragment_defines_the_same_pieces_as_its_siblings() -> None:
    """Choosing --auth or --pagination must never leave a marker without a value."""
    from catalog_sync.scaffold import _TEMPLATES, _pieces

    for kind in ("pagination", "auth"):
        files = sorted((_TEMPLATES / "fragments").glob(f"{kind}.*.tmpl"))
        assert len(files) >= 3
        defined = [set(_pieces(f"fragments/{f.name}")) for f in files]
        assert all(names == defined[0] for names in defined), kind


# --- what is written ------------------------------------------------------------------------------


@pytest.mark.parametrize(("auth", "pagination"), VARIANTS)
def test_every_variant_writes_the_same_files_and_leaves_no_marker(
    auth: str, pagination: str, tmp_path: Path
) -> None:
    directory = generated(tmp_path, "acme", auth, pagination)
    package = directory / "src" / "catalog_sync_acme"
    wanted = {
        directory / "pyproject.toml",
        directory / "README.md",
        directory / ".gitignore",
        directory / "catalog-sync.example.toml",
        package / "__init__.py",
        package / "py.typed",
        package / "settings.py",
        package / "api.py",
        package / "credentials.py",
        package / "source.py",
        package / "mappings" / "acme_item.yaml",
        package / "schemas" / "acme_item.yaml",
        directory / "tests" / "__init__.py",
        directory / "tests" / "fake_service.py",
        directory / "tests" / "test_credentials.py",
        directory / "tests" / "test_connector.py",
    }
    assert {p for p in directory.rglob("*") if p.is_file()} == wanted
    for path in wanted:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\{\{[a-z_]+\}\}", text), f"{path.name} still has a marker"


@pytest.mark.parametrize(("auth", "pagination"), VARIANTS)
def test_every_generated_python_file_parses_and_the_toml_and_yaml_load(
    auth: str, pagination: str, tmp_path: Path
) -> None:
    import yaml

    directory = generated(tmp_path, "acme", auth, pagination)
    for path in directory.rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for path in directory.glob("*.toml"):
        tomllib.loads(path.read_text(encoding="utf-8"))
    for path in directory.rglob("*.yaml"):
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)


@pytest.mark.parametrize(("auth", "pagination"), VARIANTS)
def test_the_variant_chosen_is_the_variant_written(
    auth: str, pagination: str, tmp_path: Path
) -> None:
    directory = generated(tmp_path, "acme", auth, pagination)
    api = (directory / "src" / "catalog_sync_acme" / "api.py").read_text(encoding="utf-8")
    source = (directory / "src" / "catalog_sync_acme" / "source.py").read_text(encoding="utf-8")
    walker = {
        "page": "walk_number_pages",
        "token": "walk_token_pages",
        "offset": "walk_offset_pages",
    }
    for style, name in walker.items():
        assert (name in source) is (style == pagination), (style, name)
    page_type = {"page": "RowPage", "token": "TokenPage", "offset": "OffsetPage"}
    for style, name in page_type.items():
        assert (name in api) is (style == pagination), (style, name)
    credentials = (directory / "src" / "catalog_sync_acme" / "credentials.py").read_text("utf-8")
    uses_form_login = auth in ("form", "cookie")
    assert ("FormLogin" in credentials) is uses_form_login
    assert ("BrowserSite" in credentials) is (auth == "bearer")
    assert ("API_KEY_HEADER" in credentials) is (auth == "api_key")


def test_what_has_to_change_is_marked_and_the_marks_are_where_the_work_is(tmp_path: Path) -> None:
    directory = generated(tmp_path, "acme", "bearer", "page")
    package = directory / "src" / "catalog_sync_acme"
    for name in ("api.py", "settings.py", "source.py", "credentials.py"):
        assert "TODO(connector)" in (package / name).read_text(encoding="utf-8"), name
    assert "TODO(connector)" in (directory / "tests" / "fake_service.py").read_text("utf-8")


def test_names_flow_into_the_package_the_entry_points_and_the_environment(tmp_path: Path) -> None:
    directory = generated(tmp_path, "acme_crm", "form", "token")
    project = tomllib.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["name"] == "catalog-sync-acme-crm"
    assert project["project"]["entry-points"]["catalog_sync.sources"] == {
        "acme_crm.items": "catalog_sync_acme_crm.source:AcmeCrmItemsSource.from_options"
    }
    assert project["project"]["entry-points"]["catalog_sync.credentials"] == {
        "acme_crm": "catalog_sync_acme_crm.credentials:acme_crm_credential_kind"
    }
    assert project["project"]["entry-points"]["catalog_sync.schemas"] == {
        "acme_crm_item": "catalog_sync_acme_crm:item_schema"
    }
    credentials = (directory / "src" / "catalog_sync_acme_crm" / "credentials.py").read_text(
        "utf-8"
    )
    assert 'USER_ENV = "ACME_CRM_USER"' in credentials


def test_the_example_config_carries_a_compliance_block_made_of_placeholders(
    tmp_path: Path,
) -> None:
    directory = generated(tmp_path, "acme", "bearer", "page")
    config = tomllib.loads((directory / "catalog-sync.example.toml").read_text(encoding="utf-8"))
    block = config["sources"]["acme-items"]["compliance"]
    assert set(block) == {
        "data_owner",
        "authorization",
        "authorization_ref",
        "tos_url",
        "tos_reviewed_on",
        "max_requests_per_minute",
        "retention_days",
    }
    for key in ("data_owner", "authorization", "authorization_ref", "tos_url", "tos_reviewed_on"):
        assert str(block[key]).startswith("<"), f"{key} must be a placeholder"
    assert block["max_requests_per_minute"] <= 60, "the pace starts out conservative"


def test_the_cjk_check_below_sees_cjk_text_and_lets_ascii_and_latin_through() -> None:
    assert CJK.search("plain text, caf" + chr(0xE9)) is None
    assert CJK.search("login " + chr(0x767B) + chr(0x5F55))
    assert CJK.search("full" + chr(0xFF0C) + "width")


def test_no_generated_file_holds_what_a_public_repository_may_not(tmp_path: Path) -> None:
    directory = generated(tmp_path, "acme", "form", "offset")
    local = re.compile(r"(?:^|[^A-Za-z0-9_.~-])/(?:Users|home)/[A-Za-z0-9._-]+/")
    for path in directory.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert local.search(text) is None, path.name
            assert not CJK.search(text), f"{path.name} holds CJK text"


# --- the command ----------------------------------------------------------------------------------


def test_the_command_writes_the_package_and_says_what_to_do_next(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "out" / "acme"
    code = cli.main(["new-connector", "acme", "--dir", str(target), "--auth", "cookie"])
    assert code == 0
    printed = capsys.readouterr().out
    assert "catalog-sync-acme (cookie credential, page pagination): 16 files" in printed
    assert "TODO(connector)" in printed and "connector-guide" in printed
    assert (target / "src" / "catalog_sync_acme" / "source.py").is_file()


def test_the_command_defaults_to_a_directory_in_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["new-connector", "acme_crm"]) == 0
    assert (tmp_path / "catalog-sync-acme-crm" / "pyproject.toml").is_file()


@pytest.mark.parametrize("name", ["Acme", "mock", "replay"])
def test_the_command_exits_1_and_writes_nothing_for_a_bad_or_taken_name(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["new-connector", name, "--dir", str(tmp_path / "out")]) == 1
    assert capsys.readouterr().err.startswith("error: ")
    assert not (tmp_path / "out").exists()


def test_the_parser_offers_exactly_the_documented_choices(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(["new-connector", "acme", "--auth", "oauth"])
    assert caught.value.code == 2
    assert "invalid choice: 'oauth'" in capsys.readouterr().err
    parsed = cli.build_parser().parse_args(["new-connector", "acme"])
    assert (parsed.auth, parsed.pagination) == ("bearer", "page")


# --- where catalog-sync comes from ----------------------------------------------------------------


def fake_tree(root: Path, *, project_name: str | None, layout: str = "src") -> Path:
    """A tree that has (or imitates) the layout of a checkout; returns the package directory."""
    package = root / layout / "catalog_sync"
    package.mkdir(parents=True)
    if project_name is not None:
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "{project_name}"\n', encoding="utf-8"
        )
    return package


def test_a_source_checkout_is_recognised_by_its_layout_and_project_name(tmp_path: Path) -> None:
    package = fake_tree(tmp_path / "clone", project_name="catalog-sync")
    assert find_checkout(package) == (tmp_path / "clone").resolve()


@pytest.mark.parametrize(
    ("project_name", "layout"),
    [
        (None, "src"),  # site-packages has no pyproject.toml next to it
        ("some-other-project", "src"),  # someone else's src/catalog_sync
        ("catalog-sync", "lib"),  # not the src layout
    ],
)
def test_an_installed_copy_is_not_a_checkout(
    project_name: str | None, layout: str, tmp_path: Path
) -> None:
    assert find_checkout(fake_tree(tmp_path, project_name=project_name, layout=layout)) is None


def test_this_very_repository_is_recognised_as_the_checkout() -> None:
    assert find_checkout(Path(cli.__file__).parent) == REPO_ROOT.resolve()


def test_auto_follows_where_the_tool_runs_from_and_the_explicit_choices_override(
    tmp_path: Path,
) -> None:
    clone = fake_tree(tmp_path / "clone", project_name="catalog-sync")
    wheel = fake_tree(tmp_path / "wheel", project_name=None)
    assert resolve_core("auto", package_dir=clone).checkout == (tmp_path / "clone").resolve()
    assert resolve_core("auto", package_dir=wheel).checkout is None
    assert resolve_core("path", package_dir=clone).checkout is not None
    assert resolve_core("pypi", package_dir=clone).checkout is None


def test_asking_for_a_path_without_a_checkout_is_an_error_that_says_what_to_do(
    tmp_path: Path,
) -> None:
    wheel = fake_tree(tmp_path, project_name=None)
    with pytest.raises(ConfigError, match=r"running from a source checkout.*--catalog-sync-source"):
        resolve_core("path", package_dir=wheel)
    with pytest.raises(ConfigError, match="must be one of auto, pypi, path"):
        resolve_core("conda", package_dir=wheel)


def uv_sources(directory: Path) -> dict[str, object]:
    project = tomllib.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))
    return project.get("tool", {}).get("uv", {}).get("sources", {})  # type: ignore[no-any-return]


def test_from_a_checkout_the_package_points_uv_at_it_with_a_relative_path(
    tmp_path: Path,
) -> None:
    """The bug this guards: a fresh directory's `uv sync` could not find catalog-sync, which is
    not on the package index. The path must be right from where the package really is."""
    checkout = REPO_ROOT.resolve()
    directory = tmp_path / "deeper" / "acme"
    generate(spec_for(), directory, CoreDependency(checkout))
    entry = uv_sources(directory)["catalog-sync"]
    assert isinstance(entry, dict) and entry["editable"] is True
    relative = Path(str(entry["path"]))
    assert not relative.is_absolute(), "a path relative to the package, so the tree can move"
    target = (directory / relative).resolve()
    assert target == checkout
    assert (target / "pyproject.toml").is_file()


def test_from_an_installed_wheel_the_package_depends_on_the_index_alone(tmp_path: Path) -> None:
    directory = tmp_path / "acme"
    generate(spec_for(), directory, CoreDependency())
    assert uv_sources(directory) == {}
    text = (directory / "pyproject.toml").read_text(encoding="utf-8")
    assert "tool.uv" not in text and re.search(r"\n\n\n", text) is None, "no gap left behind"
    assert "package index" in (directory / "README.md").read_text(encoding="utf-8")


def test_the_default_for_the_library_call_is_the_index(tmp_path: Path) -> None:
    generate(spec_for(), tmp_path / "acme")
    assert uv_sources(tmp_path / "acme") == {}


def test_the_command_says_where_catalog_sync_comes_from(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["new-connector", "acme", "--dir", str(tmp_path / "a")]) == 0
    from_checkout = capsys.readouterr().out
    assert "taken from the checkout this command ran from" in from_checkout
    assert "catalog-sync" in uv_sources(tmp_path / "a")

    code = cli.main(
        ["new-connector", "acme", "--dir", str(tmp_path / "b"), "--catalog-sync-source", "pypi"]
    )
    assert code == 0
    from_index = capsys.readouterr().out
    assert "taken from the package index" in from_index
    assert uv_sources(tmp_path / "b") == {}


def test_the_command_refuses_a_path_source_when_it_is_not_in_a_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("catalog_sync.scaffold.find_checkout", lambda _package_dir: None)
    code = cli.main(
        ["new-connector", "acme", "--dir", str(tmp_path / "x"), "--catalog-sync-source", "path"]
    )
    assert code == 1 and "source checkout" in capsys.readouterr().err
    assert not (tmp_path / "x").exists()


# --- a generated package, run for real ------------------------------------------------------------


@pytest.mark.parametrize(("auth", "pagination"), VARIANTS)
def test_a_generated_package_is_clean_and_green_the_moment_it_is_written(
    auth: str, pagination: str, tmp_path: Path
) -> None:
    directory = generated(tmp_path, "acme", auth, pagination)
    checks = {
        "ruff check": ["ruff", "check", "."],
        "ruff format": ["ruff", "format", "--check", "."],
        "mypy": ["mypy", "--strict", "--cache-dir", str(tmp_path / "mypy"), "src"],
        "pytest": ["pytest", "-q", "-p", "no:cacheprovider"],
    }
    # The four checks do not depend on each other, and 12 packages are generated: run them side by
    # side so the whole matrix costs about what its slowest check does per package.
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        futures = {
            label: pool.submit(run, arguments, directory) for label, arguments in checks.items()
        }
    failures = {
        label: shown(future.result())
        for label, future in futures.items()
        if future.result().returncode != 0
    }
    assert failures == {}


@pytest.mark.parametrize(("auth", "pagination"), VARIANTS)
def test_every_variant_stays_within_the_formatter_and_linter_for_the_longest_name(
    auth: str, pagination: str, tmp_path: Path
) -> None:
    """Wrapping depends on how long the names are; the templates are written so that it does not.
    A name at the limit, made of two words (the longest title, class and environment prefix), is
    the case that would show a line that only fits a short name."""
    directory = generated(tmp_path, LONGEST_NAME, auth, pagination)
    assert len(LONGEST_NAME) == 20
    for arguments in (["ruff", "check", "."], ["ruff", "format", "--check", "."]):
        result = run(arguments, directory)
        assert result.returncode == 0, shown(result)


def test_the_longest_name_package_also_passes_its_own_tests(tmp_path: Path) -> None:
    directory = generated(tmp_path, LONGEST_NAME, "bearer", "token")
    result = run(["pytest", "-q", "-p", "no:cacheprovider"], directory)
    assert result.returncode == 0, shown(result)


# --- a generated package, registered in this process ----------------------------------------------


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Generate a connector and make it discoverable here, as `pip install` would: its source on
    sys.path and a `.dist-info` carrying its entry points. Modules it imports are forgotten
    again afterwards."""
    name = "zetacorp"
    directory = tmp_path / "pkg"
    generate(spec_for(name, "bearer", "token"), directory)
    project = tomllib.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))
    dist_info = tmp_path / "site" / "catalog_sync_zetacorp-0.1.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: catalog-sync-zetacorp\nVersion: 0.1.0\n", encoding="utf-8"
    )
    lines = []
    for group, entries in project["project"]["entry-points"].items():
        lines.append(f"[{group}]")
        lines += [f"{entry} = {target}" for entry, target in entries.items()]
    (dist_info / "entry_points.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    monkeypatch.syspath_prepend(str(directory / "src"))
    yield directory
    for module in [m for m in sys.modules if m.startswith("catalog_sync_zetacorp")]:
        del sys.modules[module]


def test_the_registry_finds_the_generated_connector_by_its_entry_points(installed: Path) -> None:
    from catalog_sync_zetacorp.credentials import zetacorp_credential_kind
    from catalog_sync_zetacorp.source import ZetacorpItemsSource

    assert "zetacorp.items" in registry.available(registry.SOURCES_GROUP)
    assert registry.source_factory("zetacorp.items") == ZetacorpItemsSource.from_options
    assert registry.credential_kind_factory("zetacorp") == zetacorp_credential_kind
    schema = registry.schema_path("zetacorp_item")
    assert schema.name == "zetacorp_item.yaml" and schema.is_file()
    assert not registry.is_local_source("zetacorp.items"), "a networked connector is gated"


def test_a_second_connector_of_the_same_name_is_now_refused(installed: Path) -> None:
    with pytest.raises(ConfigError, match=r"already installed.*zetacorp\.items"):
        check_name("zetacorp")


def test_the_compliance_gate_refuses_the_generated_example_until_it_is_filled_in(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "catalog-sync.toml"
    text = (installed / "catalog-sync.example.toml").read_text(encoding="utf-8")
    config.write_text(text, encoding="utf-8")
    workspace = tmp_path / "ws"
    arguments = ["--config", str(config), "--workspace", str(workspace)]

    code = cli.main([*arguments, "inspect", "zetacorp-items"])
    assert code == 4, "the placeholders are not a compliance declaration"
    assert "compliance" in capsys.readouterr().err
    assert not workspace.exists() or not any((workspace / "credentials").glob("*"))


def test_once_the_placeholders_are_real_the_gate_lets_the_command_go_on(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (installed / "catalog-sync.example.toml").read_text(encoding="utf-8")
    filled = {
        "<the organisation whose data this is>": "Example Co",
        "<own_account | customer_authorized | contract | public_data>": "own_account",
        "<where the grant is recorded: a ticket, a wiki page, a contract number>": "tickets/EX-1",
        "<the terms of service you reviewed>": "https://app.zetacorp.example/terms",
        "<YYYY-MM-DD, the day you read them>": "2026-10-26",
    }
    for placeholder, value in filled.items():
        assert placeholder in text, placeholder
        text = text.replace(placeholder, value)
    config = tmp_path / "catalog-sync.toml"
    config.write_text(text, encoding="utf-8")
    arguments = ["--config", str(config), "--workspace", str(tmp_path / "ws")]

    code = cli.main([*arguments, "inspect", "zetacorp-items"])
    assert code == 2, "the gate is passed; what is missing now is a login"
    assert "no credential stored for 'zetacorp'" in capsys.readouterr().err
