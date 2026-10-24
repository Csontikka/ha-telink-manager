"""Tests for the Telink OTA image checks and packet framing, plus the transfer against a stand-in device."""

import asyncio
import struct
from types import SimpleNamespace

import pytest

from custom_components.telink_manager import ota


def make_image(size: int = 4100) -> bytes:
    """A minimal valid Telink image: "KNLT" at 0x08, size (16n + 4) at 0x18, image CRC in the last 4 bytes."""
    body = bytearray(b"\x5a" * size)
    struct.pack_into("<I", body, 0x08, 0x544C4E4B)
    struct.pack_into("<I", body, 0x18, size)
    struct.pack_into("<I", body, size - 4, ota.image_crc(bytes(body[: size - 4])))
    return bytes(body)


def test_crc16_modbus_reference_vector():
    assert ota.crc16_modbus(b"123456789") == 0x4B37


def test_validate_accepts_good_image():
    assert ota.validate_image(make_image()) == {"ok": True, "size": 4100}


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda b: b[:512], "too small"),
        (lambda b: struct.pack("<I", 0x0BEEF11E) + b[4:], "Zigbee"),
        (lambda b: b[:8] + b"XXXX" + b[12:], "bad header"),
        (lambda b: b[:100] + b"\x00" + b[101:], "CRC"),
        (lambda b: b + b"\xff" * 2048, "signed"),
    ],
)
def test_validate_rejects(mutate, error):
    res = ota.validate_image(mutate(make_image()))
    assert not res["ok"] and error in res["error"]


def test_validate_rejects_oversize():
    assert "standard OTA" in ota.validate_image(make_image(ota.MAX_OTA_SIZE + 20))["error"]


def test_packets_are_indexed_padded_and_crc_framed():
    data = bytes(range(40))  # 2.5 blocks -> 3, last padded with 0xFF
    pk = ota.build_packets(data)
    assert len(pk) == 3 and all(len(p) == 20 for p in pk)
    assert pk[1][:2] == b"\x01\x00" and pk[1][2:18] == data[16:32]
    assert pk[2][2:18] == data[32:] + b"\xff" * 8
    assert struct.unpack("<H", pk[0][18:])[0] == ota.crc16_modbus(pk[0][:18])


def test_end_packet():
    assert ota.end_packet(0x1520) == bytes.fromhex("02ff1f15e0ea")


def test_version_str_and_pick_file():
    assert ota.version_str(0x59) == "5.9" and ota.version_str(89) == "5.9"
    idx = {"custom": ["bin/ATC_v59.bin", "?", "/", ""]}
    assert ota._pick_file(idx, 0) == "bin/ATC_v59.bin"
    assert ota._pick_file(idx, 1) is None and ota._pick_file(idx, 2) is None and ota._pick_file(idx, 9) is None


class FakeClient:
    def __init__(self, fail_at: int | None = None):
        self.writes: list[bytes] = []
        self.fail_at = fail_at
        char = SimpleNamespace(properties=["read", "write", "write-without-response"])
        self.services = SimpleNamespace(get_characteristic=lambda uuid: char)

    async def write_gatt_char(self, uuid, data, response=False):
        assert uuid == ota.OTA_CHAR and response is False
        self.writes.append(bytes(data))

    async def read_gatt_char(self, uuid):
        blocks = len(self.writes) - 2
        return b"\x02" if self.fail_at is not None and blocks > self.fail_at else b"\x00"


