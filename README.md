# AirPlay to Bluetooth Bridge for Home Assistant

A Home Assistant Add-on that bridges AirPlay audio from iOS/macOS devices to
Amazon Echo (and other) Bluetooth speakers. Each saved speaker appears as its
own AirPlay receiver on your iPhone, and the add-on dynamically switches the
active Bluetooth connection to the speaker you select — so a single Echo can
act as a whole-home AirPlay target.

## Features

- **Multi-instance Shairport-Sync** — one isolated AirPlay receiver per saved
  speaker, each with its own custom display name. iOS sees all of them
  permanently in the AirPlay picker.
- **Dynamic D-Bus Bluetooth switcher** — when you pick a speaker in iOS, the
  add-on disconnects any currently connected device and connects the selected
  one via D-Bus, with a configurable audio buffer to absorb A2DP handshake
  latency.
- **Embedded Web UI (HA Ingress)** — a three-tab dashboard rendered inside the
  Home Assistant sidebar:
  1. **Pairing & Discovery** — scan for Bluetooth devices, pair, and assign a
     custom AirPlay name.
  2. **Saved Speakers** — edit names, test connections, and delete speakers.
  3. **Diagnostics & Debugging** — real-time logs, adapter status, audio sink
     inspection, D-Bus handshake history, log-level toggle, and daemon restart.
- **Persistent storage** — all speaker configuration is saved to
  `/data/options.json` and survives add-on restarts.
- **Auto-recovery** — failed Bluetooth connections are retried with explicit
  error reporting, and saved MACs are polled for availability.

## Requirements

- A Home Assistant system (HAOS or Supervised) running on `amd64` or `aarch64`
  hardware with a working Bluetooth adapter.
- The add-on needs `host_dbus`, `audio`, `host_network`, and `full_access`
  privileges (all declared in `config.yaml`).
- Amazon Echo (or any Bluetooth speaker) within range.

## Installation

1. Add this repository to Home Assistant:
   `Settings → Add-ons → Add-on Stores → ⋮ → Repositories`, then paste the
   GitHub URL of this repository.
2. Refresh, find **AirPlay to Bluetooth Bridge** in the store, and click
   **Install**.
3. Start the add-on. Open the Web UI from the sidebar to pair your first
   speaker.

## Usage

1. In the **Pairing & Discovery** tab, click **Scan for Bluetooth Devices**.
2. When your Echo appears, click **Pair & Configure** and enter a custom
   AirPlay name (e.g. "Wohnzimmer Sound").
3. The speaker now appears in the **Saved Speakers** tab and as an AirPlay
   target on your iPhone. Select it to start streaming.
4. Use the **Diagnostics** tab if you need to inspect logs or restart daemons.

## Configuration

The add-on options (editable in HA) are:

| Option | Default | Description |
|---|---|---|
| `log_level` | `INFO` | Logging verbosity: `INFO`, `DEBUG`, or `TRACE`. |
| `airplay_port_base` | `5000` | Base port for Shairport-Sync instances. |
| `audio_buffer_seconds` | `3` | Ring-buffer delay to absorb A2DP latency. |
| `bluetooth_retry_attempts` | `2` | Retries before a connection fails gracefully. |
| `scan_duration_seconds` | `10` | Bluetooth scan duration. |
| `speaker_db_path` | `/data/options.json` | Path to the persistent speaker database. |

## Changelog

### 1.3.0 (2026-09-01)

- Replaced the `bluetoothctl`-based pairing agent with a native Python
  BlueZ D-Bus Agent (`org.bluez.Agent1`) using `dbus-next`. The agent
  is registered at startup via `AgentManager1.RegisterAgent` and
  `RequestDefaultAgent` — no `bluetoothctl` subprocess calls.
- `Pair()` and `Connect()` now call `StopDiscovery()` first, because
  active scanning interferes with BlueZ link-key establishment.
- `Trusted` is set to `True` on the Device1 object via D-Bus before
  connecting. A2DP sink profile is connected explicitly after pairing.
- Active scanning queries BlueZ D-Bus `GetManagedObjects` for `Alias`,
  `Name`, `RSSI`, `Paired`, `Trusted`, and `Connected`.

### 1.2.0 (2026-09-01)

- Added temporary "Discoverable & Pairable Mode" (3 minutes): the
  adapter is set to `Discoverable` + `Pairable` and a BlueZ D-Bus
  Agent (`NoInputNoOutput`) auto-accepts incoming pairing requests.
  The timer reverts both properties and unregisters the agent on
  expiry.
- Active device-name resolution: after scanning, each discovered
  device is queried via `bluetoothctl info` for `Alias`, `Name`,
  `RSSI`, `Paired`, `Trusted`, and `Connected` so the frontend shows
  human-readable names instead of raw MAC addresses.
- New "Enable Pairing Mode (3 min)" button in the Pairing & Discovery
  tab with a live countdown timer and instruction text.

### 1.1.0 (2026-09-01)

- Fixed Ingress compatibility: all Web UI buttons, tabs, and API
  requests now work correctly inside the Home Assistant sidebar.
- Tab switching is fully client-side and instant.
- Added a visible error overlay with stack trace and copy button if
  the UI fails to initialize.
- Removed the invalid `rfkill` Alpine package and stale `/dev/rfkill`
  device mapping.

### 1.0.0 (2026-08-31)

- Initial release.

## License

MIT — see [LICENSE](LICENSE).
