# Changelog

## 2.0.40 (2026-09-03)

- **Fixed audio never reaching the Bluetooth speaker (critical)**: The shairport-sync config always used `output_device = "default"` even when a specific A2DP sink name was provided. Audio went to PipeWire's auto_null instead of the Bluetooth speaker. Now the config writes the actual A2DP sink name as `output_device` when available. Also fixed `audio_backend_latency` (was set to integer seconds, now uses the correct `audio_backend_latency_offset_in_seconds` at 0.25s).
- **Fixed stream-move command in bt_switch.sh**: Used `pactl list short` (lists all object types) instead of `pactl list sink-inputs short`, so existing playback streams were never moved to the BT sink on AirPlay stream start.
- **Fixed potential keepalive deadlock**: The keepalive pacat process used `stderr=PIPE` but the monitor thread only read stderr after the process exited, risking a pipe-buffer deadlock on long-running processes. Changed to `stderr=DEVNULL`.
- **Fixed keepalive not cleaned up on BT disconnect**: When a Bluetooth device disconnected, the silent keepalive stream was left running. Now `stop_instance` also stops the PipeWire keepalive for that speaker.

## 2.0.39 (2026-09-03)

- **Fixed AirPlay metadata display (track title, artist, album never shown)**: The metadata reader was creating a FIFO at a different path than shairport-sync was writing to. Shairport-sync wrote to its default pipe `/tmp/shairport-sync-metadata`, but the reader listened on a per-speaker FIFO in the runtime dir. Now the shairport-sync config explicitly sets `pipe_name` to the per-speaker FIFO path. Additionally, replaced the broken text-based pipe parser (shairport-sync writes binary metadata, not `key=value` lines) with a D-Bus poller that reads Title/Artist/Album from shairport-sync's native `org.gnome.ShairportSync` D-Bus interface every 3 seconds. The pipe is still used as a secondary signal to detect playback start.

## 2.0.38 (2026-09-03)

- **Fixed broken sink keepalive (was silently exiting immediately)**: The `pacat` keepalive process in 2.0.37 was started with `stdin=DEVNULL` and `/dev/zero` as a positional argument. `pacat` reads from stdin, not from a file argument, so it received zero bytes and exited instantly — the sink had no protection against WirePlumber removal. Now `/dev/zero` is opened as a file descriptor and piped to `pacat`'s stdin, creating a genuine continuous silence stream. A monitor thread logs if the keepalive ever exits unexpectedly.
- **Broadened WirePlumber anti-suspend rules**: Added `monitor.alsa.rules` alongside `monitor.bluez.rules` to catch Bluetooth nodes that appear under either monitor. Simplified glob to `~bluez_*` to match all Bluetooth node name variants.

## 2.0.37 (2026-09-03)

- **Fixed disappearing Bluetooth A2DP sink (root cause of no audio)**: WirePlumber was suspending and removing idle Bluetooth sink nodes after a few seconds. By the time shairport-sync or the test tone tried to play audio, the sink was gone and everything routed to `auto_null` (silence). Three fixes:
  1. **WirePlumber config**: Added `session.suspend-timeout-seconds = 0`, `node.pause-on-idle = false`, and `node.suspend-on-idle = false` for all `bluez_sink.*` and `bluez_output.*` nodes so WirePlumber never removes them.
  2. **Sink keepalive**: After the A2DP sink appears, a background `pacat` process streams silence at volume 0 to hold the PipeWire node active. This guarantees the sink stays alive until the speaker is disconnected.
  3. **Test tone guard**: The test tone now refuses to play to `auto_null` and instead logs full diagnostics, so we immediately see when the sink has vanished.
- **Fixed audio_hook.sh hardcoded runtime path**: The hook script used a hardcoded path instead of inheriting `XDG_RUNTIME_DIR` from shairport-sync's environment. Now uses the inherited value, matching the fix already applied to `bt_switch.sh`.

## 2.0.36 (2026-09-03)

- **Fixed shairport-sync crash: "pa" backend not supported**: the `-o pa` flag added in 2.0.35 caused a fatal crash because this build of shairport-sync does not expose a working "pa" backend despite being compiled with `--with-pulseaudio`. Reverted to the ALSA backend, which works through PipeWire's ALSA compatibility layer. Audio is now routed to the correct Bluetooth sink via the `PIPEWIRE_NODE` environment variable set to the A2DP sink name. This is the standard PipeWire mechanism for sink targeting.
- **Added `PIPEWIRE_NODE` to test tone playback**: the test tone now also sets `PIPEWIRE_NODE` to the Bluetooth sink, ensuring `paplay`/`aplay` route audio to the correct device instead of `auto_null`.
- **Added detailed diagnostic logging for test tone**: logs all available sinks before playback, logs each playback attempt with full command and return code/stderr, and logs warnings (not debug) on failure so issues are visible at normal log levels.

## 2.0.35 (2026-09-03) [BROKEN — do not use]

- Introduced `-o pa` flag which crashes shairport-sync. Superseded by 2.0.36.
- Fixed bt_switch.sh using wrong PipeWire socket path.

## 2.0.34 (2026-09-03)

- **Fixed WirePlumber "can't find protocol PipeWire:Protocol:Native"**: the custom `wireplumber.conf` from 2.0.32 replaced the entire WirePlumber configuration instead of supplementing it. The `context.modules` section was missing, so `libpipewire-module-protocol-native` was never loaded and WirePlumber could not connect to PipeWire at all — no Bluetooth sinks were ever created. Replaced the monolithic config with a drop-in fragment at `wireplumber.conf.d/90-headless-bluetooth.conf` that only disables `seat-monitoring` and `logind` without touching module loading.
- **Fixed NameError crash in `_force_bt_reconnect`**: the method referenced `_mac_to_path()` which is defined in `bluetooth.py`, not `pipewire.py`. Removed the unused variable assignment that caused the `NameError` and the HTTP 500 on every test-connect attempt.
- **Fixed aggressive A2DP disconnect/reconnect loop**: the 2.0.32 fix forced a `DisconnectProfile`/`ConnectProfile` cycle on every `Error.Failed` response. This created an infinite loop because BlueZ considers the profile connected and returns `Error.Failed` again on reconnect. Now treats `Error.Failed` as success (profile already connected) and defers sink detection to the polling loop.
- **Added exponential backoff for InProgress errors**: A2DP `ConnectProfile` retries now use 1s/3s/5s backoff instead of fixed 2s delays, preventing BlueZ from being overwhelmed during profile negotiation.
- **Added Flask catch-all error handler**: unhandled exceptions in API endpoints now return structured JSON `{"status": "error", "message": "..."}` instead of raw HTML 500 pages. The `test-connect` endpoint specifically catches and reports errors.

## 2.0.33 (2026-09-02)

- **Fixed shairport-sync using ALSA backend instead of PulseAudio — root cause of audio crash**: the generated `shairport-sync.conf` included BOTH an `alsa = {}` block and a `pa = {}` block when a Bluetooth sink was available. shairport-sync prioritizes ALSA when both backends are configured, so it tried to open the ALSA `default` device — which failed with "Unable to set hw parameters: I/O error" and crashed. Now only the `pa` (PulseAudio) backend is written when a Bluetooth A2DP sink exists; the `alsa` block is used exclusively as a fallback when no sink is available.

