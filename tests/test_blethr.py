"""Unit tests for the BLETHR (BLE T&H repeater) protocol layer.

Every canned reply below is a real capture from a repeater running PVVX BLETHR v1.2, so the
byte-level expectations are the firmware's, not a guess about it.
"""

import pytest

from custom_components.telink_manager import blethr
from custom_components.telink_manager.const import (
    BLETHR_CHAR,
    BLETHR_SERVICE,
    CMD_CFG,
    CMD_DEV_ID,
    CMD_EXT_BIND_KEY,
    CMD_EXT_MAC,
    CMD_MAC,
    CMD_REBOOT,
    CMD_TIME,
)

# --- captures from the live device -------------------------------------------------------------
CFG = bytes.fromhex("5500a98813e204c409")  # °C, RF 169, scan 5000 ms, window 1250..2500 ms
DEV_ID = bytes.fromhex("00000a0112000000a510008000")  # fw 1.2, services incl. the scan-device bit
MAC_REPLY = bytes.fromhex("1008eaa69a38c1a4306b00")  # [0x10][len][mac6][randmac2] -> A4:C1:38:9A:A6:EA
EXT_MAC_REPLY = bytes.fromhex("587b753038c1a4")  # [0x58][mac6], no length byte
NO_BIND_KEY = bytes.fromhex("5cff")  # the firmware's "no key set" answer


class _FakeChar:
    def __init__(self, uuid, properties):
        self.uuid = uuid
        self.properties = list(properties)


class _FakeServices:
    def __init__(self, has_service: bool, properties):
        self._service = object() if has_service else None
        self._char = _FakeChar(BLETHR_CHAR, properties)

    def get_service(self, uuid):
        return self._service if uuid == BLETHR_SERVICE else None

    def get_characteristic(self, uuid):
        return self._char if uuid == BLETHR_CHAR else None


class FakeClient:
    """Just enough of a BLE client to drive the repeater protocol.

    A write to the command characteristic is answered with the canned notification for that
    opcode, which is how the real firmware replies.
    """

    def __init__(self, replies=None, *, has_service=True, properties=("write", "notify")):
        self.replies = dict(replies or {})
        self.writes = []
        self.services = _FakeServices(has_service, properties)
        self._cb = None
        self.notify_started = 0
        self.notify_stopped = 0

    async def start_notify(self, uuid, cb):
        assert uuid == BLETHR_CHAR
        self._cb = cb
        self.notify_started += 1

    async def stop_notify(self, uuid):
        self._cb = None
        self.notify_stopped += 1

    async def write_gatt_char(self, uuid, data, response=True):
        data = bytes(data)
        self.writes.append((uuid, data, response))
        reply = self.replies.get(data[0])
        if callable(reply):
            reply = reply(data[1:])
        if reply is not None and self._cb is not None:
            self._cb(uuid, bytearray(reply))

    def payload_for(self, opcode):
        """The payload of the last write of this opcode (without the opcode byte)."""
        for _uuid, data, _resp in reversed(self.writes):
            if data[0] == opcode:
                return data[1:]
        return None

    def first_payload_for(self, opcode):
        """The payload of the FIRST write of this opcode -- the request, not the read-back."""
        for _uuid, data, _resp in self.writes:
            if data[0] == opcode:
                return data[1:]
        return None


# --- naming and detection ----------------------------------------------------------------------
def test_name_hint_matches_only_the_firmware_generated_name():
    assert blethr.looks_like_blethr("STH_9AA6EA", "A4:C1:38:9A:A6:EA")
    assert blethr.looks_like_blethr("STH_9aa6ea", "a4:c1:38:9a:a6:ea")
    # right prefix, wrong tail: not this device's name
    assert not blethr.looks_like_blethr("STH_000000", "A4:C1:38:9A:A6:EA")
    assert not blethr.looks_like_blethr("ATC_9AA6EA", "A4:C1:38:9A:A6:EA")
    assert not blethr.looks_like_blethr(None, "A4:C1:38:9A:A6:EA")
    assert not blethr.looks_like_blethr("", "A4:C1:38:9A:A6:EA")


def test_detect_follows_the_service_table():
    assert blethr.detect(FakeClient()) is True
    assert blethr.detect(FakeClient(has_service=False)) is False

    class Broken:
        @property
        def services(self):
            raise RuntimeError("no table")

    assert blethr.detect(Broken()) is False


