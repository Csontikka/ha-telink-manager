"""PVVX 11-byte config blob <-> fields (version-aware decode + build for writing).

The 0x55 response layout: 0x55 (opcode) + fw_version(1B) + 11B config (+ optional trailing 00).
The config is raw[2:13]. cfg_t byte layout (verified against pvvx app.h + live device, fw 5.3):

  [0] flg  : bit0-1 adv_type(0=atc1441,1=pvvx,2=mi_like,3=BTHome),
             bit2 comfort_smiley, bit3 blinking_time_smile, bit4 temp_F(0=C),
             bit5 show_batt, bit6 tx_measures, bit7 lp_measures
  [1] flg2 : bit0-2 smiley, bit3 adv_crypto, bit4 adv_flags,
             bit5 bt5phy, bit6 longrange, bit7 screen_off (1 = display OFF)
  [2] fw <  0x47: temp_offset (int8 / 10 -> degC)                 [legacy]
      fw >  0x50: flg3 (bit0-3 adv_interval_delay x0.625ms, bit6 date_ddmm, bit7 not_day_of_week)
      0x47..0x50: transitional firmware — byte meaning undefined, left untouched
  [3] fw <  0x47: humi_offset (int8 / 10 -> %)                    [legacy]
      fw >  0x50: event_adv_cnt (uint8, duplicates per event beacon)
      0x47..0x50: transitional firmware — byte meaning undefined, left untouched

  The cutoffs match the official TelinkMiFlasher (< 0x47 = offsets, > 0x50 = flg3/event_adv_cnt;
  the 0x47..0x50 range maps neither). Our live devices are fw 0x53 (modern).
  [4] advertising_interval : uint8 x 0.0625 s
  [5] measure_interval     : uint8 (advertising-count multiplier)
  [6] rf_tx_power          : uint8 (enum)
  [7] connect_latency      : uint8 (wire = ms/20 - 1)
  [8] min_step_time_update_lcd : uint8 (wire = s / 0.05)
  [9] hw_ver               : uint8 (read-only; observed to change across reboot in the 0x55 reply,
                             so treated as runtime — always excluded from the write verify)
  [10] averaging_measurements : uint8 (flash logger window; 0 = off)

IMPORTANT: on fw >= 0x47 the real temperature/humidity offsets are NOT in this blob; they live in
the sensor calibration (CMD 0x25: slope + zero offset). Do not write them via 0x55 on modern firmware.
"""

ADV_TYPES = {0: "atc1441", 1: "pvvx", 2: "mi_like", 3: "BTHome"}

# Firmware layout cutoffs (BCD version byte), matching TelinkMiFlasher:
#   fw <  FW_LEGACY_MAX (0x47): byte[2]/[3] = temp/humi offset (legacy)
#   fw >  FW_MODERN_MIN (0x50): byte[2]/[3] = flg3 / event_adv_cnt (modern)
#   FW_LEGACY_MAX..FW_MODERN_MIN: transitional — byte[2]/[3] meaning undefined (leave untouched)
FW_LEGACY_MAX = 0x47
FW_MODERN_MIN = 0x50

# Fields the UI may modify in the 0x55 blob. Risk tiers are enforced in the UI, not here.
# Deliberately NOT writable (risky / read-only): rf_tx_power(6), connect_latency(7), hw_ver(9),
# bt5phy, longrange, adv_crypto — those are gated elsewhere or left read-only.
WRITABLE = {
    "adv_interval_raw",
    "measure_interval",
    "measure_mult",
    "temp_F",
    "screen_off",
    "comfort_smiley",
    "smiley",
    "blinking_time_smile",
    "show_batt",
    "tx_measures",
    "lp_measures",
    "adv_type_raw",
    "adv_flags",
    "adv_delay_raw",
    "event_adv_cnt",
    "averaging",
    "lcd_refresh_raw",
    "rf_tx_power",
    "connect_latency_raw",
    "temp_offset_c",
    "humi_offset_pct",  # legacy fw < 0x47 only
}


