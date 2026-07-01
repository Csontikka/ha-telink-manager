"""Telink Manager — manage PVVX-firmware Telink thermometers from HA over BLE (standalone panel)."""

from __future__ import annotations

import hashlib
import logging
import os

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import backups, websocket_api
from .const import (
    BLE_NAME_STORAGE_KEY,
    DOMAIN,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL,
    STATIC_PATH,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


def _panel_hash(path: str) -> str:
    """Short content hash of the panel JS, used as a cache-busting query string on the module URL.

    Any edit to the file changes the hash, so the browser refetches the new build automatically —
    no hard-reload needed. Falls back to "0" if the file can't be read (still serves, just uncached).
    """
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read(), usedforsecurity=False).hexdigest()[:8]
    except OSError:
        return "0"


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

    # 1b) persisted BLE-name cache (mac -> real device name read over GATT). Its own store so the
    # scan list can show the real name instead of the MAC even after the backup history is cleared.
    ble_store: Store = Store(hass, STORAGE_VERSION, BLE_NAME_STORAGE_KEY)
    data["ble_name_store"] = ble_store
    data["ble_names"] = await ble_store.async_load() or {}

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
        panel_js = os.path.join(os.path.dirname(__file__), "www", "telink-manager-panel.js")
        ver = await hass.async_add_executor_job(_panel_hash, panel_js)
        await panel_custom.async_register_panel(
            hass,
            webcomponent_name="telink-manager-panel",
            frontend_url_path=PANEL_URL,
            module_url=f"{STATIC_PATH}/telink-manager-panel.js?h={ver}",
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
