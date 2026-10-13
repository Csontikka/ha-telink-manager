"""Unit tests for the time the device clocks are set to.

The firmware has no time zone, so it is given Home Assistant's local wall-clock time as if it were
UTC. The browser's zone must not leak in, so the backend picks the time when the caller does not.
"""

import struct
from datetime import datetime, timedelta, timezone

from custom_components.telink_manager import gatt

MAC = "A4:C1:38:12:34:56"


def _ha_now(monkeypatch, iso):
    monkeypatch.setattr(gatt.dt_util, "now", lambda: datetime.fromisoformat(iso))


def test_local_now_is_the_wall_clock_read_as_utc(monkeypatch):
    _ha_now(monkeypatch, "2026-10-13T14:30:05+02:00")
    want = int(datetime(2026, 10, 13, 14, 30, 5, tzinfo=timezone.utc).timestamp())
    assert gatt.local_now() == want


def test_local_now_follows_the_offset_across_dst(monkeypatch):
    # The same instant either side of a DST change reads an hour apart on the wall clock.
    _ha_now(monkeypatch, "2026-10-25T02:30:00+02:00")
    summer = gatt.local_now()
    _ha_now(monkeypatch, "2026-10-25T01:30:00+01:00")
    assert summer - gatt.local_now() == 3600


def test_local_now_west_of_utc(monkeypatch):
    _ha_now(monkeypatch, "2026-01-01T00:00:00-05:00")
    assert gatt.local_now() == int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())


def _capture_client(monkeypatch):
    sent = []

    async def fake_cmd(client, cmd, payload=b""):
        sent.append(payload)
        return bytes([cmd]) + payload

    async def fake_with_client(hass, mac, fn):
        return await fn(object())

    monkeypatch.setattr(gatt, "_cmd", fake_cmd)
    monkeypatch.setattr(gatt, "_with_client", fake_with_client)
    return sent


async def test_set_time_without_ts_sends_home_assistant_time(monkeypatch):
    _ha_now(monkeypatch, "2026-10-13T14:30:05+02:00")
    sent = _capture_client(monkeypatch)
    out = await gatt.async_set_time(None, MAC)
    want = gatt.local_now()
    assert struct.unpack("<I", sent[0])[0] == want
    assert out["sent"] == want and out["device_time"] == want


async def test_set_time_is_taken_after_the_connection_is_up(monkeypatch):
    # A proxy connection can take tens of seconds; the clock must not lag behind by that.
    _ha_now(monkeypatch, "2026-10-13T14:30:05+02:00")
    sent = _capture_client(monkeypatch)
    inner = gatt._with_client

    async def slow_connect(hass, mac, fn):
        _ha_now(monkeypatch, "2026-10-13T14:30:25+02:00")
        return await inner(hass, mac, fn)

    monkeypatch.setattr(gatt, "_with_client", slow_connect)
    await gatt.async_set_time(None, MAC)
    assert struct.unpack("<I", sent[0])[0] == gatt.local_now()


async def test_set_time_keeps_an_explicit_ts(monkeypatch):
    sent = _capture_client(monkeypatch)
    await gatt.async_set_time(None, MAC, 1234567890)
    assert struct.unpack("<I", sent[0])[0] == 1234567890


async def test_rename_restores_the_clock_in_home_assistant_time(monkeypatch):
    _ha_now(monkeypatch, "2026-10-13T14:30:05+02:00")
    sent = _capture_client(monkeypatch)
    monkeypatch.setattr(gatt, "NAME_REBOOT_SETTLE_S", 0)
    out = await gatt.async_set_name(None, MAC, "Kitchen", restore_clock=True)
    assert out["clock_restored"] is True
    assert struct.unpack("<I", sent[-1])[0] == gatt.local_now()


async def test_rename_leaves_the_clock_alone_unless_asked(monkeypatch):
    sent = _capture_client(monkeypatch)
    out = await gatt.async_set_name(None, MAC, "Kitchen")
    assert len(sent) == 1 and "clock_restored" not in out


def test_dst_fixture_is_one_instant():
    a = datetime.fromisoformat("2026-10-25T02:30:00+02:00")
    b = datetime.fromisoformat("2026-10-25T01:30:00+01:00")
    assert a == b and a.utcoffset() - b.utcoffset() == timedelta(hours=1)
