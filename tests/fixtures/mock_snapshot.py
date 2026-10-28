"""What the committed replay snapshot (examples/synthetic) holds, for the tests that replay it.

The snapshot is the first ITEM_COUNT materials of the Mock SaaS's seeded catalogue
(tools/make_synthetic.py), so the dataset that made it is also the oracle: a test derives what a
record must be from ``DATASET`` instead of typing values out. The snapshot's pictures are 8 x 8
PNGs, not the ones the service draws, hence ``expected_record`` fixes their size.
"""

from __future__ import annotations

from typing import cast

from catalog_sync.mock.data import MockMaterial, generate
from catalog_sync.models import JsonObject
from tests.conftest import REPO_ROOT
from tests.fixtures.mock_saas import BASE_URL, expected_material

SOURCE = "mock.materials"
ITEM_COUNT = 16
MAPPING = REPO_ROOT / "src/catalog_sync/plugins/mock/mappings/materials.yaml"
PICTURE_EDGE = 8

DATASET = generate(materials=ITEM_COUNT)
MATERIALS = DATASET.materials
ZERO_WEIGHT = tuple(m for m in MATERIALS if m.weight_gsm == 0)
UNKNOWN_FIBRE = tuple(m for m in MATERIALS if "ZZ" in m.composition)
WITH_ISSUES = tuple(m for m in MATERIALS if m in ZERO_WEIGHT or m in UNKNOWN_FIBRE)
# A material with neither trap, whose every field has a plain value.
CLEAN = next(m for m in MATERIALS if m not in WITH_ISSUES and m.folder_id != "0")
SUPPLIERS = sorted({m.supplier_name for m in MATERIALS})

_SUPPLIER_RULE = "  supplier:\n    from: [detail.supplier_name]\n"


def expected_record(material: MockMaterial) -> JsonObject:
    """The fields a correct normalisation of this material has, as the snapshot stores them."""
    expected = expected_material(BASE_URL, DATASET, material)
    for picture in cast("list[JsonObject]", expected["images"]):
        picture["width"] = picture["height"] = PICTURE_EDGE
    return expected


def sensitive_supplier_mapping() -> str:
    """The Mock materials mapping with the supplier's name marked ``sensitive``."""
    text = MAPPING.read_text(encoding="utf-8")
    assert _SUPPLIER_RULE in text, "the Mock mapping changed shape: update this helper"
    return text.replace(_SUPPLIER_RULE, _SUPPLIER_RULE + "    sensitive: true\n")
