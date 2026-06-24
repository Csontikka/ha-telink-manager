// Telink Manager — standalone HA panel (vanilla JS).
//
// Flow:
//  - On load, a Scan runs automatically.
//  - Click a row to select, then "Connect" (or double-click); the result opens in a MODAL.
//  - The modal starts in read-only VIEW mode. "Edit" switches to EDIT mode (inputs + Save/Cancel).
//  - Save writes via read-modify-write + verify, shows progress, then returns to VIEW with fresh data.
//
// Every WS call is one atomic connect+op+disconnect cycle: the backend ALWAYS disconnects in a
// finally block, so even if the user closes the modal / navigates away the link is released and the
// thermometer never gets stuck "connected". A token (_op) makes stale results be ignored.

// HTML-escape any device- or user-supplied string before it is interpolated into innerHTML.
// Device-advertised BLE names, on-device names, firmware reply strings and user-typed friendly
// names are all untrusted (a BLE advertiser in range can broadcast an arbitrary name), so every
// such value must be escaped to prevent HTML/script injection in the admin's browser.
const escHtml = (s) =>
  String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

const ADV_TYPES = [
  [0, "atc1441"],
  [1, "pvvx"],
  [2, "mi_like"],
  [3, "BTHome (HA default)"],
];

// Yellow warnings shown above Save when a risky field is changed (keyed by _collectChanges key).
const RISK_NOTES = {
  adv_type_raw: "⚠️ Changing the advertising format: Home Assistant (or whatever decodes this sensor) must support the new format, or it will stop receiving data. Reversible from this panel.",
  rf_tx_power: "⚠️ Lower TX power = shorter range. Too low and the device gets hard/impossible to reach. Change in small steps.",
  connect_latency_raw: "⚠️ An unusual connection latency can make connecting unreliable.",
};

// RF TX power options (antenna-referenced, from the flasher). value = raw cfg byte.
const RF_TX_POWER = [
  [191, "+3.0 dBm (max)"], [189, "+2.8"], [187, "+2.6"], [185, "+2.4"], [182, "+2.0"],
  [180, "+1.7"], [178, "+1.5"], [176, "+1.2"], [174, "+0.9"], [172, "+0.6"], [169, "0.0 dBm"],
  [168, "-0.1"], [164, "-1.0"], [162, "-1.4"], [160, "-1.9"], [158, "-2.5"], [156, "-3.0"],
  [154, "-3.6"], [152, "-4.3"], [150, "-5.0"], [148, "-5.8"], [146, "-6.7"], [144, "-7.7"],
  [142, "-8.7"], [140, "-9.9"], [138, "-11.4"], [136, "-13.3"], [134, "-15.9"], [132, "-19.3"],
  [130, "-25.2 dBm (min)"],
];

// Tooltips, distilled from the TelinkMiFlasher.html field descriptions.
const TIPS = {
  adv_interval: "How often the sensor broadcasts a BLE advertisement. The firmware stores it as a single byte in 0.0625 s (1/16 s) steps, so 'raw 40' means 40 × 0.0625 = 2.5 s (raw 16 = 1 s, raw 80 = 5 s). Lower = fresher data in HA but more battery drain. Range raw 1…255 (0.0625 s … ~16 s).",
  measure_mult: "Number of advertisements between two real sensor measurements. Effective measurement period = advertising interval × multiplier.",
  measure_period: "Time between two real temperature/humidity measurements (= advertising interval × measure multiplier). Editing this recomputes the multiplier.",
  effective: "How often a NEW temperature/humidity sample is taken (= advertising interval × measure multiplier). This is NOT how often data is sent — see 'Data broadcast'.",
  data_broadcast: "How often the device actually broadcasts data over BLE (= the advertising interval). Between measurements it re-sends the last measured value, so HA gets a packet every advertising interval.",
  tunit: "Temperature unit shown on the LCD (°C or °F). Does not change what HA receives.",
  disp: "Turn the LCD on or off. Off saves battery; the sensor still advertises.",
  toff: "Calibration offset added to the reported temperature (-12.5 … +12.5 °C).",
  hoff: "Calibration offset added to the reported humidity (-12.5 … +12.5 %).",
  smiley: "Which face/icon is shown on the LCD (0–7). Display only.",
  comfort_smiley: "Show a comfort indicator (smiley) based on temperature/humidity comfort zone.",
  blinking_time_smile: "Blink the time/smiley field on the LCD.",
  show_batt: "Show the battery percentage on the LCD.",
  adv_type: "Advertising format. HA's bluetooth/BTHome integration expects BTHome. Changing this to atc1441/pvvx/mi_like will make HA STOP seeing the sensor until you change it back via this tool.",
  tx_power: "Radio transmit power (firmware enum). Higher = better range, more battery use. Read-only here.",
  bt5phy: "Bluetooth 5 PHY / Long Range (Coded PHY) advertising. Read-only here.",
  adv_crypto: "Advertisements are encrypted. HA cannot decode encrypted pvvx/BTHome without the bind key. Read-only here.",
  raw: "Raw 11-byte configuration block (hex) as read back from the device.",
  firmware: "Firmware version reported by the device (Device Information Service; fallback: the version byte from the 0x55 response).",
  model: "Model number string reported by the device (Device Information Service).",
  hw_ver: "Hardware revision id reported by the device (read-only).",
  lcd_refresh: "Minimum time between LCD updates — prevents flicker from rapid sensor changes.",
  lp_meas: "Take sensor measurements in low-power sleep mode (saves battery, slightly less precise/fast).",
  tx_meas: "When BLE-connected, automatically stream all measurements to the client.",
  averaging: "Averaging window for the on-device flash logger (0 = logging off). Requires the clock to be set.",
  adv_flags: "Add the standard BLE Advertising Flags field to packets — improves compatibility with 3rd-party software.",
  adv_delay: "A pseudo-random delay (0..9.375 ms) added to each advertising interval so events drift and collide less.",
  event_adv_cnt: "How many duplicate packets are sent for each triggered/event advertisement.",
  connect_latency: "BLE connection latency stored on the device (read-only here).",
  pincode: "Intentionally NOT editable here. Setting a BLE PIN would likely lock this panel out (the HA proxy can't do BLE pairing), and a forgotten PIN needs a hardware flasher. Manage the PIN only with the flasher.",
  sensor_cal: "Calibration offset applied to the temperature/humidity reading (sensor settings, command 0x25).",
  sensor_slope: "Linear scaling factor for the raw sensor ADC value (advanced; default 65536 = ×1.0).",
  device_name: "The BLE name stored ON the thermometer (advertised name, e.g. ATC_xxxx). This is different from the Friendly name, which is only a local label in this panel. Empty = reset to factory default.",
  comfort: "Comfort thresholds that drive the comfort smiley on the LCD. Low must be below High.",
  clock: "Set the device's clock. Sends the current time adjusted to your timezone so the LCD shows local time.",
  device_clock: "The clock currently stored on the device (shown as it appears on the LCD), and how far it is from the real local time now.",
  lcd: "Show numbers on the LCD (temporary overlay, not saved). Big number has one decimal (e.g. 22.2); small number is a whole number. With a finite duration the firmware alternates the overlay with the normal reading (it blinks); tick 'Keep on screen' for a steady display, then use Clear to remove it.",
};

