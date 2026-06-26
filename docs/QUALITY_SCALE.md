# Home Assistant Quality Scale — Telink Manager

> Reference: <https://developers.home-assistant.io/docs/core/integration-quality-scale/>
> Last assessed: 2026-06-26 · Domain: `telink_manager`

## Scope note

The Home Assistant quality scale is written for **entity-based** integrations
(coordinator, devices, sensors, platforms). **Telink Manager is panel-only**: it
registers an admin sidebar panel and connects to BLE thermometers on demand. It
creates **no entities, no coordinator, no devices, no platforms, no service
actions, and needs no authentication**. As a result most rules do not apply and
are marked **exempt** with a one-line reason — that is a property of the
architecture, not a gap.

This file documents the level honestly. The `quality_scale` key is **not**
declared in `manifest.json`: it is a core-integration mechanism and would be
misleading for a panel-only custom integration where the majority of rules are
exempt.

Legend: ✅ done · ⛔ exempt (not applicable) · ⚠️ partial

## Bronze

| Rule | Status | Note |
|---|---|---|
| action-setup | ⛔ | No service actions; the panel uses WebSocket commands. |
| appropriate-polling | ⛔ | No coordinator/polling; BLE is read on demand. |
| brands | ✅ | Local `brand/icon.png` + `icon@2x.png`; accepted by HACS validation. |
| common-modules | ⛔ | No coordinator/entity base classes (panel-only). |
| config-flow-test-coverage | ✅ | `config_flow.py` at 100% test coverage. |
| config-flow | ✅ | `config_flow: true`, `async_step_user` confirmation. |
| dependency-transparency | ⛔ | `requirements: []` — no third-party packages. |
| docs-actions | ⛔ | No service actions. |
| docs-high-level-description | ✅ | README opens with a clear description. |
| docs-installation-instructions | ✅ | README → Installation + Setup. |
| docs-removal-instructions | ✅ | README → Removal. |
| entity-event-setup | ⛔ | No entities. |
| entity-unique-id | ⛔ | No entities. |
| has-entity-name | ⛔ | No entities. |
| runtime-data | ⛔ | State (`store`/`names`/`backups`) is a single global panel's, not per-entry. |
| test-before-configure | ⛔ | The flow has no connection to test (no-input confirmation). |
| test-before-setup | ⛔ | Setup registers the panel only; no external dependency to verify. |
| unique-config-entry | ✅ | `single_config_entry: true`. |

**Bronze: every applicable rule met.**

## Silver

| Rule | Status | Note |
|---|---|---|
| action-exceptions | ⛔ | No service actions. |
| config-entry-unloading | ✅ | `async_unload_entry` removes the panel. |
| docs-configuration-parameters | ⛔ | No options flow. |
| docs-installation-parameters | ⛔ | The setup step has no input fields. |
| entity-unavailable | ⛔ | No entities. |
| integration-owner | ✅ | `codeowners: ["@Csontikka"]`. |
| log-when-unavailable | ⛔ | No coordinator. Per-attempt BLE failures log at `debug`; a single `warning` is logged only when all retries are exhausted. |
| parallel-updates | ⛔ | No entity platforms. |
| reauthentication-flow | ⛔ | No authentication. |
| test-coverage | ✅ | 97.7% on the testable modules — see below. |

**Silver: every applicable rule met.**

### Test coverage

The pure-logic modules are unit-tested:

| Module | Coverage |
|---|---|
| `config_flow.py` | 100% |
| `pvvx_struct.py` | 99% |
| `const.py` | 100% |
| `backups.py` | 96% |
| **Overall (measured)** | **97.7%** |

`gatt.py` and `bulk.py` (BLE I/O over Bluetooth proxies) and the thin
WebSocket/registration glue (`websocket_api.py`, `__init__.py`) require a live
Bluetooth proxy and a running Home Assistant frontend. They are verified on a
live instance rather than mocked, and are excluded from the coverage
measurement. This is the honest limitation of an on-demand-BLE panel.

## Gold / Platinum

Most Gold rules (`devices`, `diagnostics`, `discovery`, `entity-*`,
`stale-devices`, …) are entity/device concepts that do not apply to a panel-only
integration. The documentation Gold rules are largely met by the README
(features, troubleshooting, supported devices). Platinum's `async-dependency`
and `inject-websession` are not applicable (no third-party dependency); strict
typing (`strict-typing`) could be pursued later.
