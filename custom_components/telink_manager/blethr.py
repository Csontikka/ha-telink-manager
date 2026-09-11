"""PVVX BLETHR support: the "BLE T&H repeater" firmware.

A BLETHR device is not a thermometer of its own. It listens for another thermometer's BTHome
advertisement, shows that reading on its own screen and re-broadcasts it, which is why it turns up
in the scan list looking like any other Telink device.

It speaks the same command framing as the thermometer firmware, `[opcode][payload]` in and
`[opcode][data]` back, and shares most opcodes. Two things differ and both matter here:

  * the commands live on the SPP-style characteristic 0xFFE1, not on 0x1F1F, which is why a plain
    read against one of these fails with an invalid-handle error, and
  * replies arrive as notifications rather than as a read of the characteristic just written.

Reference: https://github.com/pvvx/BLETHR (source/cmd_parser.h, source/app.h) and the vendor tool at
https://pvvx.github.io/blethr/blethr.html
"""

from __future__ import annotations

import asyncio
import logging
import struct

from .const import (
    BLETHR_CHAR,
    BLETHR_NAME_PREFIX,
    BLETHR_SERVICE,
    CMD_CFG,
    CMD_DEV_ID,
    CMD_EXT_BIND_KEY,
    CMD_EXT_MAC,
    CMD_MAC,
    CMD_REBOOT,
    CMD_TIME,
    GAP_DEVICE_NAME,
    SERVICE_EXTENDED,
)

_LOGGER = logging.getLogger(__name__)

# Radio power, as the vendor tool offers it: the register value the firmware stores, against the
# transmit power it produces. Only this band is offered, which is the one the firmware defaults into.
RF_TX_DBM = {
    191: "+3.01",
    189: "+2.81",
    187: "+2.61",
    185: "+2.39",
    182: "+1.99",
    180: "+1.73",
    178: "+1.45",
    176: "+1.17",
    174: "+0.90",
    172: "+0.58",
    169: "+0.04",
    168: "-0.14",
    164: "-0.97",
    162: "-1.42",
    160: "-1.89",
    158: "-2.48",
    156: "-3.03",
    154: "-3.61",
    152: "-4.26",
    150: "-5.03",
    148: "-5.81",
    146: "-6.67",
    144: "-7.65",
    142: "-8.65",
    140: "-9.89",
    138: "-11.4",
    136: "-13.29",
    134: "-15.88",
    132: "-19.27",
    130: "-25.18",
}
_RF_TX_MIN, _RF_TX_MAX = 130, 191

# The scan windows are stored in units of 8 us, so a stored 1250 is 10 ms. Everything this module
# exposes is in milliseconds; only the wire format uses the raw units.
SCAN_TICKS_PER_MS = 125
WINDOW_MS_MIN, WINDOW_MS_MAX = 5.0, 50.0
# The firmware clamps a non-zero scan interval into this range (see test_config in the pvvx source),
# and treats zero as "do not scan at all" rather than as a fast setting.
SCAN_INTERVAL_MS_MIN, SCAN_INTERVAL_MS_MAX = 3000, 10000

# Everything the 8-byte config struct carries. All of it has to be known before any of it is written.
CFG_KEYS = ("temp_F", "rf_tx_power", "scan_interval_ms", "scan_window_min_ms", "scan_window_max_ms")

# What the firmware writes into a fresh device: °C, +0.04 dBm, scanning off, 10..20 ms windows.
DEFAULTS = {
    "temp_F": False,
    "rf_tx_power": 169,
    "scan_interval_ms": 0,
    "scan_window_min_ms": 10.0,
    "scan_window_max_ms": 20.0,
}

# Bits of the `services` word that this firmware can report. Only the ones it actually builds are
# listed; the rest stay out so an unknown bit shows up as unknown instead of silently mislabelled.
_SERVICE_BITS = {
    0x00000001: "ota",
    0x00000004: "pincode",
    0x00000020: "screen",
    0x00000080: "th_sensor",
    0x00001000: "time_adjust",
    SERVICE_EXTENDED: "scan_device",
}


def looks_like_blethr(name: str | None, mac: str) -> bool:
    """Whether an advertised name is the one BLETHR builds for this MAC.

    The firmware writes "STH_" plus the last three MAC bytes and exposes no way to rename itself, so
    a match is a strong hint. It stays a hint: the thermometer firmware *can* be renamed, so anything
    acted on is confirmed over GATT by `detect` below.
    """
    if not name or not name.startswith(BLETHR_NAME_PREFIX):
        return False
    tail = mac.replace(":", "").upper()[-6:]
    return name[len(BLETHR_NAME_PREFIX) :].upper() == tail


