"""The built-in Mock SaaS as a connector (see ``catalog_sync.mock`` for the service itself)."""

from pathlib import Path

from catalog_sync.connector_info import ConnectorInfo
from catalog_sync.plugins.mock.settings import MATERIALS_SETTINGS, SUPPLIERS_SETTINGS

_HERE = Path(__file__).parent
_TERMS = "{base_url}/terms"  # the Mock SaaS serves its own (invented) terms page


def supplier_schema() -> Path:
    """Where the ``supplier`` entity schema is declared.

    Registered as the schema ``supplier`` (catalog_sync.schemas).
    """
    return _HERE / "schemas" / "supplier.yaml"


def composition_table() -> Path:
    """Where the fibre codes of the mock's materials are listed.

    Registered as the code table ``mock.saas/materials.composition`` (catalog_sync.code_tables).
    """
    return _HERE / "tables" / "materials.composition.yaml"


def materials_connector() -> ConnectorInfo:
    """Registered as the connector info of ``mock.materials`` (catalog_sync.connectors)."""
    return ConnectorInfo(
        summary="Invented materials in a folder tree, with pictures (the built-in Mock SaaS: "
        "start it with `catalog-sync mock serve`)",
        credential="mock",
        settings=MATERIALS_SETTINGS,
        mapping=_HERE / "mappings" / "materials.yaml",
        credential_settings=("base_url",),
        terms_url=_TERMS,
    )


def suppliers_connector() -> ConnectorInfo:
    """Registered as the connector info of ``mock.suppliers`` (catalog_sync.connectors)."""
    return ConnectorInfo(
        summary="Invented suppliers in a flat, token-paged list (the built-in Mock SaaS: "
        "start it with `catalog-sync mock serve`)",
        credential="mock",
        settings=SUPPLIERS_SETTINGS,
        mapping=_HERE / "mappings" / "suppliers.yaml",
        credential_settings=("base_url",),
        terms_url=_TERMS,
    )
