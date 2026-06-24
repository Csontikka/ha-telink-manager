"""Telink Manager WebSocket commands (scan, read, write, set_name, backups, restore, bulk read-all)."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from . import backups, bulk, gatt
from .const import DOMAIN


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "telink_manager/proxies", vol.Required("mac"): str})
@websocket_api.async_response
async def ws_proxies(hass: HomeAssistant, connection, msg):
    """Diagnostic: list every scanner/proxy that sees a MAC and whether it is connectable."""
    connection.send_result(msg["id"], gatt.async_proxies(hass, msg["mac"]))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "telink_manager/scan"})
@websocket_api.async_response
async def ws_scan(hass: HomeAssistant, connection, msg):
    devices = await gatt.async_scan(hass)
    names = hass.data.get(DOMAIN, {}).get("names", {})
    for d in devices:
        d["friend_name"] = names.get(d["mac"], "")
    connection.send_result(msg["id"], {"devices": devices})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "telink_manager/set_name",
        vol.Required("mac"): str,
        vol.Required("name"): str,
    }
)
@websocket_api.async_response
async def ws_set_name(hass: HomeAssistant, connection, msg):
    """Save (or clear, if empty) a friendly name for a MAC; persisted via Store."""
    data = hass.data.setdefault(DOMAIN, {})
    names = data.setdefault("names", {})
    mac = msg["mac"].upper()
    name = msg["name"].strip()
    if name:
        names[mac] = name
    else:
        names.pop(mac, None)
    store = data.get("store")
    if store is not None:
        await store.async_save(names)
    connection.send_result(msg["id"], {"ok": True, "mac": mac, "name": name})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "telink_manager/set_device_name",
        vol.Required("mac"): str,
        vol.Required("name"): str,
    }
)
@websocket_api.async_response
async def ws_set_device_name(hass: HomeAssistant, connection, msg):
    """Write the device's stored BLE name on the thermometer (command 0x01)."""
    result = await gatt.async_set_name(hass, msg["mac"], msg["name"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "telink_manager/set_comfort",
        vol.Required("mac"): str,
        vol.Required("t_lo"): vol.Coerce(float),
        vol.Required("t_hi"): vol.Coerce(float),
        vol.Required("h_lo"): vol.Coerce(float),
        vol.Required("h_hi"): vol.Coerce(float),
    }
)
@websocket_api.async_response
async def ws_set_comfort(hass: HomeAssistant, connection, msg):
    """Write comfort thresholds on the thermometer (command 0x20)."""
    result = await gatt.async_set_comfort(hass, msg["mac"], msg["t_lo"], msg["t_hi"], msg["h_lo"], msg["h_hi"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "telink_manager/set_time",
        vol.Required("mac"): str,
        vol.Required("ts"): vol.Coerce(int),
    }
)
@websocket_api.async_response
async def ws_set_time(hass: HomeAssistant, connection, msg):
    """Set the device clock (command 0x23). ts = TZ-adjusted unix seconds to display."""
    result = await gatt.async_set_time(hass, msg["mac"], msg["ts"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "telink_manager/set_sensor",
        vol.Required("mac"): str,
        vol.Required("t_slope"): vol.Coerce(int),
        vol.Required("t_offset_c"): vol.Coerce(float),
        vol.Required("h_slope"): vol.Coerce(int),
        vol.Required("h_offset_pct"): vol.Coerce(float),
    }
)
@websocket_api.async_response
async def ws_set_sensor(hass: HomeAssistant, connection, msg):
    """Write sensor calibration (command 0x25)."""
    result = await gatt.async_set_sensor(
        hass, msg["mac"], msg["t_slope"], msg["t_offset_c"], msg["h_slope"], msg["h_offset_pct"]
    )
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "telink_manager/sensor_default", vol.Required("mac"): str})
@websocket_api.async_response
async def ws_sensor_default(hass: HomeAssistant, connection, msg):
    """Restore factory-default sensor calibration (command 0x26)."""
    result = await gatt.async_sensor_default(hass, msg["mac"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "telink_manager/lcd",
        vol.Required("mac"): str,
        vol.Required("big_number"): vol.Coerce(int),
        vol.Optional("small_number", default=0): vol.Coerce(int),
        vol.Required("vtime_sec"): vol.Coerce(int),
        vol.Optional("flg", default=0): vol.Coerce(int),
    }
)
@websocket_api.async_response
async def ws_lcd(hass: HomeAssistant, connection, msg):
    """Show a temporary number overlay on the LCD (command 0x22)."""
    result = await gatt.async_lcd(
        hass, msg["mac"], msg["big_number"], msg["small_number"], msg["vtime_sec"], msg["flg"]
    )
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "telink_manager/raw",
        vol.Required("mac"): str,
        vol.Required("hex"): str,
        vol.Optional("expect_reply", default=True): bool,
    }
)
@websocket_api.async_response
async def ws_raw(hass: HomeAssistant, connection, msg):
    """EXPERIMENTAL: send a raw command to char 0x1F1F."""
    result = await gatt.async_raw_cmd(hass, msg["mac"], msg["hex"], msg["expect_reply"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "telink_manager/get_mac", vol.Required("mac"): str})
