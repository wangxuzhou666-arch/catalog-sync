"""``catalog-sync new-connector``: write a connector package that already passes its own tests.

The generated package is a plain Python project (its own ``pyproject.toml`` and entry points) that
reads a made-up service called after the connector. Everything the developer has to change is
marked ``TODO(connector)``: the endpoints, the field paths, the login. Everything around those
works from the first minute: the settings, the SDK wiring, a fake service that answers the way the
connector expects, tests that run the connector through pull and normalize against it, and a
compliance block whose placeholders the compliance gate refuses until they are replaced.

Files are plain text templates with ``{{name}}`` markers (no template engine, no new dependency).
A marker that has no value is an error in this module and never a surprise in the generated code;
a test checks the other direction too (no fragment goes unused). What differs between the
variants (which pagination, which kind of credential) lives in whole-file or fragment templates
chosen by name, so a variant can be read top to bottom.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import catalog_sync
from catalog_sync import __version__, registry
from catalog_sync.errors import ConfigError

AUTH_KINDS: Final = ("bearer", "cookie", "api_key", "form")
PAGINATIONS: Final = ("page", "token", "offset")
CORE_SOURCES: Final = ("auto", "pypi", "path")
DEFAULT_AUTH: Final = "bearer"
DEFAULT_PAGINATION: Final = "page"
DEFAULT_CORE_SOURCE: Final = "auto"

_TEMPLATES = Path(__file__).parent / "templates"
_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_MAX_NAME_LENGTH = 20  # keeps every generated line short enough to need no wrapping
_MARKER = re.compile(r"\{\{([a-z_]+)\}\}")
# A marker alone on its line, and the blank line after it if there is one.
_WHOLE_LINE = re.compile(r"(?m)^\{\{([a-z_]+)\}\}\n(\n(?=.))?")


@dataclass(frozen=True)
class CoreDependency:
    """Where a generated package gets catalog-sync from: the package index, or a source checkout.

    A checkout matters while catalog-sync is not on the index (or the connector is meant to track
    this very checkout): without a `[tool.uv.sources]` entry pointing at it, `uv sync` in the new
    directory fails with "catalog-sync was not found in the package registry".
    """

    checkout: Path | None = None  # absolute; None means the package index

    def uv_sources(self, directory: Path) -> str:
        """The `[tool.uv.sources]` table for a package written into `directory`, or ''."""
        if self.checkout is None:
            return ""
        relative = Path(os.path.relpath(self.checkout, directory.resolve())).as_posix()
        return (
            "# catalog-sync comes from the checkout this package was generated from, so that\n"
            "# `uv sync` works while it is not on a package index. Delete this table to use it.\n"
            "[tool.uv.sources]\n"
            f"catalog-sync = {{ path = {json.dumps(relative)}, editable = true }}"
        )

    def describe(self) -> str:
        if self.checkout is None:
            return (
                f"catalog-sync is taken from the package index (catalog-sync>={__version__}). "
                "If your index does not carry it yet, generate again from a source checkout "
                "(it is then used through [tool.uv.sources]), or add that table by hand."
            )
        return (
            "catalog-sync is taken from the checkout this command ran from, through "
            "[tool.uv.sources] in pyproject.toml (a path relative to this directory). Delete that "
            "table to use the published package instead."
        )


def find_checkout(package_dir: Path) -> Path | None:
    """The source checkout the `catalog_sync` package at `package_dir` lives in, if it does.

    A checkout (a plain clone, or an editable install of one) has the package under `<root>/src`
    next to the project's own pyproject.toml. A wheel puts it in site-packages, which has neither.
    """
    src = package_dir.resolve().parent
    root = src.parent
    pyproject = root / "pyproject.toml"
    if src.name != "src" or not pyproject.is_file():
        return None
    project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
    return root if project.get("name") == "catalog-sync" else None


def resolve_core(choice: str, *, package_dir: Path | None = None) -> CoreDependency:
    """Turn `--catalog-sync-source` into a dependency. `auto` follows where this tool runs from."""
    checkout = find_checkout(package_dir or Path(catalog_sync.__file__).parent)
    if choice == "pypi":
        return CoreDependency()
    if choice == "path" and checkout is None:
        raise ConfigError(
            "--catalog-sync-source path needs catalog-sync to be running from a source checkout, "
            "and this one is an installed package. Use --catalog-sync-source pypi, or run the "
            "command from a checkout"
        )
    if choice not in CORE_SOURCES:
        raise ConfigError(
            f"--catalog-sync-source must be one of {', '.join(CORE_SOURCES)}, got {choice!r}"
        )
    return CoreDependency(checkout)


@dataclass(frozen=True)
class ConnectorSpec:
    name: str
    auth: str
    pagination: str

    @property
    def package(self) -> str:
        return f"catalog_sync_{self.name}"

    @property
    def distribution(self) -> str:
        return f"catalog-sync-{self.name.replace('_', '-')}"

    @property
    def source_name(self) -> str:
        return f"{self.name}.items"

    @property
    def entity(self) -> str:
        return f"{self.name}_item"

    @property
    def class_prefix(self) -> str:
        return "".join(part.capitalize() for part in self.name.split("_"))

    @property
    def title(self) -> str:
        return " ".join(part.capitalize() for part in self.name.split("_"))

    @property
    def host(self) -> str:
        """A host nobody owns (the reserved ``.example`` top-level domain)."""
        return f"app.{self.name.replace('_', '-')}.example"


def check_name(name: str) -> None:
    """Refuse a name that is not a lowercase identifier, or that an installed connector uses.

    The registry is asked, not a list kept here, so a connector installed from another package
    counts too: two connectors answering to one name would make `registry` refuse both.
    """
    if not _NAME.fullmatch(name) or len(name) > _MAX_NAME_LENGTH:
        raise ConfigError(
            f"invalid connector name {name!r}: use lowercase letters, digits and single "
            f"underscores, starting with a letter, at most {_MAX_NAME_LENGTH} characters "
            "(for example `acme` or `acme_crm`)"
        )
    clashes = _clashes(ConnectorSpec(name, DEFAULT_AUTH, DEFAULT_PAGINATION))
    if clashes:
        raise ConfigError(
            f"a connector named {name!r} is already installed ({', '.join(clashes)}); "
            "choose another name, or uninstall that one"
        )


def _clashes(spec: ConnectorSpec) -> list[str]:
    sources = registry.available(registry.SOURCES_GROUP)
    found = [f"source plugin {source!r}" for source in sources if source.split(".")[0] == spec.name]
    if spec.name in registry.available(registry.CREDENTIALS_GROUP):
        found.append(f"credential kind {spec.name!r}")
    if spec.entity in registry.available(registry.SCHEMAS_GROUP):
        found.append(f"entity schema {spec.entity!r}")
    return found


def default_directory(spec: ConnectorSpec, *, cwd: Path) -> Path:
    return cwd / spec.distribution


def generate(
    spec: ConnectorSpec, directory: Path, core: CoreDependency | None = None
) -> list[Path]:
    """Write the package into `directory` (new, or empty) and return the files written."""
    check_name(spec.name)
    if spec.auth not in AUTH_KINDS:
        raise ConfigError(f"--auth must be one of {', '.join(AUTH_KINDS)}, got {spec.auth!r}")
    if spec.pagination not in PAGINATIONS:
        raise ConfigError(
            f"--pagination must be one of {', '.join(PAGINATIONS)}, got {spec.pagination!r}"
        )
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise ConfigError(f"{directory} already exists and is not empty; nothing was written")
    core = core or CoreDependency()
    values = {
        **_values(spec),
        "uv_sources": core.uv_sources(directory),
        "core_note": core.describe(),
    }
    written = []
    for relative, template in _layout(spec).items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(_read(template), values), encoding="utf-8")
        written.append(target)
    return written


def render(template: str, values: Mapping[str, str]) -> str:
    """Replace every ``{{name}}``; a value may itself hold markers (a fragment does).

    A marker alone on its line takes the whole line, and when its value is empty the line goes,
    together with the blank line after it: a variant that has nothing to add there must not leave
    a gap that the formatter would then have to remove.

    Raises ValueError for a marker without a value: a template and its values are written
    together, so a mismatch is a bug here, not input to be tolerated.
    """
    text = template
    for _ in range(3):  # fragments nest one level; three passes is more than any template needs
        text = _WHOLE_LINE.sub(lambda match: _line_value(match, values), text)
        text = _MARKER.sub(lambda match: _value(match.group(1), values), text)
        if not _MARKER.search(text):
            return text
    raise ValueError("markers still unresolved after three passes (a fragment refers to itself?)")


def _line_value(match: re.Match[str], values: Mapping[str, str]) -> str:
    value = _value(match.group(1), values)
    return f"{value}\n{match.group(2) or ''}" if value else ""


def _value(key: str, values: Mapping[str, str]) -> str:
    if key not in values:
        raise ValueError(f"the template uses {{{{{key}}}}} but no value is defined for it")
    return values[key]


def _pieces(name: str) -> dict[str, str]:
    """A fragment file holds several pieces, each introduced by a ``#@ piece_name`` line."""
    pieces: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in _read(name).splitlines():
        if line.startswith("#@ "):
            current = pieces.setdefault(line.removeprefix("#@ ").strip(), [])
        elif current is not None:
            current.append(line)
        elif line.strip():
            raise ValueError(f"{name}: text before the first '#@ piece' line")
    return {key: "\n".join(lines).strip("\n") for key, lines in pieces.items()}


