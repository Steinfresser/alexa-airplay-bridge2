#!/usr/bin/env bash
set -e

echo "[entrypoint] AirPlay to Bluetooth Bridge starting…"

# Pre-flight: verify shairport-sync is installed and runnable.
if ! command -v shairport-sync >/dev/null 2>&1; then
    echo "[entrypoint] FATAL: shairport-sync binary not found."
    echo "[entrypoint] The Docker image is broken — rebuild it."
    exit 1
fi
echo "[entrypoint] shairport-sync found: $(shairport-sync -V 2>/dev/null || echo 'version unknown')"

# Pre-flight: verify shairport-sync has at least one audio backend.
SHAIRPORT_HELP=$(shairport-sync -h 2>&1 || true)
if ! echo "$SHAIRPORT_HELP" | grep -qE 'pa|alsa|pulseaudio'; then
    echo "[entrypoint] WARNING: shairport-sync may lack an audio backend (PA/ALSA)."
    echo "[entrypoint] Audio routing may not work correctly."
fi

# Pre-flight: verify shairport-sync was compiled with tinysvcmdns (embedded mDNS).
# Without it, AirPlay devices won't be visible on iOS/macOS.
SHAIRPORT_VERSION=$(shairport-sync -V 2>&1 || true)
if ! echo "$SHAIRPORT_VERSION" | grep -qi 'tinysvcmdns'; then
    echo "[entrypoint] WARNING: shairport-sync may not include tinysvcmdns."
    echo "[entrypoint] AirPlay discovery (mDNS) will not work without it."
fi
echo "[entrypoint] Pre-flight validation complete."

DBUS_SOCKET="/var/run/dbus/system_bus_socket"

if [ ! -e "$DBUS_SOCKET" ]; then
    echo "[entrypoint] WARNING: D-Bus system bus socket not found at $DBUS_SOCKET"
    echo "[entrypoint] The add-on will start in degraded mode (bluetoothctl fallback)."
    echo "[entrypoint] Ensure host_dbus: true is set in the add-on config."
else
    echo "[entrypoint] D-Bus system bus socket found."
fi

# ---------------------------------------------------------------------------
# Private D-Bus SESSION bus for shairport-sync's own D-Bus/MPRIS interface.
#
# shairport-sync registers org.gnome.ShairportSync and
# org.mpris.MediaPlayer2.ShairportSync on this session bus
# (configured via dbus_service_bus="session" in shairport-sync.conf).
# Bluetooth (BlueZ) continues using the host's real system bus.
#
# No Avahi daemon is needed — shairport-sync is compiled with embedded
# tinysvcmdns, which broadcasts mDNS directly via UDP multicast on port 5353.
# ---------------------------------------------------------------------------
AVahi_DBUS_DIR="/run/avahi-dbus"
SESSION_BUS_ADDR="unix:path=${AVahi_DBUS_DIR}/bus"
mkdir -p "$AVahi_DBUS_DIR"

set +e
dbus-daemon --session --fork --print-address=1 \
    --address="$SESSION_BUS_ADDR" 2>/dev/null
DBUS_SESSION_RC=$?
set -e

if [ $DBUS_SESSION_RC -eq 0 ] && [ -S "${AVahi_DBUS_DIR}/bus" ]; then
    echo "[entrypoint] Private D-Bus session bus started at $SESSION_BUS_ADDR"
    export DBUS_SESSION_BUS_ADDRESS="$SESSION_BUS_ADDR"
else
    echo "[entrypoint] WARNING: Could not start private D-Bus session bus (rc=$DBUS_SESSION_RC)"
fi

# ---------------------------------------------------------------------------
# WirePlumber headless configuration.
#
# In a container there is no logind/seatd, so seat-monitoring fails to load.
# The override at /etc/wireplumber/wireplumber.conf disables it and sets
# Bluetooth to auto-switch to A2DP sink profile on connect.
# WirePlumber reads configs from XDG_CONFIG_DIRS and /etc/wireplumber/.
# ---------------------------------------------------------------------------
export XDG_CONFIG_DIRS="/etc"

# ---------------------------------------------------------------------------
# Kill any competing Bluetooth audio services.
#
# PulseAudio, bluez-alsa, or other sound servers may register the A2DP sink
# profile with BlueZ before PipeWire starts.  When that happens, PipeWire
# gets "RegisterProfile() failed: org.bluez.Error.NotPermitted" and creates
# a BT sink node that is never backed by a real transport — audio is silently
# discarded.  Killing these before PipeWire starts ensures PipeWire is the
# sole owner of the Bluetooth audio transport.
# ---------------------------------------------------------------------------
killall pulseaudio bluealsa 2>/dev/null || true
sleep 0.2

echo "[entrypoint] Configuration complete — starting bridge."
exec python3 /usr/share/alexa-airplay-bridge/run.py
