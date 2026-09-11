"""PVVX GATT layer over the HA core Bluetooth stack (proxy-based).

All BLE work runs in HA core so the ESPHome proxies are reachable. Every connect path
ends with a guaranteed, verified disconnect (_safe_disconnect) — otherwise the link
stays open and the thermometer goes silent / unreachable.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from . import backups, blethr, pvvx_struct
from .const import (
    CFG_CHAR,
    CMD_BIND_KEY,
    CMD_CFG,
    CMD_COMFORT,
    CMD_FACTORY_RESET,
    CMD_LCD,
    CMD_MAC,
    CMD_NAME,
    CMD_REBOOT,
    CMD_SENSOR,
    CMD_SENSOR_DEF,
    CMD_TIME,
    DIS_FW_REV,
    DIS_MODEL,
    DIS_SW_REV,
    DOMAIN,
    TELINK_PREFIX,
)

_LOGGER = logging.getLogger(__name__)


def _dev_lock(hass: HomeAssistant, mac: str) -> asyncio.Lock:
    """Per-device connection lock (server-side). Every connect path holds it, so there is NEVER
    more than one GATT link to the same thermometer at a time — across bulk reads, manual connects,
    multiple browser tabs, or an F5 race. A second simultaneous link wedges the device / goes silent."""
    locks = hass.data.setdefault(DOMAIN, {}).setdefault("dev_locks", {})
    mac = mac.upper()
    lock = locks.get(mac)
    if lock is None:
        lock = locks[mac] = asyncio.Lock()
    return lock


def _best_proxy(hass: HomeAssistant, addr: str) -> dict | None:
    """Which connectable scanner (ESPHome BLE proxy) would serve this MAC, and at what RSSI.

    Returns the strongest-signal connectable scanner, or None if none can reach it.
    """
    try:
        sdevs = bluetooth.async_scanner_devices_by_address(hass, addr, connectable=True)
    except Exception:  # noqa: BLE001
        return None
    best = None
    for sd in sdevs:
        scanner = getattr(sd, "scanner", None)
        name = getattr(scanner, "name", None) or getattr(scanner, "source", None)
        rssi = None
        adv = getattr(sd, "advertisement", None)
        if adv is not None:
            rssi = getattr(adv, "rssi", None)
        if best is None or (rssi is not None and (best.get("rssi") is None or rssi > best["rssi"])):
            best = {"name": name, "rssi": rssi}
    return best


def async_proxies(hass: HomeAssistant, addr: str) -> dict:
    """Diagnostic: every scanner (proxy) that currently sees this MAC, with connectable flag."""
    addr = addr.upper()
    out: list[dict] = []
    try:
        for sd in bluetooth.async_scanner_devices_by_address(hass, addr, connectable=False):
            scanner = getattr(sd, "scanner", None)
            adv = getattr(sd, "advertisement", None)
            out.append(
                {
                    "name": getattr(scanner, "name", None) or getattr(scanner, "source", None),
                    "source": getattr(scanner, "source", None),
                    "connectable": getattr(scanner, "connectable", None),
                    "rssi": getattr(adv, "rssi", None) if adv is not None else None,
                }
            )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "mac": addr, "error": repr(e)}
    out.sort(key=lambda d: -(d["rssi"] or -999))
    return {"ok": True, "mac": addr, "scanners": out}


def _scanner_kind(scanner) -> str:
    """Classify a HA Bluetooth scanner by where it comes from: esphome / shelly / local adapter / other.

    Decided from the class's module path, so no optional integration has to be imported here.
    """
    mod = f"{type(scanner).__module__}.{type(scanner).__name__}".lower()
    if "esphome" in mod:
        return "esphome"
    if "shelly" in mod:
        return "shelly"
    if mod.endswith(".hascanner") or "bluetooth.scanner" in mod or "habluetooth.scanner" in mod:
        return "local"
    return "other"


def async_coverage(hass: HomeAssistant) -> dict:
    """Coverage matrix: every scanner (local adapter / ESPHome or Shelly proxy) HA knows about, and which
    Telink thermometer each one sees at what RSSI. Pure cache read, no BLE traffic.

    Answers two questions the per-device Route column cannot: which proxies could still be brought in
    (passive ESPHome proxies that only need `active: true`), and which proxy sees which device how well.
    """
    # Internal accessor, but the HA Bluetooth integration's own diagnostics use the same one and it has
    # been stable across releases; every attribute below is read defensively so an older/newer
    # habluetooth only costs us a column, never the whole view.
    try:
        from homeassistant.components.bluetooth import _get_manager

        manager = _get_manager(hass)
        scanners = list(manager.async_current_scanners())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)}

    dom = hass.data.get(DOMAIN, {})
    names = dom.get("names", {})
    ble_names = dom.get("ble_names", {})
    proxies: list[dict] = []
    devices: dict[str, dict] = {}
    for sc in scanners:
        source = getattr(sc, "source", None) or ""
        kind = _scanner_kind(sc)
        connectable = bool(getattr(sc, "connectable", False))
        alloc = None
        try:
            a = manager.async_current_allocations(source) if source else None
            if a is not None:
                used = a.allocated
                used = len(used) if isinstance(used, (list, tuple, set)) else used
                alloc = {"slots": a.slots, "free": a.free, "used": used}
        except Exception:  # noqa: BLE001
            alloc = None
        try:
            age = sc.time_since_last_detection()
        except Exception:  # noqa: BLE001
            age = None
        mode = getattr(sc, "current_mode", None)
        mode = getattr(mode, "name", None) or (str(mode) if mode is not None else None)
        try:
            seen_map = dict(sc.discovered_devices_and_advertisement_data)
        except Exception:  # noqa: BLE001
            seen_map = {}
        seen = 0
        for addr, pair in seen_map.items():
            addr = (addr or "").upper()
            if not addr.startswith(TELINK_PREFIX):
                continue
            try:
                dev, adv = pair
            except Exception:  # noqa: BLE001
                continue
            rssi = getattr(adv, "rssi", None)
            d = devices.get(addr)
            if d is None:
                d = devices[addr] = {
                    "mac": addr,
                    "name": _clean_adv_name(getattr(dev, "name", None), addr) or ble_names.get(addr, ""),
                    "friend_name": names.get(addr, ""),
                    "ha_name": _ha_name(hass, addr),
                    "rssi": {},
                }
            d["rssi"][source] = rssi
            seen += 1
        proxies.append(
            {
                "source": source,
                "name": getattr(sc, "name", None) or source,
                "kind": kind,
                "connectable": connectable,
                # A passive ESPHome proxy becomes connectable with `bluetooth_proxy: active: true`;
                # Shelly proxies are advertisement-only by design and cannot be enabled.
                "activatable": kind == "esphome" and not connectable,
                "scanning": bool(getattr(sc, "scanning", True)),
                "mode": mode,
                "alloc": alloc,
                "age_s": round(age, 1) if isinstance(age, (int, float)) else None,
                "seen": seen,
            }
        )
    connectable_sources = {p["source"] for p in proxies if p["connectable"]}
    dev_list = []
    for d in devices.values():
        act = [r for s, r in d["rssi"].items() if s in connectable_sources and r is not None]
        anyr = [r for r in d["rssi"].values() if r is not None]
        d["best_active"] = max(act) if act else None
        d["best_any"] = max(anyr) if anyr else None
        dev_list.append(d)
    proxies.sort(key=lambda p: (not p["connectable"], not p["activatable"], (p["name"] or "").lower()))
    dev_list.sort(key=lambda d: (d["friend_name"] or d["ha_name"] or d["name"] or d["mac"]).lower())
    return {"ok": True, "proxies": proxies, "devices": dev_list}


_BTHOME_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"
_ESS_UUID = "0000181a-0000-1000-8000-00805f9b34fb"
# BTHome v2 object data lengths (bytes) — enough to walk objects to battery (0x01) / voltage (0x0C)
_BTHOME_LEN = {
    0x00: 1,
    0x01: 1,
    0x02: 2,
    0x03: 2,
    0x04: 3,
    0x05: 3,
    0x06: 2,
    0x07: 2,
    0x08: 2,
    0x09: 1,
    0x0A: 3,
    0x0B: 3,
    0x0C: 2,
    0x0D: 2,
    0x0E: 2,
    0x0F: 1,
    0x10: 1,
    0x11: 1,
    0x12: 2,
    0x13: 2,
    0x14: 2,
    0x2E: 1,
    0x2F: 1,
    0x3A: 1,
    0x3D: 2,
    0x3E: 4,
    0x3F: 2,
}


def _battery_from_adv(si) -> dict:
    """Battery % (and voltage if present) from the BLE advertisement — no connection needed.
    Supports BTHome v2 (unencrypted) and the pvvx/atc1441 0x181A custom formats."""
    sd = getattr(si, "service_data", None) or {}
    raw = sd.get(_BTHOME_UUID)
    if raw:
        b = bytes(raw)
        if b and not (b[0] & 0x01):  # bit0 of device-info byte = encrypted
            i, batt, volt = 1, None, None
            while i < len(b):
                ln = _BTHOME_LEN.get(b[i])
                if ln is None or i + 1 + ln > len(b):
                    break
                val = b[i + 1 : i + 1 + ln]
                if b[i] == 0x01:
                    batt = val[0]
                elif b[i] == 0x0C:
                    volt = int.from_bytes(val, "little") / 1000.0
                i += 1 + ln
            # many of these devices advertise battery as VOLTAGE (0x0C) instead of % (0x01)
            if batt is not None or volt is not None:
                return {"battery": batt, "battery_v": volt, "battery_src": "bthome"}
    raw = sd.get(_ESS_UUID)
    if raw:
        b = bytes(raw)
        if len(b) >= 15:  # pvvx custom: MAC6 temp2 hum2 mv2 batt1 cnt1 flags1
            return {"battery": b[12], "battery_v": int.from_bytes(b[10:12], "little") / 1000.0, "battery_src": "pvvx"}
        if len(b) >= 13:  # atc1441: MAC6 temp2(BE) hum1 batt1 mv2(BE) cnt1
            return {"battery": b[9], "battery_v": int.from_bytes(b[10:12], "big") / 1000.0, "battery_src": "atc"}
    return {"battery": None, "battery_v": None, "battery_src": None}


def _ha_name(hass: HomeAssistant, mac: str) -> str | None:
    """The user's own Home Assistant name for this thermometer (the device's name_by_user), matched
    by BLE MAC. None if there is no device or the user never set a name. Read-only, no BLE.

    ponytail: the scan only feeds Telink-OUI (A4:C1:38) MACs, so the matched device IS this
    thermometer — no extra device-type guard needed. Match both MAC cases: integrations store the
    bluetooth connection in upper or lower case (format_mac lower-cases; BTHome stores upper).

    A BLE connection stopped being unique across config entries, so the same thermometer can hold
    more than one device entry (its BTHome device, say, plus another integration's). `async_get_devices`
    is the lookup that admits this and returns all of them, which also lets us pick the entry that
    actually carries a user-given name instead of whichever one comes back first. It does not exist
    on the older cores this integration still supports, so those keep to the single-device lookup
    that is only deprecated on newer ones.
    """
    wanted = {(dr.CONNECTION_BLUETOOTH, mac), (dr.CONNECTION_BLUETOOTH, dr.format_mac(mac))}
    try:
        registry = dr.async_get(hass)
        get_all = getattr(registry, "async_get_devices", None)
        if get_all is not None:
            devices = get_all(connections=wanted)
        else:
            device = registry.async_get_device(connections=wanted)
            devices = [device] if device else []
    except Exception:  # noqa: BLE001
        return None
    for device in devices:
        if device.name_by_user:
            return device.name_by_user
    return None


def _clean_adv_name(raw: str | None, addr: str) -> str:
    """Advertised BLE name, or "" if the device doesn't advertise a real one.

    HA falls back to the MAC string when there is no Complete Local Name in the advertisement, and
    several PVVX adv formats omit it — so the "name" is often just the address. Treat that as no name;
    the panel then falls back to the real device name we read over GATT (the ble_names cache).
    """
    n = (raw or "").strip()
    if not n or n.upper().replace(":", "") == (addr or "").upper().replace(":", ""):
        return ""
    return n


async def async_remember_ble_name(hass: HomeAssistant, mac: str, name: str | None) -> None:
    """Persist (or, if empty, forget) a device's real BLE name in its own store, so the scan list can
    show it instead of the MAC even before the next connect — independent of the backup history."""
    mac = mac.upper()
    name = (name or "").strip()
    data = hass.data.get(DOMAIN, {})
    cache = data.setdefault("ble_names", {})
    if name:
        if cache.get(mac) == name:
            return
        cache[mac] = name
    else:
        if mac not in cache:
            return
        cache.pop(mac, None)
    store = data.get("ble_name_store")
    if store is not None:
        await store.async_save(cache)


def _known_repeater(hass: HomeAssistant, addr: str) -> bool:
    """Whether the last time we connected to this MAC, it turned out to be a repeater."""
    snaps = hass.data.get(DOMAIN, {}).get("backups", {}).get(addr.upper()) or []
    return bool(snaps) and backups.kind_of(snaps[-1]) == backups.KIND_REPEATER


async def async_scan(hass: HomeAssistant) -> list[dict]:
    """List discovered Telink (A4:C1:38) thermometers from the HA cache. Does NOT connect."""
    out: list[dict] = []
    ble_names = hass.data.get(DOMAIN, {}).get("ble_names", {})
    for si in bluetooth.async_discovered_service_info(hass, connectable=False):
        addr = (si.address or "").upper()
        if not addr.startswith(TELINK_PREFIX):
            continue
        proxy = _best_proxy(hass, addr)
        batt = _battery_from_adv(si)
        connectable = proxy is not None
        proxy_rssi = (proxy or {}).get("rssi")
        repeater = blethr.looks_like_blethr(si.name, addr) or _known_repeater(hass, addr)
        # A repeater publishes its name only in the scan response, which a passively scanning proxy
        # never asks for, so the advertised name can still be the one it carried before it was
        # reflashed. Prefer the name we read from the device itself when we have one.
        adv_name = _clean_adv_name(si.name, addr)
        if repeater:
            adv_name = ble_names.get(addr) or adv_name
        out.append(
            {
                "mac": addr,
                "name": adv_name,  # "" if it's just the MAC (no advertised name)
                # Two ways to spot a repeater without connecting, because neither alone is enough.
                # The firmware names itself "STH_" + MAC, but it publishes that in the scan response,
                # which only an actively scanning proxy ever asks for. Behind a passive proxy Home
                # Assistant keeps whatever name it knew before the device was reflashed, so the name
                # says nothing. What does survive is our own last snapshot of it.
                "blethr": repeater,
                "ha_name": _ha_name(hass, addr),  # the user's HA device name (name_by_user), if any
                # RSSI that matters for CONNECTING: the connectable proxy's signal when the device
                # is reachable, else the advertisement signal. This keeps RSSI consistent with the
                # Route (both describe the connectable path), so the weak-signal warning reflects the
                # link the connection will actually use — not a stronger passive-only sighting.
                "rssi": proxy_rssi if (connectable and proxy_rssi is not None) else si.rssi,
                "connectable": connectable,
                "proxy": (proxy or {}).get("name"),
                "battery": batt["battery"],
                "battery_v": batt["battery_v"],
                "battery_src": batt["battery_src"],
            }
        )
    out.sort(key=lambda d: (not d["connectable"], -(d["rssi"] or -999)))
    return out


async def _safe_disconnect(client) -> bool:
    """Guaranteed, checked disconnect: disconnect + wait until is_connected is False."""
    if client is None:
        return False
    for _ in range(4):
        try:
            if not client.is_connected:
                return True
            await asyncio.wait_for(client.disconnect(), timeout=10)
            await asyncio.sleep(1.5)
            if not client.is_connected:
                return True
        except Exception:  # noqa: BLE001
            await asyncio.sleep(1.0)
    return not client.is_connected


# The Device Information Service UUIDs now live in const.py, shared with the BLETHR reader.


async def _read_raw(client) -> bytes:
    """Request config (write 0x55) and read the stable 0x55 response.

    Response layout: 0x55 (opcode) + fw(1B) + 11B config (+ optional trailing 00).
    Requires two consecutive identical reads (data integrity) because the link can
    glitch right after connecting.
    """
    prev = None
    for _ in range(6):
        await asyncio.wait_for(client.write_gatt_char(CFG_CHAR, bytes([CMD_CFG]), response=False), timeout=6)
        await asyncio.sleep(1.2)
        raw = bytes(await asyncio.wait_for(client.read_gatt_char(CFG_CHAR), timeout=6))
        if len(raw) >= 13 and raw[0] == CMD_CFG:
            cur = raw[1:13]  # fw(1B) + 11B config
            if prev is not None and prev == cur:
                return raw
            prev = cur
        await asyncio.sleep(0.4)
    raise RuntimeError(f"no stable (matching) read; last={prev.hex() if prev else 'None'}")


async def _read_blob(client) -> bytes:
    """The 11-byte config blob (raw[2:13]) from the stable 0x55 response."""
    return (await _read_raw(client))[2:13]


async def _read_fw_info(client) -> dict:
    """Best-effort firmware info: 0x55 fw byte + standard DIS strings (never raises)."""
    info: dict = {}
    for key, uuid in (("fw_revision", DIS_FW_REV), ("sw_revision", DIS_SW_REV), ("model", DIS_MODEL)):
        try:
            v = bytes(await asyncio.wait_for(client.read_gatt_char(uuid), timeout=5))
            info[key] = v.decode("utf-8", "replace").replace("\x00", "").strip() or None
        except Exception:  # noqa: BLE001
            info[key] = None
    return info


async def _cmd(client, opcode: int, payload: bytes = b"", *, expect: bool = True, timeout: float = 6.0) -> bytes:
    """Write opcode+payload to 0x1F1F; return the response (echo opcode at [0]).

    PVVX command replies arrive as NOTIFICATIONS on the same characteristic (unlike the
    readable 0x55 config value), so we subscribe and wait for the matching opcode echo.
    """
    if not expect:
        await asyncio.wait_for(client.write_gatt_char(CFG_CHAR, bytes([opcode]) + payload, response=False), timeout=6)
        await asyncio.sleep(0.8)
        return b""

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def _cb(_char, data):
        d = bytes(data)
        if d and d[0] == opcode and not fut.done():
            fut.set_result(d)

    await client.start_notify(CFG_CHAR, _cb)
    try:
        await asyncio.sleep(0.2)
        await asyncio.wait_for(client.write_gatt_char(CFG_CHAR, bytes([opcode]) + payload, response=False), timeout=6)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raw = bytes(await asyncio.wait_for(client.read_gatt_char(CFG_CHAR), timeout=6))
            return raw if raw and raw[0] == opcode else b""
    finally:
        try:
            await client.stop_notify(CFG_CHAR)
        except Exception:  # noqa: BLE001
            pass


# Reply envelope is [0]=opcode echo, [1+]=payload (NO separate status byte — verified live).
async def _read_name(client) -> str | None:
    raw = await _cmd(client, CMD_NAME)
    if len(raw) >= 2 and raw[0] == CMD_NAME:
        return raw[1:].decode("utf-8", "replace").replace("\x00", "").strip() or None
    return None


async def _read_comfort(client) -> dict:
    raw = await _cmd(client, CMD_COMFORT)
    if len(raw) >= 9 and raw[0] == CMD_COMFORT:
        t_lo, t_hi, h_lo, h_hi = struct.unpack("<hhHH", raw[1:9])
        return {
            "comfort_t_lo": t_lo / 100,
            "comfort_t_hi": t_hi / 100,
            "comfort_h_lo": h_lo / 100,
            "comfort_h_hi": h_hi / 100,
        }
    return {}


async def _read_time(client) -> int | None:
    """Read the device clock (0x23 with no payload). Returns the stored unix seconds."""
    raw = await _cmd(client, CMD_TIME)  # no payload = query
    if len(raw) >= 5 and raw[0] == CMD_TIME:
        return struct.unpack("<I", raw[1:5])[0]
    return None


# Factory-default sensor coefficients per chip (val1_k, val1_z, val2_k, val2_z) from pvvx sensors.c.
# These are the datasheet conversion constants (NOT user gains): T = (raw*k)>>16 + z, z in 0.01°.
DEF_THCOEF = {
    1: (17500, -4500, 10000, 0),  # SHTC3
    2: (17500, -4500, 12500, -600),  # SHT4x
    3: (17500, -4500, 10000, 0),  # SHT30
    4: (16500, -4000, 10000, 0),  # CHT8305
    5: (1250, -5000, 625, 0),  # AHT2x
    6: (25606, 0, 20000, 0),  # CHT8215
}
SENSOR_NAMES = {1: "SHTC3", 2: "SHT4x", 3: "SHT30", 4: "CHT8305", 5: "AHT2x", 6: "CHT8215"}


def _decode_sensor(raw: bytes) -> dict:
    """0x25 reply = opcode + coef(12: Tk u32, Hk u32, Tz s16 x0.01C, Hz s16 x0.01%) + id(4) + i2c(1) + type(1).

    The coef holds the chip's datasheet constants, so we expose a user 'fine offset' = (z - factory_z).
    """
    out = {"sensor_raw": raw.hex(), "sensor_len": len(raw)}
    if len(raw) >= 13 and raw[0] == CMD_SENSOR:
        tk, hk, tz, hz = struct.unpack("<IIhh", raw[1:13])
        out.update({"t_slope": tk, "h_slope": hk, "t_offset_c": tz / 100, "h_offset_pct": hz / 100})
        if len(raw) >= 19:
            stype = raw[18]
            out["sensor_type"] = stype
            out["sensor_name"] = SENSOR_NAMES.get(stype, f"type {stype}")
            d = DEF_THCOEF.get(stype)
            if d:
                dk1, dz1, dk2, dz2 = d
                out["t_slope_default"] = dk1
                out["h_slope_default"] = dk2
                out["t_z_default_c"] = dz1 / 100
                out["h_z_default_pct"] = dz2 / 100
                out["t_fine_offset_c"] = round((tz - dz1) / 100, 2)
                out["h_fine_offset_pct"] = round((hz - dz2) / 100, 2)
                out["sensor_is_default"] = tk == dk1 and hk == dk2 and tz == dz1 and hz == dz2
    return out


async def _read_sensor(client) -> dict:
    return _decode_sensor(await _cmd(client, CMD_SENSOR))


async def _read_all_fields(client) -> dict:
    """Read the full PVVX state from an OPEN client: config + fw info + name + comfort + time +
    sensor + bind key. One read yields a complete backup snapshot. Used by async_read and, before
    overwriting, by async_restore (to capture the target's pre-write state)."""
    raw = await _read_raw(client)
    fields = pvvx_struct.parse(raw[2:13], raw[1])
    fields["fw_byte"] = raw[1]
    fields["fw_byte_hex"] = f"0x{raw[1]:02X}"
    # PVVX version byte: upper nibble = major, lower nibble = minor (e.g. 0x53 -> 5.3)
    fields["fw_version"] = f"{raw[1] >> 4}.{raw[1] & 0x0F}"
    fields.update(await _read_fw_info(client))
    try:
        fields["device_name"] = await _read_name(client)
    except Exception:  # noqa: BLE001
        fields["device_name"] = None
    try:
        fields.update(await _read_comfort(client))
    except Exception:  # noqa: BLE001
        pass
    try:
        fields["device_time"] = await _read_time(client)
    except Exception:  # noqa: BLE001
        fields["device_time"] = None
    try:
        fields.update(await _read_sensor(client))
    except Exception:  # noqa: BLE001
        pass
    try:
        fields["bind_key"] = _decode_bind_key(await _cmd(client, CMD_BIND_KEY))
    except Exception:  # noqa: BLE001
        fields["bind_key"] = None
    return fields


def _command_family(client) -> str | None:
    """Which command interface the connected device actually exposes.

    "blethr" for a BLE T&H repeater, "pvvx" for a thermometer, None when the service table shows
    neither — which in practice means the table is a stale cache from before a reflash.
    """
    if blethr.detect(client):
        return "blethr"
    try:
        if client.services.get_characteristic(CFG_CHAR) is not None:
            return "pvvx"
    except Exception:  # noqa: BLE001
        pass
    return None


async def async_read(hass: HomeAssistant, mac: str, retries: int = 3) -> dict:
    """Connect via proxy -> read config -> safe disconnect. Returns parsed fields."""
    mac = mac.upper()
    last_err = "no attempt"
    # Home Assistant caches each device's GATT table. Reflashing a device to different firmware moves
    # the command characteristic, and the cached table then points at a handle that no longer exists,
    # which surfaces as a bare "invalid handle". Detecting that once and rediscovering is cheaper than
    # making every caller guess.
    rediscover = False
    async with _dev_lock(hass, mac):
        for _ in range(retries):
            dev = bluetooth.async_ble_device_from_address(hass, mac, connectable=True)
            if dev is None:
                last_err = "no_connectable"
                await asyncio.sleep(5)
                continue
            client = None
            try:
                client = await asyncio.wait_for(
                    establish_connection(BleakClientWithServiceCache, dev, mac, use_services_cache=not rediscover),
                    timeout=25,
                )
                family = _command_family(client)
                # A stale table still lists the *old* firmware's characteristic, so "neither is
                # present" is not enough to catch it. The advertised name is: only BLETHR produces
                # "STH_" + MAC, so a device advertising that while the table offers no BLETHR
                # service is being described by a cache from before it was reflashed.
                expected = "blethr" if blethr.looks_like_blethr(getattr(dev, "name", None), mac) else None
                if not rediscover and (family is None or (expected and family != expected)):
                    last_err = f"cached GATT table looks stale (offers {family or 'nothing'})"
                    rediscover = True
                    try:
                        await client.clear_cache()
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                # Which firmware answered decides how to talk to it. A repeater carries its commands
                # on its own characteristic, so reading it as a thermometer only ever fails.
                if family == "blethr":
                    fields = await blethr.async_read_fields(client)
                    # A repeater that answers nothing is not a repeater. Reflashing back to the
                    # thermometer firmware leaves the cached table claiming otherwise, and reporting
                    # that as a successful read of an empty device is worse than admitting confusion.
                    if fields.get("scan_interval_ms") is None and not rediscover:
                        last_err = "device does not answer the repeater commands; rediscovering"
                        rediscover = True
                        try:
                            await client.clear_cache()
                        except Exception:  # noqa: BLE001
                            pass
                        continue
                    fields.update(await _read_fw_info(client))
                    await async_remember_ble_name(hass, mac, fields.get("device_name"))
                    return {"ok": True, "mac": mac, "firmware": "blethr", "fields": fields}
                fields = await _read_all_fields(client)
                return {"ok": True, "mac": mac, "firmware": "pvvx", "fields": fields}
            except Exception as e:  # noqa: BLE001
                last_err = repr(e)
                # Per-attempt failures are routine over BLE proxies (out of range / busy); log at
                # debug to avoid spamming the log during bulk reads. Final failure logged below.
                _LOGGER.debug("PVVX read attempt failed %s: %s", mac, last_err)
                # If talking to it failed at all, stop trusting the cached description of it. One
                # rediscovery costs a little time; believing a stale table costs every attempt.
                if not rediscover:
                    rediscover = True
                    try:
                        await client.clear_cache()
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                await _safe_disconnect(client)
            await asyncio.sleep(4)
    _LOGGER.warning("PVVX read failed for %s after retries: %s", mac, last_err)
    return {"ok": False, "mac": mac, "error": last_err}


async def async_write(hass: HomeAssistant, mac: str, changes: dict, retries: int = 3) -> dict:
    """Read-modify-write the green-tier fields, then verify (config bytes 0..8).

    Single connection: read current -> build new blob (validated) -> write -> read back
    -> compare bytes [0:9] (runtime bytes 9..10 are not compared). Guaranteed disconnect.
    """
    mac = mac.upper()
    async with _dev_lock(hass, mac):
        return await _async_write_locked(hass, mac, changes, retries)


async def _async_write_locked(hass: HomeAssistant, mac: str, changes: dict, retries: int) -> dict:
    last_err = "no attempt"
    for _ in range(retries):
        dev = bluetooth.async_ble_device_from_address(hass, mac, connectable=True)
        if dev is None:
            last_err = "no_connectable"
            await asyncio.sleep(5)
            continue
        client = None
        try:
            client = await asyncio.wait_for(establish_connection(BleakClientWithServiceCache, dev, mac), timeout=25)
            raw = await _read_raw(client)
            fw = raw[1]
            current = raw[2:13]
            try:
                target = pvvx_struct.build(current, changes, fw)
            except ValueError as ve:
                return {"ok": False, "mac": mac, "error": f"validation: {ve}"}
            if target == current:
                return {
                    "ok": True,
                    "mac": mac,
                    "unchanged": True,
                    "before": pvvx_struct.parse(current, fw),
                    "after": pvvx_struct.parse(current, fw),
                }
            await asyncio.wait_for(
                client.write_gatt_char(CFG_CHAR, bytes([CMD_CFG]) + target, response=False), timeout=6
            )
            await asyncio.sleep(1.5)
            after = await _read_blob(client)
            # compare all written bytes; byte[9]=hw_ver is read-only/runtime, excluded.
            # Guard the length first: a glitchy device could return a short read-back, and an
            # IndexError here would be misclassified as a connection error and trigger a retry.
            ok = len(after) >= 11 and bytes(after[0:9]) == bytes(target[0:9]) and after[10] == target[10]
            return {
                "ok": ok,
                "mac": mac,
                "verified": ok,
                "before": pvvx_struct.parse(current, fw),
                "after": pvvx_struct.parse(after, fw),
                "target_raw": target.hex(),
            }
        except Exception as e:  # noqa: BLE001
            last_err = repr(e)
            _LOGGER.debug("PVVX write attempt failed %s: %s", mac, last_err)
        finally:
            await _safe_disconnect(client)
        await asyncio.sleep(4)
    _LOGGER.warning("PVVX write failed for %s after retries: %s", mac, last_err)
    return {"ok": False, "mac": mac, "error": last_err}


async def _with_client(hass: HomeAssistant, mac: str, fn, retries: int = 2) -> dict:
    """Connect via proxy, run fn(client) -> dict, always safe-disconnect. Retries on failure."""
    mac = mac.upper()
    async with _dev_lock(hass, mac):
        return await _with_client_locked(hass, mac, fn, retries)


async def _with_client_locked(hass: HomeAssistant, mac: str, fn, retries: int) -> dict:
    last_err = "no attempt"
    for _ in range(retries):
        dev = bluetooth.async_ble_device_from_address(hass, mac, connectable=True)
        if dev is None:
            last_err = "no_connectable"
            await asyncio.sleep(5)
            continue
        client = None
        try:
            client = await asyncio.wait_for(establish_connection(BleakClientWithServiceCache, dev, mac), timeout=25)
            return await fn(client)
        except Exception as e:  # noqa: BLE001
            last_err = repr(e)
            _LOGGER.debug("PVVX cmd attempt failed %s: %s", mac, last_err)
        finally:
            await _safe_disconnect(client)
        await asyncio.sleep(4)
    _LOGGER.warning("PVVX cmd failed for %s after retries: %s", mac, last_err)
    return {"ok": False, "mac": mac, "error": last_err}


# A name write leaves the firmware waiting to restart; give it this long to reboot and start
# advertising again before the follow-up connection that puts the clock back.
NAME_REBOOT_SETTLE_S = 4.0


async def async_set_name(hass: HomeAssistant, mac: str, name: str, restore_ts: int | None = None) -> dict:
    """Set the device's stored BLE name (UTF-8, 1..20 B). Empty name resets to default.

    Writing the name makes the firmware restart when the link drops, and that restart wipes the
    device clock: measured live on a TS0201 Wing (fw v5.9) the clock fell from wall-clock time to a
    small counter, while a config (0x55), comfort (0x20) or LCD (0x22) write left it running.
    Writing the clock on the *same* connection does not help — the firmware acknowledges it and the
    pending restart throws it away anyway.

    So when `restore_ts` is given (unix seconds the display should show, already TZ-adjusted), the
    clock is written on a fresh connection once the device is back, advanced by however long the
    rename took. `clock_restored` says whether that write came back with the expected time; a clock
    that cannot be put back is reported, never fatal — the rename itself already succeeded.
    """
    name = (name or "").strip()
    data = name.encode("utf-8")[:20] if name else b"\x00"
    t0 = time.time()

    async def fn(client):
        raw = await _cmd(client, CMD_NAME, data)
        got = raw[1:].decode("utf-8", "replace").replace("\x00", "").strip() if len(raw) >= 2 else None
        ok = True if not name else (got == name)
        return {"ok": ok, "mac": mac.upper(), "verified": ok, "device_name": got}

    out = await _with_client(hass, mac, fn)
    if out.get("verified") and restore_ts is not None:
        try:
            await asyncio.sleep(NAME_REBOOT_SETTLE_S)
            want = int(restore_ts) + int(time.time() - t0)
            clock = await async_set_time(hass, mac, want)
            dev_t = clock.get("device_time")
            out["device_time"] = dev_t
            out["clock_restored"] = dev_t is not None and abs(dev_t - want) <= 60
        except Exception as e:  # noqa: BLE001
            out["clock_restored"] = False
            out["clock_error"] = repr(e)
    return out


async def async_set_comfort(hass: HomeAssistant, mac: str, t_lo: float, t_hi: float, h_lo: float, h_hi: float) -> dict:
    """Set comfort thresholds (°C / %RH). Stored as int16/uint16 × 0.01, little-endian."""
    if t_lo >= t_hi or h_lo >= h_hi:
        return {"ok": False, "mac": mac.upper(), "error": "validation: low must be < high"}
    if not (-40 <= t_lo <= 125 and -40 <= t_hi <= 125 and 0 <= h_lo <= 100 and 0 <= h_hi <= 100):
        return {"ok": False, "mac": mac.upper(), "error": "validation: out of range"}
    payload = struct.pack("<hhHH", round(t_lo * 100), round(t_hi * 100), round(h_lo * 100), round(h_hi * 100))

    async def fn(client):
        raw = await _cmd(client, CMD_COMFORT, payload)
        res = {}
        if len(raw) >= 9:
            a, b, c, d = struct.unpack("<hhHH", raw[1:9])
            res = {"comfort_t_lo": a / 100, "comfort_t_hi": b / 100, "comfort_h_lo": c / 100, "comfort_h_hi": d / 100}
        ok = (
            res.get("comfort_t_lo") == t_lo
            and res.get("comfort_t_hi") == t_hi
            and res.get("comfort_h_lo") == h_lo
            and res.get("comfort_h_hi") == h_hi
        )
        return {"ok": ok, "mac": mac.upper(), "verified": ok, **res}

    return await _with_client(hass, mac, fn)


async def async_set_time(hass: HomeAssistant, mac: str, ts: int) -> dict:
    """Set the device clock. ts = unix seconds the LCD should display (already TZ-adjusted)."""
    payload = struct.pack("<I", int(ts) & 0xFFFFFFFF)

    async def fn(client):
        raw = await _cmd(client, CMD_TIME, payload)
        device_time = struct.unpack("<I", raw[1:5])[0] if len(raw) >= 5 else None
        return {"ok": True, "mac": mac.upper(), "sent": int(ts), "device_time": device_time}

    return await _with_client(hass, mac, fn)


async def async_lcd(
    hass: HomeAssistant, mac: str, big_number: int, small_number: int = 0, vtime_sec: int = 10, flg: int = 0
) -> dict:
    """Show a temporary number overlay on the LCD (external_data_t).

    Layout (LYWSD03MMC): big_number s16 (x0.1) + small_number s16 (x1) + vtime_sec u16 + flg u8.
    vtime_sec=0 clears, 0xFFFF = permanent. big_number is the already-x10 value.
    """
    bn = max(-32768, min(32767, int(big_number)))
    sn = max(-32768, min(32767, int(small_number)))
    payload = struct.pack("<hhHB", bn, sn, int(vtime_sec) & 0xFFFF, int(flg) & 0xFF)

    async def fn(client):
        await asyncio.wait_for(client.write_gatt_char(CFG_CHAR, bytes([CMD_LCD]) + payload, response=False), timeout=6)
        await asyncio.sleep(0.8)
        # A round-trip read after a write-without-response makes the link close cleanly on the
        # ESPHome proxy; without it the connection can linger (device stays "connected").
        try:
            await asyncio.wait_for(client.read_gatt_char(CFG_CHAR), timeout=6)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "mac": mac.upper()}

    return await _with_client(hass, mac, fn)


async def async_set_sensor(
    hass: HomeAssistant, mac: str, t_slope: int, t_offset_c: float, h_slope: int, h_offset_pct: float
) -> dict:
    """Write sensor calibration (CMD 0x25): T = RegT*Tk/65536 + Tz, H = RegH*Hk/65536 + Hz."""
    if not (1 <= int(t_slope) <= 0xFFFFFFFF and 1 <= int(h_slope) <= 0xFFFFFFFF):
        return {"ok": False, "mac": mac.upper(), "error": "validation: slope out of range"}
    payload = struct.pack("<IIhh", int(t_slope), int(h_slope), round(t_offset_c * 100), round(h_offset_pct * 100))

    async def fn(client):
        raw = await _cmd(client, CMD_SENSOR, payload)
        res = _decode_sensor(raw)
        ok = (
            res.get("t_slope") == int(t_slope)
            and res.get("h_slope") == int(h_slope)
            and res.get("t_offset_c") == round(t_offset_c, 2)
            and res.get("h_offset_pct") == round(h_offset_pct, 2)
        )
        return {"ok": ok, "mac": mac.upper(), "verified": ok, **res}

    return await _with_client(hass, mac, fn)


async def async_sensor_default(hass: HomeAssistant, mac: str) -> dict:
    """Restore factory-default sensor calibration (CMD 0x26)."""

    async def fn(client):
        raw = await _cmd(client, CMD_SENSOR_DEF)
        res = _decode_sensor(raw) if (raw and raw[0] == CMD_SENSOR) else {}
        return {"ok": True, "mac": mac.upper(), **res}

    return await _with_client(hass, mac, fn)


async def async_raw_cmd(hass: HomeAssistant, mac: str, hex_str: str, expect_reply: bool = True) -> dict:
    """EXPERIMENTAL: send raw bytes to char 0x1F1F (byte[0]=opcode). Returns the reply hex.

    Wrong commands can misconfigure or brick the device. Advanced users only.
    """
    try:
        data = bytes.fromhex((hex_str or "").replace(" ", "").replace(":", "").replace("0x", ""))
    except ValueError:
        return {"ok": False, "mac": mac.upper(), "error": "invalid hex"}
    if not data:
        return {"ok": False, "mac": mac.upper(), "error": "empty command"}

    async def fn(client):
        if expect_reply:
            raw = await _cmd(client, data[0], data[1:], expect=True)
            reply = raw.hex() if raw else None
        else:
            await _cmd(client, data[0], data[1:], expect=False)
            try:  # round-trip read so a write-without-response closes the link cleanly
                await asyncio.wait_for(client.read_gatt_char(CFG_CHAR), timeout=6)
            except Exception:  # noqa: BLE001
                pass
            reply = None
        return {"ok": True, "mac": mac.upper(), "sent": data.hex(), "reply": reply}

    return await _with_client(hass, mac, fn)


# ---------------------------------------------------------------------------
# Dangerous commands. Each can make HA lose the device; the panel guards them
# with explicit confirmations. The MAC/key are transmitted little-endian on the
# wire (PVVX firmware convention), reversed back for display.
# ---------------------------------------------------------------------------
def _decode_mac(raw: bytes) -> str | None:
    """0x10 reply = opcode + len byte (0x08) + 6 MAC bytes (LE on the wire) + 2 random bytes.

    The 6 MAC bytes are stored little-endian, so reverse them for display.
    """
    if len(raw) >= 8 and raw[0] == CMD_MAC:
        return ":".join(f"{b:02X}" for b in raw[2:8][::-1])
    return None


def _parse_mac(text: str) -> list[int] | None:
    """Parse 'AA:BB:CC:DD:EE:FF' (':' or '-' separated) into 6 ints, or None if invalid."""
    try:
        parts = [int(x, 16) for x in (text or "").replace("-", ":").split(":") if x != ""]
    except ValueError:
        return None
    if len(parts) != 6 or any(not 0 <= b <= 255 for b in parts):
        return None
    return parts


async def async_get_mac(hass: HomeAssistant, mac: str) -> dict:
    """Read the MAC address the device currently has stored (command 0x10, no payload).

    Safe, read-only — also confirms the wire byte order against the known address.
    """

    async def fn(client):
        raw = await _cmd(client, CMD_MAC)
        got = _decode_mac(raw)
        return {"ok": got is not None, "mac": mac.upper(), "device_mac": got, "raw": raw.hex() if raw else None}

    return await _with_client(hass, mac, fn)


async def async_set_mac(hass: HomeAssistant, mac: str, new_mac: str) -> dict:
    """Set a custom MAC address (command 0x10, 6 B). Takes effect after a reboot.

    HA sees the device as a brand-new MAC afterwards: history/entities under the old
    address are orphaned. Verified by reading the stored MAC back from the echo.
    """
    parts = _parse_mac(new_mac)
    if parts is None:
        return {"ok": False, "mac": mac.upper(), "error": "validation: MAC must be 6 hex bytes (AA:BB:CC:DD:EE:FF)"}
    # Wire format: [len=0x06][6 MAC bytes little-endian]. Stored LE, so reverse the display order.
    payload = bytes([0x06]) + bytes(parts[::-1])
    want = ":".join(f"{b:02X}" for b in parts)

    async def fn(client):
        raw = await _cmd(client, CMD_MAC, payload)
        got = _decode_mac(raw)
        ok = got is not None and got.upper() == want
        return {
            "ok": ok,
            "mac": mac.upper(),
            "verified": ok,
            "device_mac": got,
            "new_mac": want,
            "note": "New MAC takes effect automatically when the connection drops "
            "(the firmware reboots on disconnect); HA then sees it as a new device.",
        }

    return await _with_client(hass, mac, fn)


def _decode_bind_key(raw: bytes) -> str | None:
    """0x18 reply = opcode echo + 16 key bytes (some firmwares omit the key for privacy)."""
    if len(raw) >= 17 and raw[0] == CMD_BIND_KEY:
        return raw[1:17].hex()
    return None


async def async_get_bind_key(hass: HomeAssistant, mac: str) -> dict:
    """Read the current encryption bind key (command 0x18, no payload). Read-only.

    Firmware replies [0x18][16 key bytes] when a key is stored, or [0x18][0xFF] (2 bytes)
    when none is set — we distinguish "no key" from a short/garbled read via no_key/key_set.
    """

    async def fn(client):
        raw = await _cmd(client, CMD_BIND_KEY)
        ack = bool(raw) and raw[0] == CMD_BIND_KEY
        key = _decode_bind_key(raw)  # 17-byte reply -> hex, otherwise None
        no_key = ack and len(raw) == 2 and raw[1] == 0xFF
        return {
            "ok": ack,
            "mac": mac.upper(),
            "bind_key": key,
            "key_set": key is not None,
            "no_key": no_key,
            "raw": raw.hex() if raw else None,
        }

    return await _with_client(hass, mac, fn)


async def async_set_bind_key(hass: HomeAssistant, mac: str, key_hex: str) -> dict:
    """Set the encryption bind key (command 0x18, exactly 16 B AES-128).

    For encrypted advertising: HA can only decode with the matching key configured.
    """
    try:
        key = bytes.fromhex((key_hex or "").replace(" ", "").replace(":", "").replace("0x", ""))
    except ValueError:
        return {"ok": False, "mac": mac.upper(), "error": "validation: invalid hex"}
    if len(key) != 16:
        return {
            "ok": False,
            "mac": mac.upper(),
            "error": "validation: bind key must be exactly 16 bytes (32 hex chars)",
        }

    async def fn(client):
        raw = await _cmd(client, CMD_BIND_KEY, key)
        got = _decode_bind_key(raw)
        accepted = bool(raw) and raw[0] == CMD_BIND_KEY  # command acknowledged
        verified = got is not None and got.lower() == key.hex().lower()
        return {"ok": accepted, "mac": mac.upper(), "verified": verified, "bind_key": got, "new_key": key.hex()}

    return await _with_client(hass, mac, fn)


async def async_factory_reset(hass: HomeAssistant, mac: str) -> dict:
    """Reset ALL configuration to firmware defaults (command 0x56).

    The reply is the fresh config; we read it back to confirm and report the new state.
    The defaults may use a non-BTHome adv format, in which case HA loses the device until
    it is reconfigured.
    """

    async def fn(client):
        await asyncio.wait_for(client.write_gatt_char(CFG_CHAR, bytes([CMD_FACTORY_RESET]), response=False), timeout=6)
        await asyncio.sleep(1.5)
        config_after = None
        try:
            raw = await _read_raw(client)  # fresh 0x55 config after reset
            config_after = pvvx_struct.parse(raw[2:13], raw[1])
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "mac": mac.upper(), "config_after": config_after}

    return await _with_client(hass, mac, fn)


