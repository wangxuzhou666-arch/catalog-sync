"""Regenerate the committed replay snapshot in examples/synthetic from the Mock SaaS world.

The snapshot is what a pull of the built-in Mock SaaS leaves in the cache: the listing rows, the
detail records and the picture references are the ones the real Mock materials source reads from
the real service (run in process, seed 1, the first ITEM_COUNT materials). Only the picture bytes
differ. The service draws them with Pillow, whose output can change between versions, so they are
replaced by tiny stdlib-made PNGs: the committed copy must be reproducible byte for byte.

    uv run python tools/make_synthetic.py

The output is deterministic (fixed run id and timestamp), so a test regenerates it into a temp
directory and compares it with the committed copy.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from catalog_sync.mock.service import MockOptions
from catalog_sync.plugins.mock.settings import MockSettings
from catalog_sync.plugins.mock.source import MockMaterialsSource
from catalog_sync.puller import Puller
from catalog_sync.snapshot import RunSnapshot
from catalog_sync.state import StateStore
from tests.fixtures.mock_saas import World
from tests.fixtures.pictures import png_bytes

RUN_ID = "synthetic"
# Two of the 16 have no weight ("0 gsm") and two a fibre code no table lists: enough to show the
# traps, few enough to keep the committed files small.
ITEM_COUNT = 16
CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def build(runs_root: Path) -> Path:
    """Write the snapshot to ``runs_root / RUN_ID`` (replacing it) and return that directory."""
    target = runs_root / RUN_ID
    if target.exists():
        shutil.rmtree(target)
    with tempfile.TemporaryDirectory() as scratch:
        pulled = _pull_the_mock(Path(scratch))
        snapshot = RunSnapshot.create(
            runs_root, run_id=RUN_ID, source=pulled.source, created_at=CREATED_AT
        )
        source_ids = pulled.item_ids()
        # Same order the puller writes in: all listings, then all details, then all assets.
        for source_id in source_ids:
            snapshot.put_item(pulled.get_item(source_id))
        for source_id in source_ids:
            detail = pulled.get_detail(source_id)
            if detail is None:
                raise RuntimeError(f"the Mock serves a detail for every material: {source_id}")
            snapshot.put_detail(source_id, detail)
        for position, source_id in enumerate(source_ids):
            refs = pulled.get_asset_refs(source_id)
            if refs is None:
                raise RuntimeError(f"the pull recorded the pictures of {source_id}")
            snapshot.put_asset_refs(source_id, refs)
            for ref in refs:
                snapshot.put_asset(ref, png_bytes(position, ref.role))
    return target


def _pull_the_mock(scratch: Path) -> RunSnapshot:
    world = World(MockOptions(materials=ITEM_COUNT))
    settings = MockSettings.from_options(
        {"credential": world.handle(), "request_gap_s": 0}, source="mock.materials"
    )
    source = MockMaterialsSource(settings, seams=world.seams)
    runs_root = scratch / "runs"
    with StateStore(scratch / "state.sqlite") as store:
        report = Puller(source, store, runs_root).pull()
    return RunSnapshot.open(runs_root / report.run_id)


def main() -> int:
    target = build(REPO_ROOT / "examples")
    print(f"wrote {target.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