## 2.0.32 (2026-09-02)

- **Fixed A2DP sink never appearing — conflicting sound server removed**: the Docker image installed both `pulseaudio-bluez` (PulseAudio's BlueZ modules) and `pipewire-spa-bluez` (PipeWire's BlueZ SPA plugin). Both competed for BlueZ A2DP transport ownership, causing `RegisterProfile() failed: org.bluez.Error.NotPermitted` and "Multiple sound server instances" warnings. PipeWire's BlueZ plugin could not register the A2DP profile, so no `bluez_sink` node was ever created for some devices. Removed `pulseaudio-bluez` from the Dockerfile — only PipeWire now manages Bluetooth audio.
- **Fixed stale A2DP transport — forced disconnect/reconnect on "already connected"**: when `ConnectProfile` returned `Error.Failed` (profile already connected), the bridge treated it as success. But the transport was often held by a stale connection or a competing sound server, so WirePlumber never created the sink node. The bridge now performs an explicit `DisconnectProfile`/`ConnectProfile` cycle when `Error.Failed` is returned, releasing the stale transport and letting WirePlumber acquire it cleanly.
- **Added WirePlumber headless configuration override**: in a Docker container there is no logind/seatd, so `monitor.bluez.seat-monitoring` fails to load and spams warnings. A new `/etc/wireplumber/wireplumber.conf` override explicitly disables `seat-monitoring` and `logind`, and configures WirePlumber to automatically switch connected Bluetooth devices to the `a2dp_sink` profile — so the `bluez_sink` node is created immediately on connect without needing a manual `set-card-profile` call.
- **Added automatic BT reconnect recovery in sink polling**: when the BlueZ card exists but no sink node appears after half the timeout, the bridge now forces a full Bluetooth disconnect/reconnect cycle (`bluetoothctl disconnect` + `connect`) to release any stale transport held by a competing sound server. This recovers devices that would otherwise never get an A2DP sink.

## 2.0.31 (2026-09-02)

- **Fixed silent test sound and AirPlay audio — A2DP transport not acquired**: PipeWire created the A2DP sink node but left it in SUSPENDED state where the Bluetooth audio transport was never actually acquired. Audio written to the sink was silently discarded — `paplay` returned success but no sound reached the speaker. The bridge now performs a suspend/resume cycle (`pactl suspend-sink`) before playing any audio, forcing WirePlumber to (re)acquire the Bluetooth transport.
- **Fixed wrong build flag — `--with-pa` corrected to `--with-pulseaudio`**: the `--with-pa` flag was not recognized by shairport-sync's `./configure` (unrecognized option), so the PulseAudio backend was never compiled in. shairport-sync fell back to the ALSA backend, which routed audio through the default PCM to the `auto_null` dummy sink instead of the Bluetooth A2DP sink. Now correctly uses `--with-pulseaudio`.
- **Fixed shairport config not writing PA sink device**: the `generate_conf` method accepted a `sink_name` parameter but never wrote it into the config file. Even with the PA backend compiled in, audio went to the default sink (which could be `auto_null`). The config now includes a `pa = { device = "<sink_name>"; }` block when a Bluetooth A2DP sink is available, routing audio directly to the correct speaker.
- **Fixed bt_switch.sh unable to recover disappeared A2DP sink**: when the A2DP sink vanished between initial connection and AirPlay stream start (as seen in the logs), `bt_switch.sh` waited 10 seconds and gave up. Now re-triggers the BlueZ card profile switch to `a2dp_sink` after 3 seconds if the sink hasn't appeared, forcing WirePlumber to recreate the sink node.
- **Longer test tone**: increased from 3 to 5 seconds, since the first 1-2 seconds are consumed by the A2DP transport handshake and were previously inaudible.

## 2.0.30 (2026-09-02)\n\n- **Fixed Hammerton decoder crash (SIGSEGV) on iOS ALAC streams**: the Hammerton decoder crashes with a segmentation fault when iOS sends ALAC frames using prediction type 15. Built the Apple ALAC decoder library from source (mikebrady/ALAC) and compiled shairport-sync with `--with-apple-alac` — the Apple decoder handles all prediction types correctly.\n- **Fixed bt_switch.sh sink detection**: removed duplicate `pactl --server` calls that bypassed env vars and suppressed errors. Now uses a single `pactl` call relying on exported `PULSE_SERVER`, logs errors instead of hiding them, and dumps available sinks when the A2DP sink is not found.\n\n## 2.0.29 (2026-09-02)\n\n- **Fixed AirPlay decoder errors — removed --with-ffmpeg**: the FFmpeg ALAC decoder was replacing the default Hammerton decoder entirely (decoders_supported bit field = 4, only FFmpeg), but FFmpeg's ALAC decoder fails on every packet from iOS (`error -1094995529`, `error -1163346256`). Removed `--with-ffmpeg` and `ffmpeg-dev`/`ffmpeg-libs` from the Dockerfile — shairport-sync now uses the Hammerton decoder (bit field = 1), which correctly handles iOS ALAC streams.\n- **Fixed bt_switch.sh not finding A2DP sink**: the script was calling `pactl` without the PipeWire/PulseAudio environment variables. `pactl` couldn't find the pipewire-pulse server and returned empty results, causing the \"A2DP sink not found after 10s\" warning. Now exports `XDG_RUNTIME_DIR`, `PULSE_SERVER`, `PULSE_RUNTIME_PATH`, and `DBUS_SESSION_BUS_ADDRESS` before any `pactl` calls.\n- **Added A2DP transport settle delay**: PipeWire creates the A2DP sink node before the Bluetooth audio transport is fully ready. Playing audio immediately results in silence. Now waits 2 seconds after the sink appears before routing audio, giving the A2DP transport time to initialize.\n\n## 2.0.28 (2026-09-02)

- **Fixed build failure from `--with-apple-alac`**: the Apple ALAC decoder library is not available in Alpine, causing the Docker build to fail. Replaced `--with-apple-alac` with `--with-ffmpeg`, which uses FFmpeg's ALAC decoder (available in Alpine as `ffmpeg-dev`). Added `ffmpeg-dev` to the build stage and `ffmpeg-libs` to the runtime stage.

## 2.0.27 (2026-09-02)

- **Fixed AirPlay stream crash (SIGSEGV)**: the Hammerton ALAC decoder (the default) has known bugs with certain iOS ALAC streams, causing segfaults (`unhandled prediction type`, `Not enough space in the output buffer`). Added `--with-apple-alac` to the shairport-sync build, enabling the Apple ALAC decoder as a fallback. The `decoders_supported` bit field now includes both hammerton (1) and apple (2), so shairport-sync can use the Apple decoder for streams the Hammerton decoder can't handle.
- **Fixed inaudible test tone**: the 1-second test tone was too short for Bluetooth's startup latency — the A2DP link needs ~2 seconds to start producing sound. Increased the tone to 3 seconds at 50% amplitude. Also reordered playback attempts to try `paplay` first (more reliable with PulseAudio sink names than `pw-play`).

