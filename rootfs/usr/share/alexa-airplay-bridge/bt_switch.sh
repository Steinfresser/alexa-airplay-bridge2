#!/bin/bash
# bt_switch.sh — PipeWire A2DP routing for AirPlay audio.
#
# Arguments: <new_mac> <airplay_name> [exit]
#
# The bridge already connects Bluetooth via D-Bus and sets the default
# sink via the Python PipeWire module.  This script is called by
# shairport-sync's audio_hook.sh when an AirPlay stream starts.
# Its only job is to ensure the A2DP sink is set as the default
# PipeWire sink and is unmuted at 100% volume.

set -u

NEW_MAC="${1:-}"
AIRPLAY_NAME="${2:-Unknown}"
MODE="${3:-connect}"
LOG_TAG="[BT-SWITCH]"

DATA_DIR="/data"
RUNTIME_DIR="${DATA_DIR}/pipewire"
PULSE_SOCKET="${RUNTIME_DIR}/pulse/native"
A2DP_WAIT_SECONDS=10

# Export PipeWire/PulseAudio env vars so pactl finds the right server.
export XDG_RUNTIME_DIR="${RUNTIME_DIR}"
export PULSE_SERVER="unix:${PULSE_SOCKET}"
export PULSE_RUNTIME_PATH="${RUNTIME_DIR}/pulse"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${RUNTIME_DIR}/bus}"

log() {
    echo "${LOG_TAG} $(date '+%H:%M:%S') $*" | tee -a "${DATA_DIR}/bridge.log" 2>/dev/null || echo "${LOG_TAG} $*"
}

if [ "$MODE" = "exit" ]; then
    log "Stream exit for ${AIRPLAY_NAME} (${NEW_MAC}) — keeping Bluetooth link alive"
    exit 0
fi

if [ -z "$NEW_MAC" ]; then
    log "ERROR: no MAC provided"
    exit 0
fi

# Normalise MAC for sink name matching (AC:EF:92:... -> AC_EF_92_...)
MAC_NORM="${NEW_MAC//:/_}"

# --- 1. Wait for A2DP sink to appear in PipeWire/PulseAudio -------------------
log "Waiting for A2DP sink to appear (up to ${A2DP_WAIT_SECONDS}s)..."
sink_name=""
for i in $(seq 1 "$A2DP_WAIT_SECONDS"); do
    sink_name="$(pactl --server "unix:${PULSE_SOCKET}" list sinks short 2>/dev/null \
        | grep -i "${MAC_NORM}" | awk '{print $2}' | head -1)"
    if [ -z "$sink_name" ]; then
        sink_name="$(pactl list sinks short 2>/dev/null \
            | grep -i "${MAC_NORM}" | awk '{print $2}' | head -1)"
    fi
    if [ -n "$sink_name" ]; then
        log "A2DP sink found: ${sink_name} (after ${i}s)"
        break
    fi
    sleep 1
done

# --- 2. Route audio to the sink -------------------------------------------
if [ -n "$sink_name" ]; then
    log "Routing audio to sink: ${sink_name}"
    pactl --server "unix:${PULSE_SOCKET}" set-default-sink "$sink_name" 2>&1 \
        | while read -r line; do log "  $line"; done
    pactl set-default-sink "$sink_name" 2>&1 | while read -r line; do log "  $line"; done
    pactl --server "unix:${PULSE_SOCKET}" set-sink-mute "$sink_name" 0 2>/dev/null || true
    pactl --server "unix:${PULSE_SOCKET}" set-sink-volume "$sink_name" 100% 2>/dev/null || true
    pactl set-sink-mute "$sink_name" 0 2>/dev/null || true
    pactl set-sink-volume "$sink_name" 100% 2>/dev/null || true
    log "Sink ${sink_name} unmuted and volume set to 100%"

    # Move any existing playback streams to the new sink.
    pactl --server "unix:${PULSE_SOCKET}" list short 2>/dev/null \
        | grep "protocol-native" | awk '{print $1}' | while read -r stream_id; do
        pactl --server "unix:${PULSE_SOCKET}" move-sink-input "$stream_id" "$sink_name" 2>/dev/null \
            || true
    done
    log "Audio routing complete for ${AIRPLAY_NAME} (${NEW_MAC})"
else
    log "WARNING: A2DP sink for ${NEW_MAC} not found after ${A2DP_WAIT_SECONDS}s — audio may route to default output"
    fallback_sink="$(pactl --server "unix:${PULSE_SOCKET}" list sinks short 2>/dev/null \
        | grep -i bluez | awk '{print $2}' | head -1)"
    if [ -z "$fallback_sink" ]; then
        fallback_sink="$(pactl list sinks short 2>/dev/null | grep -i bluez | awk '{print $2}' | head -1)"
    fi
    if [ -n "$fallback_sink" ]; then
        log "Using fallback bluez sink: ${fallback_sink}"
        pactl set-default-sink "$fallback_sink" 2>&1 | while read -r line; do log "  $line"; done
    fi
fi

exit 0
