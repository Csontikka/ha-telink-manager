"""Tests for encrypted advertising: bind key helpers, the BTHome entry update, the GATT sequence,
how clone and restore treat it, and the bulk job.

The device is a stand-in holding a config blob and a key; the config entries record what was asked of them.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from custom_components.telink_manager import bulk, encryption, gatt, pvvx_struct
from custom_components.telink_manager.const import CMD_BIND_KEY, CMD_CFG, DOMAIN

MAC = "A4:C1:38:00:00:01"
OTHER = "A4:C1:38:00:00:02"
FW = 0x53
KEY = bytes(range(16))

# Modern 11-byte config, BTHome format (byte0 & 3 == 3), encryption off.
PLAIN = bytes([0x23, 0x00, 0x05, 0x00, 40, 2, 8, 9, 100, 0x12, 0])
ENCRYPTED = pvvx_struct.with_adv_crypto(PLAIN, True)
ATC = bytes([0x20, 0x00, 0x05, 0x00, 40, 2, 8, 9, 100, 0x12, 0])  # adv type 0, not BTHome

# --- bind key helpers ----------------------------------------------------------------------------


def test_new_bindkey_is_sixteen_random_bytes():
    keys = {encryption.new_bindkey() for _ in range(5)}
    assert len(keys) == 5
    assert all(len(k) == 16 for k in keys)


def test_parse_bindkey_accepts_32_hex_chars():
    assert encryption.parse_bindkey(" " + KEY.hex().upper() + " ") == KEY


@pytest.mark.parametrize("bad", ["", "abc", "zz" * 16, "00" * 15, "00" * 17])
def test_parse_bindkey_rejects_anything_else(bad):
    with pytest.raises(ValueError):
        encryption.parse_bindkey(bad)


# --- the BTHome entry ----------------------------------------------------------------------------


def _entry(unique_id=MAC, data=None, source="bluetooth", disabled_by=None):
    return SimpleNamespace(
        entry_id="entry1", unique_id=unique_id, source=source, disabled_by=disabled_by, data=data or {}
    )


class _ConfigEntries:
    """Records what is done to the entries; `fail` makes the reload blow up."""

    def __init__(self, entries, fail=False):
        self.entries = entries
        self.fail = fail
        self.reloaded = []

    def async_entries(self, domain):
        return list(self.entries) if domain == encryption.BTHOME_DOMAIN else []

    def async_update_entry(self, entry, *, data):
        entry.data = data

    async def async_reload(self, entry_id):
        if self.fail:
            raise RuntimeError("reload failed")
        self.reloaded.append(entry_id)
        return True


@pytest.fixture
def issues(monkeypatch):
    deleted = []
    monkeypatch.setattr(encryption.ir, "async_delete_issue", lambda hass, domain, issue_id: deleted.append(issue_id))
    return deleted


def _hass(*entries, fail=False):
    return SimpleNamespace(config_entries=_ConfigEntries(list(entries), fail=fail), data={})


def _advert(encrypted):
    return SimpleNamespace(service_data={encryption.BTHOME_V2_UUID: bytes([0x41 if encrypted else 0x40, 0x00])})


@pytest.fixture(autouse=True)
def last_advert(monkeypatch):
    """The newest advert Home Assistant holds for the device; none unless a test sets `.info`."""
    state = SimpleNamespace(info=None, waits=[], timeout=False, reloaded_at_wait=None)

    async def process(hass, callback, match, mode, timeout):
        state.waits.append((match, callback(_advert(True)), callback(_advert(False))))
        state.reloaded_at_wait = list(hass.config_entries.reloaded)
        if state.timeout:
            raise TimeoutError

    monkeypatch.setattr(encryption.bluetooth, "async_last_service_info", lambda hass, mac, connectable=True: state.info)
    monkeypatch.setattr(encryption.bluetooth, "async_process_advertisements", process)
    return state


def test_entry_is_matched_by_mac_in_either_case():
    entry = _entry(MAC)
    assert encryption.bthome_entry(_hass(entry), MAC.lower()) is entry
    assert encryption.bthome_entry(_hass(_entry(MAC.lower())), MAC) is not None


def test_entry_for_another_device_or_an_ignored_one_is_not_matched():
    assert encryption.bthome_entry(_hass(_entry(OTHER)), MAC) is None
    assert encryption.bthome_entry(_hass(_entry(MAC, source="ignore")), MAC) is None
    assert encryption.bthome_entry(_hass(), MAC) is None


async def test_sync_sets_the_key_and_reloads(issues):
    entry = _entry(data={"sleepy_device": False})
    hass = _hass(entry)
    assert await encryption.async_sync_bthome(hass, MAC, KEY) == "updated"
    assert entry.data == {"sleepy_device": False, "bindkey": KEY.hex()}
    assert hass.config_entries.reloaded == ["entry1"]
    assert issues == ["encryption_removed_entry1"]


async def test_setting_a_key_clears_the_downgrade_issue_the_reload_raised(monkeypatch):
    hass = _hass(_entry())
    reloaded_at_delete = []
    monkeypatch.setattr(
        encryption.ir,
        "async_delete_issue",
        lambda h, domain, issue_id: reloaded_at_delete.append(list(hass.config_entries.reloaded)),
    )
    await encryption.async_sync_bthome(hass, MAC, KEY)
    assert reloaded_at_delete == [["entry1"]]


async def test_sync_without_a_key_removes_it_and_clears_the_downgrade_issue(issues):
    entry = _entry(data={"bindkey": KEY.hex(), "sleepy_device": False})
    hass = _hass(entry)
    assert await encryption.async_sync_bthome(hass, MAC, None) == "updated"
    assert entry.data == {"sleepy_device": False}
    assert hass.config_entries.reloaded == ["entry1"]
    assert issues == ["encryption_removed_entry1"]


async def test_removing_the_key_waits_for_a_plain_advert_before_reloading(issues, last_advert):
    # A reload replays the newest advert; an encrypted one with the key gone would open a reauth flow.
    last_advert.info = _advert(encrypted=True)
    hass = _hass(_entry(data={"bindkey": KEY.hex()}))
    assert await encryption.async_sync_bthome(hass, MAC, None) == "updated"
    assert last_advert.waits == [({"address": MAC, "connectable": False}, False, True)]
    assert last_advert.reloaded_at_wait == []
    assert hass.config_entries.reloaded == ["entry1"]


async def test_removing_the_key_still_reloads_when_no_plain_advert_arrives(issues, last_advert):
    last_advert.info = _advert(encrypted=True)
    last_advert.timeout = True
    hass = _hass(_entry(data={"bindkey": KEY.hex()}))
    assert await encryption.async_sync_bthome(hass, MAC, None) == "updated"
    assert hass.config_entries.reloaded == ["entry1"]


@pytest.mark.parametrize("info", [None, _advert(encrypted=False), SimpleNamespace(service_data={})])
async def test_removing_the_key_does_not_wait_when_the_newest_advert_is_not_encrypted(issues, last_advert, info):
    last_advert.info = info
    hass = _hass(_entry(data={"bindkey": KEY.hex()}))
    assert await encryption.async_sync_bthome(hass, MAC, None) == "updated"
    assert last_advert.waits == []
    assert hass.config_entries.reloaded == ["entry1"]


async def test_setting_a_key_never_waits_for_an_advert(last_advert, issues):
    last_advert.info = _advert(encrypted=True)
    hass = _hass(_entry())
    assert await encryption.async_sync_bthome(hass, MAC, KEY) == "updated"
    assert last_advert.waits == []


async def test_sync_reports_no_entry():
    hass = _hass()
    assert await encryption.async_sync_bthome(hass, MAC, KEY) == "no_entry"
    assert hass.config_entries.reloaded == []


async def test_sync_updates_but_does_not_reload_a_disabled_entry(issues):
    entry = _entry(disabled_by="user")
    hass = _hass(entry)
    assert await encryption.async_sync_bthome(hass, MAC, KEY) == "updated"
    assert entry.data == {"bindkey": KEY.hex()}
    assert hass.config_entries.reloaded == []


async def test_a_failed_reload_is_reported_and_never_logs_the_key(issues, caplog):
    hass = _hass(_entry(), fail=True)
    with caplog.at_level(logging.DEBUG):
        assert await encryption.async_sync_bthome(hass, MAC, KEY) == "failed"
    assert KEY.hex() not in caplog.text


# --- the GATT sequence ---------------------------------------------------------------------------


class _Client:
    def __init__(self, dev):
        self.dev = dev

    async def write_gatt_char(self, char, data, response=False):
        assert data[0] == CMD_CFG
        if not self.dev.ignore_cfg:
            self.dev.blob = bytes(data[1:])
        self.dev.log.append("cfg")
        if self.dev.cfg_raises:
            raise TimeoutError("write timed out")


def _device(
    monkeypatch,
    blob=PLAIN,
    key=None,
    fw=FW,
    repeater=False,
    ignore_cfg=False,
    drop_key=False,
    no_echo=False,
    key_unreadable=False,
    readback_fails=False,
    cfg_raises=False,
):
    """A stand-in thermometer wired in where gatt would open a connection."""
    dev = SimpleNamespace(
        blob=blob,
        key=key,
        log=[],
        ignore_cfg=ignore_cfg,
        connected=0,
        readback_fails=readback_fails,
        cfg_raises=cfg_raises,
        blob_reads=0,
    )

    async def no_sleep(*_a, **_k):
        return None

    async def read_raw(client):
        return bytes([CMD_CFG, fw]) + dev.blob + b"\x00"

    async def read_blob(client):
        dev.blob_reads += 1
        return dev.blob

    async def cmd(client, opcode, payload=b"", **_kw):
        assert opcode == CMD_BIND_KEY
        if payload:
            dev.log.append("key")
            if not drop_key:
                dev.key = bytes(payload)
            if no_echo:
                return bytes([CMD_BIND_KEY])
        return bytes([CMD_BIND_KEY]) + dev.key if dev.key else bytes([CMD_BIND_KEY, 0xFF])

    async def read_all_fields(client):
        if dev.readback_fails:
            raise RuntimeError("no stable (matching) read")
        fields = pvvx_struct.parse(dev.blob, fw)
        fields["bind_key"] = None if key_unreadable or not dev.key else dev.key.hex()
        return fields

    async def with_client(hass, mac, fn, retries=2):
        dev.connected += 1
        return await fn(_Client(dev))

    monkeypatch.setattr(gatt.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(gatt, "_read_raw", read_raw)
    monkeypatch.setattr(gatt, "_read_blob", read_blob)
    monkeypatch.setattr(gatt, "_cmd", cmd)
    monkeypatch.setattr(gatt, "_read_all_fields", read_all_fields)
    monkeypatch.setattr(gatt, "_with_client", with_client)
    monkeypatch.setattr(gatt.blethr, "detect", lambda client: repeater)
    return dev


async def test_enable_writes_the_key_then_the_flag_then_updates_bthome(monkeypatch, issues):
    dev = _device(monkeypatch)
    entry = _entry()
    hass = _hass(entry)

    out = await gatt.async_set_encryption(hass, MAC, True)

    assert out["ok"] and out["enabled"] and out["ha"] == "updated"
    assert dev.log == ["key", "cfg"]
    assert len(dev.key) == 16
    assert out["bind_key"] == dev.key.hex()
    assert pvvx_struct.parse(dev.blob, FW)["adv_crypto"] is True
    assert out["fields"]["adv_crypto"] is True and out["fields"]["bind_key"] == dev.key.hex()
    assert entry.data == {"bindkey": dev.key.hex()}
    assert hass.config_entries.reloaded == ["entry1"]
    # only the flag changed in the config
    assert dev.blob == pvvx_struct.with_adv_crypto(PLAIN, True)


async def test_enable_uses_the_key_the_user_gave(monkeypatch, issues):
    dev = _device(monkeypatch)
    out = await gatt.async_set_encryption(_hass(_entry()), MAC, True, KEY.hex())
    assert out["ok"] and dev.key == KEY and out["bind_key"] == KEY.hex()


async def test_enable_keeps_the_key_of_a_device_that_is_already_encrypted(monkeypatch, issues):
    dev = _device(monkeypatch, blob=ENCRYPTED, key=KEY)
    entry = _entry(data={"bindkey": "00" * 16})
    out = await gatt.async_set_encryption(_hass(entry), MAC, True)
    assert out["ok"] and dev.key == KEY
    assert entry.data == {"bindkey": KEY.hex()}  # re-running puts HA back in step with the device


async def test_enable_on_another_format_leaves_home_assistant_alone_and_returns_the_key(monkeypatch, issues):
    _device(monkeypatch, blob=ATC)
    entry = _entry()
    hass = _hass(entry)
    out = await gatt.async_set_encryption(hass, MAC, True)
    assert out["ok"] and out["ha"] == "not_bthome" and out["adv_type"] == "atc1441"
    assert len(out["bind_key"]) == 32
    assert entry.data == {} and hass.config_entries.reloaded == []


async def test_enable_without_a_bthome_entry_returns_the_key(monkeypatch, issues):
    _device(monkeypatch)
    out = await gatt.async_set_encryption(_hass(), MAC, True)
    assert out["ok"] and out["ha"] == "no_entry" and len(out["bind_key"]) == 32


async def test_disable_clears_the_flag_keeps_the_device_key_and_drops_the_bthome_key(monkeypatch, issues):
    dev = _device(monkeypatch, blob=ENCRYPTED, key=KEY)
    entry = _entry(data={"bindkey": KEY.hex()})
    hass = _hass(entry)

    out = await gatt.async_set_encryption(hass, MAC, False)

    assert out["ok"] and out["enabled"] is False and out["ha"] == "updated"
    assert "bind_key" not in out
    assert dev.log == ["cfg"] and dev.key == KEY
    assert pvvx_struct.parse(dev.blob, FW)["adv_crypto"] is False
    assert entry.data == {}
    assert hass.config_entries.reloaded == ["entry1"]
    assert issues == ["encryption_removed_entry1"]


async def test_disable_on_a_device_that_is_already_plain_writes_nothing_but_still_cleans_bthome(monkeypatch, issues):
    dev = _device(monkeypatch)
    entry = _entry(data={"bindkey": KEY.hex()})
    out = await gatt.async_set_encryption(_hass(entry), MAC, False)
    assert out["ok"] and dev.log == [] and entry.data == {}


async def test_old_firmware_is_refused_before_anything_is_written(monkeypatch, issues):
    dev = _device(monkeypatch, fw=0x19)
    out = await gatt.async_set_encryption(_hass(_entry()), MAC, True)
    assert not out["ok"] and out["error"].startswith("validation:")
    assert dev.log == []


async def test_a_bad_user_key_is_refused_without_connecting(monkeypatch, issues):
    dev = _device(monkeypatch)
    out = await gatt.async_set_encryption(_hass(_entry()), MAC, True, "abc")
    assert not out["ok"] and out["error"].startswith("validation:")
    assert dev.connected == 0


async def test_a_repeater_is_refused(monkeypatch, issues):
    dev = _device(monkeypatch, repeater=True)
    out = await gatt.async_set_encryption(_hass(_entry()), MAC, True)
    assert not out["ok"] and "repeater" in out["error"]
    assert dev.log == []


async def test_a_key_the_device_did_not_confirm_stops_before_the_flag_and_reports_the_key(monkeypatch, issues):
    dev = _device(monkeypatch, drop_key=True)
    entry = _entry()
    hass = _hass(entry)
    out = await gatt.async_set_encryption(hass, MAC, True)
    assert out["ok"] is False and out["maybe_changed"] is True and "did not confirm" in out["error"]
    assert len(out["bind_key"]) == 32
    assert dev.log == ["key"]
    assert entry.data == {} and hass.config_entries.reloaded == []


async def test_a_device_that_does_not_echo_the_key_is_written_but_unverified(monkeypatch, issues):
    dev = _device(monkeypatch, no_echo=True)
    entry = _entry()
    out = await gatt.async_set_encryption(_hass(entry), MAC, True)
    assert out["ok"] is False and out["maybe_changed"] is True
    assert out["bind_key"] == dev.key.hex()  # it did take the key; the caller has to see it
    assert entry.data == {}


async def test_a_failed_key_read_back_after_both_writes_still_reports_the_key(monkeypatch, issues):
    dev = _device(monkeypatch, key_unreadable=True)
    entry = _entry()
    hass = _hass(entry)
    out = await gatt.async_set_encryption(hass, MAC, True)
    assert out["ok"] is False and out["maybe_changed"] is True and "Enable again" in out["error"]
    assert out["bind_key"] == dev.key.hex()
    assert pvvx_struct.parse(dev.blob, FW)["adv_crypto"] is True
    assert "ha" not in out and entry.data == {} and hass.config_entries.reloaded == []


async def test_a_flag_the_device_did_not_keep_leaves_home_assistant_alone(monkeypatch, issues):
    _device(monkeypatch, ignore_cfg=True)
    entry = _entry()
    hass = _hass(entry)
    out = await gatt.async_set_encryption(hass, MAC, True)
    assert not out["ok"] and "ha" not in out
    assert out["maybe_changed"] is True and len(out["bind_key"]) == 32  # the key was written
    assert entry.data == {} and hass.config_entries.reloaded == []


async def test_enable_that_loses_the_link_after_the_write_reports_the_key_and_skips_home_assistant(
    monkeypatch, issues, caplog
):
    dev = _device(monkeypatch, readback_fails=True)
    entry = _entry()
    hass = _hass(entry)
    with caplog.at_level(logging.DEBUG):
        out = await gatt.async_set_encryption(hass, MAC, True)
    assert out["ok"] is False and out["maybe_changed"] is True
    assert out["bind_key"] == dev.key.hex() and "Enable again" in out["error"]
    assert pvvx_struct.parse(dev.blob, FW)["adv_crypto"] is True  # the device did change
    assert "ha" not in out and entry.data == {} and hass.config_entries.reloaded == []
    assert dev.key.hex() not in caplog.text


async def test_a_config_write_that_raises_counts_as_possibly_applied(monkeypatch, issues):
    dev = _device(monkeypatch, cfg_raises=True)
    out = await gatt.async_set_encryption(_hass(_entry()), MAC, True)
    assert out["maybe_changed"] is True and out["bind_key"] == dev.key.hex()


async def test_disable_that_loses_the_link_after_the_write_says_so_without_a_key(monkeypatch, issues):
    _device(monkeypatch, blob=ENCRYPTED, key=KEY, readback_fails=True)
    entry = _entry(data={"bindkey": KEY.hex()})
    hass = _hass(entry)
    out = await gatt.async_set_encryption(hass, MAC, False)
    assert out["maybe_changed"] is True and "bind_key" not in out and "Disable again" in out["error"]
    assert entry.data == {"bindkey": KEY.hex()} and hass.config_entries.reloaded == []


async def test_rerunning_after_a_lost_link_resyncs_with_the_same_device_key(monkeypatch, issues):
    dev = _device(monkeypatch, readback_fails=True)
    first = await gatt.async_set_encryption(_hass(_entry()), MAC, True)
    dev.readback_fails = False
    entry = _entry()
    out = await gatt.async_set_encryption(_hass(entry), MAC, True)
    assert out["ok"] and out["ha"] == "updated"
    assert out["bind_key"] == first["bind_key"] == dev.key.hex()
    assert entry.data == {"bindkey": dev.key.hex()}


async def test_a_failure_before_anything_is_written_still_goes_to_the_retry_wrapper(monkeypatch, issues):
    _device(monkeypatch, readback_fails=True)

    async def boom(client):
        raise RuntimeError("no stable read")

    monkeypatch.setattr(gatt, "_read_raw", boom)
    with pytest.raises(RuntimeError):
        await gatt.async_set_encryption(_hass(_entry()), MAC, True)


# --- clone and restore ---------------------------------------------------------------------------


def _snapshot(mac, blob, key):
    return {"mac": mac, "kind": "thermometer", "raw": blob.hex(), "bind_key": key.hex()}


async def test_clone_keeps_the_targets_encryption_flag_and_refuses_the_key(monkeypatch):
    dev = _device(monkeypatch, blob=PLAIN)
    source_blob = bytearray(ENCRYPTED)
    source_blob[4] = 80  # something else that should still be cloned
    out = await gatt.async_restore(_hass(), MAC, _snapshot(OTHER, bytes(source_blob), KEY), ["config", "bind_key"])
    assert dev.blob[4] == 80
    assert pvvx_struct.parse(dev.blob, FW)["adv_crypto"] is False
    assert dev.key is None and "key" not in dev.log
    assert out["parts"] == {"config": True, "bind_key": False}
    assert out["ok"] is False


async def test_clone_onto_an_encrypted_target_keeps_it_encrypted(monkeypatch):
    dev = _device(monkeypatch, blob=ENCRYPTED, key=KEY)
    out = await gatt.async_restore(_hass(), MAC, _snapshot(OTHER, PLAIN, bytes(16)), ["config"])
    assert out["ok"]
    assert pvvx_struct.parse(dev.blob, FW)["adv_crypto"] is True


async def test_restore_onto_the_same_device_keeps_its_encryption_flag(monkeypatch):
    """An old snapshot has the flag off; restoring it must not silence a device Home Assistant decrypts."""
    old = bytearray(PLAIN)
    old[4] = 80
    dev = _device(monkeypatch, blob=ENCRYPTED, key=KEY)
    out = await gatt.async_restore(_hass(), MAC, _snapshot(MAC, bytes(old), KEY), ["config"])
    assert out["ok"]
    assert dev.blob[4] == 80
    assert pvvx_struct.parse(dev.blob, FW)["adv_crypto"] is True


async def test_restore_takes_the_flag_from_the_state_it_already_read(monkeypatch):
    dev = _device(monkeypatch, blob=ENCRYPTED, key=KEY)
    out = await gatt.async_restore(_hass(), MAC, _snapshot(MAC, PLAIN, KEY), ["config"])
    assert out["ok"]
    assert dev.blob_reads == 1  # only the verification after the write, no second read of the old config


async def test_restore_of_a_short_config_is_refused_before_connecting(monkeypatch):
    dev = _device(monkeypatch)
    snap = _snapshot(MAC, PLAIN[:8], KEY)
    out = await gatt.async_restore(_hass(), MAC, snap, ["config"])
    assert out["ok"] is False and out["error"].startswith("validation")
    assert dev.connected == 0 and dev.log == []


async def test_restore_leaves_the_config_alone_when_the_targets_flag_could_not_be_read(monkeypatch):
    dev = _device(monkeypatch, readback_fails=True)
    out = await gatt.async_restore(_hass(), MAC, _snapshot(MAC, PLAIN, KEY), ["config"])
    assert out["parts"] == {"config": False} and dev.log == []


async def test_restore_onto_a_plain_device_does_not_switch_encryption_on(monkeypatch):
    dev = _device(monkeypatch, blob=PLAIN, key=KEY)
    out = await gatt.async_restore(_hass(), MAC, _snapshot(MAC, ENCRYPTED, KEY), ["config"])
    assert out["ok"]
    assert pvvx_struct.parse(dev.blob, FW)["adv_crypto"] is False


async def test_restore_onto_the_same_device_still_restores_the_key(monkeypatch):
    dev = _device(monkeypatch, blob=ENCRYPTED, key=bytes(16))
    out = await gatt.async_restore(_hass(), MAC, _snapshot(MAC, ENCRYPTED, KEY), ["bind_key"])
    assert out["ok"] and dev.key == KEY


# --- the bulk job --------------------------------------------------------------------------------


class _BulkHass:
    def __init__(self):
        self.data = {DOMAIN: {}}
        self.tasks = []

    def async_create_background_task(self, coro, name=None):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


def _fake_encryption(monkeypatch, results):
    calls = []

    async def fake(hass, mac, enable, key_hex=None):
        calls.append((mac, enable))
        return results[mac]

    monkeypatch.setattr(bulk.gatt, "async_set_encryption", fake)
    return calls


def _done(ha, key="ab" * 16, adv="BTHome"):
    fields = {"raw": ENCRYPTED.hex(), "fw_version": "5.3", "fw_byte": FW, "bind_key": key}
    return {"ok": True, "enabled": True, "ha": ha, "bind_key": key, "adv_type": adv, "fields": fields}


async def test_bulk_enable_counts_failures_and_collects_keys_home_assistant_did_not_get(monkeypatch):
    third = "A4:C1:38:00:00:03"
    fourth = "A4:C1:38:00:00:04"
    calls = _fake_encryption(
        monkeypatch,
        {
            MAC: _done("updated"),
            OTHER: _done("no_entry", key="cd" * 16),
            third: {"ok": False, "error": "no_connectable"},
            fourth: _done("failed", key="12" * 16),
        },
    )
    hass = _BulkHass()
    devices = [{"mac": m.lower(), "proxy": "p1"} for m in (MAC, OTHER, third, fourth)]
    started = bulk.start(hass, devices, "encrypt", True)
    assert started["ok"] and started["action"] == "encrypt" and started["enable"] is True
    await hass.tasks[0]

    res = bulk.status(hass)["result"]
    assert (res["total"], res["ok"], res["fail"]) == (4, 2, 2)
    assert res["failures"][0] == {"mac": third, "error": "no_connectable"}
    # a failed BTHome update is a failure whichever way it went, so Retry failed re-syncs it, and the key is listed
    assert [f["mac"] for f in res["failures"]] == [third, fourth] and "key is listed" in res["failures"][1]["error"]
    assert res["action"] == "encrypt" and res["enable"] is True
    assert res["manual"] == [
        {"mac": OTHER, "bind_key": "cd" * 16, "adv_type": "BTHome", "reason": "no_entry"},
        {"mac": fourth, "bind_key": "12" * 16, "adv_type": "BTHome", "reason": "failed"},
    ]
    assert sorted(calls) == [(MAC, True), (OTHER, True), (third, True), (fourth, True)]
    # each changed device was snapshotted, which is where its key is kept
    assert set(bulk.status(hass)["state"]["backups"]) == {MAC, OTHER, fourth}


async def test_bulk_disable_never_asks_the_user_for_a_key(monkeypatch):
    off = {"ok": True, "enabled": False, "ha": "not_bthome", "adv_type": "atc1441", "fields": {"raw": PLAIN.hex()}}
    _fake_encryption(monkeypatch, {MAC: off})
    hass = _BulkHass()
    bulk.start(hass, [{"mac": MAC}], "encrypt", False)
    await hass.tasks[0]
    res = bulk.status(hass)["result"]
    assert res["ok"] == 1 and res["manual"] == [] and res["enable"] is False


async def test_bulk_disable_whose_bthome_update_failed_is_a_failure_not_a_success(monkeypatch):
    off = {"ok": True, "enabled": False, "ha": "failed", "adv_type": "BTHome", "fields": {"raw": PLAIN.hex()}}
    _fake_encryption(monkeypatch, {MAC: off})
    hass = _BulkHass()
    bulk.start(hass, [{"mac": MAC}], "encrypt", False)
    await hass.tasks[0]
    res = bulk.status(hass)["result"]
    assert (res["ok"], res["fail"]) == (0, 1)
    assert res["failures"][0]["mac"] == MAC and "BTHome entry still has the key" in res["failures"][0]["error"]
    assert set(bulk.status(hass)["state"]["backups"]) == {MAC}  # the device did change


async def test_bulk_lists_a_device_that_may_have_changed_with_its_key(monkeypatch):
    lost = {"ok": False, "maybe_changed": True, "enabled": True, "bind_key": "ef" * 16, "error": "may have changed"}
    _fake_encryption(monkeypatch, {MAC: lost})
    hass = _BulkHass()
    bulk.start(hass, [{"mac": MAC}], "encrypt", True)
    await hass.tasks[0]
    res = bulk.status(hass)["result"]
    assert (res["ok"], res["fail"]) == (0, 1)
    assert res["failures"] == [{"mac": MAC, "error": "may have changed"}]
    assert res["manual"] == [{"mac": MAC, "bind_key": "ef" * 16, "reason": "maybe_changed"}]


async def test_bulk_read_is_still_the_default(monkeypatch):
    async def fake_read(hass, mac, retries=3):
        return {"ok": True, "fields": {"raw": PLAIN.hex()}}

    monkeypatch.setattr(bulk.gatt, "async_read", fake_read)
    hass = _BulkHass()
    assert bulk.start(hass, [{"mac": MAC}])["action"] == "read"
    await hass.tasks[0]
    assert bulk.status(hass)["result"]["action"] == "read"
