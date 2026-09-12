"""Unit tests for spotting a device whose readings have stopped being current.

A repeater that has lost its source keeps broadcasting the last reading it heard, so the values
alone cannot tell it from a healthy one. The packet counter can, and these cover the two pieces
that read it.
"""

from custom_components.telink_manager import gatt
from custom_components.telink_manager.const import DOMAIN

BTHOME_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"

# A real capture from a repeater: info, packet id 6, battery 0%, 25.32 C, 50.70 %, 2.933 V, and
# two count16 fields carrying its own diagnostics.
CAPTURE = bytes.fromhex("400006010002e40903ce130c750b3d14003d0e14")


class _Adv:
    def __init__(self, payload=None, uuid=BTHOME_UUID):
        self.service_data = {uuid: payload} if payload is not None else {}


class _Hass:
    def __init__(self):
        self.data: dict = {}


def test_packet_id_is_read_from_a_real_advertisement():
    assert gatt._packet_id_from_adv(_Adv(CAPTURE)) == 6


def test_packet_id_is_none_without_bthome_service_data():
    """A parked device advertises its flags and nothing else, which is not a stopped counter."""
    assert gatt._packet_id_from_adv(_Adv()) is None


def test_packet_id_is_none_when_the_advertisement_is_encrypted():
    """Bit 0 of the info byte marks encryption; without the key there is nothing to read."""
    assert gatt._packet_id_from_adv(_Adv(bytes([0x41, 0x00, 0x06]))) is None


def test_packet_id_is_none_on_an_unknown_object_id():
    """Stopping at the first field we cannot size beats walking off the end of the payload."""
    assert gatt._packet_id_from_adv(_Adv(bytes([0x40, 0xFE, 0x01, 0x00, 0x06]))) is None


def test_a_device_seen_once_reports_nothing_rather_than_guessing():
    hass = _Hass()
    assert gatt._stale_seconds(hass, "AA:BB:CC:DD:EE:FF", 7) == 0.0


def test_a_counter_that_moves_resets_the_clock(monkeypatch):
    hass = _Hass()
    now = [1000.0]
    monkeypatch.setattr(gatt.time, "monotonic", lambda: now[0])

    gatt._stale_seconds(hass, "AA:BB:CC:DD:EE:FF", 7)
    now[0] += 300
    assert gatt._stale_seconds(hass, "AA:BB:CC:DD:EE:FF", 8) == 0.0


def test_a_counter_that_stands_still_is_reported_in_seconds(monkeypatch):
    hass = _Hass()
    now = [1000.0]
    monkeypatch.setattr(gatt.time, "monotonic", lambda: now[0])

    gatt._stale_seconds(hass, "AA:BB:CC:DD:EE:FF", 7)
    now[0] += 425.5
    assert gatt._stale_seconds(hass, "AA:BB:CC:DD:EE:FF", 7) == 425.5


def test_devices_are_tracked_apart():
    """Two devices sharing one counter value must not reset each other's clock."""
    hass = _Hass()
    gatt._stale_seconds(hass, "AA:BB:CC:DD:EE:01", 7)
    gatt._stale_seconds(hass, "AA:BB:CC:DD:EE:02", 7)
    assert set(hass.data[DOMAIN]["adv_pid"]) == {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"}


def test_no_counter_means_no_judgement():
    """Devices that advertise no packet id are not stale, they are simply not measurable this way."""
    assert gatt._stale_seconds(_Hass(), "AA:BB:CC:DD:EE:FF", None) is None


# --- the interval check --------------------------------------------------------------------------


def _with_snapshot(monkeypatch, adv_interval_s):
    """Stand in for the snapshot store, which is the only thing this reads."""
    snaps = [] if adv_interval_s is None else [{"fields": {"adv_interval_s": adv_interval_s}}]
    monkeypatch.setattr(gatt.backups, "history", lambda hass, mac: snaps)
    return _Hass()


def test_matching_interval_is_reported_as_matching(monkeypatch):
    hass = _with_snapshot(monkeypatch, 5.0)
    out = gatt._source_interval_check(hass, {"ext_mac": "AA:BB:CC:DD:EE:FF", "scan_interval_ms": 5000})
    assert out == {"source_adv_interval_ms": 5000, "source_interval_ok": True}


def test_a_disagreement_of_more_than_a_hundred_milliseconds_is_a_mismatch(monkeypatch):
    """The firmware accepts a packet only within 100 ms of when it expects one, so that is the
    line: inside it the device locks on, outside it never does."""
    hass = _with_snapshot(monkeypatch, 10.0)
    out = gatt._source_interval_check(hass, {"ext_mac": "AA:BB:CC:DD:EE:FF", "scan_interval_ms": 5000})
    assert out["source_interval_ok"] is False
    assert out["source_adv_interval_ms"] == 10000


def test_a_hundred_milliseconds_of_disagreement_still_counts_as_matching(monkeypatch):
    hass = _with_snapshot(monkeypatch, 5.1)
    out = gatt._source_interval_check(hass, {"ext_mac": "AA:BB:CC:DD:EE:FF", "scan_interval_ms": 5000})
    assert out["source_interval_ok"] is True


def test_a_source_faster_than_the_firmware_can_follow_is_called_out_separately(monkeypatch):
    """Below the firmware's own minimum no setting on the repeater works, so the advice has to be
    to slow the source down rather than to change a field that will not accept the value."""
    hass = _with_snapshot(monkeypatch, 2.5)
    out = gatt._source_interval_check(hass, {"ext_mac": "AA:BB:CC:DD:EE:FF", "scan_interval_ms": 5000})
    assert out["source_interval_unusable"] is True
    assert out["source_interval_ok"] is False


def test_nothing_is_claimed_without_a_snapshot_to_judge_against(monkeypatch):
    hass = _with_snapshot(monkeypatch, None)
    assert gatt._source_interval_check(hass, {"ext_mac": "AA:BB:CC:DD:EE:FF", "scan_interval_ms": 5000}) == {}


def test_scanning_switched_off_is_a_different_problem_and_not_reported_here(monkeypatch):
    hass = _with_snapshot(monkeypatch, 5.0)
    assert gatt._source_interval_check(hass, {"ext_mac": "AA:BB:CC:DD:EE:FF", "scan_interval_ms": 0}) == {}


def test_no_source_set_is_a_different_problem_too(monkeypatch):
    hass = _with_snapshot(monkeypatch, 5.0)
    assert gatt._source_interval_check(hass, {"ext_mac": "00:00:00:00:00:00", "scan_interval_ms": 5000}) == {}