# --- parsing -----------------------------------------------------------------------------------
def test_parse_cfg_matches_the_device():
    out = blethr.parse_cfg(CFG)
    assert out["temp_F"] is False
    assert out["rf_tx_power"] == 169
    assert out["rf_tx_in_range"] is True
    assert out["scan_interval_ms"] == 5000
    assert out["scanning"] is True
    # stored in units of 8 us, so 1250 is 10 ms -- reporting the raw number as milliseconds would
    # overstate the window by a factor of 125
    assert out["scan_window_min_raw"] == 1250
    assert out["scan_window_max_raw"] == 2500
    assert out["scan_window_min_ms"] == 10.0
    assert out["scan_window_max_ms"] == 20.0
    assert out["rf_tx_dbm"] == "+0.04"
    assert out["raw"] == CFG.hex()


def test_parse_cfg_reports_a_zero_interval_as_not_scanning():
    """Zero is the firmware default and means scanning off, not "as fast as possible"."""
    off = bytes.fromhex("5500a90000e204c409")
    out = blethr.parse_cfg(off)
    assert out["scan_interval_ms"] == 0
    assert out["scanning"] is False


def test_parse_cfg_short_reply_keeps_raw_only():
    out = blethr.parse_cfg(bytes.fromhex("5500"))
    assert out == {"raw": "5500"}


def test_parse_dev_id_matches_the_device():
    out = blethr.parse_dev_id(DEV_ID)
    assert out["fw_version"] == "1.2"
    assert out["is_repeater"] is True
    assert "scan_device" in out["services"]
    assert {"ota", "pincode", "screen", "th_sensor", "time_adjust"} <= set(out["services"])


def test_parse_dev_id_short_reply_keeps_raw_only():
    out = blethr.parse_dev_id(bytes.fromhex("0000"))
    assert out == {"dev_id_raw": "0000"}


def test_mac_helpers_round_trip():
    assert blethr._mac_from_le(bytes.fromhex("7b753038c1a4")) == "A4:C1:38:30:75:7B"
    assert blethr._mac_from_le(b"\x01\x02") is None
    assert blethr._mac_to_le("A4:C1:38:30:75:7B") == bytes.fromhex("7b753038c1a4")
    assert blethr._mac_to_le("a4-c1-38-30-75-7b") == bytes.fromhex("7b753038c1a4")
    for bad in ("A4:C1:38:30:75", "nonsense", "", "ZZ:C1:38:30:75:7B"):
        with pytest.raises(ValueError):
            blethr._mac_to_le(bad)


# --- config building ---------------------------------------------------------------------------
def test_build_cfg_accepts_zero_interval_and_the_range_ends():
    current = blethr.parse_cfg(CFG)
    for changes in (
        {"scan_interval_ms": 0},
        {"scan_interval_ms": 3000},
        {"scan_interval_ms": 10000},
        {"scan_window_min_ms": 5.0},
        {"scan_window_max_ms": 50.0},
    ):
        blethr.build_cfg(current, changes)


def test_build_cfg_leaves_an_untouched_out_of_band_power_alone():
    """The firmware has a second power band the vendor tool never offers; a device already sitting
    in it must still be able to have its other settings changed."""
    current = {**blethr.parse_cfg(CFG), "rf_tx_power": 60}
    built = blethr.build_cfg(current, {"scan_interval_ms": 5000})
    assert built[1] == 60
    with pytest.raises(ValueError):
        blethr.build_cfg(current, {"rf_tx_power": 60})


def test_build_cfg_converts_milliseconds_to_the_stored_units():
    built = blethr.build_cfg(blethr.parse_cfg(CFG), {"scan_window_min_ms": 10, "scan_window_max_ms": 20})
    assert blethr.parse_cfg(b"U" + built)["scan_window_min_raw"] == 1250
    assert blethr.parse_cfg(b"U" + built)["scan_window_max_raw"] == 2500


def test_build_cfg_keeps_untouched_values():
    current = blethr.parse_cfg(CFG)
    built = blethr.build_cfg(current, {"scan_interval_ms": 6000})
    assert blethr.parse_cfg(b"\x55" + built)["scan_interval_ms"] == 6000
    # everything else survives the round trip unchanged
    for key in ("rf_tx_power", "scan_window_min_ms", "scan_window_max_ms", "temp_F"):
        assert blethr.parse_cfg(b"\x55" + built)[key] == current[key]