async def async_reboot(hass: HomeAssistant, mac: str) -> dict:
    """Reboot the device (command 0x72). The reboot is executed when the link drops,
    so we send it and let the guaranteed _safe_disconnect trigger it."""

    async def fn(client):
        await asyncio.wait_for(client.write_gatt_char(CFG_CHAR, bytes([CMD_REBOOT]), response=False), timeout=6)
        await asyncio.sleep(0.8)
        return {"ok": True, "mac": mac.upper(), "note": "Reboot executes on disconnect; the device returns shortly."}

    return await _with_client(hass, mac, fn)


async def _restore_repeater(hass: HomeAssistant, mac: str, snapshot: dict, parts: list[str]) -> dict:
    """Restore a repeater snapshot: its radio config, the thermometer it repeats, and that key."""

    async def fn(client):
        before_fields = None
        try:
            before_fields = await blethr.async_read_fields(client)
        except Exception:  # noqa: BLE001
            pass

        changes: dict = {}
        if "config" in parts and snapshot.get("raw"):
            cfg = blethr.parse_cfg(bytes.fromhex(snapshot["raw"]))
            changes.update(
                {
                    k: cfg[k]
                    for k in ("temp_F", "rf_tx_power", "scan_interval_ms", "scan_window_min_ms", "scan_window_max_ms")
                    if k in cfg
                }
            )
        if "ext_mac" in parts and snapshot.get("ext_mac"):
            changes["ext_mac"] = snapshot["ext_mac"]
        if "ext_bind_key" in parts and snapshot.get("ext_bind_key"):
            changes["ext_bind_key"] = snapshot["ext_bind_key"]
        if not changes:
            return {"ok": False, "mac": mac, "error": "nothing to restore from this snapshot"}
        try:
            written = await blethr.async_apply(client, before_fields or {}, changes)
        except ValueError as e:
            return {"ok": False, "mac": mac, "error": str(e)}
        verified = [v for k, v in written.items() if not k.endswith("_after")]
        return {
            "ok": bool(verified) and all(verified),
            "mac": mac,
            "parts": {k: v for k, v in written.items() if not k.endswith("_after")},
            "before_fields": before_fields,
        }

    return await _with_blethr_client(hass, mac, fn)


