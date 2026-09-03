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
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/data/pipewire}"
PULSE_SOCKET="${RUNTIME_DIR}/pulse/native"
A2DP_WAIT_SECONDS=10

# Export PipeWire/PulseAudio env vars so pactl finds the right server.
# If the HA supervisor set PULSE_SERVER (audio: true), use that instead of
# our own PipeWire socket — avoids A2DP profile conflict with hassio_audio.
if [ -n "${PULSE_SERVER:-}" ]; then
    log "Using HA audio (PULSE_SERVER=${PULSE_SERVER})"
    export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${RUNTIME_DIR}/bus}"
else
    export XDG_RUNTIME_DIR="${RUNTIME_DIR}"
    export PULSE_SERVER="unix:${PULSE_SOCKET}"
    export PULSE_RUNTIME_PATH="${RUNTIME_DIR}/pulse"
    export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${RUNTIME_DIR}/bus}"
fi

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

# --- DEBUG: log all environment details ---
log "DEBUG env: XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR}"
log "DEBUG env: PULSE_SERVER=${PULSE_SERVER}"
log "DEBUG env: PULSE_RUNTIME_PATH=${PULSE_RUNTIME_PATH}"
log "DEBUG env: DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS}"
log "DEBUG env: PIPEWIRE_NODE=${PIPEWIRE_NODE:-unset}"
log "DEBUG: pulse socket exists: $(test -e "${PULSE_SOCKET}" && echo YES || echo NO)"
log "DEBUG: pactl info:"
pactl info 2>&1 | head -5 | while read -r line; do log "  $line"; done
log "DEBUG: all sinks at entry:"
pactl list sinks short 2>&1 | while read -r line; do log "  $line"; done
log "DEBUG: all cards at entry:"
pactl list cards short 2>&1 | while read -r line; do log "  $line"; done
log "DEBUG: bluetoothctl info ${NEW_MAC}:"
bluetoothctl info "${NEW_MAC}" 2>&1 | grep -E "Connected|UUID|Name" | while read -r line; do log "  $line"; done

# --- 1. Wait for A2DP sink to appear in PipeWire/PulseAudio -------------------
log "Waiting for A2DP sink to appear (up to ${A2DP_WAIT_SECONDS}s)..."
sink_name=""
for i in $(seq 1 "$A2DP_WAIT_SECONDS"); do
    # Try with explicit server first, then fall back to env-based discovery.
    sink_name="$(pactl list sinks short 2>/dev/null \
        | grep -i "${MAC_NORM}" | awk '{print $2}' | head -1)"
    if [ -n "$sink_name" ]; then
        log "A2DP sink found: ${sink_name} (after ${i}s)"
        break
    fi
    # If sink not found after 3s, try re-setting the card profile to a2dp_sink.
    # WirePlumber sometimes drops the A2DP profile and needs a re-trigger.
    if [ "$i" -eq 3 ]; then
        log "Sink not found after 3s — re-triggering A2DP profile for ${NEW_MAC}"
        card_name="$(pactl list cards short 2>/dev/null \
            | grep -i "${MAC_NORM}" | awk '{print $2}' | head -1)"
        if [ -n "$card_name" ]; then
            log "Setting card profile ${card_name} -> a2dp_sink"
            pactl set-card-profile "$card_name" a2dp_sink 2>&1 \
                | while read -r line; do log "  $line"; done || true
            pactl set-card-profile "$card_name" a2dp 2>&1 \
                | while read -r line; do log "  $line"; done || true
        else
            log "No card found for ${NEW_MAC} — device may be disconnected"
        fi
    fi
    sleep 1
done

# --- 2. Route audio to the sink -------------------------------------------
if [ -n "$sink_name" ]; then
    log "Routing audio to sink: ${sink_name}"
    pactl set-default-sink "$sink_name" 2>&1 | while read -r line; do log "  $line"; done
    pactl set-sink-mute "$sink_name" 0 2>&1 | while read -r line; do log "  $line"; done
    pactl set-sink-volume "$sink_name" 100% 2>&1 | while read -r line; do log "  $line"; done
    log "Sink ${sink_name} unmuted and volume set to 100%"

    # Move any existing playback streams to the new sink.
    pactl list sink-inputs short 2>/dev/null \
        | awk '{print $1}' | while read -r stream_id; do
        pactl move-sink-input "$stream_id" "$sink_name" 2>&1 \
            | while read -r line; do log "  move $stream_id: $line"; done || true
    done
    log "Audio routing complete for ${AIRPLAY_NAME} (${NEW_MAC})"
else
    log "WARNING: A2DP sink for ${NEW_MAC} not found after ${A2DP_WAIT_SECONDS}s — audio may route to default output"
    # Dump pactl state for debugging.
    log "Available sinks:"
    pactl list sinks short 2>&1 | while read -r line; do log "  $line"; done
    fallback_sink="$(pactl list sinks short 2>/dev/null \
        | grep -i bluez | awk '{print $2}' | head -1)"
    if [ -n "$fallback_sink" ]; then
        log "Using fallback bluez sink: ${fallback_sink}"
        pactl set-default-sink "$fallback_sink" 2>&1 | while read -r line; do log "  $line"; done
    fi
fi

exit 0
