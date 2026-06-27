"""sunSale – electricity buy/sell optimiser for Home Assistant."""
from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.components.frontend import async_remove_panel
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .contract.const import CONF_PANEL_TITLE, DOMAIN
from .orchestration.coordinator import SunSaleCoordinator
from .orchestration.debug_view import SunSaleDebugView

_STATIC_REGISTERED_KEY = f"{DOMAIN}_static_registered"
_STATIC_PATH = "/sun_sale"
_PANEL_URL_DEFAULT = "sun-sale"
_PANEL_TITLE_DEFAULT = "sunSale"
_WEBCOMPONENT = "sun-sale-panel"
_PANEL_ICON = "mdi:solar-panel"
# Per-entry panel bookkeeping. ``_PANELS_KEY`` holds entry_id → {title_override};
# ``_PANEL_URLS_KEY`` is the set of sidebar URL paths currently
# registered, so a re-sync tears down exactly what it last registered (no orphan
# tabs). One panel is registered per loaded entry — see ``_async_sync_panels``.
_PANELS_KEY = f"{DOMAIN}_panels"
_PANEL_URLS_KEY = f"{DOMAIN}_panel_urls"


def _js_hash(filename: str) -> str:
    """Return the first 8 hex characters of the MD5 hash of a www JS file for cache-busting.

    Args:
        filename: Filename relative to the integration's www/ directory.

    Returns:
        8-character hex string, or "0" when the file cannot be read.
    """
    path = Path(__file__).parent / "www" / filename
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()[:8]  # noqa: S324
    except OSError:
        return "0"

def _resolve_panel_title_override(entry: ConfigEntry) -> str:
    """Return the user's custom panel title for this entry, or '' when unset.

    Reads ``CONF_PANEL_TITLE`` from options (preferred) then data. Anything that
    isn't a non-blank string degrades to '' so the automatic default applies.
    """
    raw = entry.options.get(CONF_PANEL_TITLE) or entry.data.get(CONF_PANEL_TITLE)
    return raw.strip() if isinstance(raw, str) else ""


async def _async_sync_panels(hass: HomeAssistant) -> None:
    """(Re)register exactly one sidebar panel per loaded sunSale entry.

    Tears down every panel previously registered by this integration, then
    registers a fresh one for each entry in ``hass.data[_PANELS_KEY]``. Every
    panel is titled ``sunSale`` by default; a user-set ``CONF_PANEL_TITLE``
    (an editable field in the config / options flow) overrides it per entry, so
    the user names each instance to taste. URLs must be unique: a lone entry
    keeps the clean ``/sun-sale``; with multiple entries each gets a per-entry
    ``/sun-sale-<entry_id>``. The entry id is passed to the webcomponent so it
    scopes to its own instance.

    Idempotent — always converges to exactly the current set of panels — so it is
    safe to call on every entry setup and unload.
    """
    # Tear down whatever we registered last time (leaves no orphan tabs).
    for url in hass.data.get(_PANEL_URLS_KEY, set()):
        try:
            async_remove_panel(hass, url)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass

    registry: dict[str, dict] = hass.data.get(_PANELS_KEY, {})
    registered: set[str] = set()
    if registry:
        version = _js_hash("sun-sale-panel.js")
        single = len(registry) == 1
        for entry_id, info in registry.items():
            url = _PANEL_URL_DEFAULT if single else f"{_PANEL_URL_DEFAULT}-{entry_id}"
            title = info["title_override"] or _PANEL_TITLE_DEFAULT
            await async_register_panel(
                hass,
                frontend_url_path=url,
                webcomponent_name=_WEBCOMPONENT,
                sidebar_title=title,
                sidebar_icon=_PANEL_ICON,
                module_url=f"{_STATIC_PATH}/sun-sale-panel.js?v={version}",
                require_admin=False,
                config={"entry_id": entry_id},
            )
            registered.add(url)
    hass.data[_PANEL_URLS_KEY] = registered


_DEBUG_VIEW_KEY = f"{DOMAIN}_debug_view_registered"

PLATFORMS = ["sensor", "switch", "number", "select"]

