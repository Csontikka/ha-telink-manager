"""Unit tests for reading a device's advertisement without connecting to it.

Every payload here is a real capture from a device on the bench, so what is asserted is the
firmware's behaviour rather than an idea about it.
"""

from custom_components.telink_manager import adv

# A repeater relaying its source: info, packet id 6, battery 0%, 25.32 C, 50.70 %, 2.933 V, and
# two count16 fields carrying its own diagnostics.
RELAYING = bytes.fromhex("400006010002e40903ce130c750b3d14003d0e14")

# The same device parked: packet id, its own voltage, its error count. Nothing from the source.
PARKED = bytes.fromhex("40000c0c910b3d3201")


class _Adv:
    def __init__(self, payload=None, uuid=adv.BTHOME_UUID):
        self.service_data = {uuid: payload} if payload is not None else {}


# --- walking a payload ---------------------------------------------------------------------------


def test_the_walk_yields_every_object_in_order():
    assert [oid for oid, _ in adv.bthome_objects(_Adv(RELAYING))] == [0x00, 0x01, 0x02, 0x03, 0x0C, 0x3D, 0x3D]


def test_the_walk_stops_at_an_object_it_cannot_size():
    """Guessing a length would read every field after it from the wrong offset, so the walk ends
    instead and the caller sees only what was certain."""
    assert [oid for oid, _ in adv.bthome_objects(_Adv(bytes([0x40, 0x00, 0x06, 0xFE, 0x01])))] == [0x00]


def test_the_walk_stops_rather_than_reading_past_the_end():
    """A temperature object announced with one byte of its two present."""
    assert [oid for oid, _ in adv.bthome_objects(_Adv(bytes([0x40, 0x00, 0x06, 0x02, 0x11])))] == [0x00]


def test_an_encrypted_payload_yields_nothing():
    """Bit 0 of the info byte says the rest needs a key we do not have."""
    assert list(adv.bthome_objects(_Adv(bytes([0x41, 0x00, 0x06])))) == []


def test_no_service_data_yields_nothing():
    assert list(adv.bthome_objects(_Adv())) == []


# --- battery -------------------------------------------------------------------------------------


def test_battery_reads_both_percentage_and_voltage_when_both_are_there():
    out = adv.battery_from_adv(_Adv(RELAYING))
    assert out == {"battery": 0, "battery_v": 2.933, "battery_src": "bthome"}


def test_battery_reads_voltage_alone():
    out = adv.battery_from_adv(_Adv(PARKED))
    assert out["battery"] is None
    assert out["battery_v"] == 2.961


def test_battery_falls_back_to_the_pvvx_custom_format():
    payload = bytes(10) + (2913).to_bytes(2, "little") + bytes([84, 0, 0])
    out = adv.battery_from_adv(_Adv(payload, uuid=adv.ESS_UUID))
    assert out == {"battery": 84, "battery_v": 2.913, "battery_src": "pvvx"}


def test_battery_reports_nothing_when_there_is_nothing_to_read():
    assert adv.battery_from_adv(_Adv()) == {"battery": None, "battery_v": None, "battery_src": None}


# --- packet id and what it means -----------------------------------------------------------------


def test_packet_id_is_read_from_a_real_advertisement():
    assert adv.packet_id_from_adv(_Adv(RELAYING)) == 6


def test_packet_id_is_present_in_a_parked_advertisement_too():
    """Which is the whole reason a stalled counter cannot detect a parked repeater: it keeps
    counting while it has stopped relaying."""
    assert adv.packet_id_from_adv(_Adv(PARKED)) == 12


def test_packet_id_is_none_without_a_readable_payload():
    assert adv.packet_id_from_adv(_Adv()) is None


# --- relaying ------------------------------------------------------------------------------------


def test_a_payload_carrying_the_source_reading_counts_as_relaying():
    assert adv.is_relaying(_Adv(RELAYING)) is True


def test_a_parked_payload_is_not_relaying():
    assert adv.is_relaying(_Adv(PARKED)) is False


def test_no_payload_at_all_is_neither():
    """The seconds between a disconnect and the next advertisement are a third state, and calling
    them a device that has stopped would be wrong."""
    assert adv.is_relaying(_Adv()) is None


# --- staleness -----------------------------------------------------------------------------------


def test_a_device_seen_once_gets_no_verdict():
    seen = {}
    assert adv.stale_seconds(seen, "AA:BB:CC:DD:EE:FF", 7, 1000.0) == 0.0


def test_a_counter_that_moves_resets_the_clock():
    seen = {}
    adv.stale_seconds(seen, "AA:BB:CC:DD:EE:FF", 7, 1000.0)
    assert adv.stale_seconds(seen, "AA:BB:CC:DD:EE:FF", 8, 1300.0) == 0.0


def test_a_counter_that_stands_still_is_reported_in_seconds():
    seen = {}
    adv.stale_seconds(seen, "AA:BB:CC:DD:EE:FF", 7, 1000.0)
    assert adv.stale_seconds(seen, "AA:BB:CC:DD:EE:FF", 7, 1425.5) == 425.5


def test_devices_are_tracked_apart():
    """Two devices sharing a counter value must not reset each other's clock."""
    seen = {}
    adv.stale_seconds(seen, "AA:BB:CC:DD:EE:01", 7, 1000.0)
    adv.stale_seconds(seen, "AA:BB:CC:DD:EE:02", 7, 1000.0)
    assert set(seen) == {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"}


def test_no_counter_means_no_judgement():
    assert adv.stale_seconds({}, "AA:BB:CC:DD:EE:FF", None, 1000.0) is None


def test_the_threshold_follows_the_measurement_period():
    """A counter that steps once per measurement has not stalled until several measurements have
    been missed, so the limit is the device's period rather than a number chosen for all of them."""
    assert adv.stale_threshold_s(300.0) == 900.0


def test_the_threshold_never_drops_below_two_minutes():
    """The default period is ten seconds, and one or two missed advertisements are ordinary."""
    assert adv.stale_threshold_s(10.0) == 120.0


def test_an_unknown_period_gives_no_threshold():
    """Which means no verdict at all, rather than a fixed guess that would call a thermometer
    measuring every ten minutes broken for doing what it was told."""
    assert adv.stale_threshold_s(None) is None
    assert adv.stale_threshold_s(0) is None
