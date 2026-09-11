"""Tests for the per-device backup store logic.

backups.py only ever touches ``hass.data`` (the Store is optional and skipped when absent), so a
tiny FakeHass dict covers the real logic without the HA test harness. Only async_setup's backfill
needs a real Store, so that one test uses the real ``hass`` fixture.
"""

from custom_components.telink_manager import backups
from custom_components.telink_manager.const import (
    BACKUP_LIMIT,
    BACKUP_STORAGE_KEY,
    DOMAIN,
    STORAGE_VERSION,
)

# 11-byte modern config blob (hex); byte[9]=hw_ver lives at hex chars 18:20 ("12").
RAW = "2300050028020809641200"
MAC = "A4:C1:38:11:22:33"


class FakeHass:
    def __init__(self, names=None):
        self.data = {DOMAIN: {}}
        if names:
            self.data[DOMAIN]["names"] = names


def _fields(raw=RAW, **kw):
    f = {"raw": raw, "fw_version": "5.3", "fw_byte": 0x53, "model": "LYWSD03MMC", "device_name": "ATC_1234"}
    f.update(kw)
    return f


# ---- pure helpers ----


def test_snapshot_maps_fields_and_has_unique_id():
    hass = FakeHass(names={MAC: "Kitchen"})
    a = backups.snapshot_from_fields(hass, MAC.lower(), _fields())
    b = backups.snapshot_from_fields(hass, MAC, _fields())
    assert a["id"] and b["id"] and a["id"] != b["id"]  # unique per snapshot
    assert a["mac"] == MAC and a["friendly_name"] == "Kitchen"
    assert a["comfort"] is None and a["sensor"] is None  # absent in fields

    full = backups.snapshot_from_fields(
        hass,
        MAC,
        _fields(
            comfort_t_lo=20,
            comfort_t_hi=26,
            comfort_h_lo=30,
            comfort_h_hi=60,
            t_slope=65536,
            t_offset_c=0.1,
            h_slope=65536,
            h_offset_pct=0.0,
        ),
    )
    assert full["comfort"] == {"t_lo": 20, "t_hi": 26, "h_lo": 30, "h_hi": 60}
    assert full["sensor"]["t_slope"] == 65536


def _sig(raw):
    return backups._state_sig({"raw": raw, "device_name": "x", "comfort": None, "bind_key": None, "sensor": None})


def test_state_sig_excludes_hw_ver_byte():
    hw_changed = RAW[:18] + "ff" + RAW[20:]  # only byte[9] differs
    real_change = "ff" + RAW[2:]  # byte[0] differs
    assert _sig(RAW) == _sig(hw_changed)  # hw_ver ignored for dedup
    assert _sig(RAW) != _sig(real_change)


def test_fw_byte_resolution():
    assert backups._fw_byte_of({"fw_byte": 0x53}) == 0x53
    assert backups._fw_byte_of({"fw": "5.3"}) == 0x53  # fallback to the 'X.Y' string
    assert backups._fw_byte_of({}) == 0


# ---- save / dedup / delete / limit (async, dict-backed, no Store) ----


async def test_save_dedups_including_hw_ver():
    hass = FakeHass()
    r1 = await backups.async_save(hass, backups.snapshot_from_fields(hass, MAC, _fields()))
    assert r1["count"] == 1 and not r1["deduped"]
    r2 = await backups.async_save(hass, backups.snapshot_from_fields(hass, MAC, _fields()))
    assert r2["deduped"] and r2["count"] == 1  # identical -> no new row
    hw = _fields(raw=RAW[:18] + "ff" + RAW[20:])
    r3 = await backups.async_save(hass, backups.snapshot_from_fields(hass, MAC, hw))
    assert r3["deduped"] and r3["count"] == 1  # hw_ver-only change still dedups
    r4 = await backups.async_save(hass, backups.snapshot_from_fields(hass, MAC, _fields(raw="ff" + RAW[2:])))
    assert not r4["deduped"] and r4["count"] == 2  # real change -> new row


async def test_save_rejects_incomplete():
    hass = FakeHass()
    assert (await backups.async_save(hass, {"mac": MAC}))["ok"] is False  # no raw


async def test_delete_by_id_removes_exactly_one():
    hass = FakeHass()
    await backups.async_save(hass, backups.snapshot_from_fields(hass, MAC, _fields(raw=RAW)))
    await backups.async_save(hass, backups.snapshot_from_fields(hass, MAC, _fields(raw="ff" + RAW[2:])))
    ids = [s["id"] for s in backups.list_for(hass, MAC)]
    assert len(set(ids)) == 2
    r = await backups.async_delete(hass, MAC, ids[0])
    assert r["count"] == 1
    assert [s["id"] for s in backups.list_for(hass, MAC)] == [ids[1]]


async def test_history_limit_trims_oldest():
    hass = FakeHass()
    for i in range(BACKUP_LIMIT + 5):
        raw = f"{i:02x}" + RAW[2:]  # vary byte[0] so each is a real change
        await backups.async_save(hass, backups.snapshot_from_fields(hass, MAC, _fields(raw=raw)))
    assert len(backups.list_for(hass, MAC)) == BACKUP_LIMIT