@websocket_api.async_response
async def ws_get_mac(hass: HomeAssistant, connection, msg):
    """DANGEROUS group (read-only): read the device's stored MAC (command 0x10)."""
    result = await gatt.async_get_mac(hass, msg["mac"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "telink_manager/set_mac",
        vol.Required("mac"): str,
        vol.Required("new_mac"): str,
    }
)
@websocket_api.async_response
async def ws_set_mac(hass: HomeAssistant, connection, msg):
    """DANGEROUS: set a custom MAC (command 0x10). HA sees a new device after reboot."""
    result = await gatt.async_set_mac(hass, msg["mac"], msg["new_mac"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "telink_manager/get_bind_key", vol.Required("mac"): str})
@websocket_api.async_response
async def ws_get_bind_key(hass: HomeAssistant, connection, msg):
    """DANGEROUS group (read-only): read the encryption bind key (command 0x18)."""
    result = await gatt.async_get_bind_key(hass, msg["mac"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "telink_manager/set_bind_key",
        vol.Required("mac"): str,
        vol.Required("key"): str,
    }
)
@websocket_api.async_response
async def ws_set_bind_key(hass: HomeAssistant, connection, msg):
    """DANGEROUS: set the encryption bind key (command 0x18, exactly 16 B)."""
    result = await gatt.async_set_bind_key(hass, msg["mac"], msg["key"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "telink_manager/factory_reset", vol.Required("mac"): str})
@websocket_api.async_response
async def ws_factory_reset(hass: HomeAssistant, connection, msg):
    """DANGEROUS: reset all config to firmware defaults (command 0x56)."""
    result = await gatt.async_factory_reset(hass, msg["mac"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "telink_manager/reboot", vol.Required("mac"): str})
@websocket_api.async_response
async def ws_reboot(hass: HomeAssistant, connection, msg):
    """Reboot the device (command 0x72)."""
    result = await gatt.async_reboot(hass, msg["mac"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "telink_manager/read",
        vol.Required("mac"): str,
        # bulk "Read all" passes retries=1 so weak devices fail fast instead of holding the queue
        vol.Optional("retries"): vol.All(vol.Coerce(int), vol.Range(min=1, max=5)),
    }
)
@websocket_api.async_response
async def ws_read(hass: HomeAssistant, connection, msg):
    result = await gatt.async_read(hass, msg["mac"], retries=msg.get("retries", 3))
    if result.get("ok"):
        try:  # auto-backup the just-read full state (dedup'd inside async_save)
            snap = backups.snapshot_from_fields(hass, msg["mac"], result["fields"])
            result["backup"] = await backups.async_save(hass, snap)
        except Exception:  # noqa: BLE001
            pass
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "telink_manager/backup_save",
        vol.Required("mac"): str,
        vol.Required("fields"): dict,
    }
)
@websocket_api.async_response
async def ws_backup_save(hass: HomeAssistant, connection, msg):
    """Save a full-state snapshot from the panel's current loaded fields (after a modify)."""
    snap = backups.snapshot_from_fields(hass, msg["mac"], msg["fields"])
    connection.send_result(msg["id"], await backups.async_save(hass, snap))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "telink_manager/backups_index"})
@websocket_api.async_response
async def ws_backups_index(hass: HomeAssistant, connection, msg):
    """List every device that has backups (no BLE needed) — for the global Backups view."""
    connection.send_result(msg["id"], {"ok": True, "devices": backups.index(hass)})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "telink_manager/backups_compare"})
@websocket_api.async_response
async def ws_backups_compare(hass: HomeAssistant, connection, msg):
    """Parsed last-snapshot config for every backed-up device — for the Compare matrix (no BLE)."""
    connection.send_result(msg["id"], {"ok": True, "devices": backups.compare(hass)})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "telink_manager/backups_history", vol.Required("mac"): str})
@websocket_api.async_response
async def ws_backups_history(hass: HomeAssistant, connection, msg):
    """All snapshots of one device, parsed — for the per-device history/timeline matrix (no BLE)."""
    connection.send_result(msg["id"], {"ok": True, "snapshots": backups.history(hass, msg["mac"])})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "telink_manager/backups_list", vol.Required("mac"): str})