async def async_restore(hass: HomeAssistant, target_mac: str, snapshot: dict, parts: list[str]) -> dict:
    """Restore selected parts of a snapshot onto target_mac, in one connection.

    `parts` is any subset of: config, device_name, comfort, bind_key, sensor for a thermometer, or
    config, ext_mac, ext_bind_key for a repeater. The MAC is never restored/cloned. Each part is
    written then verified; per-part ok flags are returned.

    A snapshot is only ever written back onto the same kind of device it came from. The two kinds
    store different things under the same keys — an 11-byte thermometer config against an 8-byte
    repeater one — so crossing them would push meaningless bytes into a working device.
    """
    mac = target_mac.upper()
    raw_hex = snapshot.get("raw")
    comfort = snapshot.get("comfort") or {}
    sensor = snapshot.get("sensor") or {}
    snap_kind = backups.kind_of(snapshot)
    if snap_kind == backups.KIND_REPEATER:
        return await _restore_repeater(hass, mac, snapshot, parts)

    async def fn(client):
        out: dict = {}
        if blethr.detect(client):
            return {
                "ok": False,
                "mac": mac,
                "error": "kind_mismatch: this snapshot is from a thermometer, the target is a repeater",
            }

        # Safety: capture the target's CURRENT full state before overwriting anything, so the
        # caller can save it as a backup (makes every restore/clone reversible — even onto a
        # device that had no backup yet). Same connection, so no extra BLE link.
        try:
            before_fields = await _read_all_fields(client)
        except Exception:  # noqa: BLE001
            before_fields = None

        if "config" in parts and raw_hex:
            blob = bytes.fromhex(raw_hex)[:11]
            await asyncio.wait_for(client.write_gatt_char(CFG_CHAR, bytes([CMD_CFG]) + blob, response=False), timeout=6)
            await asyncio.sleep(1.5)
            after = await _read_blob(client)
            # byte[9]=hw_ver excluded (read-only/runtime); guard length to avoid an IndexError
            # that would abort the remaining restore parts (name/comfort/bind_key/sensor).
            out["config"] = len(after) >= 11 and bytes(after[0:9]) == bytes(blob[0:9]) and after[10] == blob[10]

        if "device_name" in parts:
            nm = (snapshot.get("device_name") or "").strip()
            raw = await _cmd(client, CMD_NAME, nm.encode("utf-8")[:20] if nm else b"\x00")
            got = raw[1:].decode("utf-8", "replace").replace("\x00", "").strip() if len(raw) >= 2 else None
            out["device_name"] = True if not nm else (got == nm)

        if "comfort" in parts and comfort.get("t_lo") is not None:
            payload = struct.pack(
                "<hhHH",
                round(comfort["t_lo"] * 100),
                round(comfort["t_hi"] * 100),
                round(comfort["h_lo"] * 100),
                round(comfort["h_hi"] * 100),
            )
            raw = await _cmd(client, CMD_COMFORT, payload)
            out["comfort"] = len(raw) >= 9 and raw[0] == CMD_COMFORT

        if "bind_key" in parts and snapshot.get("bind_key"):
            try:
                key = bytes.fromhex(snapshot["bind_key"])
            except ValueError:
                key = b""
            if len(key) == 16:
                raw = await _cmd(client, CMD_BIND_KEY, key)
                out["bind_key"] = bool(raw) and raw[0] == CMD_BIND_KEY

        if "sensor" in parts and sensor.get("t_slope"):
            payload = struct.pack(
                "<IIhh",
                int(sensor["t_slope"]),
                int(sensor["h_slope"]),
                round(sensor["t_offset_c"] * 100),
                round(sensor["h_offset_pct"] * 100),
            )
            raw = await _cmd(client, CMD_SENSOR, payload)
            out["sensor"] = bool(raw) and raw[0] == CMD_SENSOR

        return {"ok": all(out.values()) if out else False, "mac": mac, "parts": out, "before_fields": before_fields}

    return await _with_client(hass, mac, fn)