# ---- read/parse helpers ----


async def test_history_compare_index_parse():
    hass = FakeHass(names={MAC: "Kitchen"})
    await backups.async_save(hass, backups.snapshot_from_fields(hass, MAC, _fields()))
    assert backups.history(hass, MAC)[0]["fields"]["adv_type"] == "BTHome"
    comp = backups.compare(hass)[0]
    assert comp["mac"] == MAC and comp["fields"]["adv_type"] == "BTHome"
    idx = backups.index(hass)[0]
    assert idx["mac"] == MAC and idx["friendly_name"] == "Kitchen" and idx["count"] == 1


async def test_unparseable_blob_is_safe():
    # a corrupt raw must not crash history/compare — the fields just come back empty.
    hass = FakeHass()
    await backups.async_save(hass, backups.snapshot_from_fields(hass, MAC, _fields(raw="zz")))
    assert backups.history(hass, MAC)[0]["fields"] == {}
    assert backups.compare(hass)[0]["fields"] == {}


# ---- async_setup backfill (needs a real Store, so the real hass fixture) ----


async def test_async_setup_backfills_missing_ids(hass, hass_storage):
    hass_storage[BACKUP_STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "data": {MAC: [{"ts": 1, "raw": RAW}]},  # legacy snapshot, no id
    }
    hass.data[DOMAIN] = {}
    await backups.async_setup(hass)
    assert backups.list_for(hass, MAC)[0].get("id")  # backfilled


# ---- device kinds ----
# A repeater snapshot and a thermometer snapshot describe different hardware while using the same
# keys, so the kind has to travel with the snapshot. Anything stored before repeaters existed is a
# thermometer, and that assumption is what keeps old histories readable.

REPEATER_FIELDS = {
    "firmware_family": "blethr",
    "raw": "5500a98813e204c409",
    "fw_version": "1.2",
    "model": "LYWSD03MMC",
    "ext_mac": "A4:C1:38:30:75:7B",
    "ext_bind_key": "13a9e53d6e106f459493f7d51af37d87",
}


def test_kind_of_defaults_to_thermometer():
    assert backups.kind_of({}) == backups.KIND_THERMOMETER
    assert backups.kind_of({"kind": backups.KIND_REPEATER}) == backups.KIND_REPEATER


def test_repeater_snapshot_keeps_what_a_repeater_has():
    hass = FakeHass(names={MAC: "Hall repeater"})
    snap = backups.snapshot_from_fields(hass, MAC, REPEATER_FIELDS)
    assert backups.kind_of(snap) == backups.KIND_REPEATER
    assert snap["ext_mac"] == "A4:C1:38:30:75:7B"
    assert snap["ext_bind_key"] == REPEATER_FIELDS["ext_bind_key"]
    assert snap["friendly_name"] == "Hall repeater"
    # a repeater has none of these, and inventing empty ones would only confuse the views
    assert "comfort" not in snap and "sensor" not in snap and "device_name" not in snap


def test_thermometer_snapshot_is_labelled_too():
    snap = backups.snapshot_from_fields(FakeHass(), MAC, _fields())
    assert backups.kind_of(snap) == backups.KIND_THERMOMETER


def test_the_two_kinds_never_dedup_into_each_other():
    hass = FakeHass()
    therm = backups.snapshot_from_fields(hass, MAC, _fields())
    rpt = backups.snapshot_from_fields(hass, MAC, REPEATER_FIELDS)
    assert backups._state_sig(therm) != backups._state_sig(rpt)


def test_history_does_not_parse_a_repeater_blob_as_a_thermometer():
    hass = FakeHass()
    hass.data[DOMAIN]["backups"] = {MAC: [backups.snapshot_from_fields(hass, MAC, REPEATER_FIELDS)]}
    row = backups.history(hass, MAC)[0]
    assert row["kind"] == backups.KIND_REPEATER
    assert row["ext_mac"] == "A4:C1:38:30:75:7B"
    assert row["fields"] == {}  # the 8-byte repeater config is not an 11-byte thermometer one


def test_compare_reports_the_kind_alongside_the_fleet():
    hass = FakeHass()
    hass.data[DOMAIN]["backups"] = {
        MAC: [backups.snapshot_from_fields(hass, MAC, REPEATER_FIELDS)],
        "A4:C1:38:44:55:66": [backups.snapshot_from_fields(hass, "A4:C1:38:44:55:66", _fields())],
    }
    rows = {r["mac"]: r for r in backups.compare(hass)}
    assert rows[MAC]["kind"] == backups.KIND_REPEATER
    assert rows[MAC]["fields"] == {}
    assert rows["A4:C1:38:44:55:66"]["kind"] == backups.KIND_THERMOMETER
    assert rows["A4:C1:38:44:55:66"]["fields"]["adv_interval_raw"] == 40
