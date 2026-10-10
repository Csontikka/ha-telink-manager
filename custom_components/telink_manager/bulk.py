"""Server-side bulk jobs: "Read all" and bulk encrypted advertising.

The whole orchestration runs in HA (a background task), with state kept in hass.data — so progress
survives a browser F5, navigating away, or closing the tab. The panel just polls bulk_status.
Each device goes through gatt, which holds a per-device lock, so connections never overlap.
Creates no entities/helpers/automations — it is reachable only via the panel's WebSocket API.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant

from . import backups, gatt
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_KEY = "bulk_job"


def _job(hass: HomeAssistant):
    return hass.data.setdefault(DOMAIN, {}).get(_KEY)


def status(hass: HomeAssistant) -> dict:
    """Serializable snapshot for the panel."""
    j = _job(hass)
    if not j:
        return {"active": False, "cancel": False, "state": None, "result": None}
    return {
        "active": j["active"],
        "action": j["action"],
        "enable": j["enable"],
        "cancel": j["cancel"],
        "state": {
            "total": j["total"],
            "done": j["done"],
            "ok": j["ok"],
            "fail": j["fail"],
            "running": list(j["running"]),
            "backups": dict(j["backups"]),
        },
        "result": j["result"],
    }


def cancel(hass: HomeAssistant) -> dict:
    j = _job(hass)
    if j and j["active"]:
        j["cancel"] = True
    return status(hass)


def dismiss(hass: HomeAssistant) -> dict:
    """Clear a finished job's result once a panel has shown it (so it won't re-pop on navigation)."""
    j = _job(hass)
    if j and not j["active"]:
        j["result"] = None
    return status(hass)


async def _remember(hass: HomeAssistant, j: dict, mac: str, fields: dict) -> None:
    """Cache the device's real BLE name and snapshot the state just read."""
    dn = fields.get("device_name")
    if dn:  # cache the real BLE name so the scan list stops showing the MAC
        await gatt.async_remember_ble_name(hass, mac, dn)
    try:
        snap = backups.snapshot_from_fields(hass, mac, fields)
        bk = await backups.async_save(hass, snap)
        if isinstance(bk, dict) and isinstance(bk.get("count"), int):
            j["backups"][mac] = bk["count"]
    except Exception:  # noqa: BLE001
        pass


async def _read_one(hass: HomeAssistant, j: dict, mac: str) -> dict:
    try:
        # gatt.async_read already has its own timeouts + per-device lock; this is a final safety cap.
        r = await asyncio.wait_for(gatt.async_read(hass, mac, retries=3), timeout=150)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)}
    if r.get("ok"):
        await _remember(hass, j, mac, r["fields"])
        return {"ok": True}
    return {"ok": False, "error": r.get("error") or "failed"}


async def _encrypt_one(hass: HomeAssistant, j: dict, mac: str) -> dict:
    # No outer timeout: cancelling between the device write and the BTHome update would strand the key.
    try:
        r = await gatt.async_set_encryption(hass, mac, j["enable"])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)}
    if r.get("maybe_changed"):  # the device may differ from what we saw; the key is not lost
        if r.get("bind_key"):
            j["manual"].append({"mac": mac, "bind_key": r["bind_key"], "reason": "maybe_changed"})
        return {"ok": False, "error": r["error"]}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "failed"}
    await _remember(hass, j, mac, r["fields"])
    if j["enable"] and r["ha"] != "updated":  # the user has to enter this key somewhere themselves
        j["manual"].append({"mac": mac, "bind_key": r["bind_key"], "adv_type": r["adv_type"], "reason": r["ha"]})
    if r["ha"] == "failed":
        # The device changed but Home Assistant did not follow: a failure either way, so Retry failed
        # re-syncs it (an encrypting device keeps its key). On enable the key is also listed above.
        if j["enable"]:
            return {
                "ok": False,
                "error": "now encrypting, but updating its BTHome entry failed; the key is listed below. "
                "Retry failed finishes it",
            }
        return {"ok": False, "error": "now plain, but its BTHome entry still has the key; Retry failed finishes it"}
    return {"ok": True}


async def _run(hass: HomeAssistant, j: dict, groups: list) -> None:
    async def worker(devs: list):
        for mac in devs:
            if j["cancel"]:
                break
            j["running"].add(mac)
            res = await (_encrypt_one if j["action"] == "encrypt" else _read_one)(hass, j, mac)
            j["running"].discard(mac)
            j["done"] += 1
            if res["ok"]:
                j["ok"] += 1
            else:
                j["fail"] += 1
                j["failures"].append({"mac": mac, "error": res["error"]})

    try:
        await asyncio.gather(*(worker(g) for g in groups))
    finally:
        j["result"] = {
            "total": j["total"],
            "ok": j["ok"],
            "fail": j["fail"],
            "failures": list(j["failures"]),
            "cancelled": j["cancel"],
            "action": j["action"],
            "enable": j["enable"],
            "manual": list(j["manual"]),
        }
        j["active"] = False


def start(hass: HomeAssistant, entries: list, action: str = "read", enable: bool = False) -> dict:
    """Start a bulk job: "read", or "encrypt" to turn encrypted advertising on or off.

    entries = [{mac, proxy}]. Grouped by proxy: serial within a proxy,
    parallel across proxies (the per-device lock in gatt is the real overlap guard)."""
    j = _job(hass)
    if j and j["active"]:
        return {"ok": False, "error": "already_running", **status(hass)}
    groups_map: dict = {}
    macs = []
    for e in entries:
        mac = (e.get("mac") or "").upper()
        if not mac:
            continue
        macs.append(mac)
        groups_map.setdefault(e.get("proxy") or "—", []).append(mac)
    if not macs:
        return {"ok": False, "error": "no_devices", **status(hass)}
    j = {
        "active": True,
        "cancel": False,
        "total": len(macs),
        "action": action,
        "enable": enable,
        "manual": [],
        "done": 0,
        "ok": 0,
        "fail": 0,
        "running": set(),
        "failures": [],
        "backups": {},
        "result": None,
    }
    hass.data.setdefault(DOMAIN, {})[_KEY] = j
    hass.async_create_background_task(_run(hass, j, list(groups_map.values())), name="telink_manager_bulk_read")
    return {"ok": True, **status(hass)}