SERVICE_FORCE_RECALCULATE = "force_recalculate"
SERVICE_FORCE_VERIFY_INVERTER_MODE = "force_verify_inverter_mode"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up sunSale from a config entry."""
    coordinator = SunSaleCoordinator(hass, entry)
    await coordinator.async_setup()

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as exc:
        raise ConfigEntryNotReady from exc

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    if not hass.is_running:
        @callback
        def _on_ha_started(_event) -> None:
            """Refresh once HA finishes starting so solar forecast entity has its watts attribute."""
            hass.async_create_task(coordinator.async_request_refresh())

        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_ha_started)
        )

    if not hass.data.get(_DEBUG_VIEW_KEY):
        hass.http.register_view(SunSaleDebugView())
        hass.data[_DEBUG_VIEW_KEY] = True

    if not hass.data.get(_STATIC_REGISTERED_KEY):
        await hass.http.async_register_static_paths([
            StaticPathConfig(
                _STATIC_PATH,
                str(Path(__file__).parent / "www"),
                cache_headers=False,
            )
        ])
        hass.data[_STATIC_REGISTERED_KEY] = True

    # One sidebar panel per loaded entry, all titled "sunSale" unless the user
    # set a custom CONF_PANEL_TITLE. Re-synced on every setup so adding a second
    # entry moves the first to a per-entry URL, and so an integration reload
    # (changed JS) re-registers with the fresh ?v=<hash> cache-buster. Options
    # changes reach here via async_reload_entry → reload.
    panels = hass.data.setdefault(_PANELS_KEY, {})
    panels[entry.entry_id] = {"title_override": _resolve_panel_title_override(entry)}
    await _async_sync_panels(hass)

    async def handle_force_recalculate(call: ServiceCall) -> None:
        """Trigger an immediate refresh on all sunSale coordinator instances."""
        for coord in hass.data[DOMAIN].values():
            await coord.async_request_refresh()

    async def handle_force_verify_inverter_mode(call: ServiceCall) -> None:
        """Run an inverter-mode verify cycle now (skip the +30 s wait).

        Useful for confirming engagement after a manual mode change without
        waiting on the scheduled verify-tick. Fans out to every configured
        sunSale instance so a multi-inverter setup verifies them all.
        """
        for coord in hass.data[DOMAIN].values():
            await coord.force_verify_inverter_mode()

    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_RECALCULATE):
        hass.services.async_register(DOMAIN, SERVICE_FORCE_RECALCULATE, handle_force_recalculate)
    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_VERIFY_INVERTER_MODE):
        hass.services.async_register(
            DOMAIN, SERVICE_FORCE_VERIFY_INVERTER_MODE,
            handle_force_verify_inverter_mode,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and remove its coordinator from hass.data.

    This entry's sidebar panel is dropped and the remaining panels re-synced
    (so removing one of two entries moves the survivor back to the clean
    ``/sun-sale`` URL). The two integration-wide services are torn down only
    when the *last* entry unloads. The HTTP debug view and the www/ static path are intentionally
    left registered: Home Assistant exposes no public API to remove an aiohttp
    route once added, and their ``_DEBUG_VIEW_KEY`` / ``_STATIC_REGISTERED_KEY``
    guards keep a later re-setup from double-registering the surviving routes.

    Args:
        hass: Home Assistant instance.
        entry: Config entry being unloaded.

    Returns:
        True when all platforms unloaded successfully.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        # Cancel the scheduled refresh and the control module's pending
        # verify-tick — otherwise a verify callback scheduled before unload
        # fires afterwards and issues real Modbus writes from a torn-down entry.
        await coordinator.async_shutdown()
        hass.data.get(_PANELS_KEY, {}).pop(entry.entry_id, None)
        await _async_sync_panels(hass)
        if not hass.data[DOMAIN]:
            _async_teardown_shared(hass)
    return unload_ok


def _async_teardown_shared(hass: HomeAssistant) -> None:
    """Remove the shared services once no sunSale entries remain.

    The sidebar panels are torn down by ``_async_sync_panels`` (called from the
    unload path with an empty registry just before this), so only the two
    fanned-out services need removing here.

    Args:
        hass: Home Assistant instance.
    """
    for service in (SERVICE_FORCE_RECALCULATE, SERVICE_FORCE_VERIFY_INVERTER_MODE):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when its options change.

    Args:
        hass: Home Assistant instance.
        entry: Config entry whose options were updated.
    """
    await hass.config_entries.async_reload(entry.entry_id)
