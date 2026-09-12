"""Reading what a device puts on the air, without connecting to it.

Everything here works from a cached advertisement, so it costs no radio traffic and can run for
every device on every refresh of the panel. It is also all pure: one walk over a payload and a
few decisions about what that payload says, which is why it lives apart from the BLE and Home
Assistant plumbing in gatt.py and can be tested on real captures.

Three questions get asked of an advertisement here, and the third is the reason the module exists:

  what is the battery      some of these devices broadcast a percentage, some only a voltage
  what is the packet id    the only field guaranteed to change between two advertisements
  is a repeater relaying   whether the reading it exists to pass on is still in the packet
"""

from __future__ import annotations

from collections.abc import Iterator

BTHOME_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"
ESS_UUID = "0000181a-0000-1000-8000-00805f9b34fb"

OBJ_PACKET_ID = 0x00
OBJ_BATTERY = 0x01
OBJ_TEMPERATURE = 0x02
OBJ_VOLTAGE = 0x0C

# BTHome v2 object data lengths in bytes. Enough to walk past everything these devices send; an
# object that is not here stops the walk rather than being guessed at, because guessing a length
# means every field after it is read from the wrong offset.
BTHOME_LEN = {
    0x00: 1,
    0x01: 1,
    0x02: 2,
    0x03: 2,
    0x04: 3,
    0x05: 3,
    0x06: 2,
    0x07: 2,
    0x08: 2,
    0x09: 1,
    0x0A: 3,
    0x0B: 3,
    0x0C: 2,
    0x0D: 2,
    0x0E: 2,
    0x0F: 1,
    0x10: 1,
    0x11: 1,
    0x12: 2,
    0x13: 2,
    0x14: 2,
    0x2E: 1,
    0x2F: 1,
    0x3A: 1,
    0x3D: 2,
    0x3E: 4,
    0x3F: 2,
}


def bthome_objects(si) -> Iterator[tuple[int, bytes]]:
    """Walk a device's BTHome service data, yielding (object id, its bytes).

    Stops rather than guessing: an object whose length is unknown, or one whose data would run off
    the end of the payload, ends the walk. Yields nothing at all for an encrypted payload, since
    bit 0 of the info byte says there is nothing readable without the key.
    """
    raw = (getattr(si, "service_data", None) or {}).get(BTHOME_UUID)
    if not raw:
        return
    data = bytes(raw)
    if not data or data[0] & 0x01:
        return
    i = 1
    while i < len(data):
        size = BTHOME_LEN.get(data[i])
        if size is None or i + 1 + size > len(data):
            return
        yield data[i], data[i + 1 : i + 1 + size]
        i += 1 + size


def battery_from_adv(si) -> dict:
    """Battery percentage, and voltage where it is there, from the advertisement alone.

    Many of these devices broadcast only a voltage, so a caller that wants a percentage has to
    derive one; `battery_src` says which format the numbers came from.
    """
    batt = volt = None
    for oid, val in bthome_objects(si):
        if oid == OBJ_BATTERY:
            batt = val[0]
        elif oid == OBJ_VOLTAGE:
            volt = int.from_bytes(val, "little") / 1000.0
    if batt is not None or volt is not None:
        return {"battery": batt, "battery_v": volt, "battery_src": "bthome"}

    raw = (getattr(si, "service_data", None) or {}).get(ESS_UUID)
    if raw:
        b = bytes(raw)
        if len(b) >= 15:  # pvvx custom: MAC6 temp2 hum2 mv2 batt1 cnt1 flags1
            return {"battery": b[12], "battery_v": int.from_bytes(b[10:12], "little") / 1000.0, "battery_src": "pvvx"}
        if len(b) >= 13:  # atc1441: MAC6 temp2(BE) hum1 batt1 mv2(BE) cnt1
            return {"battery": b[9], "battery_v": int.from_bytes(b[10:12], "big") / 1000.0, "battery_src": "atc"}
    return {"battery": None, "battery_v": None, "battery_src": None}


