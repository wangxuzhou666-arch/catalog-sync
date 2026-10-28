"""Throw-away wheels and sdists for the tests of tools/check_dist.py and of the harness checks.

The good ones pass every rule; a test copies them and breaks one thing.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from collections.abc import Mapping
from pathlib import Path

CJK = chr(0x4F60) + chr(0x597D)  # two Chinese characters, built so this file holds none

PYPROJECT = """\
[project]
name = "catalog-sync"
version = "1.0"
license = "MIT"

[project.scripts]
demo = "catalog_sync.cli:main"

[project.entry-points."catalog_sync.sources"]
alpha = "catalog_sync.plugins.alpha.source:Alpha.from_options"
"alpha.two" = "catalog_sync.plugins.alpha:two"
"""
ENTRY_POINTS = """\
[console_scripts]
demo = catalog_sync.cli:main

[catalog_sync.sources]
alpha = catalog_sync.plugins.alpha.source:Alpha.from_options
alpha.two = catalog_sync.plugins.alpha:two
"""

GOOD_WHEEL: dict[str, str | bytes] = {
    "catalog_sync/__init__.py": '__version__ = "1.0"\n',
    "catalog_sync/cli.py": "def main() -> None: ...\n",
    "catalog_sync/credentials/login.py": "KIND = 'x'\n",
    "catalog_sync/plugins/__init__.py": "",
    "catalog_sync/plugins/alpha/__init__.py": "",
    "catalog_sync/plugins/alpha/source.py": "class Alpha: ...\n",
    "catalog_sync-1.0.dist-info/METADATA": (
        "Metadata-Version: 2.5\nName: catalog-sync\nVersion: 1.0\nLicense-Expression: MIT\n"
    ),
    "catalog_sync-1.0.dist-info/entry_points.txt": ENTRY_POINTS,
    "catalog_sync-1.0.dist-info/licenses/LICENSE": "MIT License\n",
}
GOOD_SDIST: dict[str, str | bytes] = {
    "LICENSE": "MIT License\n",
    "README.md": "# demo\n",
    "README.zh-CN.md": f"# {CJK}\n",
    "pyproject.toml": PYPROJECT,
    "src/catalog_sync/__init__.py": '__version__ = "1.0"\n',
    "src/catalog_sync/credentials/login.py": "KIND = 'x'\n",
    "src/catalog_sync/plugins/alpha/source.py": "class Alpha: ...\n",
    "deploy/.env.example": "# secrets\nDB_PASSWORD=\nTOKEN=\n",
    "docs/runs.md": "how runs resume\n",
}


def write_wheel(path: Path, files: Mapping[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def write_sdist(path: Path, files: Mapping[str, str | bytes]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            raw = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(f"catalog_sync-1.0/{name}")
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    return path
