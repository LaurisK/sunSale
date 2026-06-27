"""Per-entry storage-key namespacing."""
from __future__ import annotations

from custom_components.sun_sale.contract.const import (
    DOMAIN,
    STORAGE_KEY_CAPACITY,
)
from custom_components.sun_sale.orchestration.persistent_store import (
    entry_storage_key,
)


def test_entry_storage_key_inserts_entry_after_domain():
    assert entry_storage_key("abc", STORAGE_KEY_CAPACITY) == f"{DOMAIN}_abc_capacity"
    # Only the first (domain-prefix) occurrence is namespaced.
    assert entry_storage_key("abc", f"{DOMAIN}_grid_import_power") == f"{DOMAIN}_abc_grid_import_power"