## 2.0.26 (2026-09-02)

- **Fixed A2DP sink never appearing on restart**: the root cause was stale Bluetooth sessions. When the container restarts, Bluetooth devices remain connected at the host level. WirePlumber then tries to register the A2DP profile on these already-connected devices, but BlueZ rejects it with `org.bluez.Error.NotPermitted` — so no A2DP sink is ever created and no audio plays. The bridge now disconnects all Bluetooth devices before starting PipeWire/WirePlumber, clearing stale profile registrations. The availability monitor then reconnects the devices cleanly, and WirePlumber registers the A2DP profile on the fresh connection.

## 2.0.25 (2026-09-02)

- **Simplified audio routing — fixed AirPlay audio crash**: removed the per-speaker ALSA PCM device (`bt_<mac>`) and the custom `asound.conf` / `ALSA_CONFIG_PATH` mechanism entirely. shairport-sync now uses the ALSA default device, which routes through the standard `pcm.!default { type pulse }` in `/etc/asound.conf` to pipewire-pulse, which outputs to the default sink. The bridge already sets the default sink to the A2DP Bluetooth sink via `pactl set-default-sink`, so audio flows: shairport-sync → ALSA default → pulse plugin → pipewire-pulse → A2DP sink → Bluetooth speaker. This eliminates the `error -2 / No such file or directory` crash that occurred when shairport-sync tried to open the custom PCM device.
- **Simplified bt_switch.sh — fixed false reconnect failure**: removed the `bluetoothctl`-based connection detection and retry logic that was failing because `bluetoothctl` could not see devices connected via the bridge's D-Bus agent. The script now only waits for the A2DP sink to appear in PipeWire and routes audio to it — no connection attempts, no retries, no false failures.

## 2.0.24 (2026-09-02)

- **Fixed install failure from non-existent packages**: removed `wireplumber-logind`, `elogind`, and `elogind-libs` from the Dockerfile — these packages don't exist in the Alpine version used by the HA base image, causing `apk add` to fail and the entire image build to abort. The logind module is optional for WirePlumber's BlueZ monitor in a container environment; the critical missing package is `pipewire-spa-bluez` (retained from 2.0.23), which provides the BlueZ5 SPA plugin that WirePlumber needs to create Bluetooth audio sinks.

## 2.0.23 (2026-09-02)

- **Fixed missing PipeWire BlueZ SPA plugin — root cause of A2DP sink never appearing**: the Docker image was missing the `pipewire-spa-bluez` Alpine package, which provides the `api.bluez5.enum.dbus` SPA plugin. WirePlumber could not load it and logged "PipeWire's BlueZ SPA plugin is missing or broken. Bluetooth devices will not be supported." — so no A2DP sink node was ever created, regardless of Bluetooth connection state. Added `pipewire-spa-bluez` to the Dockerfile.
- **Fixed missing WirePlumber logind module**: without `wireplumber-logind` (and `elogind`), WirePlumber skipped the BlueZ seat-monitoring component entirely ("skipping component 'monitor.bluez.seat-monitoring' because some of its dependencies were not loaded"), preventing the BlueZ monitor from starting. Added `wireplumber-logind` package and start `elogind --daemon` in the entrypoint.

## 2.0.22 (2026-09-02)

- **Fixed ALSA include syntax — root cause of no AirPlay audio**: the `<filename>` directive in the generated `asound.conf` is invalid ALSA syntax — the correct directive is `<file>`. With the wrong syntax, the standard ALSA config was never loaded, the `pulse` plugin type was never registered, and shairport-sync could not open the ALSA output device (`error -2 / No such file or directory`), causing an immediate fatal crash when AirPlay audio started.
- **Fixed bt_switch.sh false reconnect failure**: `get_connected_mac` used `bluetoothctl info` without a MAC argument, which only checks the first cached device. Since the bridge connects via D-Bus directly (not bluetoothctl), the target device was not in bluetoothctl's cache — it reported "none" and tried to reconnect, which failed 3x. Now checks the target MAC explicitly first.

## 2.0.21 (2026-09-02)

- **Fixed read-only filesystem crash — AirPlay now starts**: `/etc/` is read-only in the HA add-on container, so writing the per-speaker ALSA config to `/etc/asound.conf` crashed with `OSError: Read-only file system`. The config is now written to the writable runtime directory and loaded via `ALSA_CONFIG_PATH`. The file uses a `<filename>` include directive to pull in the standard ALSA config (`/usr/share/alsa/alsa.conf`), which registers the `pulse` plugin type before our per-speaker PCMs are parsed — combining both fixes from 2.0.19 and 2.0.20 without needing write access to `/etc/`.

## 2.0.20 (2026-09-02)

- **Fixed WirePlumber pipe deadlock — root cause of A2DP sink never appearing**: PipeWire, WirePlumber, and pipewire-pulse daemons had their stderr redirected to `subprocess.PIPE`, which nobody read. When the 64KB kernel pipe buffer filled up, the daemons blocked on write and froze — WirePlumber could not process Bluetooth device events, so no A2DP sink node was ever created. Now redirects daemon output to a log file (`pipewire-daemons.log`) which never blocks.
- **Added diagnostic dump on sink failure**: when the A2DP sink does not appear within the timeout, the bridge now dumps the full PipeWire state (sinks, cards, default sink, nodes) and the tail of the daemon log to the add-on log, so we can see exactly why WirePlumber failed to create the sink.

## 2.0.19 (2026-09-02)

- **Fixed ALSA config path — root cause of no audio**: the per-speaker ALSA config was written to a custom path and loaded via `ALSA_CONFIG_PATH`, which *replaces* the entire ALSA config tree. The standard ALSA config at `/usr/share/alsa/alsa.conf` registers the `pulse` plugin type — without it, `type pulse` in our asound.conf is unknown and ALSA silently drops all audio. Now writes per-speaker PCMs to `/etc/asound.conf` instead, which ALSA loads *in addition to* the standard config. Removed the `ALSA_CONFIG_PATH` environment variable entirely.

## 2.0.18 (2026-09-02)

- **Unmute and set volume on A2DP sink**: PipeWire sometimes creates Bluetooth A2DP sinks muted or at 0 volume. Now explicitly unmutes and sets volume to 100% when setting the default sink, when playing test tones, and when routing audio in the hook script.
- **Skip reconnect when already connected**: the audio hook script was trying to reconnect an already-connected device every time AirPlay audio started, failing 3x and exiting without routing audio. Now detects the already-connected state and skips straight to audio routing.

## 2.0.17 (2026-09-02)

- **Restored ALSA audio backend**: the compiled shairport-sync binary only includes the ALSA backend (not PA), so the `pa = {}` config block from 2.0.16 was silently ignored — shairport-sync fell back to ALSA with no output device. Restored the `alsa = {}` config block and the per-speaker `asound.conf` routing via the ALSA pulse plugin to PipeWire. The verbose diagnostics and health check from 2.0.16 are retained.

## 2.0.16 (2026-09-02)