async def async_services(hass: HomeAssistant, mac: str, fresh: bool = True) -> dict:
    """Diagnostic: the GATT service/characteristic table the device actually exposes.

    Answers "what firmware is really on this thing" when a read fails for reasons the error text
    does not explain, and shows at a glance whether Home Assistant is holding a stale table.
    """
    mac = mac.upper()
    async with _dev_lock(hass, mac):
        dev = bluetooth.async_ble_device_from_address(hass, mac, connectable=True)
        if dev is None:
            return {"ok": False, "mac": mac, "error": "no_connectable"}
        if fresh:
            # Dropping the cache only takes effect on the next connection: the services of the
            # connection that clears it were already fetched from the old copy. So do it on a
            # throwaway link first, otherwise this view reports the very staleness it exists to find.
            primer = None
            try:
                primer = await asyncio.wait_for(establish_connection(BleakClientWithServiceCache, dev, mac), timeout=25)
                await primer.clear_cache()
            except Exception:  # noqa: BLE001
                pass
            finally:
                await _safe_disconnect(primer)
        client = None
        try:
            client = await asyncio.wait_for(
                establish_connection(BleakClientWithServiceCache, dev, mac, use_services_cache=not fresh),
                timeout=25,
            )
            out = []
            for service in client.services:
                chars = []
                for char in service.characteristics:
                    chars.append(
                        {
                            "uuid": str(char.uuid),
                            "handle": getattr(char, "handle", None),
                            "properties": list(getattr(char, "properties", ()) or ()),
                        }
                    )
                out.append({"uuid": str(service.uuid), "handle": getattr(service, "handle", None), "chars": chars})
            return {
                "ok": True,
                "mac": mac,
                "adv_name": getattr(dev, "name", None),
                "family": _command_family(client),
                "services": out,
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "mac": mac, "error": repr(e)}
        finally:
            await _safe_disconnect(client)