async def test_transfer_sequence(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    pk = ota.build_packets(make_image(1024 + 16 * 3 + 4))
    c, job = FakeClient(), {}
    await ota._transfer(c, pk, job)
    assert c.writes[:2] == [b"\x00\xff", b"\x01\xff"]
    assert c.writes[2:-1] == pk and c.writes[-1] == ota.end_packet(len(pk))
    assert job["percent"] == 100.0 and job["sent"] == len(pk)


async def test_transfer_stops_on_device_error(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    pk = ota.build_packets(make_image())
    c = FakeClient(fail_at=10)
    with pytest.raises(RuntimeError, match="CRC error"):
        await ota._transfer(c, pk, {})
    assert ota.end_packet(len(pk)) not in c.writes


_real_sleep = asyncio.sleep


async def _no_sleep(_s):
    await _real_sleep(0)


# --- check / start / background run, with the BLE and HTTP layers stubbed -------------------------

MAC = "A4:C1:38:00:00:01"
INDEX = {"version": 89, "custom": ["bin/ATC_v59.bin"] * 11}


CONFIGURED = {"raw": "6f9e0506300fa904f90a3c", "device_name": "Storage 9BD2", "adv_crypto": True, "bind_key": "ab" * 16}
DEFAULTS = {"raw": "07100a062804a931310ab4", "device_name": "ATC_259BD2", "adv_crypto": False, "bind_key": "cd" * 16}


def fields(fw: int, hw: int = 10, cfg: dict = CONFIGURED) -> dict:
    base = {"fw_byte": fw, "fw_version": ota.version_str(fw), "hw_ver": hw, "hw_ver_name": "LYWSD03MMC B1.5"}
    return {**base, **cfg}


@pytest.fixture
def stubs(monkeypatch):
    """Device at v5.6 that reports v5.9 after a transfer; index and image served from memory."""
    st = SimpleNamespace(
        fw=0x56, image=make_image(), transfers=0, transfer_ok=True, reset=False, cfg=CONFIGURED, calls=[], saved=[]
    )

    async def read(hass, mac, retries=3):
        return {"ok": True, "fields": fields(st.fw, cfg=st.cfg)}

    async def restore(hass, mac, snap, parts):
        st.calls.append(("restore", snap["raw"], snap["device_name"], tuple(parts)))
        st.cfg = {**CONFIGURED, "adv_crypto": False}
        return {"ok": True}

    async def set_encryption(hass, mac, enable, key=None):
        st.calls.append(("encrypt", enable, key))
        st.cfg = CONFIGURED
        return {"ok": True, "ha": "updated"}

    async def save(hass, snap):
        st.saved.append(snap)
        return {"count": len(st.saved)}

    async def with_client(hass, mac, fn, retries=2):
        st.transfers += 1
        if not st.transfer_ok:
            return {"ok": False, "error": "link lost"}
        st.fw = 0x59
        if st.reset:
            st.cfg = DEFAULTS
        return {"ok": True}

    async def index(hass):
        return INDEX

    async def download(hass, fn):
        return st.image

    async def remember(hass, mac, name):
        return None

    monkeypatch.setattr(ota.gatt, "async_read", read)
    monkeypatch.setattr(ota.gatt, "_with_client", with_client)
    monkeypatch.setattr(ota.gatt, "async_remember_ble_name", remember)
    monkeypatch.setattr(ota.gatt, "async_restore", restore)
    monkeypatch.setattr(ota.gatt, "async_set_encryption", set_encryption)
    monkeypatch.setattr(ota.backups, "async_save", save)
    monkeypatch.setattr(ota, "_async_index", index)
    monkeypatch.setattr(ota, "_async_download", download)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    return st


async def _wait(hass):
    for _ in range(50):
        if not ota.status(hass, MAC).get("active"):
            return ota.status(hass, MAC)
        await _real_sleep(0)
    raise AssertionError("job did not finish")


async def test_check_reports_update(hass, stubs):
    r = await ota.async_check(hass, MAC)
    assert r["ok"] and r["update_available"] and r["current"] == "5.6" and r["latest"] == "5.9"
    assert r["file"] == "bin/ATC_v59.bin"


async def test_check_rejects_unknown_board_and_old_firmware(hass, stubs):
    stubs.fw = 0x45
    assert "too old" in (await ota.async_check(hass, MAC))["error"]
    stubs.fw = 0x56
    INDEX["custom"][10] = "?"
    try:
        assert "no PVVX firmware" in (await ota.async_check(hass, MAC))["error"]
    finally:
        INDEX["custom"][10] = "bin/ATC_v59.bin"


async def test_start_flashes_and_verifies(hass, stubs):
    r = await ota.async_start(hass, MAC)
    assert r["ok"] and r["job"]["state"] == "starting"
    j = await _wait(hass)
    assert j["state"] == "done" and j["after"] == "5.9" and stubs.transfers == 1


async def test_start_refuses_when_current_unless_forced(hass, stubs):
    stubs.fw = 0x59
    assert "already on" in (await ota.async_start(hass, MAC))["error"]
    assert (await ota.async_start(hass, MAC, force=True))["ok"]
    await _wait(hass)


async def test_start_rejects_bad_image(hass, stubs):
    stubs.image = make_image()[:-1] + b"\x00"
    r = await ota.async_start(hass, MAC)
    assert not r["ok"] and "CRC" in r["error"] and stubs.transfers == 0


async def test_failed_transfer_is_reported(hass, stubs):
    stubs.transfer_ok = False
    await ota.async_start(hass, MAC)
    j = await _wait(hass)
    assert j["state"] == "failed" and j["error"] == "link lost"
    assert ota.status(hass)[MAC]["state"] == "failed"


def test_settings_lost_ignores_hw_byte():
    assert ota.settings_lost(CONFIGURED, CONFIGURED) == []
    hw_only = {**CONFIGURED, "raw": CONFIGURED["raw"][:18] + "00" + CONFIGURED["raw"][20:]}
    assert ota.settings_lost(CONFIGURED, hw_only) == []
    assert set(ota.settings_lost(CONFIGURED, DEFAULTS)) == {"raw", "device_name", "adv_crypto", "bind_key"}


async def test_kept_settings_need_no_restore(hass, stubs):
    await ota.async_start(hass, MAC)
    j = await _wait(hass)
    assert j["state"] == "done" and j["settings_reset"] == [] and stubs.calls == []
    assert stubs.saved and stubs.saved[0]["raw"] == CONFIGURED["raw"]  # pre-update snapshot


async def test_reset_settings_are_restored_with_encryption(hass, stubs):
    stubs.reset = True
    await ota.async_start(hass, MAC)
    j = await _wait(hass)
    assert j["state"] == "done" and j["settings_restored"] and "bind_key" in j["settings_reset"]
    assert stubs.calls[0][:3] == ("restore", CONFIGURED["raw"], "Storage 9BD2")
    assert stubs.calls[1] == ("encrypt", True, "ab" * 16)


async def test_failed_restore_is_reported(hass, stubs, monkeypatch):
    stubs.reset = True

    async def bad_restore(hass, mac, snap, parts):
        return {"ok": False, "error": "link lost"}

    monkeypatch.setattr(ota.gatt, "async_restore", bad_restore)
    await ota.async_start(hass, MAC)
    j = await _wait(hass)
    assert j["state"] == "failed" and "restoring settings failed" in j["error"] and "Snapshots" in j["error"]
