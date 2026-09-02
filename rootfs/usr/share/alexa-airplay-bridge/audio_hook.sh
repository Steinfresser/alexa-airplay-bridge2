#!/bin/bash
# audio_hook.sh — invoked by shairport-sync before/after active audio state.
#
# Arguments: <airplay_name> <mac> <enter|exit>
#
# On "enter": this is the critical audio-switching moment. iOS has selected
# this AirPlay speaker, so we:
#   1. Log the incoming stream event.
#   2. Disconnect any other currently connected Bluetooth device.
#   3. Connect the target speaker via D-Bus.
#   4. Wait briefly for the A2DP sink to appear in PipeWire/PulseAudio.
#   5. Route audio output to the new A2DP sink.
#
# The 3-second ring buffer is configured in shairport-sync.conf via
# audio_backend_latency, which absorbs the A2DP handshake latency and
# prevents iOS AirPlay timeout errors.
#
# On "exit": log and optionally clean up.

set -u

AIRPLAY_NAME="${1:-Unknown}"
MAC="${2:-}"
PHASE="${3:-enter}"
LOG_TAG="[AIRPLAY-HOOK]"

# Resolve runtime paths.
DATA_DIR="/data"
RUNTIME_DIR="${DATA_DIR}/pipewire"
PULSE_SOCKET="${RUNTIME_DIR}/pulse/native"
DB_FILE="${DATA_DIR}/options.json"
SWITCH_SCRIPT="/usr/share/alexa-airplay-bridge/bt_switch.sh"

log() {
    echo "${LOG_TAG} $(date '+%H:%M:%S') $*" | tee -a "${DATA_DIR}/bridge.log" 2>/dev/null || echo "${LOG_TAG} $*"
}

if [ "$PHASE" = "enter" ]; then
    log "Incoming audio stream for '${AIRPLAY_NAME}' (${MAC})"

    # Delegate the full D-Bus switch + audio routing to the switcher script.
    if [ -x "$SWITCH_SCRIPT" ]; then
        "$SWITCH_SCRIPT" "$MAC" "$AIRPLAY_NAME"
    else
        log "ERROR: switch script not found at $SWITCH_SCRIPT"
    fi
elif [ "$PHASE" = "exit" ]; then
    log "Audio stream ended for '${AIRPLAY_NAME}' (${MAC})"
    # Mark streaming as inactive via the switcher (no disconnect — keep BT link).
    if [ -x "$SWITCH_SCRIPT" ]; then
        "$SWITCH_SCRIPT" "$MAC" "$AIRPLAY_NAME" exit
    fi
else
    log "Unknown phase: $PHASE"
fi

exit 0
