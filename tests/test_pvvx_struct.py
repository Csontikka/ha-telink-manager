"""Unit tests for the version-aware PVVX config blob parser/builder."""

import pytest

from custom_components.telink_manager import pvvx_struct

# A representative modern (fw >= 0x50) 11-byte config blob:
#   byte0 flg  = 0x23  -> adv_type=3 (BTHome), show_batt on
#   byte1 flg2 = 0x00
#   byte2 flg3 = 0x05  -> adv_delay_raw=5
#   byte3      = 0x00  -> event_adv_cnt=0
#   byte4      = 40    -> adv_interval 2.5 s
#   byte5      = 2     -> measure multiplier
#   byte6      = 8     -> rf_tx_power
#   byte7      = 9     -> connect latency raw
#   byte8      = 100   -> lcd refresh raw (5.0 s)
#   byte9      = 0x12  -> hw_ver (read-only / runtime)
#   byte10     = 0     -> averaging off
MODERN = bytes([0x23, 0x00, 0x05, 0x00, 40, 2, 8, 9, 100, 0x12, 0])
FW_MODERN = 0x53


def test_parse_modern_fields():
    out = pvvx_struct.parse(MODERN, fw=FW_MODERN)
    assert out["fw_layout"] == "v4.7+"
    assert out["adv_type"] == "BTHome"
    assert out["adv_type_raw"] == 3
    assert out["show_batt"] is True
    assert out["temp_F"] is False
    assert out["adv_interval_raw"] == 40
    assert out["adv_interval_s"] == 2.5
    assert out["measure_period_s"] == 5.0
    assert out["adv_delay_raw"] == 5
    assert out["event_adv_cnt"] == 0
    assert out["lcd_refresh_s"] == 5.0
    assert out["hw_ver"] == 0x12


def test_parse_short_blob_returns_error():
    out = pvvx_struct.parse(b"\x00\x01\x02", fw=FW_MODERN)
    assert "error" in out
    assert out["raw"] == "000102"


def test_parse_unknown_fw_assumed_modern():
    out = pvvx_struct.parse(MODERN, fw=0)
    assert out["fw_layout"] == "v4.7+"
    assert "adv_delay_raw" in out


def test_parse_legacy_layout_offsets():
    # fw < 0x47 -> byte2/byte3 are temp/humi offsets (signed, /10).
    blob = bytearray(MODERN)
    blob[2] = 0xF6  # -10 -> -1.0 C
    blob[3] = 0x0A  # +10 -> +1.0 %
    out = pvvx_struct.parse(bytes(blob), fw=0x40)
    assert out["fw_layout"] == "legacy"
    assert out["temp_offset_c"] == -1.0
    assert out["humi_offset_pct"] == 1.0


def test_parse_transitional_exposes_raw_only():
    out = pvvx_struct.parse(MODERN, fw=0x48)
    assert out["fw_layout"] == "transitional"
    assert out["byte2_raw"] == 0x05
    assert out["byte3_raw"] == 0x00
    assert "adv_delay_raw" not in out


def test_build_roundtrip_bitflag():
    new = pvvx_struct.build(MODERN, {"temp_F": True}, fw=FW_MODERN)
    assert pvvx_struct.parse(new, fw=FW_MODERN)["temp_F"] is True
    # other bits untouched
    assert pvvx_struct.parse(new, fw=FW_MODERN)["show_batt"] is True


def test_build_roundtrip_numeric():
    new = pvvx_struct.build(MODERN, {"adv_interval_raw": 80}, fw=FW_MODERN)
    out = pvvx_struct.parse(new, fw=FW_MODERN)
    assert out["adv_interval_raw"] == 80
    assert out["adv_interval_s"] == 5.0


def test_build_measure_mult_alias():
    new = pvvx_struct.build(MODERN, {"measure_mult": 6}, fw=FW_MODERN)
    assert pvvx_struct.parse(new, fw=FW_MODERN)["measure_interval"] == 6


def test_build_ignores_unknown_keys():
    new = pvvx_struct.build(MODERN, {"not_a_field": 123}, fw=FW_MODERN)
    assert new == MODERN


@pytest.mark.parametrize(
    "changes",
    [
        {"adv_type_raw": 4},
        {"smiley": 8},
        {"adv_interval_raw": 0},
        {"adv_delay_raw": 16},
        {"lcd_refresh_raw": 0},
        {"temp_offset_c": 99.0},  # legacy range guard (tested with legacy fw below)
    ],
)
def test_build_validation_errors(changes):
    fw = 0x40 if "temp_offset_c" in changes else FW_MODERN
    with pytest.raises(ValueError):
        pvvx_struct.build(MODERN, changes, fw=fw)


def test_build_too_short_raises():
    with pytest.raises(ValueError):
        pvvx_struct.build(b"\x00\x01", {"temp_F": True}, fw=FW_MODERN)


def test_build_legacy_offsets():
    new = pvvx_struct.build(MODERN, {"temp_offset_c": -1.0, "humi_offset_pct": 2.0}, fw=0x40)
    out = pvvx_struct.parse(new, fw=0x40)
    assert out["temp_offset_c"] == -1.0
    assert out["humi_offset_pct"] == 2.0


def test_build_all_bitflags_roundtrip():
    changes = {
        "comfort_smiley": True,
        "blinking_time_smile": True,
        "temp_F": True,
        "show_batt": True,
        "tx_measures": True,
        "lp_measures": True,
        "adv_flags": True,
        "screen_off": True,
        "smiley": 5,
        "adv_type_raw": 1,
    }
    out = pvvx_struct.parse(pvvx_struct.build(MODERN, changes, fw=FW_MODERN), fw=FW_MODERN)
    assert out["comfort_smiley"] and out["blinking_time_smile"] and out["temp_F"]
    assert out["show_batt"] and out["tx_measures"] and out["lp_measures"]
    assert out["adv_flags"] and out["screen_off"]
    assert out["smiley"] == 5
    assert out["adv_type"] == "pvvx" and out["adv_type_raw"] == 1


def test_build_clear_bitflag():
    # MODERN has show_batt set; clearing it must unset the bit.
    out = pvvx_struct.parse(pvvx_struct.build(MODERN, {"show_batt": False}, fw=FW_MODERN), fw=FW_MODERN)
    assert out["show_batt"] is False


def test_build_all_numeric_roundtrip():
    changes = {
        "rf_tx_power": 7,
        "connect_latency_raw": 4,
        "lcd_refresh_raw": 50,
        "averaging": 30,
        "measure_interval": 10,
        "event_adv_cnt": 12,
    }
    out = pvvx_struct.parse(pvvx_struct.build(MODERN, changes, fw=FW_MODERN), fw=FW_MODERN)
    assert out["rf_tx_power"] == 7
    assert out["connect_latency_raw"] == 4
    assert out["lcd_refresh_raw"] == 50
    assert out["averaging"] == 30
    assert out["measure_interval"] == 10
    assert out["event_adv_cnt"] == 12


@pytest.mark.parametrize(
    "changes",
    [
        {"measure_interval": 0},
        {"event_adv_cnt": 256},
        {"rf_tx_power": 300},
        {"connect_latency_raw": -1},
        {"averaging": 256},
        {"humi_offset_pct": -99.0},  # legacy guard
    ],
)
def test_build_more_validation_errors(changes):
    fw = 0x40 if "humi_offset_pct" in changes else FW_MODERN
    with pytest.raises(ValueError):
        pvvx_struct.build(MODERN, changes, fw=fw)