def _i8(b: int) -> int:
    return b - 256 if b >= 128 else b


def _bit(b: bytearray, idx: int, mask: int, changes: dict, key: str) -> None:
    if key in changes:
        if changes[key]:
            b[idx] |= mask
        else:
            b[idx] &= ~mask & 0xFF


def parse(blob: bytes, fw: int = 0) -> dict:
    """Decode the 11-byte config into human-readable fields. `fw` = version byte (raw[1])."""
    if len(blob) < 11:
        return {"error": f"short blob ({len(blob)} B)", "raw": blob.hex()}
    flg, flg2 = blob[0], blob[1]
    legacy = bool(fw) and fw < FW_LEGACY_MAX
    modern = (not bool(fw)) or fw > FW_MODERN_MIN  # unknown fw (0) is assumed modern (all our devices are)
    out = {
        "raw": blob.hex(),
        "fw_layout": "legacy" if legacy else ("v4.7+" if modern else "transitional"),
        # --- flg (byte0) — verified bit map ---
        "adv_type": ADV_TYPES.get(flg & 0x03, flg & 0x03),
        "adv_type_raw": flg & 0x03,
        "comfort_smiley": bool((flg >> 2) & 1),
        "blinking_time_smile": bool((flg >> 3) & 1),
        "temp_F": bool((flg >> 4) & 1),
        "show_batt": bool((flg >> 5) & 1),
        "tx_measures": bool((flg >> 6) & 1),
        "lp_measures": bool((flg >> 7) & 1),
        # --- flg2 (byte1) ---
        "smiley": flg2 & 0x07,
        "adv_crypto": bool((flg2 >> 3) & 1),
        "adv_flags": bool((flg2 >> 4) & 1),
        "bt5phy": bool((flg2 >> 5) & 1),
        "longrange": bool((flg2 >> 6) & 1),
        "screen_off": bool((flg2 >> 7) & 1),
        # --- byte4..10 (common to both layouts) ---
        "adv_interval_raw": blob[4],
        "adv_interval_s": round(blob[4] * 0.0625, 4),
        "measure_interval": blob[5],
        "measure_mult": blob[5],  # alias used by the UI
        "measure_period_s": round(blob[4] * 0.0625 * blob[5], 2),
        "rf_tx_power": blob[6],
        "connect_latency_raw": blob[7],
        "connect_latency_ms": (blob[7] + 1) * 20,
        "lcd_refresh_raw": blob[8],
        "lcd_refresh_s": round(blob[8] * 0.05, 2),
        "hw_ver": blob[9],
        "averaging": blob[10],
    }
    if legacy:
        out["temp_offset_c"] = _i8(blob[2]) / 10
        out["humi_offset_pct"] = _i8(blob[3]) / 10
    elif modern:
        flg3 = blob[2]
        out["flg3_raw"] = flg3
        out["adv_delay_raw"] = flg3 & 0x0F
        out["adv_delay_ms"] = round((flg3 & 0x0F) * 0.625, 3)
        out["date_ddmm"] = bool((flg3 >> 6) & 1)
        # NOTE: the bit7 "weekday" polarity is hardware-version dependent in the flasher;
        # we only read it, never write it, and expose the raw flg3 for callers that care.
        out["weekday_off"] = bool((flg3 >> 7) & 1)
        out["event_adv_cnt"] = blob[3]
    else:
        # transitional fw (0x47..0x50): byte[2]/[3] layout is undefined — expose raw only, never write.
        out["byte2_raw"] = blob[2]
        out["byte3_raw"] = blob[3]
    return out