def detect(client) -> bool:
    """Whether the connected device exposes the BLETHR command service. Authoritative."""
    try:
        return client.services.get_service(BLETHR_SERVICE) is not None
    except Exception:  # noqa: BLE001
        return False


class _Session:
    """One command exchange session on the BLETHR characteristic.

    Replies come back as notifications, so every request parks a future under its opcode and the
    notification handler completes it. Opcodes are unique per in-flight request, which is enough
    here because commands are issued one at a time on a single connection.
    """

    def __init__(self, client) -> None:
        self._client = client
        self._waiters: dict[int, asyncio.Future] = {}
        self._write_response = True

    def _on_notify(self, _sender, data: bytearray) -> None:
        if not data:
            return
        fut = self._waiters.get(data[0])
        if fut is not None and not fut.done():
            fut.set_result(bytes(data))

    async def __aenter__(self) -> _Session:
        # Telink builds this characteristic with plain "write"; pick from the advertised properties
        # rather than assuming, so a build that only offers write-without-response still works.
        try:
            char = self._client.services.get_characteristic(BLETHR_CHAR)
            props = set(getattr(char, "properties", ()) or ())
            self._write_response = "write" in props or not props
        except Exception:  # noqa: BLE001
            self._write_response = True
        await asyncio.wait_for(self._client.start_notify(BLETHR_CHAR, self._on_notify), timeout=10)
        return self

    async def __aexit__(self, *_exc) -> None:
        try:
            await asyncio.wait_for(self._client.stop_notify(BLETHR_CHAR), timeout=5)
        except Exception:  # noqa: BLE001
            pass

    async def cmd(self, opcode: int, payload: bytes = b"", timeout: float = 8.0) -> bytes:
        """Send one command, return the notification that echoes the same opcode."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._waiters[opcode] = fut
        try:
            await asyncio.wait_for(
                self._client.write_gatt_char(BLETHR_CHAR, bytes([opcode]) + payload, response=self._write_response),
                timeout=8,
            )
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._waiters.pop(opcode, None)


def _mac_from_le(raw: bytes) -> str | None:
    """Six little-endian MAC bytes as the usual big-endian text form."""
    if len(raw) < 6:
        return None
    return ":".join(f"{b:02X}" for b in reversed(raw[:6]))


def parse_dev_id(raw: bytes) -> dict:
    """`[0x00] revision u8, hw_version u16, sw_version u16 BCD, dev_spec_data u16, services u32`."""
    out: dict = {"dev_id_raw": raw.hex()}
    if len(raw) < 12:
        return out
    revision, hw_version, sw_version, dev_spec, services = struct.unpack("<BHHHI", raw[1:12])
    out.update(
        {
            "protocol_revision": revision,
            "hw_version_raw": hw_version,
            "sw_version_raw": sw_version,
            "fw_version": f"{sw_version >> 4 & 0xF}.{sw_version & 0xF}" if sw_version <= 0xFF else None,
            "sensor_type": dev_spec & 0x0F,
            "services_raw": services,
            "services": sorted(name for bit, name in _SERVICE_BITS.items() if services & bit),
            "is_repeater": bool(services & SERVICE_EXTENDED),
        }
    )
    return out


def parse_cfg(raw: bytes) -> dict:
    """`[0x55] flg u8, rf_tx_power u8, scan_interval u16, win_min u16, win_max u16` (all ms)."""
    out: dict = {"raw": raw.hex()}
    if len(raw) < 9:
        return out
    flg, rf_tx_power, scan_interval, win_min, win_max = struct.unpack("<BBHHH", raw[1:9])
    out.update(
        {
            "temp_F": bool(flg & 0x01),
            "flg_raw": flg,
            "rf_tx_power": rf_tx_power,
            "rf_tx_dbm": RF_TX_DBM.get(rf_tx_power),
            "rf_tx_in_range": _RF_TX_MIN <= rf_tx_power <= _RF_TX_MAX,
            "scan_interval_ms": scan_interval,
            # Zero is not "as fast as possible": the firmware takes it as scanning switched off, and
            # a repeater that is not scanning shows nothing however well the rest is configured.
            "scanning": scan_interval != 0,
            "scan_window_min_raw": win_min,
            "scan_window_max_raw": win_max,
            "scan_window_min_ms": round(win_min / SCAN_TICKS_PER_MS, 3),
            "scan_window_max_ms": round(win_max / SCAN_TICKS_PER_MS, 3),
        }
    )
    return out


async def async_read_fields(client) -> dict:
    """Everything a BLETHR device can tell us on one connection, without changing anything.

    The interesting part is `ext_mac`: the thermometer this device repeats. The panel can match it
    against the rest of the fleet, which the vendor tool cannot do.
    """
    # The radio table and the firmware defaults travel with the read, so the panel renders them from
    # one source instead of keeping its own copy that can drift.
    fields: dict = {
        "firmware_family": "blethr",
        "rf_tx_options": [[value, RF_TX_DBM[value]] for value in sorted(RF_TX_DBM, reverse=True)],
        "defaults": dict(DEFAULTS),
        "limits": {
            "scan_interval_ms": [SCAN_INTERVAL_MS_MIN, SCAN_INTERVAL_MS_MAX],
            "scan_window_ms": [WINDOW_MS_MIN, WINDOW_MS_MAX],
        },
    }
    # The firmware builds its name at boot and offers no command to change it, but it only puts that
    # name in the scan response. Read it from the GAP characteristic instead, so the panel shows what
    # the device actually calls itself rather than the name it had before it was reflashed.
    try:
        raw = bytes(await asyncio.wait_for(client.read_gatt_char(GAP_DEVICE_NAME), timeout=6))
        fields["device_name"] = raw.decode("utf-8", "replace").replace("\x00", "").strip() or None
    except Exception:  # noqa: BLE001
        fields["device_name"] = None

    async with _Session(client) as session:
        for key, opcode, parser in (
            ("dev_id", CMD_DEV_ID, parse_dev_id),
            ("cfg", CMD_CFG, parse_cfg),
        ):
            try:
                fields.update(parser(await session.cmd(opcode)))
            except Exception as e:  # noqa: BLE001
                fields[f"{key}_error"] = repr(e)

        # 0x58 answers with the six MAC bytes straight after the opcode. 0x10 puts a length byte in
        # front of them first, because its payload can also carry the two random-MAC bytes.
        for key, opcode, offset in (("ext_mac", CMD_EXT_MAC, 1), ("mac", CMD_MAC, 2)):
            try:
                reply = await session.cmd(opcode)
                fields[key] = _mac_from_le(reply[offset:])
                fields[f"{key}_raw"] = reply.hex()
            except Exception as e:  # noqa: BLE001
                fields[f"{key}_error"] = repr(e)

        try:
            reply = await session.cmd(CMD_EXT_BIND_KEY)
            fields["ext_bind_key_raw"] = reply.hex()
            key_bytes = reply[1:17]
            has_key = len(key_bytes) == 16
            fields["ext_bind_key"] = key_bytes.hex() if has_key else None
            # Only a full key counts as configured, and an all-zero one means "none set".
            fields["ext_bind_key_set"] = has_key and any(key_bytes)
        except Exception as e:  # noqa: BLE001
            fields["ext_bind_key_error"] = repr(e)

        try:
            reply = await session.cmd(CMD_TIME)
            fields["device_time"] = struct.unpack("<I", reply[1:5])[0] if len(reply) >= 5 else None
        except Exception as e:  # noqa: BLE001
            fields["device_time_error"] = repr(e)
    return fields


def _mac_to_le(mac: str) -> bytes:
    """Text MAC to the six little-endian bytes the firmware expects."""
    parts = [p for p in mac.replace("-", ":").split(":") if p != ""]
    if len(parts) != 6:
        raise ValueError(f"not a MAC address: {mac!r}")
    return bytes(int(p, 16) for p in reversed(parts))


def build_cfg(current: dict, changes: dict) -> bytes:
    """The 8-byte config payload, current values with `changes` applied, validated.

    Raises ValueError with a readable reason rather than writing something the firmware would
    silently clamp or reject.
    """
    merged = {**current, **changes}
    # The config is written as one struct, so a field nobody is changing is still sent. If we do not
    # know what the device currently holds -- a read that timed out leaves the key absent -- treating
    # it as zero would switch scanning off and drop the radio into the wrong power band. Refuse.
    unknown = [k for k in CFG_KEYS if merged.get(k) is None]
    if unknown:
        raise ValueError("current configuration unknown (" + ", ".join(sorted(unknown)) + "); read the device first")

    rf = int(merged.get("rf_tx_power") or 0)
    # Only judge a power the caller is actually setting. The firmware has a second, higher band that
    # the vendor tool does not offer, so a device already sitting in it must not be blocked from
    # having its other settings changed.
    if "rf_tx_power" in changes and not _RF_TX_MIN <= rf <= _RF_TX_MAX:
        raise ValueError(f"RF TX power must be {_RF_TX_MIN}..{_RF_TX_MAX}, got {rf}")

    interval = int(merged.get("scan_interval_ms") or 0)
    if interval and not SCAN_INTERVAL_MS_MIN <= interval <= SCAN_INTERVAL_MS_MAX:
        raise ValueError(
            f"scan interval must be 0 (off) or {SCAN_INTERVAL_MS_MIN}..{SCAN_INTERVAL_MS_MAX} ms, got {interval}"
        )

    def _window(key: str, label: str) -> int:
        ms = float(merged.get(key) or 0)
        if not WINDOW_MS_MIN <= ms <= WINDOW_MS_MAX:
            raise ValueError(f"{label} must be {WINDOW_MS_MIN}..{WINDOW_MS_MAX} ms, got {ms}")
        return round(ms * SCAN_TICKS_PER_MS)

    win_min = _window("scan_window_min_ms", "scan window min")
    win_max = _window("scan_window_max_ms", "scan window max")
    if win_min > win_max:
        raise ValueError("scan window min must not exceed max")
    flg = 0x01 if merged.get("temp_F") else 0x00
    return struct.pack("<BBHHH", flg, rf, interval, win_min, win_max)


async def async_apply(client, current: dict, changes: dict) -> dict:
    """Write the requested changes on an open connection and verify each by reading back.

    `current` is the device's last read fields, needed because the config is written as a whole
    8-byte struct: changing one value means sending the others back unchanged.
    """
    out: dict = {}
    cfg_keys = set(CFG_KEYS)

    # Build and validate every payload before sending any of them. Rejecting a bad value halfway
    # through would leave the device with some of the change applied and the caller told it failed.
    cfg_payload = build_cfg(current, changes) if cfg_keys & changes.keys() else None

    ext_mac_payload = want_mac = None
    if "ext_mac" in changes:
        # Clearing the source is writing the all-zero address, which is what the firmware takes as
        # "none". An empty payload is not a shorter way of saying that; it is a malformed command.
        want_mac = (changes["ext_mac"] or "").strip() or "00:00:00:00:00:00"
        ext_mac_payload = _mac_to_le(want_mac)

    key_payload = None
    if "ext_bind_key" in changes:
        key_hex = (changes["ext_bind_key"] or "").strip()
        # Same shape of mistake: the firmware only stores a key when it receives exactly sixteen
        # bytes, so clearing one means writing sixteen zeroes, not writing nothing.
        key_payload = bytes.fromhex(key_hex) if key_hex else bytes(16)
        if len(key_payload) != 16:
            raise ValueError("bind key must be exactly 16 bytes (32 hex characters)")

    async with _Session(client) as session:
        if cfg_payload is not None:
            await session.cmd(CMD_CFG, cfg_payload)
            after = parse_cfg(await session.cmd(CMD_CFG))
            # Compare the bytes rather than the decoded values: the firmware clamps what it does not
            # like, and a rounded-back millisecond would hide that it did.
            out["config"] = after.get("raw", "")[2:18] == cfg_payload.hex()
            out["config_after"] = after

        if ext_mac_payload is not None:
            await session.cmd(CMD_EXT_MAC, ext_mac_payload)
            reply = await session.cmd(CMD_EXT_MAC)
            got = _mac_from_le(reply[1:])
            out["ext_mac"] = got == want_mac.upper()
            out["ext_mac_after"] = got

        if key_payload is not None:
            await session.cmd(CMD_EXT_BIND_KEY, key_payload)
            reply = await session.cmd(CMD_EXT_BIND_KEY)
            stored = reply[1:17]
            # A cleared key reads back either as sixteen zeroes or as the firmware's short
            # "none set" answer; both mean the same thing.
            cleared = not any(stored) or len(stored) != 16
            out["ext_bind_key"] = cleared if not any(key_payload) else (len(stored) == 16 and stored == key_payload)

        if "device_time" in changes:
            reply = await session.cmd(CMD_TIME, struct.pack("<I", int(changes["device_time"]) & 0xFFFFFFFF))
            got = struct.unpack("<I", reply[1:5])[0] if len(reply) >= 5 else None
            out["device_time"] = got is not None and abs(got - int(changes["device_time"])) <= 60
            out["device_time_after"] = got
    return out


async def async_reboot(client) -> dict:
    """Ask the device to restart when the link drops (same opcode as the thermometer firmware)."""
    async with _Session(client) as session:
        try:
            await session.cmd(CMD_REBOOT, timeout=3)
        except TimeoutError:
            pass  # the firmware acts on disconnect and may not answer at all
    return {"ok": True}
