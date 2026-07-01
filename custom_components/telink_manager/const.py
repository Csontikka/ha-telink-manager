"""Telink Manager constants."""

DOMAIN = "telink_manager"

# PVVX custom GATT
CFG_CHAR = "00001f1f-0000-1000-8000-00805f9b34fb"  # config service 0x1F10 / char 0x1F1F
CMD_CFG = 0x55  # config blob read/write opcode
# Safe extra commands (same characteristic). Reply envelope: [0]=opcode echo, [1+]=data
# (NO status byte — verified live and against the PVVX firmware send_buf layout).
CMD_NAME = 0x01  # device name read (no payload) / set (UTF-8 1..20 B)
CMD_LCD = 0x22  # custom LCD overlay (temporary, not persisted)
CMD_COMFORT = 0x20  # comfort thresholds read/set (8 B)
CMD_TIME = 0x23  # RTC set: u32 LE unix timestamp
CMD_SENSOR = 0x25  # sensor calibration read/set: Tk(u32) Hk(u32) Tz(s16 x0.01C) Hz(s16 x0.01%)
CMD_SENSOR_DEF = 0x26  # sensor calibration -> factory default
# Dangerous commands (same characteristic). Each can lose the device from HA if misused.
CMD_MAC = 0x10  # MAC address read (no payload) / set (6 B, little-endian on the wire)
CMD_BIND_KEY = 0x18  # encryption bind key read (no payload) / set (exactly 16 B AES-128)
CMD_FACTORY_RESET = 0x56  # reset all config to firmware defaults; reply = fresh config blob
CMD_REBOOT = 0x72  # reboot the device (executed on disconnect; no payload)

# MAC prefix of flashable Telink thermometers
TELINK_PREFIX = "A4:C1:38"

# Persisted friend-name store (mac -> friendly name), via HA Store API.
STORAGE_KEY = "telink_manager_names"
STORAGE_VERSION = 1

# Persisted BLE-name cache (mac -> last device name read over GATT), via HA Store API. Lets the scan
# list show the real device name instead of the MAC before the next connect. Kept in its own store so
# it survives even if a device's backup history is deleted.
BLE_NAME_STORAGE_KEY = "telink_manager_ble_names"

# Persisted per-device full-state backups (mac -> [snapshot, ...]), via HA Store API.
BACKUP_STORAGE_KEY = "telink_manager_backups"
BACKUP_LIMIT = 20  # keep at most this many snapshots per device (oldest dropped)

# Panel
PANEL_URL = "telink-manager"
PANEL_TITLE = "Telink Manager"
PANEL_ICON = "mdi:thermometer-lines"
STATIC_PATH = "/telink_manager_static"
