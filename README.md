# Telink Manager — Home Assistant Integration

![Telink Manager](https://raw.githubusercontent.com/Csontikka/ha-telink-manager/master/images/banner-v2.png)

![GitHub release (latest by date)](https://img.shields.io/github/v/release/Csontikka/ha-telink-manager?style=plastic)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=plastic)](https://github.com/hacs/integration)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg?style=plastic)](https://github.com/Csontikka/ha-telink-manager/blob/master/LICENSE)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-donate-yellow.svg?style=plastic)](https://buymeacoffee.com/Csontikka)

> **Note:** For the best viewing experience, read this documentation on [GitHub](https://github.com/Csontikka/ha-telink-manager).

A Home Assistant admin panel for configuring **Telink BLE thermometers** running [PVVX/ATC custom firmware](https://github.com/pvvx/ATC_MiThermometer) (LYWSD03MMC, MHO-C401, CGG1, and similar `A4:C1:38:*` devices). Scan, read and write every device setting straight over your existing **ESPHome / Shelly Bluetooth proxies** — no extra hardware, no cloud, no separate app.

This is a **panel-only** integration: it adds one sidebar entry and creates **no entities, sensors or polling**. Your thermometers keep advertising to the regular Bluetooth/BTHome integration exactly as before — Telink Manager only connects on demand when you read or write a device.

## Features

- **Scan** — discover every PVVX/ATC thermometer your proxies can see, with live RSSI, connectable state and battery level (parsed from the advertisement).
- **Friendly names** — assign and persist a human name per device (stored in HA, independent of the device's own BLE name).
- **Connect & read** — pull the full configuration blob plus device name, comfort thresholds, sensor calibration and bind key.
- **Write (safe settings)** — device name, comfort zones, RTC clock, LCD/display options, advertising interval, sensor calibration (and reset to factory calibration).
- **Temporary LCD overlay** — push a custom number to the screen without persisting it.
- **Dangerous settings** (clearly flagged, admin-only) — custom MAC address, encryption bind key, factory reset and reboot.
- **RAW command** — send an arbitrary command to the PVVX config characteristic (for experimentation).
- **Backup / Restore / Clone** — every read and every change is snapshotted server-side (with de-duplication and history limit). Restore any snapshot onto the same device, or **clone** a configuration onto another device (the MAC is never cloned). A safety backup of the target is always taken before an overwrite, so any restore is reversible.
- **Compare & History** — a matrix view comparing the latest config of all backed-up devices, and a per-device timeline of every snapshot — all without touching BLE.
- **Read all** — a server-side bulk job that reads every device in parallel, grouped by proxy, with retries. It survives page refresh and navigation (progress is polled from the server) so you can walk away and come back.
- **Battery column** — estimated charge and voltage from the advertisement, colour-coded.

## Screenshots

**Main panel** — scan results with friendly names, RSSI, battery and per-device actions:

![Telink Manager panel](https://raw.githubusercontent.com/Csontikka/ha-telink-manager/master/images/panel_scan.png)

**Device configuration** — read and edit the full PVVX configuration (display, measurement, advertising):

![Device configuration](https://raw.githubusercontent.com/Csontikka/ha-telink-manager/master/images/panel_config.png)

**Compare** — every device's settings side by side, differences highlighted (from backups, no connection):

![Cross-device compare matrix](https://raw.githubusercontent.com/Csontikka/ha-telink-manager/master/images/panel_compare.png)

**Backups** — full-state snapshots per device with restore, clone and history (no connection needed):

![Per-device backups](https://raw.githubusercontent.com/Csontikka/ha-telink-manager/master/images/panel_backups.png)

## Requirements

- Home Assistant **2024.7.0** or newer.
- At least one working **Bluetooth proxy** (ESPHome `bluetooth_proxy` with `active: true`, or a Shelly Gen2/Plus device acting as a proxy) **or** a local Bluetooth adapter, within range of the thermometers. Writing settings requires an **active**, connectable proxy.
- Thermometers flashed with **PVVX or ATC custom firmware** (stock Xiaomi firmware is not supported).

## Installation

### HACS (recommended)

1. Open HACS → **Integrations**.
2. Click the three-dot menu → **Custom repositories**.
3. Add `https://github.com/Csontikka/ha-telink-manager` with category **Integration**.
4. Click **Download**.
5. Restart Home Assistant.

### Manual

1. Copy `custom_components/telink_manager/` to your HA `config/custom_components/` directory.
2. Restart Home Assistant.

## Setup

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **Telink Manager** and select it.
3. Confirm — there is nothing to configure. A **Telink Manager** entry appears in the sidebar (admin users only).

That's it. Open the panel and press **Scan**.

## Usage notes & safety

- **Reading is always safe.** It opens a short BLE connection, reads the configuration and disconnects cleanly.
- **Writing safe settings** (name, comfort, time, display, calibration) cannot brick a device — the worst case is a failed write you can simply retry.
- **Dangerous settings change device identity:**
  - **Custom MAC** — after a reboot the thermometer advertises under the new MAC, so Home Assistant (and any BTHome sensors) will see it as a *new* device. Update your other integrations accordingly.
  - **Bind key** — changing the encryption key will break decryption for any integration still using the old key.
  - **Factory reset** — clears all custom configuration back to firmware defaults.
  - These actions are grouped, labelled as dangerous, and require admin access to the panel.
- **Clone never copies the MAC.** Cloning writes a source device's settings onto a *different* target device while leaving the target's own MAC intact, so you never end up with two devices sharing one address.
- If a write fails or a device "disappears" mid-operation, it is usually a weak proxy link — move the device closer to a proxy, or use a proxy with a stronger signal, and retry.

## Removal

1. Go to **Settings → Devices & Services → Telink Manager**.
2. Click the three-dot menu → **Delete**. The sidebar panel is removed.
3. Optionally delete `custom_components/telink_manager/` and restart Home Assistant.

Saved friendly names and backups are kept in HA storage; they are only removed if you delete the corresponding `.storage/telink_manager_*` files.

## Troubleshooting

Enable debug logging by adding to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.telink_manager: debug
```

This logs every BLE connection attempt, which proxy was chosen, and the raw bytes exchanged with each device.

## Privacy & telemetry

Telink Manager collects **no telemetry and never phones home**. It runs entirely inside your Home Assistant instance: it talks only to your thermometers (over your own Bluetooth proxies) and stores friendly names and backups in your local HA storage. No usage data, device identifiers (including BLE MAC addresses), or analytics are sent to the developer or any third party.

The only place this integration ever appears in any statistics is Home Assistant's own [Analytics](https://www.home-assistant.io/integrations/analytics/), which is **opt-in**, anonymized and aggregated. If you have enabled HA Analytics, this integration's name and version are included — exactly like every other custom integration — and show up only as anonymous totals on the public [analytics dashboard](https://analytics.home-assistant.io/integrations/). You can turn it off at any time in **Settings → System → Analytics**.

## Support

Found a bug or have an idea? [Open an issue](https://github.com/Csontikka/ha-telink-manager/issues) — feedback and feature requests are welcome.

If you find this integration useful, consider [buying me a coffee](https://buymeacoffee.com/Csontikka).

## Credits

Built on the excellent reverse-engineering work of the [pvvx/ATC_MiThermometer](https://github.com/pvvx/ATC_MiThermometer) firmware project. This integration is not affiliated with Telink, Xiaomi or the PVVX project.
