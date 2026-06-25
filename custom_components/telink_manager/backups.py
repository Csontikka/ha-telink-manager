"""Per-device full-state backups, persisted via the HA Store API.

A snapshot is the complete restorable state of one thermometer: the 0x55 config blob plus the
fields that live outside it (device name, comfort, bind key, sensor calibration). Snapshots are
saved automatically on every read and after every successful modify, with dedup (skip if identical
to the device's most recent snapshot) and a per-device history limit.
"""

from __future__ import annotations

import json
import time
import uuid

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import pvvx_struct
from .const import BACKUP_LIMIT, BACKUP_STORAGE_KEY, DOMAIN, STORAGE_VERSION

# Fields that define "different" for dedup (ts / friendly_name / fw / model are excluded).
_STATE_KEYS = ("raw", "device_name", "comfort", "bind_key", "sensor")


async def async_setup(hass: HomeAssistant) -> None:
    """Load the backup store into hass.data[DOMAIN]."""
    store = Store(hass, STORAGE_VERSION, BACKUP_STORAGE_KEY)
    data = await store.async_load() or {}
    # Backfill a stable unique id onto any pre-existing snapshot that lacks one (older stores keyed
    # deletes by timestamp, which can collide within a second). Persist once if anything changed.
    changed = False
    for snaps in data.values():
        for snap in snaps:
            if not snap.get("id"):
                snap["id"] = uuid.uuid4().hex
                changed = True
    if changed:
        await store.async_save(data)
    hass.data[DOMAIN]["backup_store"] = store
    hass.data[DOMAIN]["backups"] = data


def snapshot_from_fields(hass: HomeAssistant, mac: str, fields: dict) -> dict:
    """Build a full-state snapshot from a read()/loaded fields dict."""
    mac = mac.upper()
    names = hass.data.get(DOMAIN, {}).get("names", {})
    comfort = None
    if fields.get("comfort_t_lo") is not None:
        comfort = {
            "t_lo": fields["comfort_t_lo"],
            "t_hi": fields["comfort_t_hi"],
            "h_lo": fields["comfort_h_lo"],
            "h_hi": fields["comfort_h_hi"],
        }
    sensor = None
    if fields.get("t_slope") is not None:
        sensor = {
            "t_slope": fields["t_slope"],
            "t_offset_c": fields["t_offset_c"],
            "h_slope": fields["h_slope"],
            "h_offset_pct": fields["h_offset_pct"],
        }
    return {
        "id": uuid.uuid4().hex,  # stable unique key for delete (ts can collide within one second)
        "ts": int(time.time()),
        "mac": mac,
        "friendly_name": names.get(mac, ""),
        "fw": fields.get("fw_version"),
        "fw_byte": fields.get("fw_byte"),  # needed to re-parse the raw blob exactly (layout cutoff)
        "model": fields.get("model"),
        "raw": fields.get("raw"),
        "device_name": fields.get("device_name"),
        "comfort": comfort,
        "bind_key": fields.get("bind_key"),
        "sensor": sensor,
    }


def _state_sig(snap: dict) -> str:
    # byte[9] of the 0x55 blob (hex chars 18:20) is a reboot-sensitive runtime value (hw_ver) — it
    # must NOT count as a real change for dedup, exactly like the write-verify and Compare exclude it.
    raw = snap.get("raw") or ""
    raw_norm = (raw[:18] + raw[20:]) if len(raw) >= 22 else raw
    d = {k: snap.get(k) for k in _STATE_KEYS if k != "raw"}
    d["raw"] = raw_norm
    return json.dumps(d, sort_keys=True, default=str)


def list_for(hass: HomeAssistant, mac: str) -> list:
    """Snapshots for a MAC, newest last."""
    return hass.data.get(DOMAIN, {}).get("backups", {}).get(mac.upper(), [])


def index(hass: HomeAssistant) -> list:
    """One row per device that has backups: mac, friendly name, device (BLE) name, count, last_ts.

    Needs no BLE connection — served straight from the store, so backups are reachable even for
    devices that are out of range right now.
    """
    names = hass.data.get(DOMAIN, {}).get("names", {})
    out = []
    for mac, lst in hass.data.get(DOMAIN, {}).get("backups", {}).items():
        if not lst:
            continue
        last = lst[-1]
        out.append(
            {
                "mac": mac,
                "friendly_name": names.get(mac, ""),
                "device_name": last.get("device_name"),
                "count": len(lst),
                "last_ts": last.get("last_seen") or last.get("ts"),
            }
        )
    out.sort(key=lambda d: -(d.get("last_ts") or 0))
    return out