async def _with_blethr_client(hass: HomeAssistant, mac: str, fn, retries: int = 2) -> dict:
    """Connect, insist the device really is a repeater, then run fn(client).

    Refusing up front matters more here than elsewhere: the repeater commands share opcodes with the
    thermometer ones, so sending them to the wrong device would not fail cleanly.
    """
    mac = mac.upper()
    last_err = "no attempt"
    rediscover = False
    async with _dev_lock(hass, mac):
        for _ in range(retries + 1):
            dev = bluetooth.async_ble_device_from_address(hass, mac, connectable=True)
            if dev is None:
                last_err = "no_connectable"
                await asyncio.sleep(5)
                continue
            client = None
            try:
                client = await asyncio.wait_for(
                    establish_connection(BleakClientWithServiceCache, dev, mac, use_services_cache=not rediscover),
                    timeout=25,
                )
                if not blethr.detect(client):
                    if not rediscover:  # maybe just a stale table from before it was reflashed
                        rediscover = True
                        try:
                            await client.clear_cache()
                        except Exception:  # noqa: BLE001
                            pass
                        continue
                    return {
                        "ok": False,
                        "mac": mac,
                        "error": "kind_mismatch: this device is a thermometer, not a BLE T&H repeater",
                    }
                return await fn(client)
            except Exception as e:  # noqa: BLE001
                last_err = repr(e)
                _LOGGER.debug("BLETHR cmd attempt failed %s: %s", mac, last_err)
            finally:
                await _safe_disconnect(client)
            await asyncio.sleep(4)
    _LOGGER.warning("BLETHR cmd failed for %s after retries: %s", mac, last_err)
    return {"ok": False, "mac": mac, "error": last_err}