@websocket_api.async_response
async def ws_backups_list(hass: HomeAssistant, connection, msg):
    """List saved snapshots for a MAC (newest last)."""
    connection.send_result(msg["id"], {"ok": True, "backups": backups.list_for(hass, msg["mac"])})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "telink_manager/backup_delete",
        vol.Required("mac"): str,
        vol.Required("ts"): vol.Coerce(int),
    }
)
@websocket_api.async_response
async def ws_backup_delete(hass: HomeAssistant, connection, msg):
    connection.send_result(msg["id"], await backups.async_delete(hass, msg["mac"], msg["ts"]))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "telink_manager/restore",
        vol.Required("target_mac"): str,
        vol.Required("snapshot"): dict,
        vol.Required("parts"): [str],
    }
)
@websocket_api.async_response
async def ws_restore(hass: HomeAssistant, connection, msg):
    """Restore selected parts of a snapshot onto target_mac (same device or clone). MAC never cloned."""
    result = await gatt.async_restore(hass, msg["target_mac"], msg["snapshot"], msg["parts"])
    # Safety net: save the target's pre-overwrite state (read inside the restore connection) as a
    # backup, so a clone/restore is always reversible — even onto a device that had no backup yet.
    before = result.pop("before_fields", None) if isinstance(result, dict) else None
    if before:
        try:
            snap = backups.snapshot_from_fields(hass, msg["target_mac"], before)
            result["safety_backup"] = await backups.async_save(hass, snap)
        except Exception:  # noqa: BLE001
            pass
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "telink_manager/write",
        vol.Required("mac"): str,
        vol.Required("changes"): dict,
    }
)
@websocket_api.async_response
async def ws_write(hass: HomeAssistant, connection, msg):
    result = await gatt.async_write(hass, msg["mac"], msg["changes"])
    connection.send_result(msg["id"], result)


# ----- bulk "Read all" (server-side job, survives F5/navigation) -----
@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "telink_manager/read_all",
        vol.Required("entries"): [{vol.Required("mac"): str, vol.Optional("proxy"): vol.Any(str, None)}],
    }
)
@callback
def ws_read_all(hass: HomeAssistant, connection, msg):
    connection.send_result(msg["id"], bulk.start(hass, msg["entries"]))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "telink_manager/bulk_status"})
@callback
def ws_bulk_status(hass: HomeAssistant, connection, msg):
    connection.send_result(msg["id"], bulk.status(hass))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "telink_manager/bulk_cancel"})
@callback
def ws_bulk_cancel(hass: HomeAssistant, connection, msg):
    connection.send_result(msg["id"], bulk.cancel(hass))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "telink_manager/bulk_dismiss"})
@callback
def ws_bulk_dismiss(hass: HomeAssistant, connection, msg):
    connection.send_result(msg["id"], bulk.dismiss(hass))


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_proxies)
    websocket_api.async_register_command(hass, ws_scan)
    websocket_api.async_register_command(hass, ws_raw)
    websocket_api.async_register_command(hass, ws_read)
    websocket_api.async_register_command(hass, ws_write)
    websocket_api.async_register_command(hass, ws_set_name)
    websocket_api.async_register_command(hass, ws_set_device_name)
    websocket_api.async_register_command(hass, ws_set_comfort)
    websocket_api.async_register_command(hass, ws_set_time)
    websocket_api.async_register_command(hass, ws_set_sensor)
    websocket_api.async_register_command(hass, ws_sensor_default)
    websocket_api.async_register_command(hass, ws_lcd)
    websocket_api.async_register_command(hass, ws_get_mac)
    websocket_api.async_register_command(hass, ws_set_mac)
    websocket_api.async_register_command(hass, ws_get_bind_key)
    websocket_api.async_register_command(hass, ws_set_bind_key)
    websocket_api.async_register_command(hass, ws_factory_reset)
    websocket_api.async_register_command(hass, ws_reboot)
    websocket_api.async_register_command(hass, ws_backup_save)
    websocket_api.async_register_command(hass, ws_backups_index)
    websocket_api.async_register_command(hass, ws_backups_compare)
    websocket_api.async_register_command(hass, ws_backups_history)
    websocket_api.async_register_command(hass, ws_backups_list)
    websocket_api.async_register_command(hass, ws_backup_delete)
    websocket_api.async_register_command(hass, ws_restore)
    websocket_api.async_register_command(hass, ws_read_all)
    websocket_api.async_register_command(hass, ws_bulk_status)
    websocket_api.async_register_command(hass, ws_bulk_cancel)
    websocket_api.async_register_command(hass, ws_bulk_dismiss)
