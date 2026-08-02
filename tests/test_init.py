"""Tests for __init__.py — async_setup_entry and async_unload_entry."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.sun_sale import async_setup_entry, async_unload_entry
from custom_components.sun_sale.contract.const import DOMAIN


def make_hass(domain_data: dict | None = None) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: domain_data} if domain_data is not None else {}
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    hass.services = MagicMock()
    hass.services.has_service.return_value = False
    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    return hass


def make_entry(entry_id: str = "test_entry") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.add_update_listener.return_value = MagicMock()
    return entry


async def test_async_setup_entry_registers_coordinator():
    hass = make_hass()
    entry = make_entry()

    with patch("custom_components.sun_sale.SunSaleCoordinator") as MockCoord:
        mock_coord = MagicMock()
        mock_coord.async_setup = AsyncMock()
        mock_coord.async_config_entry_first_refresh = AsyncMock()
        MockCoord.return_value = mock_coord

        result = await async_setup_entry(hass, entry)

    assert result is True
    assert DOMAIN in hass.data
    assert hass.data[DOMAIN]["test_entry"] is mock_coord


async def test_async_setup_entry_registers_debug_view_once():
    hass = make_hass()
    entry = make_entry()

    with patch("custom_components.sun_sale.SunSaleCoordinator") as MockCoord:
        mock_coord = MagicMock()
        mock_coord.async_setup = AsyncMock()
        mock_coord.async_config_entry_first_refresh = AsyncMock()
        MockCoord.return_value = mock_coord

        await async_setup_entry(hass, entry)

    hass.http.register_view.assert_called_once()


async def test_async_setup_entry_registers_service():
    hass = make_hass()
    entry = make_entry()

    with patch("custom_components.sun_sale.SunSaleCoordinator") as MockCoord:
        mock_coord = MagicMock()
        mock_coord.async_setup = AsyncMock()
        mock_coord.async_config_entry_first_refresh = AsyncMock()
        MockCoord.return_value = mock_coord

        await async_setup_entry(hass, entry)

    # Both force_recalculate and force_verify_inverter_mode should register.
    service_names = {
        c.args[1] for c in hass.services.async_register.call_args_list
    }
    assert "force_recalculate" in service_names
    assert "force_verify_inverter_mode" in service_names


async def test_async_setup_entry_skips_duplicate_debug_view():
    hass = make_hass()
    hass.data["sun_sale_debug_view_registered"] = True
    entry = make_entry()

    with patch("custom_components.sun_sale.SunSaleCoordinator") as MockCoord:
        mock_coord = MagicMock()
        mock_coord.async_setup = AsyncMock()
        mock_coord.async_config_entry_first_refresh = AsyncMock()
        MockCoord.return_value = mock_coord

        await async_setup_entry(hass, entry)

    hass.http.register_view.assert_not_called()


# ---------------------------------------------------------------------------
# Per-entry sidebar panel registration + naming
# ---------------------------------------------------------------------------

async def test_single_entry_panel_default_name_and_clean_url():
    from custom_components.sun_sale import _PANELS_KEY, _async_sync_panels
    hass = make_hass()
    hass.data[_PANELS_KEY] = {"e1": {"title_override": ""}}
    with patch("custom_components.sun_sale.async_register_panel", new=AsyncMock()) as reg, \
            patch("custom_components.sun_sale.async_remove_panel"):
        await _async_sync_panels(hass)
    reg.assert_called_once()
    kw = reg.call_args.kwargs
    assert kw["frontend_url_path"] == "sun-sale"
    assert kw["sidebar_title"] == "sunSale"
    assert kw["config"] == {"entry_id": "e1"}


async def test_multiple_entries_share_default_name_with_unique_urls():
    from custom_components.sun_sale import _PANELS_KEY, _async_sync_panels
    hass = make_hass()
    hass.data[_PANELS_KEY] = {
        "e1": {"title_override": ""},
        "e2": {"title_override": "Barn"},   # user-customised the second
    }
    with patch("custom_components.sun_sale.async_register_panel", new=AsyncMock()) as reg, \
            patch("custom_components.sun_sale.async_remove_panel"):
        await _async_sync_panels(hass)
    by_entry = {c.kwargs["config"]["entry_id"]: c.kwargs for c in reg.call_args_list}
    # Default title for both unless overridden; URLs are per-entry (unique).
    assert by_entry["e1"]["frontend_url_path"] == "sun-sale-e1"
    assert by_entry["e1"]["sidebar_title"] == "sunSale"
    assert by_entry["e2"]["frontend_url_path"] == "sun-sale-e2"
    assert by_entry["e2"]["sidebar_title"] == "Barn"


async def test_user_title_override_wins():
    from custom_components.sun_sale import _PANELS_KEY, _async_sync_panels
    hass = make_hass()
    hass.data[_PANELS_KEY] = {"e1": {"title_override": "My Solar"}}
    with patch("custom_components.sun_sale.async_register_panel", new=AsyncMock()) as reg, \
            patch("custom_components.sun_sale.async_remove_panel"):
        await _async_sync_panels(hass)
    assert reg.call_args.kwargs["sidebar_title"] == "My Solar"


async def test_sync_removes_previously_registered_panels():
    from custom_components.sun_sale import _PANEL_URLS_KEY, _PANELS_KEY, _async_sync_panels
    hass = make_hass()
    hass.data[_PANEL_URLS_KEY] = {"sun-sale-stale"}
    hass.data[_PANELS_KEY] = {}        # no entries left
    with patch("custom_components.sun_sale.async_register_panel", new=AsyncMock()) as reg, \
            patch("custom_components.sun_sale.async_remove_panel") as rm:
        await _async_sync_panels(hass)
    rm.assert_called_once_with(hass, "sun-sale-stale")
    reg.assert_not_called()
    assert hass.data[_PANEL_URLS_KEY] == set()


def test_resolve_panel_title_override_reads_options_then_data():
    from custom_components.sun_sale import _resolve_panel_title_override
    e = MagicMock()
    e.options = {"panel_title": "  Custom  "}
    e.data = {}
    assert _resolve_panel_title_override(e) == "Custom"
    # Non-string (e.g. absent) → empty so the automatic default applies.
    e2 = MagicMock()
    e2.options = {}
    e2.data = {}
    assert _resolve_panel_title_override(e2) == ""


async def test_async_unload_entry_removes_coordinator_on_success():
    coord = MagicMock()
    coord.async_shutdown = AsyncMock()
    hass = make_hass(domain_data={"test_entry": coord})
    entry = make_entry()

    result = await async_unload_entry(hass, entry)

    assert result is True
    assert "test_entry" not in hass.data[DOMAIN]
    # Verify timer + scheduled refresh must be cancelled so no callback
    # fires against torn-down state after unload.
    coord.async_shutdown.assert_awaited_once()


async def test_async_unload_entry_keeps_coordinator_on_failure():
    coord = MagicMock()
    coord.async_shutdown = AsyncMock()
    hass = make_hass(domain_data={"test_entry": coord})
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
    entry = make_entry()

    result = await async_unload_entry(hass, entry)

    assert result is False
    assert "test_entry" in hass.data[DOMAIN]
    # A failed platform unload leaves the coordinator live; it must not be
    # shut down.
    coord.async_shutdown.assert_not_awaited()