def build(base_blob: bytes, changes: dict, fw: int = 0) -> bytes:
    """Read-modify-write: apply writable changes onto a freshly read blob (version-aware).

    Raises ValueError on out-of-range values. Unknown / non-writable keys are ignored.
    """
    if len(base_blob) < 11:
        raise ValueError(f"base blob too short ({len(base_blob)} B)")
    b = bytearray(base_blob)
    legacy = bool(fw) and fw < FW_LEGACY_MAX
    modern = (not bool(fw)) or fw > FW_MODERN_MIN

    # --- flg (byte0) ---
    _bit(b, 0, 0x04, changes, "comfort_smiley")
    _bit(b, 0, 0x08, changes, "blinking_time_smile")
    _bit(b, 0, 0x10, changes, "temp_F")
    _bit(b, 0, 0x20, changes, "show_batt")
    _bit(b, 0, 0x40, changes, "tx_measures")
    _bit(b, 0, 0x80, changes, "lp_measures")
    if "adv_type_raw" in changes:
        v = int(changes["adv_type_raw"])
        if not 0 <= v <= 3:
            raise ValueError("adv_type_raw must be 0..3")
        b[0] = (b[0] & ~0x03 & 0xFF) | v

    # --- flg2 (byte1) ---
    if "smiley" in changes:
        v = int(changes["smiley"])
        if not 0 <= v <= 7:
            raise ValueError("smiley must be 0..7")
        b[1] = (b[1] & ~0x07 & 0xFF) | v
    _bit(b, 1, 0x10, changes, "adv_flags")
    _bit(b, 1, 0x80, changes, "screen_off")
    # bt5phy(0x20)/longrange(0x40)/adv_crypto(0x08): intentionally not writable here (risky).

    # --- byte2/byte3 (version-dependent) ---
    if legacy:
        if "temp_offset_c" in changes:
            v = round(float(changes["temp_offset_c"]) * 10)
            if not -127 <= v <= 127:
                raise ValueError("temp_offset_c must be -12.7..+12.7")
            b[2] = v & 0xFF
        if "humi_offset_pct" in changes:
            v = round(float(changes["humi_offset_pct"]) * 10)
            if not -127 <= v <= 127:
                raise ValueError("humi_offset_pct must be -12.7..+12.7")
            b[3] = v & 0xFF
    elif modern:
        if "adv_delay_raw" in changes:
            v = int(changes["adv_delay_raw"])
            if not 0 <= v <= 15:
                raise ValueError("adv_delay_raw must be 0..15")
            b[2] = (b[2] & 0xF0) | v
        if "event_adv_cnt" in changes:
            v = int(changes["event_adv_cnt"])
            if not 0 <= v <= 255:
                raise ValueError("event_adv_cnt must be 0..255")
            b[3] = v & 0xFF
    # else: transitional fw (0x47..0x50) — byte[2]/[3] meaning undefined, leave untouched.

    # --- byte4 advertising interval ---
    if "adv_interval_raw" in changes:
        v = int(changes["adv_interval_raw"])
        if not 1 <= v <= 255:
            raise ValueError("adv_interval_raw must be 1..255 (x0.0625 s)")
        b[4] = v

    # --- byte5 measure interval (a.k.a. measure_mult) ---
    mi = changes.get("measure_interval", changes.get("measure_mult"))
    if mi is not None:
        v = int(mi)
        if not 1 <= v <= 255:
            raise ValueError("measure_interval must be 1..255")
        b[5] = v

    # --- byte6 RF TX power (enum) ---
    if "rf_tx_power" in changes:
        v = int(changes["rf_tx_power"])
        if not 0 <= v <= 255:
            raise ValueError("rf_tx_power must be 0..255")
        b[6] = v

    # --- byte7 connect latency ---
    if "connect_latency_raw" in changes:
        v = int(changes["connect_latency_raw"])
        if not 0 <= v <= 255:
            raise ValueError("connect_latency_raw must be 0..255")
        b[7] = v

    # --- byte8 min LCD refresh ---
    if "lcd_refresh_raw" in changes:
        v = int(changes["lcd_refresh_raw"])
        if not 1 <= v <= 255:
            raise ValueError("lcd_refresh_raw must be 1..255")
        b[8] = v

    # --- byte10 averaging measurements ---
    if "averaging" in changes:
        v = int(changes["averaging"])
        if not 0 <= v <= 255:
            raise ValueError("averaging must be 0..255")
        b[10] = v

    return bytes(b)
