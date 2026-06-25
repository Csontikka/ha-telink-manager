"""Telink Manager — manage PVVX/ATC thermometers from HA over BLE proxies (standalone panel)."""

from __future__ import annotations

import logging
import os

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import backups, websocket_api
from .const import (
    DOMAIN,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL,
    STATIC_PATH,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Telink Manager: load stores, register WS commands, static assets and the panel."""
    data = hass.data.setdefault(DOMAIN, {})

    # 1) persisted friend names (mac -> name) via the HA Store API.
    # One-time migration: inherit names from the old "pvvx_manager_names" store if present.
    store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    names = await store.async_load()
    if not names:
        legacy = await Store(hass, STORAGE_VERSION, "pvvx_manager_names").async_load()
        if legacy:
            names = legacy
            await store.async_save(names)
    data["store"] = store
    data["names"] = names or {}

    # 2) persisted per-device backups (mac -> [snapshot, ...])
    await backups.async_setup(hass)

    # 3) WebSocket commands (telink_manager/scan, /read, /write, ...) — register once.
    if not data.get("_ws_registered"):
        websocket_api.async_register(hass)
        data["_ws_registered"] = True

    # 4) serve the static frontend (the www/ folder) — register once.
    if not data.get("_static_registered"):
        www_dir = os.path.join(os.path.dirname(__file__), "www")
        await hass.http.async_register_static_paths([StaticPathConfig(STATIC_PATH, www_dir, False)])
        data["_static_registered"] = True

    # 5) register the standalone sidebar panel.
    if not data.get("_panel_registered"):
        await panel_custom.async_register_panel(
            hass,
            webcomponent_name="telink-manager-panel",
            frontend_url_path=PANEL_URL,
            module_url=f"{STATIC_PATH}/telink-manager-panel.js",
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            require_admin=True,
        )
        data["_panel_registered"] = True

    _LOGGER.info("Telink Manager loaded (panel: /%s)", PANEL_URL)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove the sidebar panel. WS commands and static paths stay (not removable, harmless)."""
    data = hass.data.get(DOMAIN, {})
    if data.get("_panel_registered"):
        frontend.async_remove_panel(hass, PANEL_URL)
        data["_panel_registered"] = False
    return True