async def async_blethr_write(hass: HomeAssistant, mac: str, current: dict, changes: dict) -> dict:
    """Apply changes to a repeater and read the whole device back, so the caller sees the result."""

    target = (changes.get("ext_mac") or "").upper()
    if target and target == mac.upper():
        return {"ok": False, "mac": mac.upper(), "error": "a repeater cannot repeat itself"}

    async def fn(client):
        # The config goes to the device as one struct, so every field not being changed is sent back
        # as it is. Read that from the device rather than trusting what the caller last saw: a stale
        # or empty snapshot would otherwise be written back over settings nobody touched.
        try:
            base = await blethr.async_read_fields(client)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "mac": mac.upper(), "error": f"could not read the device: {e!r}; nothing written"}
        if any(base.get(k) is None for k in blethr.CFG_KEYS):
            # Falling back to whatever the caller last saw is how a stale or empty snapshot ends up
            # being written over settings nobody touched. Better to write nothing at all.
            return {
                "ok": False,
                "mac": mac.upper(),
                "error": "could not read the current configuration; nothing written",
            }
        try:
            written = await blethr.async_apply(client, base, changes)
        except ValueError as e:
            return {"ok": False, "mac": mac.upper(), "error": str(e)}
        fields = await blethr.async_read_fields(client)
        fields.update(await _read_fw_info(client))
        verified = [v for k, v in written.items() if not k.endswith("_after")]
        return {
            "ok": bool(verified) and all(verified),
            "mac": mac.upper(),
            "written": written,
            "fields": fields,
        }

    return await _with_blethr_client(hass, mac, fn)


