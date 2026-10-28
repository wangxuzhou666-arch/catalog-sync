"""The User-Agent policy as a test: a browser string needs a documented reason.

The SDK default names the tool; a connector that sends a browser's string instead must say so, and
why, in the document that states the policy. These helpers find such connectors in source and read
the policy section of a document, so a test can tie the two together::

    for package in connectors_sending_a_browser_string(Path("src")):
        assert package in policy_section(Path("README.md").read_text("utf-8"))
"""

from __future__ import annotations

from pathlib import Path

BROWSER_MARKER = "Mozilla/"
POLICY_HEADING = "### User-Agent policy"


def connectors_sending_a_browser_string(root: Path) -> list[str]:
    """The names of the directories under `root` with a ``.py`` file that holds a browser string.

    A connector keeps its headers in one directory, so the directory name identifies it.
    """
    return sorted(
        {
            path.parent.name
            for path in root.rglob("*.py")
            if BROWSER_MARKER in path.read_text(encoding="utf-8")
        }
    )


def policy_section(document: str, heading: str = POLICY_HEADING) -> str:
    """The text under `heading`, up to the next ``###`` heading; empty when there is none."""
    _, _, rest = document.partition(heading)
    section, _, _ = rest.partition("\n### ")
    return section