- **Switched audio backend from ALSA to PulseAudio**: shairport-sync now uses the `pa` backend directly instead of the fragile ALSA + pulse-plugin indirection. This eliminates the per-speaker `asound.conf` files entirely — shairport-sync connects straight to pipewire-pulse via `PULSE_SERVER` and outputs to the named A2DP sink.
- **Verbose diagnostics**: `log_verbosity` increased from 0 to 3 and `statistics` enabled so shairport-sync startup output is visible in the add-on log. Previously the process started silently with no way to diagnose failures.
- **Post-start health check**: after starting shairport-sync, a background thread waits 2 seconds and verifies the process is still alive. If it crashed immediately, the exit code is logged — making it obvious whether the issue is audio backend, config, or mDNS.
- **Command-line verbose flag**: shairport-sync is now started with `-v` (verbose) in addition to the config file diagnostics settings.

## 2.0.15 (2026-09-02)

- **AirPlay discovery fix (embedded tinysvcmdns — final solution)**: the Alpine community `shairport-sync` package links against Avahi, which requires a D-Bus system bus for mDNS. With `host_network: true`, the host already runs avahi-daemon on port 5353, and the D-Bus security policy refuses to let the container register Avahi services. Every workaround (private session bus, private system bus, shared host Avahi, daemon-mode tweaks) failed because the Avahi client library hardcodes the system bus path. This version compiles shairport-sync from source with `--with-tinysvcmdns`, embedding the mDNS responder directly into the binary. It broadcasts UDP multicast on port 5353 itself — no Avahi daemon, no D-Bus dependency, no policy conflicts. Multiple mDNS responders coexist on the same port via SO_REUSEADDR.
- **Multi-stage Docker build**: the Dockerfile now uses a two-stage build. Stage 1 compiles shairport-sync from the official GitHub source with `--with-alsa --with-pa --with-soxr --with-ssl=openssl --with-tinysvcmdns --with-metadata --with-dbus-interface`. Stage 2 copies only the binary into the runtime image.
- **Removed Avahi**: removed `avahi`, `avahi-tools`, and `avahi-compat-libdns_sd` packages from the Dockerfile, removed `rootfs/etc/avahi/avahi-daemon.conf`, and removed all Avahi startup code from `entrypoint.sh`.
- **CI validation**: the CI pipeline now checks that `shairport-sync -V` output contains `tinysvcmdns` to prevent shipping a broken image.

## 2.0.14 (2026-09-02)

- **AirPlay discovery fix (private system bus for Avahi)**: the Avahi client library inside shairport-sync always connects to the D-Bus **system bus** — it ignores `DBUS_SESSION_BUS_ADDRESS`. The host's system bus refuses to let the container register the Avahi service (D-Bus policy denies `org.freedesktop.Avahi`). Previous versions tried session buses, shared host Avahi, and daemon-mode tweaks; none worked because the fundamental issue was the bus type. This version starts a **private system bus** inside the container with a permissive policy, runs avahi-daemon on it, and exports `AVAHI_SYSTEM_BUS_ADDRESS` so shairport-sync connects to our private Avahi. Bluetooth continues using the host's real system bus for BlueZ.

## 2.0.13 (2026-09-02)

- **AirPlay discovery fix (shared system Avahi)**: the AirPlay client library uses the D-Bus system bus to reach Avahi. The add-on now checks for the existing Home Assistant/host Avahi service and uses it instead of starting a competing instance. A local Avahi daemon is started only when the shared system bus has no Avahi service.
- **System-bus diagnostics**: startup now confirms that Avahi is actually available before shairport-sync starts.

## 2.0.12 (2026-09-02)

- **AirPlay discovery fix (avahi-daemon daemonization)**: avahi-daemon was
  started with `-D` (daemonize), which forks a child process that loses the
  `DBUS_SESSION_BUS_ADDRESS` environment variable. The child could not
  connect to the private D-Bus session bus and failed silently, so no
  `_airplay._tcp` mDNS service was ever published — iOS/macOS could not
  discover the AirPlay receiver. Switched to foreground mode (background
  `&`) so the process inherits the correct D-Bus address.
- **Avahi stack isolation**: changed `disallow-other-stacks` from `no` to
  `yes` to prevent conflicts with the host's avahi-daemon when
  `host_network: true` is active. Without this, two avahi-daemon instances
  compete for the same mDNS multicast traffic on port 5353.

## 2.0.11 (2026-09-02)

- **D-Bus/MPRIS warnings fixed**: shairport-sync was trying to acquire its
  D-Bus (`org.gnome.ShairportSync`) and MPRIS (`org.mpris.MediaPlayer2.ShairportSync`)
  interfaces on the **system bus**, but the add-on uses a private **session bus**.
  Added `dbus_service_bus = "session"` and `mpris_service_bus = "session"` to
  the generated shairport-sync config so both interfaces register on the
  correct bus. This eliminates the two startup warnings and enables
  remote-control (AVRCP) commands from the Echo Show to reach shairport-sync.
- **Realtime scheduling**: added `SYS_NICE` to the privileged capabilities in
  `config.yaml` so shairport-sync can set realtime thread priority. Without
  it, the "Can not set realtime properties of a thread" warning appears and
  audio may glitch under CPU load.

## 2.0.10 (2026-09-02)

- **AirPlay discovery fix (D-Bus)**: the previous approach (`enable-dbus=no`)
  was incorrect — Alpine's `shairport-sync` uses `avahi-compat-libdns_sd`,
  which communicates with `avahi-daemon` over D-Bus. Without D-Bus, Avahi
  starts but shairport-sync cannot register its `_airplay._tcp` service,
  so iOS/macOS never see the receiver. Switched to `enable-dbus=yes` and
  start a private D-Bus session bus inside the container (`/run/avahi-dbus/bus`)
  before launching `avahi-daemon`. Both `avahi-daemon` and `shairport-sync`
  share this bus, avoiding conflicts with the host's Avahi on the system bus.
- **D-Bus bus propagation**: `shairport.py` and `run.py` now respect an
  existing `DBUS_SESSION_BUS_ADDRESS` from the entrypoint instead of
  unconditionally creating their own session bus. This ensures shairport-sync
  talks to the same Avahi instance.
- **Safe fallback**: if `dbus-daemon` fails to start, the entrypoint does
  not export the bus address, so `run.py` falls back to creating its own
  session bus — the container always starts.

## 2.0.9 (2026-09-02)

- **AirPlay discovery fix**: restored Avahi mDNS/Bonjour daemon to the
  Docker image. The Alpine `shairport-sync` package links against Avahi
  (not embedded tinysvcmdns), so without `avahi-daemon` running,
  shairport-sync starts but iOS/macOS cannot discover the AirPlay
  receivers. Added `avahi`, `avahi-tools`, and `avahi-compat-libdns_sd`
  packages, a container-safe `avahi-daemon.conf` (D-Bus disabled,
  `disallow-other-stacks=yes` to prevent host conflicts), and Avahi
  startup in `entrypoint.sh` (non-fatal on failure).
