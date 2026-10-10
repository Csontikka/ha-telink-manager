"""Encrypted advertising: bind key handling and keeping Home Assistant's BTHome entry in step."""

from __future__ import annotations

import logging
import secrets

from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

_LOGGER = logging.getLogger(__name__)

BTHOME_DOMAIN = "bthome"
CONF_BINDKEY = "bindkey"  # the key core BTHome keeps the hex bind key under in its entry data
ADV_TYPE_BTHOME = 3  # adv_type_raw of the BTHome format


def new_bindkey() -> bytes:
    return secrets.token_bytes(16)


def parse_bindkey(key_hex: str) -> bytes:
    """A bind key typed by the user: 32 hex characters."""
    try:
        key = bytes.fromhex((key_hex or "").strip())
    except ValueError:
        key = b""
    if len(key) != 16:
        raise ValueError("bind key must be exactly 16 bytes (32 hex chars)")
    return key


def bthome_entry(hass: HomeAssistant, mac: str) -> ConfigEntry | None:
    """The core BTHome entry for this MAC. BTHome uses the upper-case address as its unique id."""
    for entry in hass.config_entries.async_entries(BTHOME_DOMAIN):
        if entry.source != SOURCE_IGNORE and (entry.unique_id or "").upper() == mac.upper():
            return entry
    return None


async def async_sync_bthome(hass: HomeAssistant, mac: str, key: bytes | None) -> str:
    """Set (or, with key=None, remove) the bind key on this device's BTHome entry and reload it.

    Returns "updated", "no_entry" or "failed". Must run after the device itself has changed: BTHome
    rejects plaintext adverts once it holds a key, and asks for a key when adverts turn encrypted.
    """
    entry = bthome_entry(hass, mac)
    if entry is None:
        return "no_entry"
    try:
        data = dict(entry.data)
        if key is not None:
            data[CONF_BINDKEY] = key.hex()
        else:
            data.pop(CONF_BINDKEY, None)
        if data != dict(entry.data):
            hass.config_entries.async_update_entry(entry, data=data)
        if key is None:
            # BTHome raises this when plaintext arrives while it holds a key and clears it only on
            # decrypted data, so it would stay behind after we drop the key.
            ir.async_delete_issue(hass, BTHOME_DOMAIN, f"encryption_removed_{entry.entry_id}")
        if entry.disabled_by is None:
            await hass.config_entries.async_reload(entry.entry_id)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Could not update the BTHome entry for %s", mac, exc_info=True)
        return "failed"
    return "updated"
