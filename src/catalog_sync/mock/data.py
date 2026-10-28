"""The Mock SaaS's contents: a small, seeded and obviously fake catalogue.

Everything is invented. Company names are "Demo ..." or "Sample ...", e-mail addresses live under
example.com, countries are made up. The same seed gives the same catalogue on every machine, which
is what lets tests use this module as the oracle for what a pull should have stored.

The data is shaped to reproduce the trouble real services cause:

* materials sit in a folder tree, some at its root;
* a material's listing row has a picture and a price of 0; its detail has the real price, the
  weight, the composition and a second picture, and an empty ``thumbs``: neither is a superset of
  the other;
* about one material in eight has a weight of 0 ("not filled in") and one in eight a fibre code
  that no code table lists.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

ROOT_FOLDER_ID = "0"
DEFAULT_MATERIALS = 24
DEFAULT_SUPPLIERS = 9
CURRENCY = "USD"

# Fibre codes the mock's composition strings use; plugins/mock/tables lists their meanings.
FIBRE_CODES = ("CO", "PES", "LI", "WO", "EA", "PA", "VI")
UNKNOWN_FIBRE_CODE = "ZZ"
_WIDTHS = ("150cm", "148", '58"', '57/58"')
_CENT = Decimal("0.01")
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
_SUPPLIER_NAMES = (
    "Demo Mill Co.",
    "Sample Textiles Ltd.",
    "Placeholder Weavers",
    "Example Knits Inc.",
    "Test Dye Works",
    "Fictional Fibres GmbH",
    "Mock Spinners",
    "Imaginary Looms",
    "Synthetic Yarn Co.",
)
_COUNTRIES = ("Demoland", "Exampleland", "Testonia", "Mockistan")
_CERTIFICATIONS = ("Demo Cert A", "Demo Cert B", "Sample Standard 100", "Test Label Gold")
_FAMILIES = {"0": "Plain", "f-woven": "Woven", "f-twill": "Twill", "f-denim": "Denim"}


@dataclass(frozen=True)
class MockFolder:
    id: str
    name: str
    parent_id: str


@dataclass(frozen=True)
class MockMaterial:
    id: str
    folder_id: str
    code: str
    name: str
    price: Decimal
    weight_gsm: int  # 0 means "not filled in"
    width: str  # as the service writes it: centimetres, inches, or an inch range
    composition: str  # "60%CO/40%PES"
    supplier_id: str
    supplier_name: str
    updated_at: str  # ISO 8601, the service's change marker


@dataclass(frozen=True)
class MockSupplier:
    id: str
    name: str
    country: str
    rating: float
    lead_time_days: int
    certifications: tuple[str, ...]
    contact_email: str
    address: dict[str, str]
    listed_at: str


@dataclass(frozen=True)
class MockDataset:
    seed: int
    folders: tuple[MockFolder, ...]
    materials: tuple[MockMaterial, ...]
    suppliers: tuple[MockSupplier, ...]

    def folder(self, folder_id: str) -> MockFolder | None:
        return next((f for f in self.folders if f.id == folder_id), None)

    def has_folder(self, folder_id: str) -> bool:
        return folder_id == ROOT_FOLDER_ID or self.folder(folder_id) is not None

    def subfolders(self, folder_id: str) -> tuple[MockFolder, ...]:
        return tuple(f for f in self.folders if f.parent_id == folder_id)

    def materials_in(self, folder_id: str) -> tuple[MockMaterial, ...]:
        return tuple(m for m in self.materials if m.folder_id == folder_id)

    def material(self, material_id: str) -> MockMaterial | None:
        return next((m for m in self.materials if m.id == material_id), None)

    def folder_path(self, folder_id: str) -> str:
        """Names from the first level down, joined by "/"; "" for the root."""
        names: list[str] = []
        current = self.folder(folder_id)
        while current is not None:
            names.append(current.name)
            current = self.folder(current.parent_id)
        return "/".join(reversed(names))


FOLDERS = (
    MockFolder("f-woven", "Demo Woven", ROOT_FOLDER_ID),
    MockFolder("f-knit", "Demo Knit", ROOT_FOLDER_ID),
    MockFolder("f-twill", "Twills", "f-woven"),
    MockFolder("f-denim", "Denim", "f-woven"),
    MockFolder("f-jersey", "Jersey", "f-knit"),
)
_FOLDER_CYCLE = (ROOT_FOLDER_ID, "f-woven", "f-twill", "f-denim", "f-knit", "f-jersey")


def generate(
    seed: int = 1, *, materials: int = DEFAULT_MATERIALS, suppliers: int = DEFAULT_SUPPLIERS
) -> MockDataset:
    rng = random.Random(seed)  # noqa: S311 — fake catalogue data, nothing to do with security
    supplier_rows = tuple(_supplier(rng, index) for index in range(suppliers))
    material_rows = tuple(_material(rng, index, suppliers) for index in range(materials))
    return MockDataset(seed, FOLDERS, material_rows, supplier_rows)


def _supplier_name(index: int) -> str:
    base = _SUPPLIER_NAMES[index % len(_SUPPLIER_NAMES)]
    return base if index < len(_SUPPLIER_NAMES) else f"{base} {index // len(_SUPPLIER_NAMES) + 1}"


def _supplier(rng: random.Random, index: int) -> MockSupplier:
    number = index + 1
    certifications = tuple(rng.sample(_CERTIFICATIONS, rng.randint(0, 3)))
    return MockSupplier(
        id=f"sup-{number:03d}",
        name=_supplier_name(index),
        country=_COUNTRIES[rng.randrange(len(_COUNTRIES))],
        rating=round(rng.uniform(3.0, 5.0), 1),
        lead_time_days=rng.randint(5, 60),
        certifications=certifications,
        contact_email=f"supplier-{number:03d}@example.com",
        address={"line1": f"{number} Demo Street", "city": "Demo City", "postcode": "00000"},
        listed_at=(_EPOCH + timedelta(days=index)).isoformat().replace("+00:00", "Z"),
    )


def _material(rng: random.Random, index: int, supplier_count: int) -> MockMaterial:
    folder_id = _FOLDER_CYCLE[index % len(_FOLDER_CYCLE)]
    family = _FAMILIES.get(folder_id, "Knit")
    number = index + 1
    weight = 0 if index % 8 == 7 else rng.randrange(90, 400, 10)
    supplier_number = rng.randint(1, max(supplier_count, 1))
    return MockMaterial(
        id=f"mat-{number:04d}",
        folder_id=folder_id,
        code=f"MOCK-{number:04d}",
        name=f"Demo {family} {number:02d}",
        price=(Decimal(rng.randint(300, 4500)) / 100).quantize(_CENT),
        weight_gsm=weight,
        width=_WIDTHS[rng.randrange(len(_WIDTHS))],
        composition=_composition(rng, is_unknown_code=index % 8 == 5),
        supplier_id=f"sup-{supplier_number:03d}",
        supplier_name=_supplier_name(supplier_number - 1),
        updated_at=(_EPOCH + timedelta(minutes=number)).isoformat().replace("+00:00", "Z"),
    )


def _composition(rng: random.Random, *, is_unknown_code: bool) -> str:
    first, second = rng.sample(FIBRE_CODES, 2)
    share = rng.choice((50, 60, 70, 80, 95))
    if is_unknown_code:
        second = UNKNOWN_FIBRE_CODE
    return f"{share}%{first}/{100 - share}%{second}"