- **BT disconnect debounce**: `stop_for_disconnected` now waits 5
  seconds and re-checks BT state before stopping AirPlay. Transient BT
  link drops (common during pairing, profile switches, and Echo
  reconnections) no longer kill AirPlay instantly.
- **Dual-monitor race fix**: the Shairport lifecycle monitor no longer
  starts/stops instances based on BT state — that is now exclusively
  driven by the `AvailabilityMonitor`. The lifecycle monitor only
  detects crashed processes and restarts them if BT is still connected.
  This eliminates stop/start short-circuits between the two monitors.
- **Longer A2DP sink wait**: sink poll timeout increased from 30s to
  45s, with an additional profile-switch retry + 15s poll if the first
  round fails. Gives slower Echo devices more time for A2DP negotiation.
- **Avahi isolation**: Avahi runs with `enable-dbus=no` (standalone,
  no D-Bus dependency), `disallow-other-stacks=yes` (no host conflict
  with `host_network: true`), and the `avahi` user is explicitly
  created in the Dockerfile. Avahi startup failure is non-fatal — the
  bridge continues without AirPlay discovery rather than crashing.
- Added version number display in the Web UI (bottom-left corner).

## 2.0.2 (2026-09-02)

- Fix Docker build failure: replaced the multi-stage source compilation
  of shairport-sync with the pre-compiled Alpine community package.
  The source build was failing in the Home Assistant Supervisor due to
  missing build dependencies. The Alpine `shairport-sync` package
  includes PulseAudio, ALSA, and mDNS support out of the box.
- Simplified the Dockerfile to a single-stage build using the
  `ghcr.io/home-assistant/*-base:latest` base image with all packages
  installed via `apk add`.
- Updated the entrypoint pre-flight checks to validate that
  shairport-sync is installed and runnable (rather than checking for
  specific compile-time features like `tinysvcmdns`).
- Updated the CI pipeline to validate shairport-sync installation
  rather than compile-time feature flags.

## 2.0.1 (2026-09-02)

- Fix Docker compilation dependencies for PipeWire/PulseAudio and ALSA
  backends. Added `dbus-dev` to the build-stage `apk add` list so
  `--with-dbus-interface` compiles successfully.
- Eliminate Avahi dependency by compiling Shairport Sync with embedded
  `tinysvcmdns` (app-aircast model). No external mDNS daemon required.
- Add automated pre-flight self-checks and GitHub Actions CI build
  validation pipeline. The Dockerfile build stage already validates
  that shairport-sync was compiled with `tinysvcmdns` and at least one
  audio backend; the CI pipeline runs `docker build` on every push and
  pull request to catch regressions before they reach users.
- Resolve D-Bus device path loss (`device path missing`) during
  re-pairing via a dedicated `bt_action_lock` thread lock. All
  Bluetooth actions that resolve, create, or remove device paths now
  serialize through this lock, preventing the lifecycle monitor from
  racing with user-initiated pair/connect/disconnect calls.

## 2.0.0 (2026-09-02)

Major release: dynamic PipeWire sink handling, metadata passthrough, Alexa
voice control backchannel, and a complete dashboard overhaul.

### Dynamic PipeWire A2DP Sink Handling & Auto-Reload

- **Faster sink polling**: `wait_for_bluetooth_sink` now polls every 1 second
  with a 15-second timeout (down from 2s/45s), matching the ~7s window that
  PipeWire needs to expose `bluez_sink.XX_XX...a2dp_sink`.
- **Auto-reload on sink detection**: when the A2DP sink appears, the bridge
  automatically calls `pactl set-default-sink`, regenerates the
  `shairport-sync.conf` with the dedicated BT sink, and restarts the Shairport
  instance — no more falling back to `sink=default` and getting stuck.
- **New `set_default_sink` method**: explicit wrapper for
  `pactl set-default-sink`, called from both the sink-poll callback and
  `route_to_sink`.

### Deprecation & D-Bus Cleanup

- Config template uses `diagnostics = { log_verbosity = 0; };` (not the
  deprecated `log_verbosity` in `general`).
- MPRIS / native D-Bus bindings are safely guarded so missing D-Bus interfaces
  no longer produce startup warnings.

### Unified Overview Dashboard & Topology Graph

- Four-node topology flow: AirPlay Client → Shairport Receiver → PipeWire
  Audio Engine → Echo Show / BT Speaker.
- Dynamic status badges with explicit color coding: `IDLE`, `CONNECTING`,
  `CONNECTED`, `BRIDGED (Streaming)`, `BUFFERING`, `DISCONNECTED`.
- New `CONNECTING` badge style (sky blue) added to the CSS.

### AirPlay Overview Panel & Now Playing Display

- Dedicated AirPlay panel showing active receiver port, mapped BT output
  sink, and stream state.
- Live Now Playing card displaying song title, artist & album, cover art
  thumbnail (when available), and playback state.
- New `/api/now-playing` endpoint serves cached metadata for all active
  streams; the UI polls it every 3 seconds.

### Metadata Passthrough to Echo Show

- Shairport-sync config now includes `metadata = { enabled = "yes";
  include_cover_art = "yes"; };`.
- A background thread reads the shairport-sync metadata pipe, parses track
  info (title, artist, album, cover art), and caches it for the Now Playing
  display.
- Track metadata is forwarded to BlueZ via the MediaItem1 interface so the
  Echo Show screen displays the current playing song.

### Bi-Directional Alexa Control Backchannel

- The bridge listens for incoming BlueZ AVRCP control signals from the Echo
  Show (e.g. "Alexa, next song", "Alexa, pause").
- `Next`, `Previous`, `Play`, `Pause`, and `Stop` commands are forwarded
  via D-Bus to shairport-sync to control the connected Apple device.

### Versioning

- Version bumped to 2.0.0 across `config.yaml`, `package.json`,
  `package-lock.json`, and the Dockerfile label.

## 1.8.0 (2026-09-02)

Architectural overhaul based on the `hassio-addons/app-aircast` model:

- **Build-time validation**: The Dockerfile now verifies that shairport-sync
  was compiled with both `tinysvcmdns` (embedded mDNS) and at least one audio
  backend (`pa` or `alsa`) immediately after `make install`. If either check
  fails, the Docker build fails — preventing broken images from ever shipping.
- **Runtime pre-flight checks**: The entrypoint now runs the same validation
  before starting the Python daemon. If shairport-sync lacks an audio backend
  or embedded mDNS, the container exits immediately with a clear error
  message instead of entering an infinite crash loop.
- **Crash loop elimination**: shairport-sync instances that crash more than
  5 times now have their error logs suppressed (instead of spamming every 10
  seconds indefinitely). Crash counters are reset on Bluetooth disconnect,
  so a reconnection gets a fresh start.
- **Race condition fix in `start_for_connected`**: The `_starting` flag is now
  held for the entire duration of the background sink-poll thread, preventing
  concurrent `start_for_connected` calls from spawning duplicate poll threads.
  If the poll thread exits early (no PipeWire), the flag is properly cleaned
  up in a `finally` block.
- **Race condition fix in `stop_for_disconnected`**: Now resets crash counters
  and the `_starting` flag before stopping, ensuring a clean slate for the
  next connection cycle.

