"""Unit tests for which listener a failed connection is blamed on.

The note names a proxy and a signal. When something can connect, that has to be the proxy the
connection went through, not a stronger passive listener that never tried. Bluetooth is stood in for.
"""

from types import SimpleNamespace

from custom_components.telink_manager import gatt

MAC = "A4:C1:38:12:34:56"


def _seen(name, rssi, connectable):
    return SimpleNamespace(
        scanner=SimpleNamespace(name=name, source=name, connectable=connectable),
        advertisement=SimpleNamespace(rssi=rssi),
    )


def _bluetooth(monkeypatch, seen, *, can_connect, last=None):
    def by_address(hass, mac, connectable=True):
        return [s for s in seen if s.scanner.connectable] if connectable else list(seen)

    monkeypatch.setattr(gatt.bluetooth, "async_scanner_devices_by_address", by_address)
    monkeypatch.setattr(
        gatt.bluetooth,
        "async_ble_device_from_address",
        lambda hass, mac, connectable=True: object() if can_connect else None,
    )
    monkeypatch.setattr(gatt.bluetooth, "async_last_service_info", lambda hass, mac, connectable=False: last)


def test_the_connecting_proxy_is_named_not_a_stronger_passive_one(monkeypatch):
    _bluetooth(monkeypatch, [_seen("passive-gw", -66, False), _seen("proxy-c3", -77, True)], can_connect=True)
    note = gatt._unreachable(None, MAC, "TimeoutError()")
    assert "-77 dBm by proxy-c3" in note
    assert "passive-gw" not in note


def test_the_best_connectable_proxy_is_named(monkeypatch):
    _bluetooth(monkeypatch, [_seen("proxy-a", -85, True), _seen("proxy-b", -70, True)], can_connect=True)
    assert "-70 dBm by proxy-b" in gatt._unreachable(None, MAC, "TimeoutError()")


def test_with_only_passive_listeners_the_strongest_is_named(monkeypatch):
    _bluetooth(monkeypatch, [_seen("passive-a", -80, False), _seen("passive-b", -60, False)], can_connect=False)
    note = gatt._unreachable(None, MAC, "no_connectable")
    assert "-60 dBm by passive-b" in note
    assert "cannot open a connection" in note


def test_the_cached_advert_is_used_when_no_scanner_lists_it(monkeypatch):
    _bluetooth(monkeypatch, [], can_connect=False, last=SimpleNamespace(rssi=-90, source="AA:BB"))
    assert "-90 dBm by AA:BB" in gatt._unreachable(None, MAC, "no_connectable")


def test_nothing_heard_at_all(monkeypatch):
    _bluetooth(monkeypatch, [], can_connect=False)
    assert "nothing has heard this device" in gatt._unreachable(None, MAC, "TimeoutError()")