class TelinkManagerPanel extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    this._maybeAutoScan();
  }

  connectedCallback() {
    if (this._rendered) {
      // reused element re-attached to the DOM: re-check the (server-side) bulk job and show progress
      this._startBulkPolling();
      return;
    }
    this._rendered = true;
    this._selected = null;    // selected MAC (not connected)
    this._selectedRssi = null;
    this._loaded = null;      // last read fields (baseline for diff)
    this._names = {};         // mac -> friendly name (from last scan)
    this._devs = [];          // last scan result
    this._sortKey = null;     // current sort column (null = backend order: connectable + signal)
    this._sortDir = "desc";
    this._busy = false;       // a scan/read is in flight
    this._op = 0;             // op token; Cancel/close/navigate bumps it so stale results are ignored
    this._autoScanned = false;
    this._lcdPermanentActive = false;   // a permanent LCD overlay was left on the device
    this._lcdPermanentMac = null;
    this._inflightMacs = new Set();   // MACs with a backend connect op still running (independent of UI/Cancel)
    this._connTimer = null;
    this._backupMacs = new Map();     // mac -> backup count (for the Backup column)
    this.innerHTML = `
      <style>
        /* ---- design tokens (layered on the active HA theme, so it fits any theme) ---- */
        telink-manager-panel {
          --tm-accent: var(--primary-color, #03a9f4);
          --tm-accent-soft: color-mix(in srgb, var(--primary-color, #03a9f4) 16%, transparent);
          --tm-bg: var(--card-background-color, #1e1e1e);
          --tm-bg-2: var(--secondary-background-color, #181818);
          --tm-text: var(--primary-text-color, #e8eaed);
          --tm-text-2: var(--secondary-text-color, #9aa0a6);
          --tm-border: var(--divider-color, #3a3a3a);
          --tm-danger: #e5484d;
          --tm-warn: #ffb300;
          --tm-ok: #4caf50;
          --tm-radius: 8px;
          --tm-radius-lg: 12px;
        }
        .wrap { padding: 18px; max-width: 1000px; margin: 0 auto;
          font-family: var(--paper-font-body1_-_font-family, system-ui, sans-serif);
          color: var(--tm-text); }
        h1 { font-size: 20px; font-weight: 600; letter-spacing: .01em; } h3 { margin: 14px 0 6px; font-weight: 600; }
        /* Button color semantics (consistent across the whole panel):
           - default (blue, filled) = primary / confirming action (Scan, Connect, Edit, Save, the safe "Set …")
           - .ghost / .cancel (neutral outline) = secondary / navigation / cancel (Back, Close, Cancel, Read current, Randomize, Clear, Reboot)
           - .choice (accent outline chip) = pick-one option in a chooser (e.g. CSV / YAML)
           - .danger (red, filled) = destructive / risky (Set MAC, Set bind key, Factory reset, Send raw) */
        button { background: var(--tm-accent); color: #fff; border: 1px solid transparent;
          border-radius: var(--tm-radius); padding: 8px 15px; cursor: pointer; font-size: 14px;
          font-weight: 500; margin-right: 8px; line-height: 1.2;
          transition: filter .15s, background .15s, border-color .15s, color .15s, transform .05s; }
        button:hover:not(:disabled) { filter: brightness(1.08); }
        button:active:not(:disabled) { transform: translateY(1px); }
        button:focus-visible { outline: 2px solid var(--tm-accent); outline-offset: 2px; }
        button:disabled { opacity: .4; cursor: default; }
        button.ghost, button.cancel { background: transparent; color: var(--tm-text-2);
          border: 1px solid var(--tm-border); }
        button.ghost:hover:not(:disabled), button.cancel:hover:not(:disabled) {
          color: var(--tm-text); border-color: var(--tm-accent); background: var(--tm-accent-soft); filter: none; }
        button.choice { background: var(--tm-accent-soft); color: var(--tm-accent);
          border: 1px solid var(--tm-accent); font-weight: 600; }
        button.choice:hover:not(:disabled) { background: var(--tm-accent); color: #fff; filter: none; }
        button.danger { background: var(--tm-danger); color: #fff; border-color: transparent; }
        .status { margin-top: 10px; font-size: 13px; color: var(--tm-text-2); min-height: 18px; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; }
        th, td { text-align: center; padding: 7px 8px; border-bottom: 1px solid var(--tm-border); font-size: 13px; }
        th { text-align: center; }
        thead th { background: var(--tm-bg-2); border-bottom: 2px solid var(--tm-accent); text-transform: uppercase; letter-spacing: .05em; font-size: 11px; color: var(--tm-text-2); padding: 10px 8px; position: sticky; top: 0; z-index: 1; }
        th.sortable { cursor: pointer; user-select: none; white-space: nowrap; }
        th.sortable:hover { color: var(--tm-text); }
        tr.dev { cursor: pointer; transition: background .12s; } tr.dev:hover { background: var(--tm-bg-2); }
        tr.sel { background: color-mix(in srgb, var(--tm-accent) 22%, transparent) !important; box-shadow: inset 3px 0 0 var(--tm-accent); }
        tr.busy { cursor: default; }
        .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; }
        .on { background: var(--tm-ok); } .off { background: #777; }
        .fld { display: flex; align-items: center; padding: 5px 0; font-size: 13px; }
        .lab { width: 240px; color: var(--tm-text-2); cursor: help; }
        input, select { background: var(--tm-bg-2);
          color: var(--tm-text); border: 1px solid var(--tm-border);
          border-radius: 6px; padding: 6px 8px; font-size: 13px; transition: border-color .15s, box-shadow .15s; }
        input:focus, select:focus { outline: none; border-color: var(--tm-accent);
          box-shadow: 0 0 0 2px var(--tm-accent-soft); }
        input[type=number] { width: 110px; }
        input[type=checkbox] { width: auto; accent-color: var(--tm-accent); }
        input.fname { width: 130px; }
        .ro .lab { color: #888; } .ro b { color: #bbb; }
        .muted { color: var(--tm-text-2); font-size: 12px; }
        .warn { color: #ff7a7a; font-size: 12px; }
        .topbar { display: flex; align-items: center; justify-content: space-between; }
        .bmc { font-size: 12px; color: var(--tm-text-2); text-decoration: none;
          opacity: .7; padding: 5px 10px; border: 1px solid var(--tm-border);
          border-radius: var(--tm-radius); white-space: nowrap; transition: all .15s; }
        .bmc:hover { opacity: 1; color: #ffcc33; border-color: #ffcc33; }
        .dangerzone { margin-top: 12px; padding: 12px; border: 1px solid var(--tm-danger);
          background: color-mix(in srgb, var(--tm-danger) 8%, transparent); border-radius: var(--tm-radius-lg); }
        .advzone { margin-top: 12px; padding: 12px; border: 1px solid var(--tm-warn);
          background: color-mix(in srgb, var(--tm-warn) 8%, transparent); border-radius: var(--tm-radius-lg); }
        .advnote { color: #ffc16a; font-size: 12px; }
        /* modal */
        .overlay { position: fixed; inset: 0; background: rgba(0,0,0,.5);
          backdrop-filter: blur(2px); display: flex; align-items: center; justify-content: center; z-index: 9999; }
        .modal { background: var(--tm-bg); color: var(--tm-text);
          border: 1px solid var(--tm-border); border-radius: var(--tm-radius-lg); padding: 18px;
          width: min(660px, 94vw); max-height: 88vh; overflow: hidden; display: flex; flex-direction: column;
          box-shadow: 0 18px 50px rgba(0,0,0,.55); }
        .modal-head { display: flex; align-items: center; justify-content: space-between; flex: 0 0 auto; padding-bottom: 12px; border-bottom: 1px solid var(--tm-border); }
        .modal-head h3 { margin: 0; font-weight: 600; }
        #m-body { flex: 1 1 auto; overflow-y: auto; min-height: 0; padding: 10px 0; }
        #m-status { flex: 0 0 auto; }
        .x { background: transparent; color: var(--tm-text-2); font-size: 18px; padding: 2px 8px; margin: 0; border: none; border-radius: 6px; }
        .x:hover:not(:disabled) { background: var(--tm-bg-2); color: var(--tm-text); filter: none; }
        .actions { margin-top: 0; padding-top: 12px; border-top: 1px solid var(--tm-border); display: flex; gap: 8px; flex: 0 0 auto; }
        .spinner { display: inline-block; width: 13px; height: 13px; border: 2px solid var(--tm-border);
          border-top-color: var(--tm-accent); border-radius: 50%;
          animation: spin .8s linear infinite; vertical-align: middle; margin-right: 6px; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .sig { display: inline-flex; gap: 2px; align-items: flex-end; height: 14px; vertical-align: middle; margin-right: 6px; }
        .sig i { width: 3px; border-radius: 1px; display: inline-block; }
      </style>
      <div class="wrap">
        <div class="topbar">
          <h1>🌡️ Telink Manager <span class="muted" style="font-size:11px;font-weight:normal">by Csontikka @ 2026</span></h1>
          <a class="bmc" href="https://buymeacoffee.com/csontikka" target="_blank" rel="noopener"
             title="Support the developer — buy me a coffee">☕ Buy me a coffee</a>
        </div>
        <div style="display:flex; align-items:center; justify-content:space-between; gap:10px; margin-top:8px">
          <div>
            <button id="read" disabled>Connect</button>
            <button id="cancel" class="cancel" style="display:none">Cancel</button>
          </div>
          <div style="display:flex; align-items:center; gap:10px">
            <button id="compare-btn" class="ghost" title="Compare all sensors' settings side by side (from backups, no connection)">📊 Compare</button>
            <button id="backups-btn" class="ghost" title="Saved backups for all devices (no connection needed)">🗄️ Backups</button>
            <button id="readall-btn" class="ghost" title="Connect to many devices in one go and back them up (parallel by proxy)">📖 Read all</button>
            <button id="scan">Scan</button>
          </div>
        </div>
        <div class="status" id="status"></div>
        <div id="bulk-strip" style="display:none"></div>
        <div id="list"></div>
      </div>
      <div id="modalRoot"></div>`;
    this.querySelector("#scan").onclick = () => this._scan();
    this.querySelector("#read").onclick = () => this._readSelected();
    this.querySelector("#cancel").onclick = () => this._cancel();
    // Backups: if a device is selected, open ITS backups directly; otherwise the global picker.
    this.querySelector("#backups-btn").onclick = () =>
      this._selected ? this._openDeviceBackups(this._selected) : this._openBackupsModal();
    this.querySelector("#compare-btn").onclick = () => this._openCompareModal();
    this.querySelector("#readall-btn").onclick = () => this._openReadAllDialog();
    this._maybeAutoScan();
    // Re-attach to a "Read all" that is still running (or just finished) on the BACKEND, so its
    // progress strip / result reappear after navigating away and back (or after an F5).
    this._startBulkPolling();
  }

  disconnectedCallback() {
    // Navigated away: drop any in-flight single-read result. The backend still finishes + disconnects.
    this._op++;
    this._busy = false;
    // Stop polling on this detached element — the job keeps running server-side; a fresh panel
    // re-attaches to it on connect.
    this._stopBulkPolling();
    // Also stop the connect elapsed-timer so a detached element leaves no interval running.
    this._stopConnTimer();
  }

  _maybeAutoScan() {
    if (this._rendered && !this._autoScanned && this._hass && this._hass.connection) {
      this._autoScanned = true;
      this._scan();
    }
  }

  _status(t) { this.querySelector("#status").textContent = t || ""; }
  _mstatus(t, busy) {
    const el = this.querySelector("#m-status");
    if (el) el.innerHTML = (busy ? `<span class="spinner"></span>` : "") + (t || "");
  }

  async _ws(msg) { return await this._hass.connection.sendMessagePromise(msg); }

  // Readable error text from anything (string, Error, or a HA WS error object {code,message}).
  _errMsg(e) {
    if (e == null) return "unknown error";
    if (typeof e === "string") return e;
    if (e.message) return e.message;
    if (e.error) return typeof e.error === "string" ? e.error : (e.error.message || JSON.stringify(e.error));
    if (e.code) return `${e.code}`;
    try { return JSON.stringify(e); } catch (_) { return String(e); }
  }

  async _saveName(mac, name) {
    try {
      const r = await this._ws({ type: "telink_manager/set_name", mac, name });
      this._names[mac] = r.name || "";
      const dev = this._devs.find(d => d.mac === mac);
      if (dev) dev.friend_name = r.name || "";
      this._status(`Friendly name for ${mac}: ${r.name || "(cleared)"}`);
    } catch (e) {
      this._status("Save name failed: " + this._errMsg(e));
    }
  }

  // ---- top-panel busy guard (scan/read) ----
  _beginOp(label, topCancel = true) {
    this._busy = true;
    const token = ++this._op;
    this._status(label);
    this.querySelector("#scan").disabled = true;
    this.querySelector("#read").disabled = true;
    const ra = this.querySelector("#readall-btn"); if (ra) ra.disabled = true;
    const c = this.querySelector("#cancel"); c.style.display = topCancel ? "inline-block" : "none"; c.disabled = false;
    this.querySelectorAll("tr.dev").forEach(tr => tr.classList.add("busy"));
    return token;
  }
  _endOp() {
    this._busy = false;
    this.querySelector("#scan").disabled = false;
    this.querySelector("#read").disabled = this._readDisabled();
    const ra = this.querySelector("#readall-btn"); if (ra) ra.disabled = this._bulkActiveNow();
    this.querySelector("#cancel").style.display = "none";
    this.querySelectorAll("tr.dev").forEach(tr => tr.classList.remove("busy"));
  }

  // Connect stays disabled only while THIS selected device still has a backend op running — a
  // second overlapping connect to the SAME thermometer can wedge it. Other devices stay connectable.
  _readDisabled() {
    return !this._selected || this._inflightMacs.has(this._selected);
  }
  _cancel() {
    this._op++;
    this._endOp();
    this._status("Cancelled. The background operation will still finish and the device will be safely disconnected.");
  }

  // ---- scan ----
  async _scan() {
    // Scanning is allowed during a bulk read: the server-side job has its own device list, so
    // refreshing the panel's list here can't disturb it — and a returning panel can repopulate.
    if (this._busy) return;
    const prevSelected = this._selected;   // keep the selection across a re-scan
    const token = this._beginOp("Scanning…");
    this._loaded = null;
    try {
      const r = await this._ws({ type: "telink_manager/scan" });
      if (token !== this._op) return;
      const devs = r.devices || [];
      this._devs = devs;
      this._names = {};
      devs.forEach(d => { this._names[d.mac] = d.friend_name || ""; });
      // keep the previous selection if it's still present; otherwise select the first row
      // (so the page-load auto-scan leaves the first device selected).
      const keep = (prevSelected && devs.some(d => d.mac === prevSelected)) ? prevSelected
                 : (devs[0] ? devs[0].mac : null);
      this._selected = keep;
      const sel = devs.find(d => d.mac === keep);
      this._selectedRssi = sel && sel.rssi != null ? sel.rssi : null;
      this._status(`${devs.length} Telink thermometer(s). Click a row to select, then "Connect". Click a column header to sort.`);
      try {  // which devices have backups (for the Backup column) — no BLE, from the store
        const bi = await this._ws({ type: "telink_manager/backups_index" });
        if (token === this._op) this._backupMacs = new Map(((bi && bi.devices) || []).map((d) => [d.mac, d.count]));
      } catch (e) { /* ignore */ }
      this._renderDevTable();
    } catch (e) {
      if (token === this._op) this._status("Error: " + this._errMsg(e));
    } finally {
      if (token === this._op) this._endOp();
    }
  }

  _sortVal(d, k) {
    switch (k) {
      case "friendly": return (d.friend_name || "").toLowerCase();
      case "name": return (d.name || "").toLowerCase();
      case "mac": return d.mac || "";
      case "rssi": return d.rssi == null ? -999 : d.rssi;
      case "connectable": return d.connectable ? 1 : 0;
      case "proxy": return (d.proxy || "").toLowerCase();
      case "backup": return (this._backupMacs && this._backupMacs.get(d.mac)) || 0;
      case "battery": { const b = this._battInfo(d); return b ? b.pct : -1; }
      default: return "";
    }
  }

  // Backup column cell: green "● N" (click → that device's backups) if it has any, red dot if none.
  _backupCell(mac) {
    const n = this._backupMacs && this._backupMacs.get(mac);
    if (n) return `<span class="bk-dot" data-mac="${mac}" style="cursor:pointer;color:#4caf50;font-weight:600" title="${n} backup(s) — click to view this device's backups">● ${n}</span>`;
    return `<span class="bk-dot-empty dot" data-mac="${mac}" style="background:#e53935;cursor:pointer" title="No backup yet — click to connect and create the first one"></span>`;
  }

  // Battery from the advertisement (no connection). Some devices broadcast % directly (BTHome obj
  // 0x01); others only broadcast voltage (0x0C) -> estimate % from a CR2032 curve (2.2V=0, 3.0V=100).
  _battInfo(d) {
    if (d.battery != null) return { pct: d.battery, est: false, v: d.battery_v };
    if (d.battery_v != null) {
      const pct = Math.max(0, Math.min(100, Math.round((d.battery_v * 1000 - 2200) / 8)));
      return { pct, est: true, v: d.battery_v };
    }
    return null;
  }

  _battCell(d) {
    const b = this._battInfo(d);
    if (!b) return `<span class="muted">—</span>`;
    const color = b.pct < 20 ? "#e53935" : (b.pct < 40 ? "#ffb300" : "#4caf50");
    const tip = b.est
      ? `≈ estimated from ${b.v != null ? b.v.toFixed(2) + " V" : "voltage"} (device broadcasts voltage, not %)`
      : `advertised battery level${b.v != null ? ` · ${b.v.toFixed(2)} V` : ""}`;
    return `<span title="${tip}" style="color:${color};font-weight:600">${b.est ? "~" : ""}${b.pct}%</span>`;
  }

  // Open ONE device's backups directly (from the scan list), not the global device picker.
  _openDeviceBackups(mac) {
    this._modalShell("🗄️ Backups");
    this._backupsForDevice(mac);
  }

  _sortBy(key) {
    if (this._sortKey === key) {
      this._sortDir = this._sortDir === "asc" ? "desc" : "asc";
    } else {
      this._sortKey = key;
      this._sortDir = (key === "rssi" || key === "connectable") ? "desc" : "asc";
    }
    this._renderDevTable();
  }

  _renderDevTable() {
    const esc = escHtml;
    let devs = this._devs.slice();
    if (this._sortKey) {
      const dir = this._sortDir === "asc" ? 1 : -1;
      devs.sort((a, b) => {
        const va = this._sortVal(a, this._sortKey), vb = this._sortVal(b, this._sortKey);
        if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
        return String(va).localeCompare(String(vb)) * dir;
      });
    }
    const arrow = (k) => this._sortKey === k ? (this._sortDir === "asc" ? " ▲" : " ▼") : "";
    const th = (k, label) => `<th class="sortable" data-sort="${k}">${label}${arrow(k)}</th>`;
    this.querySelector("#list").innerHTML = `
      <table><thead><tr>
        <th></th>${th("friendly", "Friendly name")}${th("name", "BLE name")}${th("mac", "MAC")}
        ${th("rssi", "RSSI")}${th("proxy", "Route")}${th("battery", "Battery")}${th("backup", "Backup")}
      </tr></thead>
      <tbody>${devs.map(d => `
        <tr class="dev${d.mac === this._selected ? " sel" : ""}" data-mac="${d.mac}" data-rssi="${d.rssi ?? ""}">
          <td><span class="dot ${d.connectable ? "on" : "off"}"></span></td>
          <td><input class="fname" data-mac="${d.mac}" value="${esc(d.friend_name)}" placeholder="name…"></td>
          <td>${escHtml(d.name) || "?"}</td><td>${escHtml(d.mac)}</td><td>${this._rssiCell(d.rssi)}</td>
          
          <td>${d.proxy ? String(d.proxy).replace(/\s*\(.*\)\s*$/, "") : "—"}</td>
          <td>${this._battCell(d)}</td>
          <td>${this._backupCell(d.mac)}</td>
        </tr>`).join("")}</tbody></table>`;
    this.querySelectorAll("th.sortable").forEach(h =>
      h.onclick = () => this._sortBy(h.dataset.sort));
    this.querySelectorAll("tr.dev").forEach(tr => {
      tr.onclick = () => this._select(tr);
      tr.ondblclick = () => { this._select(tr); this._readSelected(); };
    });
    this.querySelectorAll(".bk-dot").forEach((el) =>
      el.onclick = (e) => { e.stopPropagation(); this._openDeviceBackups(el.dataset.mac); });
    // No backup yet: don't drop into an empty screen — ask to connect first; a successful read
    // creates the first backup, then we land on this device's backups screen.
    this.querySelectorAll(".bk-dot-empty").forEach((el) =>
      el.onclick = async (e) => {
        e.stopPropagation();
        const mac = el.dataset.mac;
        const tr = this.querySelector(`tr.dev[data-mac="${mac}"]`);
        if (tr) this._select(tr);
        const ok = await this._confirm(
          `This device has no backups yet.\n\nTo create one, connect to it now — its current settings will be read and saved as the first backup.`,
          { okText: "Connect", cancelText: "Cancel" });
        if (ok) this._readSelected({ thenBackups: true });
      });
    this.querySelectorAll("input.fname").forEach(inp => {
      inp.addEventListener("dblclick", (e) => e.stopPropagation());
      inp.addEventListener("change", () => {
        const m = inp.dataset.mac;
        // Skip the WS round-trip + Store write when the value is unchanged.
        if (inp.value !== (this._names[m] || "")) this._saveName(m, inp.value);
      });
    });
  }

  _select(tr) {
    if (this._busy) return;
    this.querySelectorAll("tr.dev").forEach(x => x.classList.remove("sel"));
    tr.classList.add("sel");
    this._selected = tr.dataset.mac;
    this._selectedRssi = tr.dataset.rssi !== "" ? parseInt(tr.dataset.rssi, 10) : null;
    // Connect is enabled unless THIS device still has a backend op finishing (other devices are free).
    this.querySelector("#read").disabled = this._readDisabled();
  }

  // ---- read -> modal (opens immediately, shows loading/progress in the modal) ----
  async _readSelected(opts = {}) {
    // Guard against a quick Cancel + reconnect opening a SECOND overlapping connect to the SAME
    // thermometer (two simultaneous GATT links via the proxy can wedge it / go silent). Per-MAC,
    // so connecting to a DIFFERENT device while one is still finishing is allowed.
    if (this._busy || this._bulkActiveNow() || !this._selected || this._inflightMacs.has(this._selected)) return;
    const mac = this._selected;
    const sig = this._rssiInfo(this._selectedRssi);
    if (this._selectedRssi != null && sig.tier !== "ok") {
      const bad = sig.tier === "bad";
      const msg = bad
        ? `Very weak signal: ${this._selectedRssi} dBm.\n\n` +
          `A connection will almost certainly fail or hang. Strongly recommended: move the ` +
          `thermometer or a BLE proxy much closer before trying.\n\nConnect anyway (risky)?`
        : `Weak signal: ${this._selectedRssi} dBm.\n\n` +
          `Connecting may be slow or fail. For a reliable connection, move the thermometer or a ` +
          `BLE proxy closer (better than -80 dBm).\n\nTry to connect anyway?`;
      const proceed = await this._confirm(msg, {
        okText: bad ? "Connect anyway (risky)" : "Connect anyway",
        cancelText: "Cancel", danger: true });
      if (!proceed) {
        this._status(`Read cancelled — weak signal (${this._selectedRssi} dBm). Move it closer and Scan again.`);
        return;
      }
    }
    this._openLoadingModal(mac, "Connecting… (via proxy, may take a few s)");
    const token = this._beginOp(`Reading ${mac} …`, false);
    this._inflightMacs.add(mac);  // backend op started; only cleared when it truly finishes (finally)
    try {
      const r = await this._ws({ type: "telink_manager/read", mac });
      if (token !== this._op) return;       // cancelled / modal closed
      if (!r.ok) { this._modalError(mac, "Read failed: " + r.error); this._status(`Read failed: ${mac}`); return; }
      this._loaded = r.fields;
      this._status(`Read OK: ${mac}`);
      if (r.backup && typeof r.backup.count === "number") {  // refresh the scan-list Backup column now
        this._backupMacs.set(mac, r.backup.count);
        this._renderDevTable();
      }
      // Came from a red "no backup" dot: now that the first backup exists, go to its backups screen.
      if (opts.thenBackups) this._openDeviceBackups(mac);
      else this._modalView(mac, r.fields);
    } catch (e) {
      if (token === this._op) { this._modalError(mac, "Error: " + this._errMsg(e)); }
    } finally {
      // Backend read+disconnect has now actually completed. Clear this MAC and re-enable Connect HERE
      // (not when Cancel was pressed) so a second overlapping connect cannot start while it was running.
      this._inflightMacs.delete(mac);
      if (token === this._op) {
        this._endOp();
      } else {
        const rb = this.querySelector("#read");
        if (rb) rb.disabled = this._readDisabled();
        this._status(`${mac} safely disconnected.`);
      }
    }
  }

  // ---- modal ----
  _modalTitle(mac) {
    const fn = this._names[mac];
    const dev = (this._devs || []).find((d) => d.mac === mac); const ble = dev && dev.name && dev.name !== fn ? dev.name : null; return [fn, ble, mac].filter(Boolean).join(" · ");
  }

  _modalShell(title) {
    this.querySelector("#modalRoot").innerHTML = `
      <div class="overlay" id="overlay">
        <div class="modal">
          <div class="modal-head">
            <h3 id="m-title">${escHtml(title)}</h3>
            <button class="x" id="m-x" title="Close">✕</button>
          </div>
          <div id="m-body"></div>
          <div class="status" id="m-status"></div>
          <div class="actions" id="m-actions"></div>
        </div>
      </div>`;
    this.querySelector("#m-x").onclick = () => this._closeModal();
    this.querySelector("#overlay").addEventListener("click", (e) => {
      if (e.target.id === "overlay") this._closeModal();
    });
  }

  _openLoadingModal(mac, text) {
    this._modalShell(this._modalTitle(mac));
    const dev = (this._devs || []).find((d) => d.mac === mac) || {};
    const route = dev.proxy || "nearest BLE proxy";
    const rssi = this._selectedRssi != null ? this._selectedRssi : dev.rssi;
    const sig = this._rssiInfo(rssi);
    const weak = sig.tier !== "ok"
      ? `<div class="warn" style="margin-top:8px">Weak signal — connecting over the proxy may be slow or fail.</div>`
      : "";
    this.querySelector("#m-body").innerHTML = `
      <div class="fld"><span class="lab">Route</span><b>${route}</b></div>
      <div class="fld"><span class="lab">Signal</span><span>${this._rssiCell(rssi)}</span></div>
      <div class="fld"><span class="lab">Elapsed</span><b id="m-conn-elapsed">0 s</b></div>
      <div class="muted" style="margin-top:8px">Connecting over a BLE proxy typically takes 5–20 s.</div>
      ${weak}`;
    this.querySelector("#m-actions").innerHTML =
      `<button id="m-cancel-load" class="cancel" style="margin-left:auto">Cancel</button>`;
    this.querySelector("#m-cancel-load").onclick = () => this._closeModal();
    this._mstatus(text, true);
    // live elapsed-seconds counter; cleared when the view/error renders or the modal closes
    this._stopConnTimer();
    let s = 0;
    this._connTimer = setInterval(() => {
      const el = this.querySelector("#m-conn-elapsed");
      if (!el) { this._stopConnTimer(); return; }
      el.textContent = `${++s} s`;
    }, 1000);
  }

  _stopConnTimer() {
    if (this._connTimer) { clearInterval(this._connTimer); this._connTimer = null; }
  }

  _modalError(mac, text) {
    this._stopConnTimer();
    this._mstatus("❌ " + text);
    this.querySelector("#m-body").innerHTML = "";
    this.querySelector("#m-actions").innerHTML = `
      <button id="m-retry">Retry</button>
      <button id="m-close" class="ghost" style="margin-left:auto">Close</button>`;
    this.querySelector("#m-retry").onclick = () => this._readSelected();
    this.querySelector("#m-close").onclick = () => this._closeModal();
  }

  _openModal(mac, f) {
    this._modalShell(this._modalTitle(mac));
    this._modalView(mac, f);
  }

  async _closeModal() {
    this._stopConnTimer();
    // If a permanent LCD overlay is still on the device, ask whether to clear it.
    if (this._lcdPermanentActive && this._lcdPermanentMac) {
      const mac = this._lcdPermanentMac;
      const doClear = await this._confirm(
        `A permanent number is still shown on ${mac}'s LCD.\n\nClear it now, or leave it on the screen?`,
        { okText: "Clear it", cancelText: "Leave on screen" });
      this._op++;
      this.querySelector("#modalRoot").innerHTML = "";
      this._endOp();
      this._lcdPermanentActive = false;
      this._lcdPermanentMac = null;
      if (doClear) {
        this._status(`Clearing LCD on ${mac} …`);
        try {
          await this._ws({ type: "telink_manager/lcd", mac, big_number: 0, small_number: 0, vtime_sec: 0 });
          this._status(`LCD cleared on ${mac}.`);
        } catch (e) {
          this._status("LCD clear failed: " + this._errMsg(e));
        }
      } else {
        this._status(`⚠️ A permanent number was left on ${mac}'s LCD (open it again → Commands → Clear to remove).`);
      }
      return;
    }
    this._op++;                 // ignore any in-flight read/write result
    this.querySelector("#modalRoot").innerHTML = "";
    this._endOp();              // release the top-panel guard if a read was running
    if (this._inflightMacs.size)
      this._status("Finishing the connection safely in the background — Connect to this device re-enables in a few seconds.");
  }

  _viewRows(f) {
    const t = (k) => TIPS[k] ? ` title="${TIPS[k].replace(/"/g, "&quot;")}"` : "";
    const yn = (b) => (b ? "yes" : "no");
    const row = (lab, val, tip) =>
      `<div class="fld"><span class="lab"${t(tip)}>${lab}</span><b>${escHtml(val)}</b></div>`;
    const h = (s) => `<h3>${s}</h3>`;
    const sign = (n) => (n > 0 ? "+" : "") + n;
    const legacy = f.fw_layout === "legacy";
    const comfort = (f.comfort_t_lo != null)
      ? `${f.comfort_t_lo}–${f.comfort_t_hi} °C, ${f.comfort_h_lo}–${f.comfort_h_hi} %` : "—";
    const sensorChip = f.sensor_name
      ? `${f.sensor_name} — ${f.sensor_is_default ? "factory default" : "calibrated"}` : "—";
    const sensorFine = (f.t_fine_offset_c != null)
      ? `T ${sign(f.t_fine_offset_c)} °C, H ${sign(f.h_fine_offset_pct)} %` : "—";
    return `<div class="ro">
      ${h("Device")}
      ${row("Firmware", this._fwString(f), "firmware")}
      ${f.model ? row("Model", f.model, "model") : ""}
      ${f.hw_ver != null ? row("Hardware ver", f.hw_ver, "hw_ver") : ""}
      ${row("Device name (on device)", f.device_name || "—", "device_name")}
      ${row("Device clock", this._clockStr(f.device_time), "device_clock")}

      ${h("Display")}
      ${row("Temperature unit", f.temp_F ? "°F" : "°C", "tunit")}
      ${row("Display", f.screen_off ? "OFF" : "ON", "disp")}
      ${row("Smiley", f.smiley, "smiley")}
      ${row("Comfort smiley", yn(f.comfort_smiley), "comfort_smiley")}
      ${row("Blinking time/smile", yn(f.blinking_time_smile), "blinking_time_smile")}
      ${row("Show battery", yn(f.show_batt), "show_batt")}
      ${row("Min LCD refresh", `${f.lcd_refresh_s} s`, "lcd_refresh")}

      ${h("Measurement")}
      ${row("Measure multiplier", f.measure_mult, "measure_mult")}
      ${row("Effective measurement", this._humanInterval(f.measure_period_s), "effective")}
      ${row("Data broadcast (adv)", this._humanInterval(f.adv_interval_s), "data_broadcast")}
      ${row("Low-power mode", yn(f.lp_measures), "lp_meas")}
      ${row("TX measures (stream)", yn(f.tx_measures), "tx_meas")}
      ${row("Averaging to flash", f.averaging === 0 ? "off" : f.averaging, "averaging")}

      ${h("Advertising")}
      ${row("Advertising interval", `${f.adv_interval_s} s <span class="muted">(${f.adv_interval_raw} × 0.0625 s)</span>`, "adv_interval")}
      ${row("Advertising type", f.adv_type, "adv_type")}
      ${row("Advertising flags", yn(f.adv_flags), "adv_flags")}
      ${legacy ? "" : row("Pseudo-random delay", `${f.adv_delay_ms} ms`, "adv_delay")}
      ${legacy ? "" : row("Event beacon duplicates", f.event_adv_cnt, "event_adv_cnt")}
      ${row("Encrypted beacon", yn(f.adv_crypto), "adv_crypto")}
      ${row("BT5 PHY / long range", `${yn(f.bt5phy)} / ${yn(f.longrange)}`, "bt5phy")}

      ${h("Radio / connection")}
      ${row("RF TX power", f.rf_tx_power, "tx_power")}
      ${row("Connect latency", `${f.connect_latency_ms} ms`, "connect_latency")}
      ${row("PIN code", `<span class="muted">flasher only — not editable here</span>`, "pincode")}

      ${h("Sensor calibration")}
      ${f.sensor_name ? row("Sensor chip", sensorChip, "sensor_cal") : ""}
      ${row("Fine offset (T / H)", sensorFine, "sensor_cal")}

      ${h("Comfort zone")}
      ${row("Comfort range", comfort, "comfort")}

      ${h("Raw")}
      ${row("Raw (11 B)", f.raw, "raw")}
    </div>`;
  }

  _modalView(mac, f) {
    this._stopConnTimer();
    this._loaded = f;
    this._mstatus("");          // clear any leftover "Reading…/Writing…" progress
    this.querySelector("#m-title").textContent = this._modalTitle(mac);
    this.querySelector("#m-body").innerHTML = this._viewRows(f);
    this.querySelector("#m-actions").innerHTML = `
      <button id="m-edit">Edit</button>
      <button id="m-cmds" class="ghost">Commands</button>
      <button id="m-close" class="ghost" style="margin-left:auto">Close</button>`;
    this.querySelector("#m-edit").onclick = () => this._modalEdit(mac, this._loaded);
    this.querySelector("#m-cmds").onclick = () => this._modalCommands(mac, this._loaded);
    this.querySelector("#m-close").onclick = () => this._closeModal();
  }

  _modalEdit(mac, f) {
    const t = (k) => TIPS[k] ? ` title="${TIPS[k].replace(/"/g, "&quot;")}"` : "";
    const num = (id, val, min, max, step) =>
      `<input type="number" id="${id}" value="${val}" min="${min}" max="${max}" step="${step}">`;
    const sel = (id, val, opts) =>
      `<select id="${id}">${opts.map(([v, txt]) =>
        `<option value="${v}" ${String(v) === String(val) ? "selected" : ""}>${txt}</option>`).join("")}</select>`;
    const chk = (id, val) => `<input type="checkbox" id="${id}" ${val ? "checked" : ""}>`;

    const legacy = f.fw_layout === "legacy";
    this.querySelector("#m-body").innerHTML = `
      <h3>Display</h3>
      <div class="fld"><span class="lab"${t("tunit")}>Temperature unit</span>
        ${sel("tunit", f.temp_F ? "F" : "C", [["C", "°C"], ["F", "°F"]])}</div>
      <div class="fld"><span class="lab"${t("disp")}>Display</span>
        ${sel("disp", f.screen_off ? "off" : "on", [["on", "ON"], ["off", "OFF"]])}</div>
      <div class="fld"><span class="lab"${t("smiley")}>Smiley (icon 0–7)</span>
        ${sel("smiley", f.smiley, [0,1,2,3,4,5,6,7].map(n => [n, String(n)]))}</div>
      <div class="fld"><span class="lab"${t("comfort_smiley")}>Comfort smiley</span>
        ${chk("comfort", f.comfort_smiley)}</div>
      <div class="fld"><span class="lab"${t("blinking_time_smile")}>Blinking time/smile</span>
        ${chk("blink", f.blinking_time_smile)}</div>
      <div class="fld"><span class="lab"${t("show_batt")}>Show battery</span>
        ${chk("sbatt", f.show_batt)}</div>
      <div class="fld"><span class="lab"${t("lcd_refresh")}>Min LCD refresh (s)</span>
        ${num("lcdref", f.lcd_refresh_s, 0.5, 12.75, 0.05)}</div>

      <h3>Measurement</h3>
      <div class="fld"><span class="lab"${t("measure_mult")}>Measure multiplier</span>
        ${num("mmult", f.measure_mult, 2, 255, 1)}
        <span class="muted">&nbsp;min 2 (firmware rejects 1)</span></div>
      <div class="fld"><span class="lab"${t("measure_period")}>Measure period (s)</span>
        ${num("mper", f.measure_period_s, 0.0625, 4080, 0.5)}
        <span class="muted">&nbsp;= adv × mult</span></div>
      <div class="fld"><span class="lab"${t("effective")}>Effective measurement</span>
        <b id="effmeas">${this._humanInterval(f.measure_period_s)}</b></div>
      <div class="fld"><span class="lab"${t("lp_meas")}>Low-power mode</span>
        ${chk("lpmeas", f.lp_measures)}</div>
      <div class="fld"><span class="lab"${t("tx_meas")}>TX measures (stream when connected)</span>
        ${chk("txmeas", f.tx_measures)}</div>
      <div class="fld"><span class="lab"${t("averaging")}>Averaging to flash (0 = off)</span>
        ${num("avgmeas", f.averaging, 0, 255, 1)}</div>

      <h3>Advertising</h3>
      <div class="fld"><span class="lab"${t("adv_interval")}>Advertising interval (s)</span>
        ${num("adv_s", f.adv_interval_s, 0.0625, 15.9375, 0.0625)}
        <span class="muted">&nbsp;= raw ${f.adv_interval_raw} × 0.0625 s/step</span></div>
      <div class="fld"><span class="lab"${t("adv_flags")}>Advertising flags</span>
        ${chk("advflags", f.adv_flags)}</div>
      ${legacy ? "" : `<div class="fld"><span class="lab"${t("adv_delay")}>Pseudo-random delay (ms)</span>
        ${num("advdelay", f.adv_delay_ms, 0, 9.375, 0.625)}</div>
      <div class="fld"><span class="lab"${t("event_adv_cnt")}>Event beacon duplicates</span>
        ${num("evtcnt", f.event_adv_cnt, 0, 255, 1)}</div>`}

      <div class="fld"><span class="lab"${t("adv_type")}>Advertising type</span>
        ${sel("advtype", f.adv_type_raw, ADV_TYPES)}</div>

      <h3>Radio</h3>
      <div class="fld"><span class="lab"${t("tx_power")}>RF TX power</span>
        ${sel("rftx", f.rf_tx_power, RF_TX_POWER.some(([v]) => v === f.rf_tx_power)
          ? RF_TX_POWER : [[f.rf_tx_power, `${f.rf_tx_power} (current)`], ...RF_TX_POWER])}</div>
      <div class="fld"><span class="lab"${t("connect_latency")}>Connect latency (ms)</span>
        ${num("clat", f.connect_latency_ms, 20, 5120, 20)}</div>

      <h3>Read-only (info)</h3>
      <div class="ro">
        <div class="fld"><span class="lab"${t("firmware")}>Firmware</span><b>${escHtml(this._fwString(f))}</b></div>
        ${f.model ? `<div class="fld"><span class="lab"${t("model")}>Model</span><b>${escHtml(f.model)}</b></div>` : ""}
        <div class="fld"><span class="lab"${t("bt5phy")}>BT5 PHY / long range</span><b>${f.bt5phy} / ${f.longrange}</b></div>
        <div class="fld"><span class="lab"${t("adv_crypto")}>Encrypted beacon</span><b>${f.adv_crypto}</b></div>
        <div class="fld"><span class="lab"${t("raw")}>Raw (11 B)</span><b>${escHtml(f.raw)}</b></div>
      </div>
      <div class="advzone" id="risk-note" style="display:none"></div>`;

    this.querySelector("#m-actions").innerHTML = `
      <button id="m-save" disabled>Save</button>
      <button id="m-cancel" class="ghost" style="margin-left:auto">Cancel</button>`;
    this.querySelector("#m-save").onclick = () => this._save(mac);
    this.querySelector("#m-cancel").onclick = () => { this._mstatus(""); this._modalView(mac, this._loaded); };

    // Save is enabled only when something actually changed vs. the loaded baseline.
    const refreshSave = () => {
      const { changes, diff } = this._collectChanges();
      const s = this.querySelector("#m-save");
      if (s) s.disabled = diff.length === 0;
      const rn = this.querySelector("#risk-note");
      if (rn) {
        const notes = Object.keys(changes).filter((k) => RISK_NOTES[k]).map((k) => RISK_NOTES[k]);
        rn.innerHTML = notes.map((n) => `<div class="advnote">${n}</div>`).join("");
        rn.style.display = notes.length ? "block" : "none";
      }
    };
    const body = this.querySelector("#m-body");
    body.addEventListener("input", refreshSave);
    body.addEventListener("change", refreshSave);

    // measure period <-> multiplier two-way sync + live effective-measurement label
    const advEl = this.querySelector("#adv_s");
    const multEl = this.querySelector("#mmult");
    const perEl = this.querySelector("#mper");
    const effEl = this.querySelector("#effmeas");
    const advS = () => Math.max(0.0625, parseFloat(advEl.value) || f.adv_interval_s);
    const updateEff = () => {
      const m = Math.max(2, parseInt(multEl.value, 10) || 2);
      effEl.textContent = this._humanInterval(advS() * m);
    };
    advEl.addEventListener("input", () => { perEl.value = (advS() * Math.max(2, parseInt(multEl.value, 10) || 2)).toFixed(2); updateEff(); refreshSave(); });
    multEl.addEventListener("input", () => { perEl.value = (advS() * Math.max(2, parseInt(multEl.value, 10) || 2)).toFixed(2); updateEff(); refreshSave(); });
    perEl.addEventListener("input", () => { multEl.value = Math.min(255, Math.max(2, Math.round((parseFloat(perEl.value) || 0) / advS()))); updateEff(); refreshSave(); });
    refreshSave();   // initial: no changes -> Save disabled
  }

  _modalCommands(mac, f) {
    this._mstatus("");
    const t = (k) => TIPS[k] ? ` title="${TIPS[k].replace(/"/g, "&quot;")}"` : "";
    const num = (id, val, min, max, step) =>
      `<input type="number" id="${id}" value="${val}" min="${min}" max="${max}" step="${step}">`;
    const c = {
      t_lo: f.comfort_t_lo != null ? f.comfort_t_lo : 20,
      t_hi: f.comfort_t_hi != null ? f.comfort_t_hi : 26,
      h_lo: f.comfort_h_lo != null ? f.comfort_h_lo : 40,
      h_hi: f.comfort_h_hi != null ? f.comfort_h_hi : 60,
    };
    const esc = escHtml;
    this.querySelector("#m-body").innerHTML = `
      <h3>Device name <span class="lab"${t("device_name")} style="width:auto">(stored on the device)</span></h3>
      <div class="muted">Different from the Friendly name (which is only a local label here). Empty = factory default.</div>
      <div class="fld">
        <input type="text" id="dname" maxlength="20" value="${esc(f.device_name)}" placeholder="ATC_xxxx" style="width:200px">
        <button id="c-name">Set name</button>
      </div>

      <h3>Comfort thresholds</h3>
      <div class="fld"><span class="lab"${t("comfort")}>Temperature (°C) low / high</span>
        ${num("c_tlo", c.t_lo, -40, 125, 0.1)} ${num("c_thi", c.t_hi, -40, 125, 0.1)}</div>
      <div class="fld"><span class="lab"${t("comfort")}>Humidity (%) low / high</span>
        ${num("c_hlo", c.h_lo, 0, 100, 0.1)} ${num("c_hhi", c.h_hi, 0, 100, 0.1)}</div>
      <div class="fld"><button id="c-comfort">Set comfort</button></div>

      <h3>Sensor calibration${f.sensor_name ? ` <span class="muted" style="width:auto">(${f.sensor_name}${f.sensor_is_default ? ", factory default" : ", calibrated"})</span>` : ""}</h3>
      <div class="muted">Fine offset added on top of the chip's factory calibration. The slope stays at factory; "Set default" reverts everything.</div>
      <div class="fld"><span class="lab"${t("sensor_cal")}>Temp fine offset (°C)</span>
        ${num("s_tfine", f.t_fine_offset_c != null ? f.t_fine_offset_c : 0, -20, 20, 0.01)}</div>
      <div class="fld"><span class="lab"${t("sensor_cal")}>Humidity fine offset (%)</span>
        ${num("s_hfine", f.h_fine_offset_pct != null ? f.h_fine_offset_pct : 0, -20, 20, 0.01)}</div>
      <div class="fld"><button id="c-sensor">Set offset</button>
        <button id="c-sensor-def" class="ghost">Set default</button></div>

      <h3>Clock</h3>
      <div class="fld"><span class="lab"${t("device_clock")}>Device clock now</span>
        <b id="dev-clock">${this._clockStr(f.device_time)}</b></div>
      <div class="fld"><span class="lab"${t("clock")}>Set device clock</span>
        <button id="c-time">Set to current local time</button></div>

      <h3>LCD overlay (temporary)</h3>
      <div class="fld"><span class="lab"${t("lcd")}>Big number (one decimal)</span>
        ${num("lcd_big", 22.2, -3276, 3276, 0.1)}</div>
      <div class="fld"><span class="lab"${t("lcd")}>Small number (whole)</span>
        ${num("lcd_small", 0, -99, 99, 1)}</div>
      <div class="fld"><span class="lab"${t("lcd")}>Keep on screen (permanent)</span>
        <input type="checkbox" id="lcd_perm"></div>
      <div class="fld" id="lcd-dur-row"><span class="lab"${t("lcd")}>Duration (s)</span>
        ${num("lcd_dur", 10, 1, 65534, 1)}</div>
      <div class="fld"><button id="c-lcd">Show on LCD</button>
        <button id="c-lcd-clear" class="ghost">Clear</button></div>

      <h3>Power</h3>
      <div class="fld"><span class="lab">Reboot the device</span>
        <button id="c-reboot" class="ghost">Reboot</button></div>

      <div class="dangerzone">
        <h3>⛔ Dangerous device commands</h3>
        <div class="warn">Each of these can make Home Assistant lose this device. They are confirmed individually.</div>

        <h4 style="margin:12px 0 4px">Custom MAC address</h4>
        <div class="muted">HA tracks devices by MAC: after a change (and a reboot) this shows up as a brand-new device and the old history / entities are orphaned.</div>
        <div class="fld"><span class="lab">New MAC</span>
          <input type="text" id="mac-new" value="${esc(mac)}" placeholder="A4:C1:38:xx:xx:xx" style="width:200px">
          <button id="c-mac-rand" class="ghost">Randomize</button></div>
        <div class="fld"><button id="c-mac-read" class="ghost">Read current</button>
          <button id="c-mac-set" class="danger">Set MAC</button></div>

        <h4 style="margin:14px 0 4px">Encryption bind key</h4>
        <div class="muted">Exactly 16 bytes (32 hex chars), for encrypted advertising. HA can only decode the data if the same key is configured on the HA side.</div>
        <div class="fld"><span class="lab">Bind key (hex)</span>
          <input type="text" id="bk-new" maxlength="47" placeholder="32 hex chars" style="width:280px"></div>
        <div class="fld"><button id="c-bk-read" class="ghost">Read current</button>
          <button id="c-bk-rand" class="ghost">Randomize</button>
          <button id="c-bk-set" class="danger">Set bind key</button></div>

        <h4 style="margin:14px 0 4px">Factory reset</h4>
        <div class="fld"><span class="lab">Reset all settings to factory defaults</span>
          <button id="c-factory" class="danger">Factory reset</button></div>
      </div>

      <div class="dangerzone">
        <h3>⛔ Experimental — raw command</h3>
        <div class="warn">Sends raw bytes to characteristic 0x1F1F (byte 0 = opcode). Wrong commands can
          misconfigure or brick the device. Advanced users only.</div>
        <div class="fld"><span class="lab">Hex (e.g. 55 = read config)</span>
          <input type="text" id="rawhex" placeholder="55" style="width:220px"></div>
        <div class="fld"><span class="lab">Expect a reply</span><input type="checkbox" id="rawreply" checked></div>
        <div class="fld"><button id="c-raw" class="danger">Send raw</button></div>
        <div class="fld"><span class="lab">Reply</span><b id="rawout" style="word-break:break-all">—</b></div>
      </div>`;

    this.querySelector("#m-actions").innerHTML = `
      <button id="m-cmd-close" class="ghost" style="margin-left:auto">Close</button>`;
    this.querySelector("#m-cmd-close").onclick = () => this._closeModal();

    this.querySelector("#c-name").onclick = async () => {
      const name = this.querySelector("#dname").value;
      const r = await this._runCmd("Setting device name…",
        { type: "telink_manager/set_device_name", mac, name },
        (r) => `✅ Device name set: ${escHtml(r.device_name) || "(default)"}`);
      if (r && r.ok) { this._loaded.device_name = r.device_name; this._autoBackup(mac); }
    };
    this.querySelector("#c-comfort").onclick = async () => {
      const t_lo = parseFloat(this.querySelector("#c_tlo").value);
      const t_hi = parseFloat(this.querySelector("#c_thi").value);
      const h_lo = parseFloat(this.querySelector("#c_hlo").value);
      const h_hi = parseFloat(this.querySelector("#c_hhi").value);
      const r = await this._runCmd("Setting comfort thresholds…",
        { type: "telink_manager/set_comfort", mac, t_lo, t_hi, h_lo, h_hi }, () => "✅ Comfort thresholds set.");
      if (r && r.ok) { Object.assign(this._loaded,
        { comfort_t_lo: t_lo, comfort_t_hi: t_hi, comfort_h_lo: h_lo, comfort_h_hi: h_hi }); this._autoBackup(mac); }
    };
    this.querySelector("#c-sensor").onclick = async () => {
      const tf = parseFloat(this.querySelector("#s_tfine").value) || 0;
      const hf = parseFloat(this.querySelector("#s_hfine").value) || 0;
      const t_offset_c = (f.t_z_default_c != null ? f.t_z_default_c : 0) + tf;
      const h_offset_pct = (f.h_z_default_pct != null ? f.h_z_default_pct : 0) + hf;
      const t_slope = f.t_slope_default || f.t_slope || 65536;
      const h_slope = f.h_slope_default || f.h_slope || 65536;
      const r = await this._runCmd("Setting sensor fine offset…",
        { type: "telink_manager/set_sensor", mac, t_slope, t_offset_c, h_slope, h_offset_pct },
        () => "✅ Sensor fine offset set.");
      if (r && r.ok) { Object.assign(this._loaded,
        { t_fine_offset_c: tf, h_fine_offset_pct: hf, t_offset_c: r.t_offset_c, h_offset_pct: r.h_offset_pct, sensor_is_default: false }); this._autoBackup(mac); }
    };
    this.querySelector("#c-sensor-def").onclick = async () => {
      if (!(await this._confirm("Reset sensor calibration to factory defaults?", { okText: "Reset" }))) return;
      const r = await this._runCmd("Resetting sensor calibration…",
        { type: "telink_manager/sensor_default", mac }, () => "✅ Sensor calibration reset to factory default.");
      if (r && r.ok) { Object.assign(this._loaded, { t_fine_offset_c: 0, h_fine_offset_pct: 0,
        t_offset_c: r.t_offset_c != null ? r.t_offset_c : this._loaded.t_offset_c,
        h_offset_pct: r.h_offset_pct != null ? r.h_offset_pct : this._loaded.h_offset_pct,
        sensor_is_default: true }); this._autoBackup(mac); }
    };
    this.querySelector("#c-time").onclick = async () => {
      const ts = Math.floor(Date.now() / 1000) - new Date().getTimezoneOffset() * 60;
      const r = await this._runCmd("Setting clock…",
        { type: "telink_manager/set_time", mac, ts },
        (r) => `✅ Clock set. Device: ${this._clockStr(r.device_time)}`);
      if (r && r.ok) {
        this._loaded.device_time = r.device_time;
        const el = this.querySelector("#dev-clock");
        if (el) el.textContent = this._clockStr(r.device_time);
      }
    };
    // Permanent and Duration are mutually exclusive: hide Duration when "keep on screen" is on.
    const permEl = this.querySelector("#lcd_perm");
    const durRow = this.querySelector("#lcd-dur-row");
    const syncDur = () => { durRow.style.display = permEl.checked ? "none" : ""; };
    permEl.addEventListener("change", syncDur);
    syncDur();

    this.querySelector("#c-lcd").onclick = async () => {
      const big_number = Math.round((parseFloat(this.querySelector("#lcd_big").value) || 0) * 10);
      const small_number = parseInt(this.querySelector("#lcd_small").value, 10) || 0;
      const permanent = permEl.checked;
      const vtime_sec = permanent ? 0xFFFF : (parseInt(this.querySelector("#lcd_dur").value, 10) || 1);
      const r = await this._runCmd("Sending to LCD…",
        { type: "telink_manager/lcd", mac, big_number, small_number, vtime_sec },
        () => permanent ? "✅ Shown on LCD (permanent — use Clear to remove)." : "✅ Sent to LCD.");
      if (r && r.ok) {
        this._lcdPermanentActive = permanent;
        this._lcdPermanentMac = permanent ? mac : null;
      }
    };
    this.querySelector("#c-lcd-clear").onclick = async () => {
      const r = await this._runCmd("Clearing LCD overlay…",
        { type: "telink_manager/lcd", mac, big_number: 0, small_number: 0, vtime_sec: 0 }, () => "✅ LCD overlay cleared.");
      if (r && r.ok) { this._lcdPermanentActive = false; this._lcdPermanentMac = null; }
    };
    this.querySelector("#c-raw").onclick = async () => {
      const hex = this.querySelector("#rawhex").value;
      const expect_reply = this.querySelector("#rawreply").checked;
      const out = this.querySelector("#rawout");
      if (!(await this._confirm(
        `Send raw command "${hex}" to the device?\n\nWrong commands can misconfigure or brick it. Advanced users only.`,
        { okText: "Send", danger: true }))) return;
      const r = await this._runCmd("Sending raw command…",
        { type: "telink_manager/raw", mac, hex, expect_reply },
        (res) => `✅ Sent ${escHtml(res.sent)}${res.reply ? " · reply " + escHtml(res.reply) : ""}`);
      if (out) out.textContent = r ? (r.ok ? (r.reply || "(no reply)") : (r.error || "failed")) : "(cancelled)";
    };

    // --- Dangerous device commands ---
    const randHex = (n) => {
      const a = new Uint8Array(n);
      crypto.getRandomValues(a);
      return Array.from(a, (b) => b.toString(16).padStart(2, "0"));
    };
    const macInput = this.querySelector("#mac-new");
    this.querySelector("#c-mac-rand").onclick = () => {
      macInput.value = "A4:C1:38:" + randHex(3).join(":").toUpperCase();
    };
    this.querySelector("#c-mac-read").onclick = async () => {
      const r = await this._runCmd("Reading MAC…",
        { type: "telink_manager/get_mac", mac },
        (r) => `✅ Stored MAC: ${escHtml(r.device_mac) || "(unknown)"}`);
      if (r && r.ok && r.device_mac) macInput.value = r.device_mac;
    };
    this.querySelector("#c-mac-set").onclick = async () => {
      const newMac = (macInput.value || "").trim();
      if (!(await this._confirm(
        `Set a new MAC address (${newMac})?\n\nHome Assistant will see this as a NEW device after a reboot — the old history and entities are orphaned.`,
        { okText: "Set MAC", danger: true }))) return;
      await this._runCmd("Setting MAC…",
        { type: "telink_manager/set_mac", mac, new_mac: newMac },
        (r) => `✅ MAC set to ${escHtml(r.device_mac)}. It takes effect now (the device reboots as the link drops); ` +
               `HA will see it as a NEW device — close and Scan again to find it under the new MAC.`);
    };
    this.querySelector("#c-bk-read").onclick = async () => {
      const r = await this._runCmd("Reading bind key…",
        { type: "telink_manager/get_bind_key", mac },
        (r) => `✅ Bind key: ${escHtml(r.bind_key) || "(not returned by firmware)"}`);
      if (r && r.ok && r.bind_key) this.querySelector("#bk-new").value = r.bind_key;
    };
    this.querySelector("#c-bk-rand").onclick = () => {
      this.querySelector("#bk-new").value = randHex(16).join("");
    };
    this.querySelector("#c-bk-set").onclick = async () => {
      const key = (this.querySelector("#bk-new").value || "").trim();
      if (!(await this._confirm(
        "Set a new encryption bind key?\n\nHome Assistant cannot decode this device's data unless the same key is configured on the HA side.",
        { okText: "Set key", danger: true }))) return;
      const r = await this._runCmd("Setting bind key…",
        { type: "telink_manager/set_bind_key", mac, key },
        (r) => r.verified ? "✅ Bind key set and verified." : "✅ Bind key command accepted.");
      if (r && r.ok) { this._loaded.bind_key = (r.bind_key || key).toLowerCase(); this._autoBackup(mac); }
    };
    this.querySelector("#c-reboot").onclick = async () => {
      if (!(await this._confirm(
        "Reboot this device now?\n\nIt restarts and comes back in a few seconds; advertising pauses briefly. Settings are kept.",
        { okText: "Reboot" }))) return;
      await this._runCmd("Rebooting…",
        { type: "telink_manager/reboot", mac },
        () => "✅ Reboot sent — the device returns shortly.");
    };
    this.querySelector("#c-factory").onclick = async () => {
      if (!(await this._confirm(
        "FACTORY RESET this device?\n\nALL settings revert to firmware defaults. The default advertising format may not be BTHome, in which case HA loses the device until you reconfigure it.",
        { okText: "Factory reset", danger: true }))) return;
      const r = await this._runCmd("Factory resetting…",
        { type: "telink_manager/factory_reset", mac },
        () => "✅ Factory reset done. Re-read the device to see the defaults.");
      if (r && r.ok && r.config_after) this._loaded = Object.assign({}, this._loaded, r.config_after);
    };
  }

  async _runCmd(label, msg, after) {
    const token = ++this._op;
    const lock = (b) => this.querySelectorAll("#m-body button, #m-actions button, #m-x")
      .forEach(x => { x.disabled = b; });
    lock(true);
    this._mstatus(label, true);
    try {
      const r = await this._ws(msg);
      if (token !== this._op) return null;
      this._mstatus(r.ok ? (after ? after(r) : "✅ Done.") : ("❌ " + (r.error || "failed")));
      return r;
    } catch (e) {
      if (token === this._op) this._mstatus("❌ Error: " + this._errMsg(e));
      return null;
    } finally {
      if (token === this._op) lock(false);
    }
  }

  // The device stores a local-adjusted epoch; read UTC fields back to get the displayed local time.
  _fmtClock(epoch) {
    if (!epoch) return null;
    const d = new Date(epoch * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
           `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
  }

  _clockDiff(epoch) {
    if (!epoch) return "";
    const nowLocal = Math.floor(Date.now() / 1000) - new Date().getTimezoneOffset() * 60;
    const diff = epoch - nowLocal;
    const a = Math.abs(diff);
    if (a <= 3) return "in sync";
    const dir = diff > 0 ? "ahead" : "behind";
    if (a < 60) return `${a}s ${dir}`;
    if (a < 3600) return `${Math.round(a / 60)} min ${dir}`;
    if (a < 86400) return `${Math.round(a / 3600)} h ${dir}`;
    return `${Math.round(a / 86400)} d ${dir}`;
  }

  _clockStr(epoch) {
    if (!epoch) return "—";
    return `${this._fmtClock(epoch)} (${this._clockDiff(epoch)})`;
  }

  // tier: "ok" = no prompt, "weak" = normal warning, "bad" = strong warning (connect likely fails).
  // Green stays strictly above -80 so the color matches the -80 connect-warning threshold.
  _rssiInfo(rssi) {
    if (rssi == null) return { bars: 0, color: "#777", tier: "weak" };
    if (rssi >= -70) return { bars: 4, color: "#4caf50", tier: "ok" };    // excellent
    if (rssi > -80) return { bars: 3, color: "#8bc34a", tier: "ok" };     // good (-79..-71)
    if (rssi > -90) return { bars: 2, color: "#ffb300", tier: "weak" };   // -80..-89 → warn
    if (rssi > -97) return { bars: 1, color: "#ff7043", tier: "bad" };    // -90..-96 → strong warn
    return { bars: 0, color: "#e53935", tier: "bad" };                    // ≤-97 → strong warn
  }

  _rssiCell(rssi) {
    const { bars, color } = this._rssiInfo(rssi);
    const heights = [4, 7, 10, 13];
    const icon = heights.map((h, i) =>
      `<i style="height:${h}px;background:${i < bars ? color : '#444'}"></i>`).join("");
    const txt = rssi == null ? "" : `${rssi} dBm`;
    return `<span class="sig" title="${txt}">${icon}</span><span style="color:${color}">${txt}</span>`;
  }

  // Custom centered confirm dialog (the native confirm() can't be positioned). Returns a Promise<bool>.
  _confirm(message, opts = {}) {
    const okText = opts.okText || "OK";
    const cancelText = opts.cancelText || "Cancel";
    const danger = opts.danger ? "danger" : "";
    const safe = String(message).replace(/&/g, "&amp;").replace(/</g, "&lt;");
    return new Promise((resolve) => {
      const host = document.createElement("div");
      host.className = "overlay";
      host.style.zIndex = "10001";
      host.innerHTML = `
        <div class="modal" style="max-width:460px">
          <div style="white-space:pre-wrap; font-size:14px; line-height:1.45">${safe}</div>
          <div class="actions" style="justify-content:flex-end">
            <button class="ghost" data-act="cancel">${cancelText}</button>
            <button class="${danger}" data-act="ok">${okText}</button>
          </div>
        </div>`;
      const done = (val) => { host.remove(); resolve(val); };
      host.addEventListener("click", (e) => {
        if (e.target === host) return done(false);
        const act = e.target.dataset && e.target.dataset.act;
        if (act === "ok") done(true);
        else if (act === "cancel") done(false);
      });
      this.appendChild(host);
    });
  }

  _fwString(f) {
    const v = f.fw_version || f.sw_revision ||
      (f.fw_byte_hex ? `byte ${f.fw_byte_hex}` : null);
    if (!v) return "?";
    // fw_revision holds the source/vendor (e.g. "github.com/pvvx"), shown as a hint
    const src = f.fw_revision && f.fw_revision !== v ? ` · ${f.fw_revision}` : "";
    return `v${v}${src}`;
  }

  _humanInterval(s) {
    s = Number(s);
    if (!s || s <= 0) return "—";
    const fmt = (n) => (Number.isInteger(n) ? n : n.toFixed(1));
    if (s < 60) return `every ${fmt(s)} s`;
    if (s < 3600) {
      const m = Math.floor(s / 60), sec = Math.round(s % 60);
      return sec ? `every ${m} min ${sec} s` : `every ${m} min`;
    }
    const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    return m ? `every ${h} h ${m} min` : `every ${h} h`;
  }

  _collectChanges() {
    const f = this._loaded;
    const changes = {}, diff = [];
    const advS = parseFloat(this.querySelector("#adv_s").value);
    const advRaw = Math.round(advS / 0.0625);
    if (advRaw !== f.adv_interval_raw) {
      changes.adv_interval_raw = advRaw;
      diff.push(`Advertising interval: ${f.adv_interval_s}s → ${(advRaw * 0.0625).toFixed(4)}s`);
    }
    const mmult = parseInt(this.querySelector("#mmult").value, 10);
    if (mmult !== f.measure_mult) { changes.measure_mult = mmult; diff.push(`Measure mult: ${f.measure_mult} → ${mmult}`); }
    const tF = this.querySelector("#tunit").value === "F";
    if (tF !== f.temp_F) { changes.temp_F = tF; diff.push(`Temp unit: ${f.temp_F ? "°F" : "°C"} → ${tF ? "°F" : "°C"}`); }
    const sOff = this.querySelector("#disp").value === "off";
    if (sOff !== f.screen_off) { changes.screen_off = sOff; diff.push(`Display: ${f.screen_off ? "OFF" : "ON"} → ${sOff ? "OFF" : "ON"}`); }
    const lcdRaw = Math.round((parseFloat(this.querySelector("#lcdref").value) || 0) / 0.05);
    if (lcdRaw !== f.lcd_refresh_raw) { changes.lcd_refresh_raw = lcdRaw; diff.push(`Min LCD refresh: ${f.lcd_refresh_s}s → ${(lcdRaw * 0.05).toFixed(2)}s`); }
    const smiley = parseInt(this.querySelector("#smiley").value, 10);
    if (smiley !== f.smiley) { changes.smiley = smiley; diff.push(`Smiley: ${f.smiley} → ${smiley}`); }
    const comfort = this.querySelector("#comfort").checked;
    if (comfort !== f.comfort_smiley) { changes.comfort_smiley = comfort; diff.push(`Comfort smiley: ${f.comfort_smiley} → ${comfort}`); }
    const blink = this.querySelector("#blink").checked;
    if (blink !== f.blinking_time_smile) { changes.blinking_time_smile = blink; diff.push(`Blinking time/smile: ${f.blinking_time_smile} → ${blink}`); }
    const sbatt = this.querySelector("#sbatt").checked;
    if (sbatt !== f.show_batt) { changes.show_batt = sbatt; diff.push(`Show battery: ${f.show_batt} → ${sbatt}`); }
    const lp = this.querySelector("#lpmeas").checked;
    if (lp !== f.lp_measures) { changes.lp_measures = lp; diff.push(`Low-power mode: ${f.lp_measures} → ${lp}`); }
    const txm = this.querySelector("#txmeas").checked;
    if (txm !== f.tx_measures) { changes.tx_measures = txm; diff.push(`TX measures: ${f.tx_measures} → ${txm}`); }
    const avg = parseInt(this.querySelector("#avgmeas").value, 10) || 0;
    if (avg !== f.averaging) { changes.averaging = avg; diff.push(`Averaging to flash: ${f.averaging} → ${avg}`); }
    const advflags = this.querySelector("#advflags").checked;
    if (advflags !== f.adv_flags) { changes.adv_flags = advflags; diff.push(`Advertising flags: ${f.adv_flags} → ${advflags}`); }
    const advdelayEl = this.querySelector("#advdelay");
    if (advdelayEl) {
      const dRaw = Math.round((parseFloat(advdelayEl.value) || 0) / 0.625);
      if (dRaw !== f.adv_delay_raw) { changes.adv_delay_raw = dRaw; diff.push(`Pseudo-random delay: ${f.adv_delay_ms}ms → ${(dRaw * 0.625).toFixed(3)}ms`); }
    }
    const evtEl = this.querySelector("#evtcnt");
    if (evtEl) {
      const ev = parseInt(evtEl.value, 10);
      if (ev !== f.event_adv_cnt) { changes.event_adv_cnt = ev; diff.push(`Event beacon duplicates: ${f.event_adv_cnt} → ${ev}`); }
    }
    const rftxEl = this.querySelector("#rftx");
    if (rftxEl) {
      const rftx = parseInt(rftxEl.value, 10);
      if (rftx !== f.rf_tx_power) { changes.rf_tx_power = rftx; diff.push(`RF TX power: ${f.rf_tx_power} → ${rftx}`); }
    }
    const clatEl = this.querySelector("#clat");
    if (clatEl) {
      const clatRaw = Math.max(0, Math.round((parseFloat(clatEl.value) || 20) / 20) - 1);
      if (clatRaw !== f.connect_latency_raw) { changes.connect_latency_raw = clatRaw; diff.push(`Connect latency: ${f.connect_latency_ms}ms → ${(clatRaw + 1) * 20}ms`); }
    }
    const advType = parseInt(this.querySelector("#advtype").value, 10);
    if (advType !== f.adv_type_raw) {
      changes.adv_type_raw = advType;
      const name = (ADV_TYPES.find(([v]) => v === advType) || [advType, advType])[1];
      diff.push(`⚠️ Advertising type: ${f.adv_type} → ${name}`);
    }
    return { changes, diff };
  }

  _setModalBusy(b) {
    const s = this.querySelector("#m-save"); if (s) s.disabled = b;
    const c = this.querySelector("#m-cancel"); if (c) c.disabled = b;
    const x = this.querySelector("#m-x"); if (x) x.disabled = b;
  }

  async _save(mac) {
    const { changes, diff } = this._collectChanges();
    if (diff.length === 0) { this._mstatus("No changes to write."); return; }
    const token = ++this._op;
    this._setModalBusy(true);
    this._mstatus("Writing… (connect → write → verify)", true);
    try {
      const r = await this._ws({ type: "telink_manager/write", mac, changes });
      if (token !== this._op) return;       // modal closed / navigated away
      if (r.ok && r.verified) {
        // merge: r.after is config-only — keep name/comfort/bind_key/sensor from the prior read
        this._loaded = Object.assign({}, this._loaded, r.after);
        this._modalView(mac, this._loaded);  // back to view with fresh data
        this._mstatus("✅ Saved & verified." + (r.target_raw ? "  ·  sent 55" + r.target_raw : ""));
        this._autoBackup(mac);               // snapshot the new state
      } else if (r.ok && r.unchanged) {
        this._modalView(mac, this._loaded);
        this._mstatus("No effective change.");
      } else {
        this._mstatus("❌ Write failed: " + (r.error || "verify mismatch"));
        this._setModalBusy(false);
      }
    } catch (e) {
      if (token === this._op) { this._mstatus("❌ Error: " + this._errMsg(e)); this._setModalBusy(false); }
    }
  }

  // Silently snapshot the current full loaded state (server dedups vs the last snapshot).
  // Called after every successful modify so the post-change state is never lost.
  _autoBackup(mac) {
    if (!this._loaded || !this._loaded.raw) return;
    this._ws({ type: "telink_manager/backup_save", mac, fields: this._loaded })
      .then((r) => {
        if (r && typeof r.count === "number") { this._backupMacs.set(mac, r.count); this._renderDevTable(); }
      })
      .catch(() => {});
  }

  _bkTs(ts) { try { return new Date(ts * 1000).toLocaleString(); } catch (e) { return String(ts); } }

  // Styled modal title: icon + friendly name (bold) + BLE name / MAC (muted, smaller).
  _devTitleHtml(mac, icon) {
    const dev = (this._devs || []).find((d) => d.mac === mac);
    const ble = dev && dev.name ? dev.name : "";
    const name = this._names[mac] || ble || mac;
    const sub = [ble && ble !== name ? ble : "", mac].filter(Boolean).join(" · ");
    return `${icon ? icon + " " : ""}<b>${escHtml(name)}</b>` +
      (sub ? ` <span style="font-weight:400;font-size:12px;color:var(--secondary-text-color,#999)">${escHtml(sub)}</span>` : "");
  }

  // ===== Read all (bulk read): connect to many devices, parallel by proxy, back each up =====
  _signalOf(rssi) {
    if (rssi == null) return { tier: "unknown", lab: "no RSSI", color: "var(--tm-text-2,#9aa0a6)" };
    const i = this._rssiInfo(rssi);
    if (i.tier === "ok") return { tier: "ok", lab: "ok", color: i.color };
    if (i.tier === "weak") return { tier: "weak", lab: "weak — may be slow/fail", color: i.color };
    return { tier: "bad", lab: "very weak — will likely fail", color: i.color };
  }

  // Which devices are checked for a given preset + RSSI threshold.
  _raDefaultSel(preset, threshold) {
    const sel = new Set();
    (this._devs || []).forEach((d) => {
      if (!d.connectable) return;                          // can't read a non-connectable device
      if (d.rssi != null && d.rssi < threshold) return;    // below the skip threshold
      if (preset === "nobk" && (this._backupMacs.get(d.mac) || 0) > 0) return;  // already has a backup
      sel.add(d.mac);
    });
    return sel;
  }

  _openReadAllDialog() {
    if (this._bulkActiveNow()) return;
    if (!this._devs || !this._devs.length) { this._status("Scan first — no devices to read."); return; }
    this._modalShell("📖 Read all devices");
    this._raPreset = "nobk";
    this._raThreshold = -85;
    this._raSel = this._raDefaultSel(this._raPreset, this._raThreshold);
    this._renderReadAllDialog();
  }

  _renderReadAllDialog() {
    const devs = (this._devs || []).slice().sort((a, b) => (b.rssi ?? -999) - (a.rssi ?? -999));
    const sel = this._raSel, th = this._raThreshold;
    const preset = (id, lab) => `<label style="cursor:pointer;margin-right:14px;font-size:13px"><input type="radio" name="ra-preset" value="${id}" ${this._raPreset === id ? "checked" : ""}> ${lab}</label>`;
    const rows = devs.map((d) => {
      const sig = this._signalOf(d.rssi);
      const nbk = this._backupMacs.get(d.mac) || 0;
      const disabled = !d.connectable;
      const name = this._names[d.mac] || d.name || d.mac;
      return `<tr style="${disabled ? "opacity:.5" : ""}">
        <td style="text-align:center"><input type="checkbox" class="ra-pick" data-mac="${d.mac}" ${sel.has(d.mac) ? "checked" : ""} ${disabled ? "disabled" : ""}></td>
        <td style="text-align:left;white-space:nowrap">${escHtml(name)}</td>
        <td style="white-space:nowrap">${this._rssiCell(d.rssi)}</td>
        <td>${nbk ? `<span style="color:#4caf50">●${nbk}</span>` : `<span style="color:#e53935">●</span>`}</td>
        <td style="text-align:left;color:${disabled ? "var(--tm-text-2,#9aa0a6)" : sig.color};font-size:12px;white-space:nowrap">${disabled ? "not connectable" : sig.lab}</td>
      </tr>`;
    }).join("");
    const chosen = devs.filter((d) => sel.has(d.mac));
    const veryWeak = chosen.filter((d) => this._signalOf(d.rssi).tier === "bad").length;
    const unreach = devs.filter((d) => !d.connectable).length;
    const belowTh = devs.filter((d) => d.connectable && d.rssi != null && d.rssi < th && !sel.has(d.mac)).length;
    const warn = veryWeak ? `<div class="warn" style="margin-top:6px">⚠️ ${veryWeak} selected device(s) have very weak signal — they'll likely fail or be slow. Move them or a BLE proxy closer first.</div>` : "";
    this.querySelector("#m-body").innerHTML = `
      <div style="margin-bottom:6px">${preset("nobk", "Only without backup")}${preset("all", "All devices")}${this._raPreset === "custom" ? `<span class="muted">· custom selection</span>` : ""}</div>
      <div class="fld"><span class="lab">Skip weaker than</span>
        <input type="number" id="ra-th" value="${th}" step="1" style="width:80px"><span class="muted" style="margin-left:6px">dBm</span></div>
      <div class="muted" style="margin:4px 0 8px">Will read <b style="color:var(--tm-text)">${chosen.length}</b> device(s) · skipping ${devs.length - chosen.length} (${unreach} unreachable, ${belowTh} below threshold). Runs in the background, parallel by proxy.</div>
      ${warn}
      <div id="ra-scroll" style="overflow:auto;max-height:46vh;margin-top:6px">
        <table style="font-size:12px"><thead><tr>
          <th style="width:30px"></th><th style="text-align:left">Device</th><th>Signal</th><th>Backup</th><th style="text-align:left">Status</th>
        </tr></thead><tbody>${rows}</tbody></table>
      </div>`;
    this.querySelector("#m-actions").innerHTML = `
      <button id="ra-start" ${chosen.length ? "" : "disabled"}>Start (${chosen.length})</button>
      <button id="ra-cancel" class="ghost" style="margin-left:auto">Cancel</button>`;
    this.querySelectorAll('input[name="ra-preset"]').forEach((r) => r.onchange = (e) => {
      this._raPreset = e.target.value;
      this._raSel = this._raDefaultSel(this._raPreset, this._raThreshold);
      this._renderReadAllDialog();
    });
    const thEl = this.querySelector("#ra-th");
    if (thEl) thEl.onchange = (e) => {
      const v = parseInt(e.target.value, 10);
      this._raThreshold = isNaN(v) ? -85 : v;
      if (this._raPreset !== "custom") this._raSel = this._raDefaultSel(this._raPreset, this._raThreshold);
      this._renderReadAllDialog();
    };
    this.querySelectorAll(".ra-pick").forEach((c) => c.onchange = () => {
      const mac = c.dataset.mac;
      if (c.checked) this._raSel.add(mac); else this._raSel.delete(mac);
      this._raPreset = "custom";
      const sc = this.querySelector("#ra-scroll"); const top = sc ? sc.scrollTop : 0;
      this._renderReadAllDialog();
      const sc2 = this.querySelector("#ra-scroll"); if (sc2) sc2.scrollTop = top;
    });
    this.querySelector("#ra-cancel").onclick = () => this._closeModal();
    const start = this.querySelector("#ra-start");
    if (start) start.onclick = () => { const macs = [...this._raSel]; this._closeModal(); this._startReadAll(macs); };
  }

  _bulkActiveNow() { return !!(this._bulkStatus && this._bulkStatus.active); }

  // The job runs on the BACKEND (survives F5 / navigation / tab close). The panel only starts it
  // and polls its status — so any panel instance, even a freshly created one, sees the live progress.
  async _startReadAll(macs) {
    if (!macs.length) return;
    const entries = macs.map((mac) => ({ mac, proxy: ((this._devs || []).find((d) => d.mac === mac) || {}).proxy || "" }));
    this._bulkResultShown = false;
    try {
      await this._ws({ type: "telink_manager/read_all", entries });
    } catch (e) {
      this._status("Read all failed to start: " + this._errMsg(e));
      return;
    }
    this._startBulkPolling();
  }

  _startBulkPolling() {
    if (this._bulkPollTimer) return;
    this._bulkPollTimer = setInterval(() => this._bulkPoll(), 1500);
    this._bulkPoll();
  }

  _stopBulkPolling() {
    if (this._bulkPollTimer) { clearInterval(this._bulkPollTimer); this._bulkPollTimer = null; }
  }

  async _bulkPoll() {
    let st;
    try { st = await this._ws({ type: "telink_manager/bulk_status" }); }
    catch (e) { return; }
    this._bulkStatus = st;
    this._renderBulkStrip(st);
    // reflect the backups read so far in the device table
    if (st.state && st.state.backups) {
      let changed = false;
      Object.entries(st.state.backups).forEach(([mac, cnt]) => {
        if (this._backupMacs.get(mac) !== cnt) { this._backupMacs.set(mac, cnt); changed = true; }
      });
      if (changed && this.querySelector("#list")) this._renderDevTable();
    }
    const ra = this.querySelector("#readall-btn"); if (ra) ra.disabled = !!st.active || this._busy;
    const rd = this.querySelector("#read"); if (rd) rd.disabled = !!st.active || this._readDisabled();
    // show the result once on whichever panel is live, then dismiss it server-side
    if (!st.active && st.result && !this._bulkResultShown) {
      this._bulkResultShown = true;
      const res = st.result;
      this._status(`Read all ${res.cancelled ? "cancelled" : "done"}: ✓${res.ok} read, ✗${res.fail} failed.`);
      this._ws({ type: "telink_manager/bulk_dismiss" }).catch(() => {});
      this._showReadAllResult(res, res.cancelled);
    }
    if (!st.active && (!st.result || this._bulkResultShown)) this._stopBulkPolling();
  }

  _renderBulkStrip(st) {
    const el = this.querySelector("#bulk-strip");
    if (!el) return;
    const s = st && st.state;
    if (!st || !st.active || !s) { el.style.display = "none"; el.innerHTML = ""; return; }
    const nameOf = (m) => this._names[m] || ((this._devs || []).find((d) => d.mac === m) || {}).name || m;
    const running = (s.running || []).map(nameOf);
    const runTxt = running.length ? ` · <span class="muted">now: ${running.slice(0, 3).join(", ")}${running.length > 3 ? ` +${running.length - 3}` : ""}</span>` : "";
    const cancelling = st.cancel ? ` · <span style="color:var(--tm-warn)">cancelling — finishing current devices safely…</span>` : "";
    // numerator = devices STARTED (done + currently in progress), so the first one shows 1/N, not 0/N
    const started = Math.min(s.total, s.done + (s.running ? s.running.length : 0));
    el.style.cssText = "display:block;margin-top:8px;padding:8px 12px;border:1px solid var(--tm-border);border-radius:var(--tm-radius);background:var(--tm-bg-2)";
    el.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:13px">
        <span class="spinner"></span><b>Reading… ${started}/${s.total}</b>
        <span style="color:#4caf50">✓ ${s.ok}</span><span style="color:#e53935">✗ ${s.fail}</span>
        ${runTxt}${cancelling}
        <button id="bulk-cancel" class="ghost" style="margin-left:auto" ${st.cancel ? "disabled" : ""}>Cancel</button>
      </div>`;
    const cb = this.querySelector("#bulk-cancel");
    if (cb) cb.onclick = () => { this._ws({ type: "telink_manager/bulk_cancel" }).catch(() => {}); this._bulkPoll(); };
  }

  _showReadAllResult(s, cancelled) {
    this._modalShell("📖 Read all — results");
    const failRows = (s.failures || []).map((f) => {
      const d = (this._devs || []).find((x) => x.mac === f.mac) || {};
      const name = this._names[f.mac] || d.name || f.mac;
      const sig = this._signalOf(d.rssi);
      const hint = (sig.tier === "bad" || sig.tier === "weak") ? " → move it or a BLE proxy closer"
        : (f.error === "timeout" ? " → timed out" : "");
      return `<tr><td style="text-align:left">${escHtml(name)}</td><td>${this._rssiCell(d.rssi)}</td><td style="text-align:left;color:#ff7a7a">${escHtml(f.error)}${hint}</td></tr>`;
    }).join("");
    const hasFail = s.failures && s.failures.length;
    this.querySelector("#m-body").innerHTML = `
      <div style="font-size:14px;margin-bottom:8px">${cancelled ? "Cancelled. " : ""}<span style="color:#4caf50">✓ ${s.ok} read</span> · <span style="color:#e53935">✗ ${s.fail} failed</span> of ${s.total}.</div>
      ${hasFail ? `<div style="overflow:auto;max-height:50vh"><table style="font-size:12px"><thead><tr><th style="text-align:left">Device</th><th>Signal</th><th style="text-align:left">Reason</th></tr></thead><tbody>${failRows}</tbody></table></div>` : `<div class="muted">All selected devices were read successfully. 🎉</div>`}`;
    this.querySelector("#m-actions").innerHTML = `
      ${hasFail ? `<button id="ra-retry">Retry failed (${s.failures.length})</button>` : ""}
      <button id="ra-done" class="ghost" style="margin-left:auto">Close</button>`;
    const rt = this.querySelector("#ra-retry");
    if (rt) rt.onclick = () => { const macs = s.failures.map((f) => f.mac); this._closeModal(); this._startReadAll(macs); };
    this.querySelector("#ra-done").onclick = () => this._closeModal();
  }

  // ---- Compare: side-by-side config matrix for ALL devices (scan ∪ backups), no connect ----
  async _openCompareModal() {
    this._modalShell("📊 Compare");
    const modalEl = this.querySelector(".modal");
    if (modalEl) modalEl.style.width = "min(1150px, 96vw)";
    this._mstatus("Loading…", true);
    let scanDevs = this._devs || [];
    try {
      if (!scanDevs.length) {
        const sr = await this._ws({ type: "telink_manager/scan" });
        scanDevs = (sr && sr.devices) || [];
        this._devs = scanDevs;
        scanDevs.forEach((d) => { this._names[d.mac] = d.friend_name || ""; });
      }
    } catch (e) { /* ignore */ }
    let cmp = [];
    try {
      const cr = await this._ws({ type: "telink_manager/backups_compare" });
      cmp = (cr && cr.devices) || [];
    } catch (e) { /* ignore */ }
    this._mstatus("");

    // union by MAC: every scanned device + every backed-up device (so no-backup devices show too)
    const byMac = {};
    scanDevs.forEach((d) => {
      byMac[d.mac] = { mac: d.mac, friendly: this._names[d.mac] || d.friend_name || "",
        ble: d.name || "", route: d.proxy || "", rssi: d.rssi, f: {} };
    });
    cmp.forEach((c) => {
      const r = byMac[c.mac] || (byMac[c.mac] = { mac: c.mac, friendly: "", ble: "", route: "", rssi: null, f: {} });
      if (!r.friendly) r.friendly = c.friendly_name || "";
      if (!r.ble) r.ble = c.device_name || "";
      r.fw = c.fw; r.last_ts = c.last_ts; r.comfort = c.comfort;
      r.bind_key_set = c.bind_key_set; r.f = c.fields || {};
    });
    const rows = Object.values(byMac);
    const COLS = [
      { k: "friendly", lab: "Friendly", tip: "The local friendly name you set for this device.", get: (r) => r.friendly || "" },
      { k: "ble", lab: "BLE name", tip: "The device's advertised BLE name (e.g. ATC_xxxx).", get: (r) => r.ble || "" },
      { k: "mac", lab: "MAC", tip: "Bluetooth MAC address.", get: (r) => r.mac },
      { k: "fw", lab: "fw", tip: "PVVX firmware version.", cmp: true, get: (r) => r.fw || "" },
      ...this._cmpConfigCols(),
      { k: "lastbk", lab: "Last backup", tip: "When this device's state was last backed up or re-confirmed.", get: (r) => r.last_ts ? this._bkTs(r.last_ts) : "—" },
    ];
    this._cmpRows = rows; this._cmpCols = COLS; this._cmpMode = "compare";
    this._cmpBusy = this._cmpBusy || new Set();
    this._cmpOnlyDiff = false;
    this._cmpSelected = new Set();
    this._cmpOnlySelected = false;
    this._recomputeCompare();
    this._renderCompare();

    this.querySelector("#m-actions").innerHTML = `
      <button id="cmp-export" class="ghost">⬇ Export</button>
      <button id="cmp-close" class="ghost" style="margin-left:auto">Close</button>`;
    this.querySelector("#cmp-export").onclick = () => this._exportCompare();
    this.querySelector("#cmp-close").onclick = () => this._closeModal();
  }

  // Shared config columns (same getters for the Compare matrix and the per-device History matrix).
  _cmpConfigCols() {
    const yn = (v) => v == null ? "" : (v ? "yes" : "no");
    return [
      { k: "adv", lab: "Adv (s)", tip: "Advertising interval — how often the device broadcasts a BLE packet (seconds).", cmp: true, get: (r) => r.f.adv_interval_s },
      { k: "mult", lab: "Meas mult", tip: "Measure multiplier — number of advertisements between two real measurements.", cmp: true, get: (r) => r.f.measure_mult },
      { k: "period", lab: "Effective (s)", tip: "Effective measurement period = advertising interval × measure multiplier (seconds).", cmp: true, get: (r) => r.f.measure_period_s },
      { k: "unit", lab: "Unit", tip: "Temperature unit shown on the LCD (°C / °F).", cmp: true, get: (r) => r.f.temp_F == null ? "" : (r.f.temp_F ? "°F" : "°C") },
      { k: "disp", lab: "Display", tip: "LCD on or off.", cmp: true, get: (r) => r.f.screen_off == null ? "" : (r.f.screen_off ? "OFF" : "ON") },
      { k: "smiley", lab: "Smiley", tip: "Which face/icon (0–7) is shown on the LCD.", cmp: true, get: (r) => r.f.smiley },
      { k: "comfsm", lab: "Comfort smiley", tip: "Show the comfort smiley when readings are in the comfort range.", cmp: true, get: (r) => yn(r.f.comfort_smiley) },
      { k: "blink", lab: "Blinking", tip: "Blink the time/smiley on the LCD.", cmp: true, get: (r) => yn(r.f.blinking_time_smile) },
      { k: "batt", lab: "Show batt", tip: "Show the battery indicator on the LCD.", cmp: true, get: (r) => yn(r.f.show_batt) },
      { k: "tx", lab: "TX meas", tip: "Stream measurements while connected (TX measures).", cmp: true, get: (r) => yn(r.f.tx_measures) },
      { k: "lp", lab: "Low-power", tip: "Low-power measurement mode (saves battery).", cmp: true, get: (r) => yn(r.f.lp_measures) },
      { k: "advtype", lab: "Adv type", tip: "Advertising format: BTHome / pvvx / atc1441 / mi_like. HA decodes BTHome.", cmp: true, get: (r) => r.f.adv_type },
      { k: "advdelay", lab: "Adv delay (ms)", tip: "Pseudo-random extra delay added to each advertisement (ms).", cmp: true, get: (r) => r.f.adv_delay_ms },
      { k: "evcnt", lab: "Event cnt", tip: "How many times each event beacon (e.g. reed/trigger) is repeated.", cmp: true, get: (r) => r.f.event_adv_cnt },
      { k: "avg", lab: "Averaging", tip: "Averaging window written to flash (0 = off).", cmp: true, get: (r) => r.f.averaging },
      { k: "lcdref", lab: "LCD refresh (s)", tip: "Minimum LCD refresh interval (seconds).", cmp: true, get: (r) => r.f.lcd_refresh_s },
      { k: "txpw", lab: "RF TX pwr", tip: "RF transmit power (firmware enum value). Lower = less range, less battery.", cmp: true, get: (r) => r.f.rf_tx_power },
      { k: "lat", lab: "Conn lat (ms)", tip: "BLE connection latency (ms).", cmp: true, get: (r) => r.f.connect_latency_ms },
      { k: "comfort", lab: "Comfort", tip: "Comfort thresholds: temperature low–high °C / humidity low–high %.", cmp: true, get: (r) => r.comfort ? `${r.comfort.t_lo}-${r.comfort.t_hi}°C / ${r.comfort.h_lo}-${r.comfort.h_hi}%` : "" },
      { k: "bind", lab: "Bind key", tip: "Whether an encryption bind key is set on the device.", cmp: true, get: (r) => r.bind_key_set ? "set" : (r.f.raw ? "—" : "") },
    ];
  }

  _recomputeCompare() {
    const rows = this._cmpRows || [], COLS = this._cmpCols || [];
    const val = (c, r) => { const v = c.get(r); return v == null || v === "" ? "" : String(v); };
    COLS.forEach((c) => {
      if (!c.cmp) return;
      const vals = rows.map((r) => val(c, r)).filter((v) => v !== "");
      c._distinct = [...new Set(vals)];
      c._uniform = c._distinct.length <= 1;
    });
  }

  // Per-device History: every snapshot of one device as a row (timeline), to see what changed over time.
  async _openHistoryModal(mac) {
    this._modalShell("📊 History");
    const modalEl = this.querySelector(".modal");
    if (modalEl) modalEl.style.width = "min(1150px, 96vw)";
    this.querySelector("#m-title").innerHTML = this._devTitleHtml(mac, "📊") +
      ` <span style="font-size:11px;font-weight:400;background:var(--secondary-background-color,#2a2a2a);padding:2px 8px;border-radius:10px;margin-left:6px;color:var(--secondary-text-color,#aaa)">history</span>`;
    this._mstatus("Loading…", true);
    let snaps = [];
    try {
      const r = await this._ws({ type: "telink_manager/backups_history", mac });
      snaps = (r && r.snapshots) || [];
    } catch (e) { /* ignore */ }
    this._mstatus("");
    const rows = snaps.slice().reverse().map((s) => ({
      ts: s.ts, device_name: s.device_name, fw: s.fw, comfort: s.comfort,
      bind_key_set: s.bind_key_set, f: s.fields || {},
    }));
    const COLS = [
      { k: "ts", lab: "Time", tip: "When this backup was taken.", get: (r) => this._bkTs(r.ts) },
      { k: "name", lab: "Device name", tip: "Device name stored on the device at that time.", cmp: true, get: (r) => r.device_name || "" },
      { k: "fw", lab: "fw", tip: "PVVX firmware version.", cmp: true, get: (r) => r.fw || "" },
      ...this._cmpConfigCols(),
    ];
    this._cmpRows = rows; this._cmpCols = COLS; this._cmpMode = "history";
    this._cmpOnlyDiff = false;
    this._recomputeCompare();
    this._renderCompare();

    this.querySelector("#m-actions").innerHTML = `
      <button id="h-back" class="ghost">‹ Backups</button>
      <button id="cmp-export" class="ghost">⬇ Export</button>
      <button id="h-close" class="ghost" style="margin-left:auto">Close</button>`;
    // "‹ Backups" returns to the global Backups list (matching its label), not this one device's screen.
    this.querySelector("#h-back").onclick = () => this._openBackupsModal();
    this.querySelector("#cmp-export").onclick = () => this._exportCompare();
    this.querySelector("#h-close").onclick = () => this._closeModal();
  }

  async _compareRescan(mac) {
    if (!mac || this._cmpBusy.has(mac)) return;
    this._cmpBusy.add(mac);
    this._renderCompare();
    try {
      const r = await this._ws({ type: "telink_manager/read", mac });
      if (r && r.ok) {
        const f = r.fields, row = (this._cmpRows || []).find((x) => x.mac === mac);
        if (row) {
          row.f = f;
          row.fw = f.fw_version;
          row.last_ts = Math.floor(Date.now() / 1000);
          row.bind_key_set = !!f.bind_key;
          if (f.comfort_t_lo != null) row.comfort = { t_lo: f.comfort_t_lo, t_hi: f.comfort_t_hi, h_lo: f.comfort_h_lo, h_hi: f.comfort_h_hi };
          if (!row.ble && f.device_name) row.ble = f.device_name;
        }
      }
    } catch (e) { /* ignore */ }
    this._cmpBusy.delete(mac);
    this._recomputeCompare();
    this._renderCompare();
  }

  _renderCompare() {
    const onlyDiff = !!this._cmpOnlyDiff;
    const selMode = this._cmpMode === "compare";   // row selection only makes sense across devices
    const selSet = this._cmpSelected || (this._cmpSelected = new Set());
    const onlySel = selMode && !!this._cmpOnlySelected;
    const all = this._cmpCols || [];
    let rows = this._cmpRows || [];
    if (onlySel) rows = rows.filter((r) => selSet.has(r.mac));
    const cols = all.filter((c) => !onlyDiff || !c.cmp || !c._uniform);
    const val = (c, r) => { const v = c.get(r); return v == null || v === "" ? "" : String(v); };
    const allSel = selMode && rows.length > 0 && rows.every((r) => selSet.has(r.mac));
    // dedicated sticky columns: [checkbox][re-read ⟳], so the header tick + the ⟳ each sit in their own
    // column above the row controls; the Friendly column then holds just the name.
    const CKW = 36, RSW = 38, BG = "var(--card-background-color,#1e1e1e)";
    const friendlyLeft = selMode ? `${CKW + RSW}px` : "0";
    const selTh = selMode
      ? `<th style="position:sticky;left:0;z-index:4;width:${CKW}px;text-align:center"><input type="checkbox" id="cmp-sel-all" ${allSel ? "checked" : ""} title="Select all" style="vertical-align:middle"></th>` +
        `<th title="Re-read each device" style="position:sticky;left:${CKW}px;z-index:4;width:${RSW}px;text-align:center">⟳</th>` : "";
    const head = selTh + cols.map((c, i) => {
      const sticky = i === 0 ? `position:sticky;left:${friendlyLeft};z-index:3;` : "";
      return `<th title="${(c.tip || c.lab).replace(/"/g, "&quot;")}" style="cursor:help;${sticky}">${c.lab}</th>`;
    }).join("");
    const body = rows.map((r) => {
      const busy = r.mac && this._cmpBusy && this._cmpBusy.has(r.mac);
      const ctrlTds = selMode
        ? `<td style="position:sticky;left:0;z-index:2;width:${CKW}px;text-align:center;background:${BG}">${r.mac ? `<input type="checkbox" class="cmp-sel" data-mac="${r.mac}" ${selSet.has(r.mac) ? "checked" : ""} style="vertical-align:middle">` : ""}</td>` +
          `<td style="position:sticky;left:${CKW}px;z-index:2;width:${RSW}px;text-align:center;background:${BG}">${r.mac ? `<button class="cmp-rescan ghost" data-mac="${r.mac}" ${busy ? "disabled" : ""} style="padding:1px 6px" title="Re-read this device now (background)">${busy ? "…" : "⟳"}</button>` : ""}</td>` : "";
      const tds = cols.map((c, i) => {
        const v = val(c, r);
        if (i === 0) {
          return `<td style="position:sticky;left:${friendlyLeft};background:${BG};font-weight:600;white-space:nowrap;text-align:left">${escHtml(v) || "—"}</td>`;
        }
        const bg = (c.cmp && !c._uniform && v !== "") ? `background:hsl(${(c._distinct.indexOf(v) * 67) % 360} 45% 22%);` : "";
        return `<td style="${bg}white-space:nowrap">${escHtml(v) || "—"}</td>`;
      }).join("");
      return `<tr>${ctrlTds}${tds}</tr>`;
    }).join("");
    const count = this._cmpMode === "history"
      ? `${rows.length} backup${rows.length === 1 ? "" : "s"}`
      : `${rows.length} device${rows.length === 1 ? "" : "s"}`;
    const note = this._cmpMode === "history"
      ? `All saved backups of this device, newest first. Cells with the same value share a colour, so changes over time stand out.`
      : `All devices (scanned or backed-up). Settings are from each device's last backup; identical values share a colour. ⟳ = re-read.`;
    const diffLabel = this._cmpMode === "history" ? "Only changed columns" : "Only differing columns";
    const selFilter = selMode
      ? `<label style="cursor:pointer;white-space:nowrap;font-size:13px"><input type="checkbox" id="cmp-onlysel" ${onlySel ? "checked" : ""}> Show only selected${selSet.size ? ` <span class="muted">(${selSet.size})</span>` : ""}</label>` : "";
    this.querySelector("#m-body").innerHTML = `
      <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:8px">
        <label style="cursor:pointer;white-space:nowrap;font-size:13px"><input type="checkbox" id="cmp-diff" ${onlyDiff ? "checked" : ""}> ${diffLabel}</label>
        ${selFilter}
        <span class="muted">${note} (${count})</span></div>
      <div id="cmp-scroll" style="overflow:auto;max-height:60vh">
        <table style="font-size:12px"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>
      </div>`;
    const cb = this.querySelector("#cmp-diff");
    if (cb) cb.onchange = (e) => { this._cmpOnlyDiff = e.target.checked; this._renderCompare(); };
    const os = this.querySelector("#cmp-onlysel");
    if (os) os.onchange = (e) => { this._cmpOnlySelected = e.target.checked; this._renderCompare(); };
    const sa = this.querySelector("#cmp-sel-all");
    if (sa) sa.onchange = (e) => {
      if (e.target.checked) rows.forEach((r) => r.mac && selSet.add(r.mac));
      else rows.forEach((r) => selSet.delete(r.mac));
      this._renderCompare();
    };
    this.querySelectorAll(".cmp-sel").forEach((c) => c.onchange = () => {
      const mac = c.dataset.mac;
      if (c.checked) selSet.add(mac); else selSet.delete(mac);
      const sc = this.querySelector("#cmp-scroll");
      const top = sc ? sc.scrollTop : 0, left = sc ? sc.scrollLeft : 0;
      this._renderCompare();
      const sc2 = this.querySelector("#cmp-scroll");
      if (sc2) { sc2.scrollTop = top; sc2.scrollLeft = left; }
    });
    if (selMode)
      this.querySelectorAll(".cmp-rescan").forEach((b) => b.onclick = (e) => { e.stopPropagation(); this._compareRescan(b.dataset.mac); });
  }

  // Ask CSV or YAML, then export the current matrix (Compare or History).
  async _exportCompare() {
    const fmt = await this._chooseExportFormat();
    if (fmt === "csv") this._exportCompareCsv();
    else if (fmt === "yaml") this._exportCompareYaml();
  }

  _chooseExportFormat() {
    return new Promise((resolve) => {
      const host = document.createElement("div");
      host.className = "overlay";
      host.style.zIndex = "10001";
      host.innerHTML = `
        <div class="modal" style="max-width:380px">
          <div style="font-size:15px;font-weight:600;margin-bottom:2px">Export format</div>
          <div class="muted" style="margin-bottom:6px">Choose a file format to download.</div>
          <div class="actions" style="border-top:none;padding-top:6px">
            <button class="ghost" data-act="cancel" style="margin-right:auto">Cancel</button>
            <button class="choice" data-act="csv">CSV</button>
            <button class="choice" data-act="yaml">YAML</button>
          </div>
        </div>`;
      const done = (val) => { host.remove(); resolve(val); };
      host.addEventListener("click", (e) => {
        if (e.target === host) return done(null);
        const act = e.target.dataset && e.target.dataset.act;
        if (act === "csv" || act === "yaml") done(act);
        else if (act === "cancel") done(null);
      });
      this.appendChild(host);
    });
  }

  _download(name, text, mime) {
    const blob = new Blob([text], { type: mime });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  _exportCompareCsv() {
    const rows = this._cmpRows || [], COLS = this._cmpCols || [];
    const esc = (s) => `"${String(s == null ? "" : s).replace(/"/g, '""')}"`;
    const lines = [COLS.map((c) => esc(c.lab)).join(",")];
    rows.forEach((r) => lines.push(COLS.map((c) => esc(c.get(r))).join(",")));
    this._download("telink-compare.csv", "﻿" + lines.join("\r\n"), "text/csv;charset=utf-8");
  }

  _exportCompareYaml() {
    const rows = this._cmpRows || [], COLS = this._cmpCols || [];
    // JSON-encode every key/value: JSON scalars are valid YAML, so quoting/escaping is handled for free.
    const q = (v) => JSON.stringify(v == null || v === "" ? "" : String(v));
    const out = ["# Telink compare export"];
    rows.forEach((r) => {
      COLS.forEach((c, i) => out.push(`${i === 0 ? "- " : "  "}${q(c.lab)}: ${q(c.get(r))}`));
    });
    this._download("telink-compare.yaml", out.join("\n") + "\n", "text/yaml;charset=utf-8");
  }

  // Global entry: list every device that has backups (no connection needed), pick one to drill in.
  async _openBackupsModal() {
    this._modalShell("🗄️ Backups");
    this._mstatus("Loading…", true);
    let devs = [];
    try {
      const r = await this._ws({ type: "telink_manager/backups_index" });
      devs = (r && r.devices) || [];
    } catch (e) { /* ignore */ }
    this._mstatus("");
    const rows = devs.map((d) => {
      const fr = d.friendly_name || "", dn = d.device_name || "";
      return `<div class="fld bkdev" data-mac="${d.mac}" style="cursor:pointer;justify-content:space-between;border-bottom:1px solid var(--divider-color,#333)">
        <div><b>${escHtml(fr || dn || d.mac)}</b>${fr && dn ? ` <span class="muted">· ${escHtml(dn)}</span>` : ""}
          <div class="muted" style="font-size:11px">${escHtml(d.mac)} · ${d.count} backup${d.count > 1 ? "s" : ""}</div></div>
        <div style="white-space:nowrap;display:flex;align-items:center;gap:8px">
          <button class="bk-hist ghost" data-mac="${d.mac}" style="padding:2px 8px" title="History — what changed over time">📊 History</button>
          <span class="muted">${this._bkTs(d.last_ts)} ›</span></div>
      </div>`;
    }).join("") ||
      `<div class="muted">No backups yet. Scan → Connect to a device — a backup is saved automatically on read.</div>`;
    this.querySelector("#m-body").innerHTML = `
      <div class="muted">Devices with saved backups — pick one to view & restore its backups. Browsing needs no connection.</div>
      ${rows}`;
    this.querySelector("#m-actions").innerHTML =
      `<button id="m-bx-close" class="ghost" style="margin-left:auto">Close</button>`;
    this.querySelector("#m-bx-close").onclick = () => this._closeModal();
    this.querySelectorAll(".bkdev").forEach((el) => el.onclick = () => this._backupsForDevice(el.dataset.mac));
    this.querySelectorAll(".bk-hist").forEach((b) => b.onclick = (e) => { e.stopPropagation(); this._openHistoryModal(b.dataset.mac); });
  }

  async _backupsForDevice(mac) {
    this.querySelector("#m-title").innerHTML = this._devTitleHtml(mac, "🗄️");
    this._mstatus("Loading backups…", true);
    let list = [];
    try {
      const r = await this._ws({ type: "telink_manager/backups_list", mac });
      list = (r && r.backups) || [];
    } catch (e) { /* ignore */ }
    this._mstatus("");
    const empty = list.length === 0;
    const rows = list.slice().reverse().map((s) => {
      const i = list.indexOf(s);
      return `<div class="fld" style="justify-content:space-between;border-bottom:1px solid var(--divider-color,#333)">
        <div><b>${this._bkTs(s.ts)}</b> <span class="muted">· fw ${s.fw || "?"}</span></div>
        <div style="white-space:nowrap">
          <button class="bk-use" data-i="${i}">Restore…</button>
          <button class="bk-clone ghost" data-i="${i}">Clone…</button>
          <button class="bk-del ghost" data-i="${i}">🗑</button></div>
      </div>`;
    }).join("");

    const emptyState = `
      <div style="text-align:center;padding:30px 18px">
        <div style="font-size:42px;line-height:1;opacity:.45">🗄️</div>
        <div style="font-size:15px;font-weight:600;margin-top:12px;color:var(--primary-text-color,#e6e6e6)">No backups yet</div>
        <div style="font-size:12.5px;margin-top:7px;max-width:380px;margin-inline:auto;line-height:1.55;color:var(--secondary-text-color,#9aa0a6)">
          A full-state backup — config, name, comfort, bind key &amp; sensor — is saved
          <b style="color:var(--primary-text-color,#ccc)">automatically</b> every time you read this device, and after each change.
          Just <b style="color:var(--primary-text-color,#ccc)">Connect &amp; Read</b> it to create the first one.
        </div>
      </div>`;

    this.querySelector("#m-body").innerHTML = empty
      ? `<div id="bk-list">${emptyState}</div><div id="bk-restore" style="display:none"></div>`
      : `<div id="bk-intro" class="muted">Full-state backups (config + name + comfort + bind key + sensor), newest first.
           Auto-saved on read and after each change (deduped, last 20 kept).</div>
         <div id="bk-list">${rows}</div>
         <div id="bk-restore" style="display:none"></div>`;

    this.querySelector("#m-actions").innerHTML = `
      <button id="m-bk-back" class="ghost">‹ Devices</button>
      <button id="m-bk-history" class="ghost"${empty ? ' disabled title="No history yet — this device has no backups"' : ""}>📊 History</button>
      <button id="m-bk-close" class="ghost" style="margin-left:auto">Close</button>`;
    this.querySelector("#m-bk-back").onclick = () => this._openBackupsModal();
    if (!empty) this.querySelector("#m-bk-history").onclick = () => this._openHistoryModal(mac);
    this.querySelector("#m-bk-close").onclick = () => this._closeModal();

    this.querySelectorAll(".bk-del").forEach((b) => b.onclick = async () => {
      const s = list[+b.dataset.i];
      if (!(await this._confirm("Delete this backup?", { okText: "Delete", danger: true }))) return;
      const r = await this._ws({ type: "telink_manager/backup_delete", mac, ts: s.ts }).catch(() => null);
      // Keep the main-page Backup count in sync with the new total.
      if (r && typeof r.count === "number") { this._backupMacs.set(mac, r.count); this._renderDevTable(); }
      this._backupsForDevice(mac);
    });
    this.querySelectorAll(".bk-use").forEach((b) => b.onclick = () => this._showRestore(mac, list[+b.dataset.i], "restore"));
    this.querySelectorAll(".bk-clone").forEach((b) => b.onclick = () => this._showRestore(mac, list[+b.dataset.i], "clone"));
  }

  // mode "restore" = back onto the SAME device; mode "clone" = copy onto ANOTHER device (never the MAC).
  _showRestore(mac, snap, mode) {
    const box = this.querySelector("#bk-restore");
    if (!box || !snap) return;
    const isClone = mode === "clone";
    // while the restore/clone panel is open, hide the backup list + intro + History button
    const showList = (show) => {
      ["bk-list", "bk-intro"].forEach((id) => { const el = this.querySelector("#" + id); if (el) el.style.display = show ? "" : "none"; });
      const h = this.querySelector("#m-bk-history"); if (h) h.style.display = show ? "" : "none";
    };
    const closeRestore = () => { box.style.display = "none"; box.innerHTML = ""; showList(true); };
    showList(false);

    let targetCtrl;
    if (isClone) {
      const others = (this._devs || []).filter((d) => d.mac !== snap.mac);
      if (!others.length) {
        box.style.display = "";
        box.innerHTML = `<div class="advzone" style="margin-top:12px">
          <div class="warn">No other device to clone to — run a Scan first so other devices appear.</div>
          <div class="fld"><button id="rs-cancel" class="ghost">← Back</button></div></div>`;
        this.querySelector("#rs-cancel").onclick = closeRestore;
        return;
      }
      // Label: "friendly - ble name (mac)"; if no friendly, just "ble name (mac)". Sorted A→Z.
      const optData = others.map((d) => {
        const fr = this._names[d.mac] || "", ble = d.name || d.mac;
        return { mac: d.mac, label: fr ? `${fr} - ${ble} (${d.mac})` : `${ble} (${d.mac})` };
      }).sort((a, b) => a.label.localeCompare(b.label));
      const opts = optData.map((o) => `<option value="${escHtml(o.mac)}">${escHtml(o.label)}</option>`).join("");
      targetCtrl = `<div class="fld"><span class="lab">Clone to device</span>
        <select id="rs-target" style="max-width:340px">
          <option value="" disabled selected>Select a device to clone to…</option>${opts}</select>
        <button id="rs-cancel" class="ghost" style="margin-left:8px">← Back</button></div>`;
    } else {
      targetCtrl = `<input type="hidden" id="rs-target" value="${snap.mac}">`;
    }
    const ck = (id, lab, tip) => `<label title="${tip}" style="margin-right:14px;cursor:pointer"><input type="checkbox" id="${id}"> ${lab}</label>`;
    const who = this._names[mac] || mac;
    const intro = isClone
      ? `Copy the ticked settings from this backup onto ANOTHER device (nothing ticked by default). The MAC is never cloned.`
      : `Restore the ticked settings to <b>${who}</b> from this backup (nothing ticked by default).`;
    box.style.display = "";
    box.innerHTML = `
      <div class="advzone" style="margin-top:12px">
        <h4 style="margin:0 0 6px">${isClone ? "Clone" : "Restore"} backup · ${this._bkTs(snap.ts)}</h4>
        <div class="muted">${intro}</div>
        <div class="muted" style="margin-top:5px">🛡️ The ${isClone ? "target" : ""} device's current settings are saved as a backup automatically on connect, before anything is overwritten — so this stays reversible.</div>
        <div style="margin:8px 0">
          ${ck("rs-config", "Config", "The full 0x55 configuration: adv interval, measure, display, flags, tx power, latency, etc.")}
          ${ck("rs-name", "Device name", "The BLE device name stored on the thermometer.")}
          ${ck("rs-comfort", "Comfort", "Comfort thresholds (temperature & humidity low/high).")}
          ${ck("rs-bind", "Bind key", "The 16-byte encryption bind key.")}
          ${ck("rs-sensor", "Sensor", "Sensor calibration (slope + offset).")}</div>
        ${targetCtrl}
        <div class="fld"><button id="rs-go" class="danger" disabled>${isClone ? "Clone" : "Restore"}</button>
          ${isClone ? "" : `<button id="rs-cancel" class="ghost">← Back</button>`}</div>
      </div>`;
    this.querySelector("#rs-cancel").onclick = closeRestore;
    // The action button stays disabled until at least one item is ticked
    // (and, for clone, until a real target device is chosen) — running it otherwise is a no-op.
    const goBtn = this.querySelector("#rs-go");
    const targetEl = this.querySelector("#rs-target");
    const updateGo = () => {
      const anyTicked = ["rs-config", "rs-name", "rs-comfort", "rs-bind", "rs-sensor"]
        .some((id) => this.querySelector("#" + id).checked);
      const hasTarget = !isClone || !!targetEl.value;
      goBtn.disabled = !(anyTicked && hasTarget);
    };
    ["rs-config", "rs-name", "rs-comfort", "rs-bind", "rs-sensor"]
      .forEach((id) => { this.querySelector("#" + id).onchange = updateGo; });
    if (isClone) targetEl.onchange = updateGo;
    this.querySelector("#rs-go").onclick = async () => {
      const parts = [];
      if (this.querySelector("#rs-config").checked) parts.push("config");
      if (this.querySelector("#rs-name").checked) parts.push("device_name");
      if (this.querySelector("#rs-comfort").checked) parts.push("comfort");
      if (this.querySelector("#rs-bind").checked) parts.push("bind_key");
      if (this.querySelector("#rs-sensor").checked) parts.push("sensor");
      if (!parts.length) { this._mstatus(`Select at least one item to ${isClone ? "clone" : "restore"}.`); return; }
      const target = this.querySelector("#rs-target").value;
      const msg = isClone
        ? `Clone [${parts.join(", ")}] onto ${target}?\n\nIts current settings will be overwritten from this backup. The MAC is NOT cloned.`
        : `Restore [${parts.join(", ")}] to ${target}?\n\nThe current settings will be overwritten from this backup.`;
      if (!(await this._confirm(msg, { okText: isClone ? "Clone" : "Restore", danger: true }))) return;
      const r = await this._runCmd(isClone ? "Cloning…" : "Restoring…",
        { type: "telink_manager/restore", target_mac: target, snapshot: snap, parts },
        (r) => `✅ ${isClone ? "Cloned" : "Restored"}: ` + Object.entries(r.parts || {}).map(([k, v]) => `${k}${v ? "✓" : "✗"}`).join("  "));
      // The result is a MIX (restored parts + untouched parts) — re-read the target so its exact
      // post-op state gets backed up accurately (the _loaded-merge is only safe for single-area edits).
      if (r && r.ok) {
        this._mstatus(`✅ Done — re-reading ${target}…`, true);
        await this._ws({ type: "telink_manager/read", mac: target }).catch(() => {});
        this._backupsForDevice(mac);   // refresh this device's backup list
      }
    };
  }
}
customElements.define("telink-manager-panel", TelinkManagerPanel);