@pytest.mark.parametrize(
    "changes",
    [
        {"rf_tx_power": 129},
        {"rf_tx_power": 192},
        {"scan_interval_ms": 70000},
        {"scan_window_min_ms": -1},
        {"scan_window_min_ms": 20, "scan_window_max_ms": 10},
        {"scan_interval_ms": 2999},
        {"scan_window_min_ms": 4.9},
        {"scan_window_max_ms": 50.1},
    ],
)
def test_build_cfg_refuses_out_of_range(changes):
    with pytest.raises(ValueError):
        blethr.build_cfg(blethr.parse_cfg(CFG), changes)


# --- reading -----------------------------------------------------------------------------------
async def test_read_fields_decodes_both_mac_replies():
    """The two MAC commands frame their answer differently, and getting that wrong shifts the
    address by a byte, which is exactly what happened before this test existed."""
    client = FakeClient(
        {
            CMD_DEV_ID: DEV_ID,
            CMD_CFG: CFG,
            CMD_MAC: MAC_REPLY,
            CMD_EXT_MAC: EXT_MAC_REPLY,
            CMD_EXT_BIND_KEY: NO_BIND_KEY,
            CMD_TIME: bytes.fromhex("23") + (1789065252).to_bytes(4, "little"),
        }
    )
    fields = await blethr.async_read_fields(client)
    assert fields["firmware_family"] == "blethr"
    # the panel renders the radio table and the firmware defaults from the read, not its own copy
    assert fields["defaults"]["scan_interval_ms"] == 0
    assert [169, "+0.04"] in fields["rf_tx_options"]
    assert fields["limits"]["scan_interval_ms"] == [3000, 10000]
    assert fields["mac"] == "A4:C1:38:9A:A6:EA"
    assert fields["ext_mac"] == "A4:C1:38:30:75:7B"
    assert fields["ext_bind_key"] is None
    assert fields["ext_bind_key_set"] is False
    assert fields["device_time"] == 1789065252
    assert fields["scan_interval_ms"] == 5000
    assert client.notify_started == 1 and client.notify_stopped == 1


async def test_read_fields_reports_a_full_bind_key():
    key = "13a9e53d6e106f459493f7d51af37d87"
    client = FakeClient(
        {
            CMD_DEV_ID: DEV_ID,
            CMD_CFG: CFG,
            CMD_MAC: MAC_REPLY,
            CMD_EXT_MAC: EXT_MAC_REPLY,
            CMD_EXT_BIND_KEY: bytes([CMD_EXT_BIND_KEY]) + bytes.fromhex(key),
            CMD_TIME: bytes.fromhex("23") + (1).to_bytes(4, "little"),
        }
    )
    fields = await blethr.async_read_fields(client)
    assert fields["ext_bind_key"] == key
    assert fields["ext_bind_key_set"] is True


async def test_read_fields_survives_a_command_that_never_answers():
    """One silent command must not cost the rest of the read."""
    client = FakeClient(
        {
            CMD_DEV_ID: DEV_ID,
            CMD_CFG: CFG,
            CMD_MAC: MAC_REPLY,
            CMD_EXT_BIND_KEY: NO_BIND_KEY,
            CMD_TIME: bytes.fromhex("23") + (1).to_bytes(4, "little"),
        }
    )  # CMD_EXT_MAC stays silent
    fields = await blethr.async_read_fields(client)
    assert fields["scan_interval_ms"] == 5000
    assert fields["mac"] == "A4:C1:38:9A:A6:EA"
    assert "ext_mac_error" in fields
    assert client.notify_stopped == 1  # the session still closed cleanly


# --- writing -----------------------------------------------------------------------------------
async def test_apply_writes_the_whole_config_struct():
    current = blethr.parse_cfg(CFG)
    after = bytes.fromhex("5500a97017e204c409")  # same, with scan_interval 6000
    client = FakeClient({CMD_CFG: after})
    out = await blethr.async_apply(client, current, {"scan_interval_ms": 6000})
    assert out["config"] is True
    assert out["config_after"]["scan_interval_ms"] == 6000
    # the device is sent the complete 8-byte struct, not just the changed field
    assert len(client.first_payload_for(CMD_CFG)) == 8


async def test_apply_sets_and_verifies_the_source_mac():
    client = FakeClient({CMD_EXT_MAC: EXT_MAC_REPLY})
    out = await blethr.async_apply(client, blethr.parse_cfg(CFG), {"ext_mac": "A4:C1:38:30:75:7B"})
    assert out["ext_mac"] is True
    assert client.first_payload_for(CMD_EXT_MAC) == bytes.fromhex("7b753038c1a4")


