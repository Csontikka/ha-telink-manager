"""Firmware update (Telink OTA) for PVVX thermometers, over the same Bluetooth proxies.

Protocol, as the PVVX TelinkMiFlasher speaks it to the Telink OTA characteristic (...2b12):
  00ff, 01ff                        start
  idx(u16 LE) + 16 B data + crc16   one block per write; crc16/MODBUS over idx + data, LE
  02ff + last_idx + ~last_idx       end: the device checks the image CRC and reboots into it
Every 8 blocks the characteristic is read back; a non-zero first byte is an OTA error code.

The image goes to the inactive flash bank and is only switched to after its CRC checks out, so a
transfer that dies halfway leaves the device on its current firmware. A new firmware version may come
up with factory settings, so the configuration read just before flashing is snapshotted and, if the
device returns different, written back afterwards (encryption included, with Home Assistant's BTHome
entry kept in step). Only plain BLE images up to
128 KiB are handled: no Zigbee OTA, no signed "back to original" images, no extended (>128 KiB) OTA.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
import zlib

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import backups, gatt
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

OTA_CHAR = "00010203-0405-0607-0809-0a0b0c0d2b12"
FW_BASE_URL = "https://raw.githubusercontent.com/pvvx/ATC_MiThermometer/master/"
FW_INDEX = "firmware.json"
MAX_OTA_SIZE = 0x20000  # standard BLE OTA region; larger images need the extended OTA we do not do
MIN_HW_LAYOUT_FW = 0x48  # from this firmware on, config byte 9 is the plain hardware id
SYNC_EVERY = 8  # blocks between status read-backs (same cadence as the web flasher)
INDEX_TTL_S = 3600

OTA_ERRORS = {
    1: "lost one or more packets",
    2: "CRC error in data",
    3: "error writing data to flash",
    4: "lost the last packets",
    5: "timeout",
    6: "firmware CRC check failed",
}

_JOBS = "ota_jobs"
_INDEX = "ota_index"


# --- image handling (pure) -------------------------------------------------------------------------


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def image_crc(data: bytes) -> int:
    """Telink's image CRC: CRC-32 without the final inversion, i.e. the bitwise NOT of zlib's."""
    return zlib.crc32(data) ^ 0xFFFFFFFF


def validate_image(data: bytes) -> dict:
    """Check a Telink BLE OTA image; return {"ok", "size"} or {"ok": False, "error"}."""
    size = len(data)
    if size < 1024:
        return {"ok": False, "error": "image too small"}
    if struct.unpack_from("<I", data, 0)[0] == 0x0BEEF11E:
        return {"ok": False, "error": "Zigbee OTA images are not supported"}
    if size > MAX_OTA_SIZE:
        return {"ok": False, "error": f"image is {size} bytes; only up to {MAX_OTA_SIZE} (standard OTA) is supported"}
    if struct.unpack_from("<I", data, 0x08)[0] != 0x544C4E4B:  # "KNLT"
        return {"ok": False, "error": "not a Telink OTA image (bad header)"}
    hsize = struct.unpack_from("<I", data, 0x18)[0]
    if hsize > size or (hsize & 0x0F) != 4:
        return {"ok": False, "error": "invalid size field in the image header"}
    if image_crc(data[: hsize - 4]) != struct.unpack_from("<I", data, hsize - 4)[0]:
        return {"ok": False, "error": "image CRC mismatch"}
    if size - hsize > 1024:
        return {"ok": False, "error": "signed images are not supported"}
    return {"ok": True, "size": size}


def build_packets(data: bytes) -> list[bytes]:
    """Data blocks (idx + 16 B + crc16), padded with 0xFF to a whole block."""
    if len(data) % 16:
        data = data + b"\xff" * (16 - len(data) % 16)
    out = []
    for i in range(len(data) // 16):
        body = struct.pack("<H", i) + data[i * 16 : i * 16 + 16]
        out.append(body + struct.pack("<H", crc16_modbus(body)))
    return out


def end_packet(count: int) -> bytes:
    last = count - 1
    return b"\x02\xff" + struct.pack("<HH", last, ~last & 0xFFFF)


def version_str(v: int | None) -> str | None:
    """Firmware byte (0x59) or index version (89) -> "5.9"."""
    if v is None:
        return None
    return f"{(v >> 4) & 0x0F}.{v & 0x0F}"


# --- firmware index / download ---------------------------------------------------------------------


async def _async_index(hass: HomeAssistant) -> dict:
    dom = hass.data.setdefault(DOMAIN, {})
    cached = dom.get(_INDEX)
    if cached and time.monotonic() - cached["t"] < INDEX_TTL_S:
        return cached["data"]
    session = async_get_clientsession(hass)
    async with asyncio.timeout(20):
        async with session.get(FW_BASE_URL + FW_INDEX) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    dom[_INDEX] = {"t": time.monotonic(), "data": data}
    return data


def _pick_file(index: dict, hw_ver: int) -> str | None:
    files = index.get("custom") or []
    fn = files[hw_ver] if 0 <= hw_ver < len(files) else None
    if not fn or fn in ("?", "/"):
        return None
    return fn


async def _async_download(hass: HomeAssistant, fn: str) -> bytes:
    url = fn if fn.startswith("https:") else FW_BASE_URL + fn
    session = async_get_clientsession(hass)
    async with asyncio.timeout(60):
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


async def async_check(hass: HomeAssistant, mac: str) -> dict:
    """Read the device and the PVVX firmware index: what runs now, and what an update would flash."""
    mac = mac.upper()
    r = await gatt.async_read(hass, mac)
    if not r.get("ok"):
        return {"ok": False, "mac": mac, "error": r.get("error") or "read failed"}
    f = r["fields"]
    fw, hw = f.get("fw_byte"), f.get("hw_ver")
    out = {
        "ok": True,
        "mac": mac,
        "current": f.get("fw_version") or version_str(fw),
        "hw_ver": hw,
        "hw_name": f.get("hw_ver_name"),
    }
    if fw is None or fw < MIN_HW_LAYOUT_FW:
        return {**out, "ok": False, "error": "firmware too old to identify the board; use the web flasher"}
    try:
        index = await _async_index(hass)
    except Exception as e:  # noqa: BLE001
        return {**out, "ok": False, "error": f"could not load the PVVX firmware index: {e!r}"}
    fn = _pick_file(index, hw)
    if not fn:
        return {**out, "ok": False, "error": f"no PVVX firmware listed for hardware id {hw}"}
    latest = int(index["version"])
    return {**out, "latest": version_str(latest), "file": fn, "update_available": latest > fw, "_fields": f}


# --- the transfer ----------------------------------------------------------------------------------


def _jobs(hass: HomeAssistant) -> dict:
    return hass.data.setdefault(DOMAIN, {}).setdefault(_JOBS, {})


def status(hass: HomeAssistant, mac: str | None = None) -> dict:
    jobs = _jobs(hass)
    if mac:
        return dict(jobs.get(mac.upper()) or {"state": None})
    return {m: dict(j) for m, j in jobs.items()}


async def _transfer(client, packets: list[bytes], job: dict) -> None:
    char = client.services.get_characteristic(OTA_CHAR)
    if char is None:
        raise RuntimeError("no Telink OTA characteristic on this device")
    with_response = "write-without-response" not in char.properties

    async def send(p: bytes) -> None:
        await asyncio.wait_for(client.write_gatt_char(OTA_CHAR, p, response=with_response), timeout=8)

    await send(b"\x00\xff")
    await send(b"\x01\xff")
    await asyncio.sleep(0.3)
    total = len(packets)
    for i, p in enumerate(packets):
        await send(p)
        if (i + 1) % SYNC_EVERY == 0:
            st = bytes(await asyncio.wait_for(client.read_gatt_char(OTA_CHAR), timeout=8))
            if st and st[0]:
                raise RuntimeError(
                    f"device reported OTA error {st[0]} ({OTA_ERRORS.get(st[0], 'unknown')}) at block {i}"
                )
            job["sent"] = i + 1
            job["percent"] = round((i + 1) * 100 / total, 1)
    await send(end_packet(total))
    job["sent"] = total
    job["percent"] = 100.0
    await asyncio.sleep(1.0)  # the device verifies and reboots when the link drops


# Settings that identify a device's behaviour; if any of them changed across the update, put them back.
_KEEP = (
    "raw",
    "device_name",
    "adv_crypto",
    "bind_key",
    "comfort_t_lo",
    "comfort_t_hi",
    "comfort_h_lo",
    "comfort_h_hi",
    "t_slope",
    "h_slope",
    "t_offset_c",
    "h_offset_pct",
)


def settings_lost(before: dict, after: dict) -> list[str]:
    """Which kept settings differ after the update (hw byte 9 of the config is runtime, not compared)."""
    lost = []
    for k in _KEEP:
        a, b = before.get(k), after.get(k)
        if k == "raw" and a and b:
            a, b = a[:18] + a[20:], b[:18] + b[20:]
        if a is not None and a != b:
            lost.append(k)
    return lost


async def _restore_settings(hass: HomeAssistant, job: dict, mac: str, before: dict) -> None:
    snap = backups.snapshot_from_fields(hass, mac, before)
    parts = ["config", "device_name", "comfort", "bind_key", "sensor"]
    r = await gatt.async_restore(hass, mac, snap, parts)
    if not r.get("ok"):
        raise RuntimeError(f"restoring settings failed: {r.get('error') or r.get('parts')}")
    if before.get("adv_crypto"):
        # restore keeps the device's own encryption flag; turn it back on with the old key (syncs BTHome)
        await asyncio.sleep(3)
        e = await gatt.async_set_encryption(hass, mac, True, before.get("bind_key"))
        if not e.get("ok"):
            raise RuntimeError(f"re-enabling encryption failed: {e.get('error')}")


async def _run(
    hass: HomeAssistant, job: dict, mac: str, packets: list[bytes], want: str | None, before: dict | None = None
) -> None:
    job["state"] = "flashing"
    start = time.monotonic()

    async def fn(client):
        await _transfer(client, packets, job)
        return {"ok": True}

    try:
        # One attempt only: a retry would restart the transfer, which is the caller's decision.
        res = await gatt._with_client(hass, mac, fn, retries=1)
        job["elapsed_s"] = round(time.monotonic() - start, 1)
        if not res.get("ok"):
            job.update(state="failed", error=res.get("error") or "transfer failed")
            return
        job["state"] = "verifying"
        await asyncio.sleep(10)
        r = await gatt.async_read(hass, mac, retries=3)
        now = r.get("fields", {}).get("fw_version") if r.get("ok") else None
        job["after"] = now
        if now is None:
            job.update(state="unverified", error="transfer finished but the device could not be read back yet")
        elif want and now != want:
            job.update(state="failed", error=f"device still reports v{now} (CRC check on the device failed?)")
        else:
            lost = settings_lost(before, r["fields"]) if before else []
            job["settings_reset"] = lost
            if lost:
                job["state"] = "restoring"
                try:
                    await _restore_settings(hass, job, mac, before)
                except Exception as e:  # noqa: BLE001
                    job.update(
                        state="failed",
                        error=f"updated to v{now}, but {e}. Restore the pre-update snapshot from Snapshots.",
                    )
                    return
                job["settings_restored"] = True
            job["state"] = "done"
            try:
                await gatt.async_remember_ble_name(hass, mac, before.get("device_name") if before else None)
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        _LOGGER.warning("OTA for %s failed: %r", mac, e)
        job.update(state="failed", error=repr(e))
    finally:
        job["active"] = False


async def async_start(hass: HomeAssistant, mac: str, force: bool = False) -> dict:
    """Check, download, validate, then flash in the background. Poll status() for progress."""
    mac = mac.upper()
    jobs = _jobs(hass)
    if (jobs.get(mac) or {}).get("active"):
        return {"ok": False, "mac": mac, "error": "an update is already running for this device"}
    info = await async_check(hass, mac)
    before = info.pop("_fields", None)
    if not info.get("ok"):
        return info
    if not info["update_available"] and not force:
        return {**info, "ok": False, "error": f"already on v{info['current']} (latest v{info['latest']})"}
    try:
        image = await _async_download(hass, info["file"])
    except Exception as e:  # noqa: BLE001
        return {**info, "ok": False, "error": f"download failed: {e!r}"}
    v = validate_image(image)
    if not v["ok"]:
        return {**info, "ok": False, "error": v["error"]}
    packets = build_packets(image)
    if before:  # the pre-update state, kept in the device's snapshot history
        try:
            await backups.async_save(hass, backups.snapshot_from_fields(hass, mac, before))
        except Exception:  # noqa: BLE001
            _LOGGER.debug("pre-update snapshot of %s not saved", mac)
    job = {
        "active": True,
        "state": "starting",
        "mac": mac,
        "file": info["file"],
        "from": info["current"],
        "to": info["latest"],
        "blocks": len(packets),
        "sent": 0,
        "percent": 0.0,
        "error": None,
        "started": time.time(),
    }
    jobs[mac] = job
    snapshot = dict(job)  # the task may start eagerly and move the job on before we return
    hass.async_create_background_task(
        _run(hass, job, mac, packets, info["latest"], before), name=f"telink_manager_ota_{mac}"
    )
    return {**info, "ok": True, "job": snapshot}