def _fw_byte_of(snap: dict) -> int:
    """The version byte for re-parsing (snapshot stores fw_byte now; fall back to the 'X.Y' string)."""
    fb = snap.get("fw_byte")
    if isinstance(fb, int):
        return fb
    try:
        maj, mi = str(snap.get("fw") or "").split(".")
        return (int(maj) << 4) | int(mi)
    except Exception:
        return 0  # parse() treats 0 as modern (all our devices are 5.x)


def history(hass: HomeAssistant, mac: str) -> list:
    """All snapshots of ONE device, each parsed into config fields — for the per-device timeline."""
    out = []
    for s in list_for(hass, mac):
        fields = {}
        if s.get("raw"):
            try:
                fields = pvvx_struct.parse(bytes.fromhex(s["raw"]), _fw_byte_of(s))
            except Exception:  # noqa: BLE001
                fields = {}
        out.append(
            {
                "ts": s.get("ts"),
                "device_name": s.get("device_name"),
                "fw": s.get("fw"),
                "comfort": s.get("comfort"),
                "bind_key_set": bool(s.get("bind_key")),
                "fields": fields,
            }
        )
    return out


def compare(hass: HomeAssistant) -> list:
    """For every device with backups: the LAST snapshot's parsed config + meta, for the matrix.

    No BLE needed — parses the stored 0x55 blob with pvvx_struct so the whole fleet is comparable.
    """
    names = hass.data.get(DOMAIN, {}).get("names", {})
    out = []
    for mac, lst in hass.data.get(DOMAIN, {}).get("backups", {}).items():
        if not lst:
            continue
        s = lst[-1]
        fields = {}
        if s.get("raw"):
            try:
                fields = pvvx_struct.parse(bytes.fromhex(s["raw"]), _fw_byte_of(s))
            except Exception:  # noqa: BLE001
                fields = {}
        out.append(
            {
                "mac": mac,
                "friendly_name": names.get(mac, ""),
                "device_name": s.get("device_name"),
                "fw": s.get("fw"),
                "last_ts": s.get("last_seen") or s.get("ts"),
                "comfort": s.get("comfort"),
                "bind_key_set": bool(s.get("bind_key")),
                "sensor": s.get("sensor"),
                "fields": fields,
            }
        )
    return out


async def async_save(hass: HomeAssistant, snapshot: dict) -> dict:
    """Append a snapshot (dedup vs the most recent; trim to BACKUP_LIMIT)."""
    mac = (snapshot.get("mac") or "").upper()
    if not mac or not snapshot.get("raw"):
        return {"ok": False, "error": "incomplete snapshot"}
    backups = hass.data[DOMAIN].setdefault("backups", {})
    lst = backups.setdefault(mac, [])
    now = snapshot.get("ts") or int(time.time())
    store = hass.data[DOMAIN].get("backup_store")
    if lst and _state_sig(lst[-1]) == _state_sig(snapshot):
        # identical state: don't add a row, but record that we saw/confirmed it again now.
        lst[-1]["last_seen"] = now
        if store is not None:
            await store.async_save(backups)
        return {"ok": True, "deduped": True, "count": len(lst), "last_seen": now}
    snapshot["last_seen"] = now
    lst.append(snapshot)
    if len(lst) > BACKUP_LIMIT:
        del lst[0 : len(lst) - BACKUP_LIMIT]
    if store is not None:
        await store.async_save(backups)
    return {"ok": True, "deduped": False, "count": len(lst)}


async def async_delete(hass: HomeAssistant, mac: str, snap_id: str) -> dict:
    """Delete the snapshot with the given unique id for a MAC."""
    mac = mac.upper()
    backups = hass.data[DOMAIN].setdefault("backups", {})
    lst = backups.get(mac, [])
    new = [s for s in lst if s.get("id") != snap_id]
    backups[mac] = new
    store = hass.data[DOMAIN].get("backup_store")
    if store is not None:
        await store.async_save(backups)
    return {"ok": True, "count": len(new)}