async def test_apply_reports_a_source_mac_that_did_not_take():
    client = FakeClient({CMD_EXT_MAC: EXT_MAC_REPLY})  # answers with the old address
    out = await blethr.async_apply(client, blethr.parse_cfg(CFG), {"ext_mac": "A4:C1:38:AA:BB:CC"})
    assert out["ext_mac"] is False


async def test_apply_refuses_a_bind_key_of_the_wrong_length():
    client = FakeClient({CMD_EXT_BIND_KEY: NO_BIND_KEY})
    with pytest.raises(ValueError):
        await blethr.async_apply(client, blethr.parse_cfg(CFG), {"ext_bind_key": "0011"})


async def test_apply_sets_the_clock():
    ts = 1789083088
    client = FakeClient({CMD_TIME: bytes([CMD_TIME]) + ts.to_bytes(4, "little")})
    out = await blethr.async_apply(client, blethr.parse_cfg(CFG), {"device_time": ts})
    assert out["device_time"] is True
    assert out["device_time_after"] == ts


async def test_reboot_tolerates_no_reply():
    client = FakeClient()  # the firmware acts on disconnect and may answer nothing
    assert (await blethr.async_reboot(client))["ok"] is True
    assert client.payload_for(CMD_REBOOT) == b""
    assert client.notify_stopped == 1


# --- refusals that protect a working device ----------------------------------------------------
def test_build_cfg_refuses_when_the_current_config_is_unknown():
    """A read that timed out leaves the config keys absent. Treating them as zero would drop the
    radio into the wrong power band and switch scanning off, and the firmware writes it to flash
    immediately, so the only safe answer is to write nothing."""
    for missing in blethr.CFG_KEYS:
        base = {k: v for k, v in blethr.parse_cfg(CFG).items() if k != missing}
        with pytest.raises(ValueError, match="current configuration unknown"):
            blethr.build_cfg(base, {})
        # but a value the caller supplies is not unknown, so the same gap is fine when it is set
        blethr.build_cfg(base, {missing: blethr.parse_cfg(CFG)[missing]})


def test_build_cfg_refuses_an_empty_base_outright():
    with pytest.raises(ValueError, match="current configuration unknown"):
        blethr.build_cfg({}, {"scan_interval_ms": 5000})


async def test_apply_clears_the_source_by_writing_the_all_zero_address():
    """The firmware only accepts exactly six MAC bytes, so clearing the source is writing zeroes.
    An empty payload is a malformed command, not a shorter way of saying the same thing."""
    zero = bytes([CMD_EXT_MAC]) + bytes(6)
    client = FakeClient({CMD_EXT_MAC: zero})
    out = await blethr.async_apply(client, blethr.parse_cfg(CFG), {"ext_mac": ""})
    assert client.first_payload_for(CMD_EXT_MAC) == bytes(6)
    assert out["ext_mac"] is True


async def test_apply_clears_the_bind_key_with_sixteen_zero_bytes():
    """The firmware stores a key only when it is handed exactly sixteen bytes, so an empty payload
    leaves the previous key in place while looking like a successful clear."""
    client = FakeClient({CMD_EXT_BIND_KEY: bytes([CMD_EXT_BIND_KEY]) + bytes(16)})
    out = await blethr.async_apply(client, blethr.parse_cfg(CFG), {"ext_bind_key": ""})
    assert client.first_payload_for(CMD_EXT_BIND_KEY) == bytes(16)
    assert out["ext_bind_key"] is True


async def test_apply_accepts_the_short_no_key_answer_as_cleared():
    client = FakeClient({CMD_EXT_BIND_KEY: NO_BIND_KEY})
    out = await blethr.async_apply(client, blethr.parse_cfg(CFG), {"ext_bind_key": ""})
    assert out["ext_bind_key"] is True


async def test_apply_writes_nothing_when_a_payload_is_rejected():
    """Every payload is built before the first one is sent, so a bad value cannot leave the device
    half-changed while the caller is told the write failed."""
    client = FakeClient({CMD_CFG: CFG, CMD_EXT_MAC: EXT_MAC_REPLY})
    with pytest.raises(ValueError):
        await blethr.async_apply(client, blethr.parse_cfg(CFG), {"scan_interval_ms": 5000, "ext_mac": "not-a-mac"})
    assert client.writes == []
