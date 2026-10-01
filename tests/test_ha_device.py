"""Unit tests for reading a thermometer's own name and room out of Home Assistant's registries.

Nothing here touches Bluetooth. The registries are stood in for, because the question is only which
device entry the answer is taken from when a thermometer has more than one.
"""

from types import SimpleNamespace

from custom_components.telink_manager import gatt

MAC = "A4:C1:38:12:34:56"


def _dev(name_by_user=None, area_id=None):
    return SimpleNamespace(name_by_user=name_by_user, area_id=area_id)


def _registries(monkeypatch, devices, areas, *, new_lookup=True):
    """Install a device registry holding `devices` and an area registry holding `areas` (id -> name)."""
    if new_lookup:
        registry = SimpleNamespace(async_get_devices=lambda connections: list(devices))
    else:
        registry = SimpleNamespace(async_get_device=lambda connections: devices[0] if devices else None)
    monkeypatch.setattr(gatt.dr, "async_get", lambda hass: registry)
    area_reg = SimpleNamespace(
        async_get_area=lambda area_id: SimpleNamespace(name=areas[area_id]) if area_id in areas else None
    )
    monkeypatch.setattr(gatt.ar, "async_get", lambda hass: area_reg)


def test_name_and_area_come_from_the_device_entry(monkeypatch):
    _registries(monkeypatch, [_dev("Fridge 1", "kitchen")], {"kitchen": "Kitchen"})
    assert gatt._ha_device(None, MAC) == ("Fridge 1", "Kitchen")


def test_a_device_never_put_in_a_room_has_no_area(monkeypatch):
    _registries(monkeypatch, [_dev("Fridge 1")], {"kitchen": "Kitchen"})
    assert gatt._ha_device(None, MAC) == ("Fridge 1", None)


def test_no_device_entry_gives_neither(monkeypatch):
    _registries(monkeypatch, [], {})
    assert gatt._ha_device(None, MAC) == (None, None)


def test_the_named_entry_decides_the_room_when_it_has_one(monkeypatch):
    """Two entries in two rooms: the one the user named is the one they look after."""
    _registries(
        monkeypatch,
        [_dev(None, "garage"), _dev("Fridge 1", "kitchen")],
        {"garage": "Garage", "kitchen": "Kitchen"},
    )
    assert gatt._ha_device(None, MAC) == ("Fridge 1", "Kitchen")


def test_an_unnamed_entry_still_supplies_the_room(monkeypatch):
    """The named entry was never placed, but another entry for the same thermometer was."""
    _registries(monkeypatch, [_dev("Fridge 1"), _dev(None, "kitchen")], {"kitchen": "Kitchen"})
    assert gatt._ha_device(None, MAC) == ("Fridge 1", "Kitchen")


def test_an_area_that_no_longer_exists_reads_as_none(monkeypatch):
    _registries(monkeypatch, [_dev("Fridge 1", "deleted")], {})
    assert gatt._ha_device(None, MAC) == ("Fridge 1", None)


def test_older_cores_use_the_single_device_lookup(monkeypatch):
    _registries(monkeypatch, [_dev("Fridge 1", "kitchen")], {"kitchen": "Kitchen"}, new_lookup=False)
    assert gatt._ha_device(None, MAC) == ("Fridge 1", "Kitchen")


def test_a_registry_failure_is_not_fatal(monkeypatch):
    def boom(hass):
        raise RuntimeError("registry not loaded")

    monkeypatch.setattr(gatt.dr, "async_get", boom)
    assert gatt._ha_device(None, MAC) == (None, None)