## 1.7.3 (2026-09-02)

- Fixed "No audio backend found" crash: shairport-sync was compiled with
  `--with-pa` only, but the PulseAudio backend was silently not detected
  during `./configure` in the HA base image. Added `--with-alsa` so
  shairport-sync always has at least one working audio backend.
- Switched the per-speaker shairport-sync config from the `pa` section to
  the `alsa` section. Each speaker now gets a dedicated ALSA PCM
  (`bt_<mac>`) that routes through the `alsa-plugins-pulse` plugin to
  PipeWire's pipewire-pulse server, then to the Bluetooth A2DP sink.
- Added `/etc/asound.conf` with a default `pulse` PCM so ALSA routes all
  audio through PipeWire by default. Per-speaker PCM definitions are
  written dynamically at runtime.

## 1.7.2 (2026-09-02)

- Fixed runtime crash: added `libconfig`, `popt`, `libdaemon`, and `soxr`
  to the runtime stage's `apk add` list. The build stage installed the
  `-dev` packages for compilation, but the runtime stage was missing the
  shared libraries (`libconfig.so.15`, `libpopt.so.0`) that shairport-sync
  dynamically links against, causing an immediate crash with
  `Error loading shared library` on every start.

## 1.7.1 (2026-09-02)

- Fixed Docker build failure: removed `openssl-libs` and `pipewire-dev`
  from the build stage (these packages do not exist in the Alpine
  repository used by the HA base image). Added the missing
  `libconfig-dev` package, which shairport-sync requires to compile.
- Version bump to avoid collision with the previously released 1.7.0.

## 1.7.0 (2026-09-02)

### Architectural transition: embedded mDNS (Option A — tinysvcmdns)

Removed Avahi and its D-Bus dependency entirely. shairport-sync is now
built from source in a multi-stage Docker build with
`--with-tinysvcmdns --with-ssl=openssl --with-pa --with-soxr`. The
embedded tinysvcmdns library advertises AirPlay services directly via
UDP multicast on port 5353 — no external mDNS daemon, no D-Bus policy
negotiation, no `avahi-daemon` process.

This fixes the root cause of AirPlay discovery failure: the host D-Bus
system bus refused to let the add-on own `org.freedesktop.Avahi`
(`dbus_bus_request_name(): Request to own name refused by policy`).
Previous versions (1.6.x) tried to work around this with private D-Bus
daemons and config tweaks; all of that is now eliminated.

Removed:
- `avahi` and `avahi-tools` Alpine packages from the Dockerfile
- `rootfs/etc/avahi/avahi-daemon.conf` configuration file
- All Avahi startup code in `run.py` (`_start_avahi`,
  `_run_avahi_foreground`)
- The `mkdir /run/avahi-daemon` command from the Dockerfile

The add-on already has `host_network: true` in `config.yaml`, so UDP
multicast on port 5353 reaches the LAN/WLAN interface directly.

### Bluetooth A2DP audio routing: eliminated auto_null fallback

Previous versions started shairport-sync immediately after Bluetooth
connect, using the PA default sink (`auto_null`) as a placeholder, then
polled for the A2DP sink and restarted. This caused audio to route to
a dummy sink when the A2DP sink was slow to appear.

New behavior:
1. After `Connect()` succeeds, the code waits 1.5s for the base
   connection to stabilize, then calls `ConnectProfile(A2DP_UUID)`
   with up to 3 retries and 2s delay between attempts.
2. `set_card_profile` is called to force the BlueZ card to `a2dp_sink`
   so WirePlumber creates the `bluez_sink.*.a2dp_sink` node. It retries
   up to 5 times with 1s delay, since the card may not appear in
   `pactl list cards` immediately after connect.
3. `start_for_connected` does NOT start shairport-sync with the PA
   default sink. If no A2DP sink is found, it defers start and polls
   in a background thread. shairport-sync only starts once the real
   `bluez_sink.*` sink is confirmed in PipeWire.
4. `start_instance` also refuses to use `auto_null` when Bluetooth is
   connected — it returns False and lets the polling thread handle
   the start.
5. The sink polling timeout increased from 30s to 45s to accommodate
   slower Echo devices that need more time for A2DP profile negotiation.

### Home Assistant configuration verification

Confirmed `config.yaml` has all required settings:
- `host_network: true` — mDNS multicast and AirPlay audio ports reach LAN
- `audio: true` — host audio device/PipeWire socket access
- `host_dbus: true` — BlueZ D-Bus system bus access for Bluetooth
- `full_access: true` — container can access host devices
- `privileged: [SYS_ADMIN, NET_ADMIN]` — required for Bluetooth and
  network multicast
- `ingress: true` with `ingress_port: 8099` — Flask Web UI
- AirPlay dynamic ports start at 5002+ (configurable via `airplay_port_base`)

## 1.6.7 (2026-09-02)

- Fixed Docker build failure: removed the `avahi-users` package from the
  Dockerfile — it does not exist in the Alpine package repository and
  caused `apk add` to fail with an unknown error during the HA Supervisor
  image build. The base `avahi` package already provides the avahi user/group.

## 1.6.6 (2026-09-02)

- Fixed Avahi startup: removed invalid --no-drop-root flag (not recognized
  by avahi-daemon) and increased startup wait from 1s to 1.5s. avahi-daemon
  stderr is now captured and logged so startup failures are visible instead
  of silent.
- Fixed critical sink matching bug: find_bluetooth_sink() had a generic
  fallback that matched ANY bluez sink with a2dp_sink suffix, regardless
  of which device it belonged to. This caused device 2C:71:FF:EE:8F:44
  to incorrectly use the sink for 08:12:A5:72:A6:A9. The fallback is
  removed — sinks are now matched strictly by MAC address.
- Fixed log spam: "Found sink" messages from find_bluetooth_sink() are
  now logged at DEBUG level instead of INFO, so the system log is no
  longer flooded with repeated sink discovery messages every 2 seconds.

## 1.6.5 (2026-09-02)

- Fixed Avahi startup: avahi-daemon now runs in foreground mode in a
  background thread instead of daemonizing with -D, which silently failed
  in the container because no avahi user exists. The --no-drop-root flag
  is used so the daemon can run as root in the container.
- Fixed A2DP sink never appearing: after Bluetooth connects, the BlueZ
  card profile is now explicitly set to a2dp_sink via pactl
  set-card-profile. Without this, WirePlumber may leave the card on an
  inactive or HFP profile and never create the bluez_sink node, so audio
  goes to the PA default instead of the Bluetooth speaker.

## 1.6.4 (2026-09-02)

- Fixed Bluetooth pairing broken by Avahi: moved avahi-daemon startup
  from the entrypoint script to after Bluetooth and PipeWire init in
  run.py. Starting Avahi before the BlueZ pairing agent was registered
  disrupted the D-Bus system bus and caused all pairing attempts to fail
  with ConnectionAttemptFailed.
- Added avahi-daemon.conf configured for the container environment
  (IPv4-only, no chroot, no IFF running check).
- Increased wait after device removal before re-pairing from 1s to 3s
  to give BlueZ more time to clean up the D-Bus object.
