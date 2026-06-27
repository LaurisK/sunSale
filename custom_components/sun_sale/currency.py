"""Display-currency resolution and label helpers.

sunSale's optimisation math is currency-neutral — every monetary figure is a
per-kWh quantity in whatever unit the configured price source supplies, so the
planner produces correct decisions regardless of market. The *only* thing the
currency code affects is presentation: the unit labels and icons on the sensors
/ number entities and the axis/tooltip text in the panel.

This module centralises that presentation concern so the consumers
(``sensor.py``, ``number.py`` and — via ``debug_view`` — the panel JS) agree on
one resolved code and one icon mapping. It is intentionally separate from
``ha_state.py``, which by contract imports nothing from the sunSale package.
"""
from __future__ import annotations

from typing import Any

from .contract.const import CONF_CURRENCY, DEFAULT_CURRENCY

# ISO-4217 code → Material Design Icons glyph. Codes with no dedicated MDI glyph
# (the Nordic krona currencies, most others) fall back to a generic cash icon.
_CURRENCY_ICONS: dict[str, str] = {
    "EUR": "mdi:currency-eur",
    "USD": "mdi:currency-usd",
    "GBP": "mdi:currency-gbp",
    "JPY": "mdi:currency-jpy",
    "CNY": "mdi:currency-cny",
    "INR": "mdi:currency-inr",
    "RUB": "mdi:currency-rub",
    "TRY": "mdi:currency-try",
    "ILS": "mdi:currency-ils",
    "PHP": "mdi:currency-php",
    "KRW": "mdi:currency-krw",
    "BRL": "mdi:currency-brl",
}

_DEFAULT_ICON = "mdi:cash"


def resolve_currency(entry: Any) -> str:
    """Return the display currency code for a config entry.

    Reads ``CONF_CURRENCY`` from the entry's options (preferred, so an options
    edit takes effect) then its data, falling back to :data:`DEFAULT_CURRENCY`.
    The result is upper-cased and stripped; a blank value falls back too.

    Args:
        entry: The Home Assistant config entry.

    Returns:
        An ISO-4217-style currency code (e.g. ``"EUR"``, ``"SEK"``).
    """
    raw = entry.options.get(CONF_CURRENCY) or entry.data.get(CONF_CURRENCY)
    code = (raw or "").strip().upper()
    return code or DEFAULT_CURRENCY


def currency_unit(code: str) -> str:
    """Return the bare currency unit label (the code itself), e.g. ``"EUR"``."""
    return code


def per_kwh_unit(code: str) -> str:
    """Return the per-kWh unit label for ``code``, e.g. ``"EUR/kWh"``."""
    return f"{code}/kWh"


def currency_icon(code: str) -> str:
    """Return the MDI icon for ``code``, or a generic cash icon when unmapped.

    Args:
        code: ISO-4217-style currency code.

    Returns:
        A ``mdi:`` icon string.
    """
    return _CURRENCY_ICONS.get(code.upper(), _DEFAULT_ICON)