def packet_id_from_adv(si) -> int | None:
    """The BTHome packet counter, which these devices step once per measurement.

    Not once per advertisement: the firmware advertises the same payload repeatedly between
    measurements, so how often this changes is the measurement period and not the advertising
    interval. Anything judging staleness from it has to know that period.
    """
    for oid, val in bthome_objects(si):
        if oid == OBJ_PACKET_ID:
            return val[0]
    return None


def is_relaying(si) -> bool | None:
    """Whether a repeater's advertisement still carries the reading it exists to pass on.

    A repeater that has given up on its source advertises only its own state: a packet counter,
    its voltage and its error count. The counter keeps moving, so nothing that watches for a
    stalled counter will notice, and a consumer keeps showing the last relayed temperature because
    it has no reason to drop it. What changes is that the temperature leaves the packet, and that
    is unambiguous the first time it is seen.

    None when there is no readable BTHome payload to judge at all.
    """
    seen_any = False
    for oid, _val in bthome_objects(si):
        seen_any = True
        if oid == OBJ_TEMPERATURE:  # only ever comes from the source
            return True
    return False if seen_any else None


def stale_seconds(seen: dict, mac: str, packet_id: int | None, now: float) -> float | None:
    """How long this device's packet counter has stood still, in seconds.

    `seen` is the caller's own {mac: (packet_id, when)} memory, updated in place. None when there
    is no counter to judge, and 0.0 the first time one is seen, because a single sighting cannot
    tell a device that has stopped from one that simply has not measured yet.
    """
    if packet_id is None:
        return None
    prev = seen.get(mac)
    if prev is None or prev[0] != packet_id:
        seen[mac] = (packet_id, now)
        return 0.0
    return round(now - prev[1], 1)


def stale_threshold_s(measure_period_s: float | None) -> float | None:
    """How long a counter may stand still before the device has stopped, for this device.

    The counter steps once per measurement, and that period is configurable from 0.0625 s up to
    about 68 minutes. A fixed threshold therefore either misses a slow device that really has
    stopped or, far worse, calls a healthy one stopped: the default is 10 s but nothing stops
    someone setting minutes, and a device measuring every five of them would be marked as broken
    for doing exactly what it was told.

    None when the period is unknown, which means no claim is made rather than a guess. Two minutes
    is the floor, so a fast device is not reported on the strength of one or two missed
    advertisements.
    """
    if not measure_period_s or measure_period_s <= 0:
        return None
    return max(120.0, 3.0 * measure_period_s)


def unreachable_note(
    error: str | None, *, rssi: int | None = None, source: str | None = None, connectable: bool = False
) -> str:
    """Why a connection failed, told apart into the three cases that need different actions.

    A bare `TimeoutError()` is the same message for three unrelated situations, and the useful
    information is already in Home Assistant: whether anything has heard the device lately, how
    strongly, and whether any adapter or proxy that can open a connection is among the listeners.
    On the bench a thermometer sat at -72 dBm, plainly alive in every list, and refused every
    connection from either server for an hour. That is not a broken device and not a flat battery,
    and a timeout says neither.

    Repeaters get their own note as well, because a repeater that stopped searching advertises at
    10.24 s and is hard to catch for a reason that does not apply to anything else.
    """
    err = (error or "").strip() or "no connection"
    if rssi is None:
        return (
            err + ": nothing has heard this device recently, so there was nothing to connect to. "
            "It is out of range of every adapter and proxy, switched off, or its battery is flat."
        )
    where = f" by {source}" if source else ""
    if not connectable:
        return (
            err + f": heard at {rssi} dBm{where}, but only by a listener that cannot open a "
            "connection. Passive proxies report a device without being able to talk to it. This "
            "needs a connectable proxy within range of it."
        )
    return (
        err + f": heard at {rssi} dBm{where} and something in range can open connections, but the "
        "connection did not complete. At this signal that is usually distance: an advertisement "
        "carries further than a connection holds. A proxy nearer to it is the fix, not a retry."
    )