def _read(name: str) -> str:
    return (_TEMPLATES / name).read_text(encoding="utf-8")


def _layout(spec: ConnectorSpec) -> dict[str, str]:
    """Output path -> template, for this variant."""
    source = f"src/{spec.package}"
    return {
        "pyproject.toml": "pyproject.toml.tmpl",
        "README.md": "README.md.tmpl",
        ".gitignore": "gitignore.tmpl",
        "catalog-sync.example.toml": "catalog-sync.example.toml.tmpl",
        f"{source}/__init__.py": "package/__init__.py.tmpl",
        f"{source}/py.typed": "empty.tmpl",
        f"{source}/settings.py": "package/settings.py.tmpl",
        f"{source}/api.py": "package/api.py.tmpl",
        f"{source}/credentials.py": f"package/credentials.{spec.auth}.py.tmpl",
        f"{source}/source.py": "package/source.py.tmpl",
        f"{source}/mappings/{spec.entity}.yaml": "package/mapping.yaml.tmpl",
        f"{source}/schemas/{spec.entity}.yaml": "package/schema.yaml.tmpl",
        "tests/__init__.py": "empty.tmpl",
        "tests/fake_service.py": "tests/fake_service.py.tmpl",
        "tests/test_credentials.py": f"tests/test_credentials.{spec.auth}.py.tmpl",
        "tests/test_connector.py": "tests/test_connector.py.tmpl",
    }


def _values(spec: ConnectorSpec) -> dict[str, str]:
    values = {
        "name": spec.name,
        "env_prefix": spec.name.upper(),
        "title": spec.title,
        "class_prefix": spec.class_prefix,
        "package": spec.package,
        "distribution": spec.distribution,
        "source_name": spec.source_name,
        "entity": spec.entity,
        "host": spec.host,
        "core_version": __version__,
        "auth": spec.auth,
        "pagination": spec.pagination,
    }
    for fragment in (
        f"fragments/pagination.{spec.pagination}.tmpl",
        f"fragments/auth.{spec.auth}.tmpl",
    ):
        values.update(_pieces(fragment))
    return values
