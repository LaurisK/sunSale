"""Shared Home Assistant credential resolution for the dev tools.

Used by both the integration validation harness (``tools/integration_check.py``)
and the deploy script (``tools/deploy.py``). Credentials are **never** committed
to the repo.

Resolution precedence (first non-empty wins):

  1. Explicit CLI argument (``--url`` / ``--token``).
  2. Environment variables ``HA_URL`` / ``HA_TOKEN``.
  3. A local secrets file ``tools/secrets.json`` (gitignored).
     See ``tools/secrets.example.json`` for the format.

A credential that cannot be resolved from any source raises
``CredentialsError`` so a misconfigured tool fails loudly instead of silently
hitting the wrong server.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# tools/checks/credentials.py -> parent.parent == tools/
_TOOLS_DIR = Path(__file__).resolve().parent.parent
SECRETS_PATH = _TOOLS_DIR / "secrets.json"
EXAMPLE_PATH = _TOOLS_DIR / "secrets.example.json"


class CredentialsError(RuntimeError):
    """Raised when an HA URL or token cannot be resolved from any source."""


def _load_secrets_file() -> dict:
    """Return the parsed local secrets file, or ``{}`` when it is absent.

    Returns:
        Parsed JSON object, or an empty dict when ``secrets.json`` does not exist.

    Raises:
        CredentialsError: When the file exists but cannot be read or parsed.
    """
    try:
        with SECRETS_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        raise CredentialsError(f"Could not read {SECRETS_PATH}: {exc}") from exc


def resolve_credentials(
    cli_url: str | None = None,
    cli_token: str | None = None,
) -> tuple[str, str]:
    """Resolve the HA base URL and access token from CLI args, env, or the file.

    Args:
        cli_url: Value passed via ``--url`` (highest precedence), or ``None``.
        cli_token: Value passed via ``--token`` (highest precedence), or ``None``.

    Returns:
        ``(url, token)`` tuple, both guaranteed non-empty.

    Raises:
        CredentialsError: When either value cannot be resolved from any source.
    """
    data = _load_secrets_file()
    url = cli_url or os.environ.get("HA_URL") or data.get("ha_url")
    token = cli_token or os.environ.get("HA_TOKEN") or data.get("ha_token")

    missing = []
    if not url:
        missing.append("HA URL (--url, HA_URL env, or 'ha_url' in secrets.json)")
    if not token:
        missing.append("HA token (--token, HA_TOKEN env, or 'ha_token' in secrets.json)")
    if missing:
        raise CredentialsError(
            "Missing credentials: "
            + "; ".join(missing)
            + f".\nCopy {EXAMPLE_PATH.name} to {SECRETS_PATH.name} and fill it in, "
            "or pass --url/--token, or set the HA_URL/HA_TOKEN environment variables."
        )
    return url, token
