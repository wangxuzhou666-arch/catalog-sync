"""The Mock SaaS as a test fixture: a backend for the fake transport, and what a pull must yield.

Two ways to reach the same ``MockService``: through ``FakeTransport`` (no socket at all, one clock
shared with the client's sleeping so that Retry-After and token expiry cost no real time), and over
real loopback HTTP in test_mock_e2e.py. ``expected_*`` turn the seeded dataset into the rows a
correct pull-normalize-load must have produced, so the dataset is the oracle for every test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from catalog_sync.credentials.base import Credential, CredentialHandle
from catalog_sync.mock.data import CURRENCY, MockDataset, MockMaterial, MockSupplier
from catalog_sync.mock.service import MockOptions, MockService, Request
from catalog_sync.models import JsonObject
from catalog_sync.normalize.parsers import parse_width_cm
from catalog_sync.testing.fake_transport import FakeResponse, RecordedRequest
from catalog_sync.testing.oracle import picked, sorted_issues
from catalog_sync.testing.world import ServiceWorld, WorldClock

__all__ = ["BASE_URL", "World", "expected_material", "expected_supplier", "picked", "sorted_issues"]

BASE_URL = "http://127.0.0.1:8765"
HOST = "127.0.0.1:8765"
FIBRES = {
    "CO": "Cotton",
    "PES": "Polyester",
    "LI": "Linen",
    "WO": "Wool",
    "EA": "Elastane",
    "PA": "Polyamide",
    "VI": "Viscose",
}


@dataclass
class MockBackend:
    """A FakeTransport responder that hands every request to a MockService."""

    service: MockService

    def __call__(self, recorded: RecordedRequest) -> FakeResponse:
        parts = urlsplit(recorded.url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        response = self.service.handle(
            Request(recorded.method, target, recorded.headers, parts.netloc, recorded.body or b"")
        )
        headers = {"Content-Type": response.content_type, **dict(response.headers)}
        return FakeResponse(response.status, response.body, headers)


class World(ServiceWorld):
    """A Mock SaaS and a client wired together without a network."""

    def __init__(self, options: MockOptions | None = None) -> None:
        self.options = options or MockOptions()
        clock = WorldClock()
        self.service = MockService(self.options, clock=clock)
        super().__init__(MockBackend(self.service), clock=clock)

    @property
    def dataset(self) -> MockDataset:
        return self.service.dataset

    def listing_row(self, material_id: str) -> JsonObject:
        """The row the service lists for a material, found the way a client would: by paging
        through the folder the material sits in."""
        material = self.dataset.material(material_id)
        assert material is not None, material_id
        bearer = self.handle().load().headers["authorization"]
        page = 1
        while True:
            target = f"/api/v1/materials?folder_id={material.folder_id}&page={page}&per_page=50"
            reply = self.service.handle(Request("GET", target, {"Authorization": bearer}, HOST))
            data = json.loads(reply.body)["data"]
            for row in data["rows"]:
                if row["id"] == material_id:
                    return row  # type: ignore[no-any-return]
            page += 1

    def log_of(self, prefix: str) -> list[int]:
        """The statuses the service answered for paths starting with `prefix`."""
        return [e.status for e in self.service.request_log if e.path.startswith(prefix)]

    def handle(self) -> CredentialHandle:
        """A credential handle holding a token this world's service issued just now."""
        account = self.options.account
        body = json.dumps({"username": account.user, "password": account.password}).encode()
        reply = self.service.handle(
            Request("POST", "/api/login", {"Content-Type": "application/json"}, "x", body)
        )
        token = json.loads(reply.body)["data"]["token"]
        credential = Credential({"authorization": f"Bearer {token}"}, {}, None, "jti")
        return CredentialHandle(
            "mock", lambda: credential, "Renew it with: catalog-sync login mock"
        )


# --- what a correct run must have stored ---------------------------------------------------------


def expected_material(base_url: str, dataset: MockDataset, material: MockMaterial) -> JsonObject:
    """The fields of the normalised row (JSON form) the materials mapping must produce."""
    shares = [part.split("%") for part in material.composition.split("/")]
    issues: list[str] = []
    composition = []
    for percent, code in shares:
        fiber = FIBRES.get(code)
        if fiber is None:
            issues.append(f"composition:unknown_code:{code}")
        composition.append({"fiber": fiber, "percent": float(percent), "raw": f"{percent}%{code}"})
    if material.weight_gsm == 0:
        issues.insert(0, "weight_gsm:source_zero")
    path = dataset.folder_path(material.folder_id)
    return {
        "source": "mock.materials",
        "source_id": material.id,
        "code": material.code,
        "name": material.name,
        "composition": composition,
        "composition_raw": material.composition,
        "weight_gsm": None if material.weight_gsm == 0 else float(material.weight_gsm),
        "width_cm": parse_width_cm(material.width, {}).value,
        "price": {"amount": str(material.price), "currency": CURRENCY},
        "supplier": material.supplier_name,
        "folder_paths": [path] if path else [],
        "extra": {"mock": {"supplier_id": material.supplier_id}},
        "issues": sorted(issues),
        "images": [
            {
                "role": "thumb",
                "source_url": f"{base_url}/media/thumb/{material.id}.png",
                "local_path": f"assets/{material.id}+thumb.bin",
                "width": 96,
                "height": 96,
            },
            {
                "role": "swatch",
                "source_url": f"{base_url}/media/swatch/{material.id}.jpg",
                "local_path": f"assets/{material.id}+swatch.bin",
                "width": 192,
                "height": 192,
            },
        ],
    }


def expected_supplier(supplier: MockSupplier) -> JsonObject:
    return {
        "source": "mock.suppliers",
        "source_id": supplier.id,
        "name": supplier.name,
        "country": supplier.country,
        "rating": supplier.rating,
        "lead_time_days": supplier.lead_time_days,
        "certifications": list(supplier.certifications),
        "address": dict(supplier.address),
        "listed_at": supplier.listed_at.replace("Z", "+00:00"),
        "issues": [],
    }
