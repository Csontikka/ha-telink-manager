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
    CMD_TIME,
    SERVICE_EXTENDED,
)

_LOGGER = logging.getLogger(__name__)

# Same table the thermometer firmware uses, kept here so a BLETHR reply can name its own radio power.
_RF_TX_MIN, _RF_TX_MAX = 130, 191

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
            "rf_tx_in_range": _RF_TX_MIN <= rf_tx_power <= _RF_TX_MAX,
            "scan_interval_ms": scan_interval,
            "scan_window_min_ms": win_min,
            "scan_window_max_ms": win_max,
        }
    )
    return out


async def async_read_fields(client) -> dict:
    """Everything a BLETHR device can tell us on one connection, without changing anything.

    The interesting part is `ext_mac`: the thermometer this device repeats. The panel can match it
    against the rest of the fleet, which the vendor tool cannot do.
    """
    fields: dict = {"firmware_family": "blethr"}
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
