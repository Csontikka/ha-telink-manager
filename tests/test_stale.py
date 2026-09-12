"""Unit tests for the two checks that need Home Assistant's own state to answer.

Reading an advertisement is covered in test_adv.py; these are the parts that compare a device
against what we have stored about another one, or against what we asked it to do.
"""

from custom_components.telink_manager import gatt


class _Hass:
    def __init__(self):
        self.data: dict = {}


# --- the interval check --------------------------------------------------------------------------


def _with_snapshot(monkeypatch, adv_interval_s):
    """Stand in for the snapshot store, which is the only thing this reads."""
    fields = {} if adv_interval_s is None else {"adv_interval_s": adv_interval_s}
    monkeypatch.setattr(gatt.backups, "last_fields", lambda hass, mac: fields)
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


# --- naming what a write could not store ---------------------------------------------------------


def test_a_clamped_field_is_named_with_both_values():
    """Several of these fields are quantised and the device rounds to what it can hold. Saying so
    beats a bare failure for a write that mostly landed."""
    msg = gatt._write_mismatch({"measure_mult": 1}, {"measure_mult": 2, "adv_interval_raw": 160})
    assert "measure_mult" in msg
    assert "asked for 1" in msg
    assert "stored 2" in msg


def test_fields_that_landed_are_not_mentioned():
    msg = gatt._write_mismatch(
        {"measure_mult": 1, "adv_interval_raw": 160}, {"measure_mult": 2, "adv_interval_raw": 160}
    )
    assert "adv_interval_raw" not in msg


def test_every_moved_field_is_listed():
    msg = gatt._write_mismatch({"a": 1, "b": 2}, {"a": 9, "b": 8})
    assert "a: asked for 1, device stored 9" in msg
    assert "b: asked for 2, device stored 8" in msg


def test_a_difference_outside_the_requested_fields_still_says_something_useful():
    """The read-back can differ in a byte this write never named. That is still worth reporting,
    and reporting it as a clamp of a field the caller set would be wrong."""
    msg = gatt._write_mismatch({"measure_mult": 2}, {"measure_mult": 2})
    assert "did not set" in msg


def test_a_source_just_inside_the_reachable_range_is_not_called_unusable(monkeypatch):
    """The firmware accepts a period within 100 ms of the configured one, so a source at 2.9375 s
    can be followed by setting 3000: calling that unusable would send someone to change a source
    that is fine."""
    hass = _with_snapshot(monkeypatch, 2.9375)
    out = gatt._source_interval_check(hass, {"ext_mac": "AA:BB:CC:DD:EE:FF", "scan_interval_ms": 3000})
    assert "source_interval_unusable" not in out
    assert out["source_interval_ok"] is True


def test_a_source_slower_than_the_range_is_unusable_too(monkeypatch):
    """The upper end matters as much as the lower: advising someone to set 10500 would send them to
    a field that refuses it."""
    hass = _with_snapshot(monkeypatch, 10.5)
    out = gatt._source_interval_check(hass, {"ext_mac": "AA:BB:CC:DD:EE:FF", "scan_interval_ms": 10000})
    assert out["source_interval_unusable"] is True
    assert out["source_interval_ok"] is False


def test_an_unusable_source_is_never_also_reported_as_matching(monkeypatch):
    """Both were true at once for a source between 2900 and 2999 ms, and the panel gates its
    warning on the match, so nothing at all was shown."""
    hass = _with_snapshot(monkeypatch, 2.5)
    out = gatt._source_interval_check(hass, {"ext_mac": "AA:BB:CC:DD:EE:FF", "scan_interval_ms": 3000})
    assert out["source_interval_unusable"] is True
    assert out["source_interval_ok"] is False