- Added pairing diagnostics: device paired/connected/RSSI state is now
  logged before each Pair() call so failures can be diagnosed.

## 1.6.3 (2026-09-02)

- Fixed AirPlay discovery: added the Avahi mDNS/Bonjour daemon to the
  container and start it before the bridge. Shairport-Sync can run and
  route audio correctly without Avahi, but iOS and macOS cannot see the
  receiver because there is no `_airplay._tcp` service advertisement.
- Fixed duplicate Shairport startup during pairing: the pairing request
  and lifecycle monitor could start the same receiver simultaneously,
  creating two sink-poll threads and competing restarts. Starts are now
  guarded per Bluetooth device.
- Kept the existing 30-second A2DP sink wait and dedicated PipeWire sink
  routing, so slow Echo profile negotiation no longer prevents startup.

## 1.6.2 (2026-09-02)

- Fixed fatal Shairport-Sync crash: removed `dbus_service_bus = "none"`
  and `mpris_service_bus = "none"` from the config template. These values
  are invalid — shairport-sync only accepts `"system"` or `"session"` —
  and caused an immediate fatal error on every start, which in turn
  triggered the crash-loop backoff and blocked all restarts even after
  the A2DP sink was found. The D-Bus/MPRIS warnings are harmless and
  no longer appear when the config is valid.
- Fixed crash backoff blocking recovery: the `_poll_for_sink_and_reload`
  callback now resets the crash counter and backoff timer before
  restarting Shairport with the dedicated A2DP sink, so a config fix
  is never blocked by stale crash state from the initial failed start.
- Reduced log noise: `find_bluetooth_sink()` polling messages
  ("Searching for BT sink…" / "No A2DP sink found") are now logged at
  DEBUG level instead of INFO, so the system log stays readable during
  the 30-second polling window. Only the final "sink appeared" or
  "did not appear" results are logged at INFO/WARNING.
- Increased A2DP sink poll timeout from 15s to 30s with 2s interval to
  handle slow A2DP profile negotiation on some Echo devices.
- Improved crash diagnostics: the config file path is now included in
  crash log messages so the exact configuration that failed can be
  inspected.

## 1.6.1 (2026-09-02)

- Fixed AirPlay not working after pairing: the pairing endpoint now uses
  `start_for_connected()` instead of `start_instance()`, so the async
  sink-polling thread is launched. When the A2DP sink appears ~5 seconds
  after Bluetooth connects, Shairport is automatically stopped,
  reconfigured with the dedicated BT sink, and restarted — audio now
  routes to the Bluetooth speaker instead of the PA default.
- Fixed Shairport-Sync D-Bus and MPRIS warnings: replaced the invalid
  `mpris`/`dbus` top-level blocks with `dbus_service_bus = "none"` and
  `mpris_service_bus = "none"` under the `general` section, which is the
  correct shairport-sync configuration syntax.
- Fixed topology dashboard layout: the 4-node flow chart now uses a
  flex row on desktop and stacks vertically on mobile, with arrow
  indicators properly centered between nodes.

## 1.6.0 (2026-09-02)

- Fixed missing test sound file: the Dockerfile now installs
  `alsa-utils-sounds`. If the stock WAV file is still missing at runtime,
  `pipewire.py` generates a 1-second 440 Hz sine-wave WAV in-memory as a
  fallback so the "Test Connect" button always has audio to play.
- Fixed PipeWire A2DP sink detection: `find_bluetooth_sink()` now matches
  any sink line containing the MAC in underscore or dot format, or any
  line containing both `bluez` and `a2dp_sink`. The detected sink name
  (e.g. `bluez_sink.08_12_A5_72_A6_A9.a2dp_sink`) is written into the
  shairport-sync config as the PA `device` so audio routes directly to
  the Echo Show.
- Fixed Shairport-Sync restart loop: the lifecycle monitor now detects
  crashed processes (non-zero exit code) and increments a crash counter
  with exponential backoff (30s → 60s → 120s → 300s) before allowing a
  restart. Previously the monitor spawned a new process every 10 seconds
  without checking if the previous one crashed.
- Enhanced diagnostic logging: a background reader thread now logs
  shairport-sync stdout/stderr in real-time at INFO level so startup
  parameters, audio library init, and errors appear in the System Log
  immediately — not only when the process exits.
- The "Copy System Log" button (added in v1.3.9) is confirmed working in
  the Diagnostics & Debug tab.

## 1.3.9 (2026-09-02)

- Fixed Shairport-Sync crash loop: the lifecycle monitor no longer spawns
  a new `shairport-sync` process every 10 seconds when the previous
  instance exited with an error. A crash counter with exponential
  backoff (30s → 60s → 120s → 300s) prevents endless restarts. Process
  stdout/stderr is captured and logged on crash for diagnostics.
- Shairport-Sync is now launched with explicit `PULSE_SERVER`,
  `XDG_RUNTIME_DIR`, `PIPEWIRE_RUNTIME_DIR`, `PULSE_RUNTIME_PATH`, and
  `DBUS_SESSION_BUS_ADDRESS` environment variables so it connects to
  the correct PipeWire/PulseAudio socket inside the container.
- Fixed Bluetooth re-connect `org.bluez.Error.Failed`: if `Connect()` is
  called on a device whose D-Bus `Connected` property is already `True`
  (including A2DP), the D-Bus `Connect()` call is skipped entirely.
- Improved test sound playback: the `/api/speakers/<mac>/test-connect`
  endpoint now tries `pw-play`, `paplay` (with `PULSE_SERVER`), and
  `aplay` (with PulseAudio device) as fallbacks so the test tone routes
  correctly to the active PipeWire Bluetooth sink.
- Added "Copy System Log" button in the Diagnostics & Debug tab: copies
  the entire system log to the clipboard with a temporary confirmation
  toast. Falls back to `execCommand("copy")` for older browsers or
  non-secure contexts (HA Ingress iframes).

## 1.3.4 (2026-09-02)

- Fixed container startup crash caused by invalid `dbus-next` agent method
  signatures: replaced Python type-annotation string literals (`"o"`, `"s"`,
  `"u"`) with explicit `in_signature` / `out_signature` keyword arguments on
  every `@method` decorator so BlueZ accepts the exported agent interface.
- Added crash-proof startup wrapping in `run.py`: config loading, engine
  initialisation, and the main run loop are each guarded by try/except blocks
  that print the full traceback to stdout/stderr before exiting.
- D-Bus system bus socket check now waits up to 10 seconds for
  `/var/run/dbus/system_bus_socket` to appear instead of failing immediately,
  then falls back to a session bus, and finally logs a warning if neither is
  available — the add-on starts in degraded mode rather than crashing.
- New `entrypoint.sh` script: checks for the D-Bus socket, logs clear warnings,
  and execs into `run.py`. The Dockerfile CMD now uses it.
- New `requirements.txt` listing `dbus-next`, `Flask`, and `gunicorn`; the
  Dockerfile installs from it before the rootfs overlay.
- `repository.yaml` URL updated to the actual GitHub repository.

## 1.3.3 (2026-09-01)