async def async_blethr_reboot(hass: HomeAssistant, mac: str) -> dict:
    """Restart a repeater (it acts on the reboot once the link drops)."""

    async def fn(client):
        await blethr.async_reboot(client)
        return {"ok": True, "mac": mac.upper()}

    return await _with_blethr_client(hass, mac, fn)


async def async_blethr_wake(hass: HomeAssistant, mac: str) -> dict:
    """Start a repeater searching for its source again, without restarting it.

    The firmware gives up on a source that has been silent long enough and stops scanning. Nothing
    brings it back on its own: not the source returning, not time passing. What does is the end of
    any connection, because the disconnect handler restarts the search from the beginning. So
    connecting and letting go is a remote recovery, and it costs the device a single search rather
    than a reboot, which would also lose its uptime and its running averages.

    The fields come back with it so the caller can see what the device will now look for, and
    whether it is configured to look at all: a repeater with no interval set is not stuck, it is
    switched off, and waking it would change nothing.
    """

    async def fn(client):
        fields = await blethr.async_read_fields(client)
        return {
            "ok": True,
            "mac": mac.upper(),
            "ext_mac": fields.get("ext_mac"),
            "scanning": fields.get("scanning"),
            "fields": fields,
        }

    # The search restarts when this returns and the connection is dropped, not while it is open:
    # the firmware stops scanning for as long as a client is connected.
    return await _with_blethr_client(hass, mac, fn)
