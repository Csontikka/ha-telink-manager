"""Encrypted advertising: bind key handling and keeping Home Assistant's BTHome entry in step."""

from __future__ import annotations

import logging
import secrets

from homeassistant.components import bluetooth
from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

_LOGGER = logging.getLogger(__name__)

BTHOME_DOMAIN = "bthome"
CONF_BINDKEY = "bindkey"  # the key core BTHome keeps the hex bind key under in its entry data
ADV_TYPE_BTHOME = 3  # adv_type_raw of the BTHome format
BTHOME_V2_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"
BTHOME_ENCRYPTED_BIT = 0x01  # bit 0 of the first service-data byte
PLAIN_ADVERT_TIMEOUT_S = 15  # covers the advertising interval plus a proxy's batching delay


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


def _is_encrypted(info: bluetooth.BluetoothServiceInfoBleak) -> bool:
    data = info.service_data.get(BTHOME_V2_UUID)
    return bool(data) and bool(data[0] & BTHOME_ENCRYPTED_BIT)


async def _async_wait_for_plain_advert(hass: HomeAssistant, mac: str) -> None:
    """Return once the newest advert Home Assistant holds for the device is not encrypted.

    Reloading the entry replays that advert into the fresh BTHome coordinator. If it is still an
    encrypted one and the key is gone, BTHome starts a reauth flow that would stay open.
    """
    last = bluetooth.async_last_service_info(hass, mac, connectable=False)
    if last is None or not _is_encrypted(last):
        return
    try:
        await bluetooth.async_process_advertisements(
            hass,
            lambda info: not _is_encrypted(info),
            {"address": mac.upper(), "connectable": False},
            bluetooth.BluetoothScanningMode.PASSIVE,
            PLAIN_ADVERT_TIMEOUT_S,
        )
    except TimeoutError:
        _LOGGER.debug("No plaintext advert from %s yet; reloading its BTHome entry anyway", mac)


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
        if entry.disabled_by is None:
            if key is None:
                await _async_wait_for_plain_advert(hass, mac)
            await hass.config_entries.async_reload(entry.entry_id)
        # BTHome raises this when plaintext arrives while it holds a key and clears it only on
        # decrypted data. After a disable it would stay behind for good. After an enable the
        # reload replays the last plaintext advert into the entry that now has the key, so it
        # shows until the first encrypted advert. Waiting for that advert instead is no option:
        # the old entry has no key yet and would open a reauth flow for it.
        ir.async_delete_issue(hass, BTHOME_DOMAIN, f"encryption_removed_{entry.entry_id}")
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Could not update the BTHome entry for %s", mac, exc_info=True)
        return "failed"
    return "updated"
