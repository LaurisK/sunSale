"""Read-only Home Assistant REST client and connection constants."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

DEBUG_PATH = "/api/sun_sale/debug"


STATE_PATH = "/api/states/{entity_id}"


class HAClient:
    """Read-only HA REST client for the validation harness."""

    def __init__(self, base_url: str, token: str, timeout: float = 10.0) -> None:
        """Store HA base URL, bearer token, and per-request timeout.

        Args:
            base_url: Root URL of the HA instance (trailing slash trimmed).
            token: Long-lived access token used as the bearer credential.
            timeout: Per-request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _get(self, path: str) -> Any:
        """Issue an authenticated GET against the HA API and parse the JSON body.

        Args:
            path: API path beginning with ``/api``.

        Returns:
            Parsed JSON payload (dict, list, or scalar).
        """
        req = urllib.request.Request(
            self.base_url + path,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def debug(self) -> list[dict]:
        """Fetch the sunSale debug snapshot list, one entry per coordinator.

        Returns:
            List of per-coordinator debug dicts as served by the integration.

        Raises:
            ValueError: When the debug endpoint returns a non-list payload.
        """
        payload = self._get(DEBUG_PATH)
        if not isinstance(payload, list):
            raise ValueError(f"Expected list from {DEBUG_PATH}, got {type(payload).__name__}")
        return payload

    def state(self, entity_id: str) -> dict | None:
        """Return the HA state dict for ``entity_id``, or ``None`` on HTTP 404.

        Args:
            entity_id: HA entity ID to look up.

        Returns:
            State dict, or ``None`` when the entity is unknown.
        """
        try:
            return self._get(STATE_PATH.format(entity_id=entity_id))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