- Fixed BlueZ agent registration by exporting a real `dbus-next` `ServiceInterface` with typed `org.bluez.Agent1` methods.
- Fixed `UnknownObject` errors after unpairing by checking managed D-Bus objects, rediscovering removed device paths before pairing, and avoiding stale paths.
- Device removal now safely handles already-removed objects and clears the connected-device cache.
- Shairport speaker metadata is retained after a disconnect so the lifecycle monitor can restart the daemon after Bluetooth reconnects.
- Added `airplay_active` to `/api/speakers`; the UI now shows AirPlay On when the actual Shairport process is running.

## 1.3.2 (2026-09-01)

- Fixed Echo re-pairing conflict: before calling `Device1.Pair()`, the
  manager now checks if the device is already paired in BlueZ D-Bus. If it
  is, or if `Pair()` returns `org.bluez.Error.AlreadyExists`, the device is
  removed via `Adapter1.RemoveDevice()`, waited 1 second, then paired fresh.
- New `POST /api/bluetooth/unpair` endpoint: removes a device completely
  from BlueZ D-Bus (`RemoveDevice`) and from `storage.json`, so it can be
  cleanly paired again from the UI.
- New "Unpair" button in the Saved Speakers tab (amber) that calls the
  unpair endpoint and refreshes the list.
- Shairport-Sync lifecycle is now automatically synced with Bluetooth
  connection state: a background monitor thread (10 s interval) checks
  each managed speaker. When BT connects and a PipeWire A2DP sink exists,
  Shairport is started. When BT disconnects, Shairport is stopped. When BT
  reconnects, Shairport is restarted.
- The `AvailabilityMonitor` now drives Shairport lifecycle transitions
  on connect/disconnect state changes (in addition to the background
  monitor).
- `/api/speakers` now reports `airplay_on` (true only if Shairport process
  is running AND either BT is connected or a PipeWire A2DP sink exists) and
  `has_sink` (whether a PipeWire sink was found for the MAC). The UI badge
  now shows "AirPlay On" (green) or "AirPlay Off" (red) based on this.
- Added `is_device_connected()` method to `BluetoothManager` for checking
  individual device connection state via D-Bus.
- Added `pid_is_running()` alias to `ShairportManager` for explicit process
  status checks.

## 1.3.1 (2026-09-01)

- Fixed `TypeError: int() argument must be a string, a bytes-like object
  or a real number, not 'Variant'` during Bluetooth scanning. The
  `GetManagedObjects` D-Bus call returns property values wrapped in
  `dbus-next` `Variant` objects. Added a `_unwrap()` helper that
  extracts the inner `.value` and applied it to all property reads
  (`RSSI`, `Alias`, `Name`, `Address`, `Paired`, `Trusted`,
  `Connected`) in `_poll_managed_objects`.

## 1.3.0 (2026-09-01)

- Replaced the ``bluetoothctl``-based pairing agent with a native
  Python BlueZ D-Bus Agent (``org.bluez.Agent1``) using ``dbus-next``.
  The agent is registered at startup via
  ``AgentManager1.RegisterAgent`` and ``RequestDefaultAgent`` — no
  ``bluetoothctl`` subprocess calls for agent management.
- All required ``Agent1`` callbacks (``Release``, ``RequestPinCode``,
  ``DisplayPinCode``, ``RequestPasskey``, ``DisplayPasskey``,
  ``RequestConfirmation``, ``RequestAuthorization``,
  ``AuthorizeService``, ``Cancel``) auto-accept without throwing
  D-Bus errors.
- ``Pair()`` and ``Connect()`` now call ``StopDiscovery()`` first,
  because active scanning interferes with BlueZ link-key
  establishment (root cause of
  ``org.bluez.Error.ConnectionAttemptFailed``).
- ``Trusted`` is set to ``True`` on the Device1 object via D-Bus
  before connecting.
- A2DP sink profile (``0000110b-...``) is connected explicitly
  after successful pairing via ``Device1.ConnectProfile``.
- Active scanning now queries BlueZ D-Bus ``GetManagedObjects`` for
  ``Alias``, ``Name``, ``RSSI``, ``Paired``, ``Trusted``, and
  ``Connected`` — returning human-readable names to the frontend.
- Adapter properties (``Discoverable``, ``Pairable``, ``Powered``)
  are set via D-Bus properties, with ``bluetoothctl`` fallback.
- Added ``dbus-next`` Python package to the Docker image.

## 1.2.0 (2026-09-01)

- Added temporary "Discoverable & Pairable Mode" (3 minutes): the
  adapter is set to `Discoverable` + `Pairable` and a BlueZ D-Bus
  Agent (`NoInputNoOutput`) auto-accepts incoming pairing requests.
  The timer reverts both properties and unregisters the agent on
  expiry.
- New API endpoints: `POST /api/bluetooth/discoverable` (start),
  `DELETE /api/bluetooth/discoverable` (stop), and
  `GET /api/bluetooth/discoverable` (status with remaining seconds).
- Active device-name resolution: after scanning, each discovered
  device is queried via `bluetoothctl info` for `Alias`, `Name`,
  `RSSI`, `Paired`, `Trusted`, and `Connected` so the frontend shows
  human-readable names instead of raw MAC addresses.
- New "Enable Pairing Mode (3 min)" button in the Pairing & Discovery
  tab with a live countdown timer and instruction text.
- Auto-refreshes the saved speakers list every 15 seconds while
  pairing mode is active.

## 1.1.1 (2026-09-01)

- Fixed static asset 404s under HA Ingress: replaced `url_for('static')`
  with relative paths (`static/css/app.css`, `static/js/app.js`) so the
  browser resolves them under the Ingress subpath instead of the domain
  root.
- Added 404 and 500 error handlers so API failures return structured
  JSON instead of raw HTML error pages.

## 1.1.0 (2026-09-01)

- Fixed Home Assistant Ingress compatibility: all API calls now use
  relative paths derived from `window.API_BASE` instead of hardcoded
  absolute paths, so buttons, tabs, and network requests work inside
  the Ingress iframe.
- Tab switching is now fully client-side (DOM visibility toggling)
  with no dependency on API responses or external libraries.
- Added global error handlers (`window.onerror`,
  `window.onunhandledrejection`) with a visible diagnostic overlay
  showing the full stack trace and a copy-to-clipboard button.
- Added `console.log` debugging traces for every user interaction.
- Fixed Flask `template_folder` / `static_folder` path resolution so
  `index.html` and static assets are found from `bridge/app.py`.
- Set `static_url_path="/static"` explicitly in the Flask app.
- Removed `rfkill` from the Alpine `apk add` list (provided by
  `util-linux`) and removed the stale `/dev/rfkill` device entry from
  `config.yaml`.

## 1.0.0 (2026-08-31)

- Initial release.
- Multi-instance Shairport-Sync AirPlay receiver generation.
- Dynamic D-Bus Bluetooth switcher with A2DP audio routing.
- Embedded HA Ingress Web UI with pairing, management, and diagnostics.
- Persistent JSON speaker storage in `/data/options.json`.
- Auto-recovery with configurable retry attempts.
