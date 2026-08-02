"""Regenerate the golden store-payload fixtures for ``test_store_roundtrip``.

Writes one ``tests/fixtures/store_payloads/<storage_key>.json`` per singleton
store (the serialised representative value) plus the stale-schema capacity
fixture that pins the 2026-06-13 purge regression.

**Only run this when a stored schema legitimately changes** — i.e. alongside a
``STORAGE_VERSION`` bump or an in-codec migration path. The fixtures are
otherwise append-only: regenerating them silently would defeat the drift guard
they exist to provide.

Run: ``python tools/gen_store_fixtures.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.sun_sale.orchestration.store_codecs import (  # noqa: E402
    SINGLETON_STORE_SPECS,
)
from tests.test_store_roundtrip import (  # noqa: E402
    _SINGLETON_REPRESENTATIVE,
    FIXTURE_DIR,
)


def main() -> int:
    """Write every golden fixture from the current serialisers.

    Returns:
        Process exit code (always 0).
    """
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for spec in SINGLETON_STORE_SPECS:
        value = _SINGLETON_REPRESENTATIVE[spec.storage_key]()
        payload = spec.serialize(value)
        path = FIXTURE_DIR / f"{spec.storage_key}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"wrote {path.relative_to(_REPO_ROOT)}")

    # Stale-schema capacity fixture: schema_version 1 with fabricated poison
    # observations that from_dict must discard (see test docstring). Hand-frozen
    # — not derived from any current serialiser.
    stale = {
        "schema_version": 1,
        "nominal_capacity_kwh": 28.0,
        "observations": [
            {
                "timestamp": "2026-06-01T10:00:00+00:00",
                "soc_start": 0.5,
                "soc_end": 0.9,
                "energy_kwh": 6.65,
                "direction": "charge",
            }
        ],
    }
    stale_path = FIXTURE_DIR / "capacity_stale_schema.json"
    stale_path.write_text(json.dumps(stale, indent=2, sort_keys=True) + "\n")
    print(f"wrote {stale_path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
