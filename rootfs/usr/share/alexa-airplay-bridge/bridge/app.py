"""Flask Web UI server with HA Ingress support and all REST API endpoints."""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import BadRequest

from .logger import clear_log_buffer, get_log_buffer, get_log_level, set_log_level
from .validation import (
    MAX_NAME_LENGTH,
    is_valid_display_name,
    normalise_mac,
    sanitise_display_name,
)

if TYPE_CHECKING:
    from .run import BridgeEngine

_LOG = logging.getLogger("app")

# The add-on runs with ``host_network: true``, so the listener is bound on the
# host's real interfaces. Home Assistant's Ingress proxy reaches it from the
# Supervisor network; nothing else on the LAN may talk to it, because none of
# these endpoints carry their own authentication.
_DEFAULT_ALLOWED_CIDRS = (
    "127.0.0.0/8",
    "::1/128",
    "172.30.32.0/23",
)

_BAD_JSON = {"status": "error", "message": "Malformed request body"}
_BAD_MAC = {"status": "error", "message": "Invalid Bluetooth address"}
_BAD_NAME = {
    "status": "error",
    "message": (
        "Invalid display name. Use letters, numbers, spaces and "
        "- . + & ( ) ' only, up to " + str(MAX_NAME_LENGTH) + " characters."
    ),
}

_DISCOVERABLE_DURATION = 180  # 3 minutes


def _allowed_networks() -> list:
    raw = os.environ.get("SUPERVISOR_ALLOWED_CIDRS", "")
    cidrs = [c.strip() for c in raw.split(",") if c.strip()] or list(_DEFAULT_ALLOWED_CIDRS)
    networks = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            _LOG.warning("[Auth] Ignoring invalid allowed CIDR %r", cidr)
    return networks


def _json_body() -> dict:
    """Parse a JSON request body, raising BadRequest on malformed input."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        raise BadRequest("body must be a JSON object")
    return data


def create_app(engine: "BridgeEngine") -> Flask:
    """Create and configure the Flask application."""
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, "templates"),
        static_folder=os.path.join(base_dir, "static"),
        static_url_path="/static",
    )
    app.config["JSON_SORT_KEYS"] = False

    allowed = _allowed_networks()
    pair_lock = threading.Lock()

    @app.before_request
    def _restrict_to_ingress():  # type: ignore[return-value]
        """Reject anything that did not arrive through Home Assistant."""
        remote = request.remote_addr or ""
        try:
            addr = ipaddress.ip_address(remote)
        except ValueError:
            _LOG.warning("[Auth] Rejected request with unparsable source address %r", remote)
            return jsonify({"status": "error", "message": "Forbidden"}), 403
        if addr.is_loopback or any(addr in net for net in allowed):
            return None
        _LOG.warning("[Auth] Rejected %s %s from %s (outside Home Assistant)",
                     request.method, request.path, remote)
        return jsonify({"status": "error", "message": "Forbidden"}), 403

    @app.errorhandler(BadRequest)
    def _handle_bad_request(_exc):  # type: ignore[return-value]
        return jsonify(_BAD_JSON), 400

    @app.errorhandler(404)
    def _handle_404(_exc):  # type: ignore[return-value]
        if request.path.startswith("/api/"):
            return jsonify({"status": "error", "message": "Not found"}), 404
        return jsonify({"status": "error", "message": "Not found"}), 404

    @app.errorhandler(500)
    def _handle_500(exc):  # type: ignore[return-value]
        _LOG.error("[Web UI] 500 error on %s %s: %s", request.method, request.path, exc, exc_info=True)
        msg = str(exc) if str(exc) else "Internal server error"
        return jsonify({"status": "error", "message": msg}), 500

    @app.errorhandler(Exception)
    def _handle_unhandled(exc):  # type: ignore[return-value]
        _LOG.error("[Web UI] Unhandled exception on %s %s: %s", request.method, request.path, exc, exc_info=True)
        return jsonify({"status": "error", "message": str(exc)}), 500

    # ------------------------------------------------------------------ pages
    @app.route("/")
    def index() -> str:
        return render_template("index.html")

    @app.route("/api/status", methods=["GET"])
    def api_status() -> "flask.Response":
        """Return the live AirPlay, Bluetooth, and PipeWire topology."""
        playback = engine.pipewire.list_playback()
        active_client = None
        for line in playback.splitlines():
            stripped = line.strip()
            low = stripped.lower()
            if "application.name" in low or "media.name" in low or "client.name" in low:
                if "=" in stripped:
                    value = stripped.split("=", 1)[1].strip().strip('"')
                    if value and value.lower() not in ("pipewire", "pulseaudio"):
                        active_client = value
                        break

        bluetooth_devices = []
        airplay_instances = []
        for speaker in engine.db.list_speakers():
            bt_status = engine.bt.get_device_status(speaker.mac)
            connected = bt_status.get("connected", "no") == "yes"
            sink = engine.pipewire.find_bluetooth_sink(speaker.mac)
            running = engine.shairport.is_running(speaker.mac)
            if not connected:
                airplay_status = "DISCONNECTED"
            elif running and sink:
                airplay_status = "BRIDGED"
            elif running:
                airplay_status = "BUFFERING"
            elif connected and not sink:
                airplay_status = "CONNECTING"
            else:
                airplay_status = "IDLE"
            bluetooth_devices.append({
                "mac": speaker.mac,
                "name": speaker.name,
                "paired": bt_status.get("paired", "yes" if speaker.paired else "no") == "yes",
                "connected": connected,
                "status": "CONNECTED" if connected else "DISCONNECTED",
                "active_sink": sink,
            })
            airplay_instances.append({
                "name": speaker.name,
                "port": speaker.port,
                "active_client": active_client if running else None,
                "status": airplay_status,
                "mapped_sink": sink,
            })

        default_sink = engine.pipewire.get_default_sink()
        now_playing = engine.shairport.get_all_now_playing()
        return jsonify({
            "status": "ok",
            "bluetooth_devices": bluetooth_devices,
            "airplay_instances": airplay_instances,
            "now_playing": now_playing,
            "system_audio": {
                "default_sink": default_sink,
                "volume": engine.pipewire.get_volume(),
                "muted": engine.pipewire.get_mute_state(),
            },
        })

    @app.route("/api/now-playing", methods=["GET"])
    def api_now_playing() -> "flask.Response":
        """Return live Now Playing metadata for all active AirPlay streams."""
        tracks = engine.shairport.get_all_now_playing()
        return jsonify({
            "status": "ok",
            "tracks": tracks,
        })

    # ------------------------------------------------------------------ scan
    @app.route("/api/scan", methods=["POST"])
    def api_scan() -> "flask.Response":
        duration = engine.config.scan_duration_seconds
        if request.is_json:
            try:
                duration = int((request.get_json(silent=True) or {}).get("duration", duration))
            except (TypeError, ValueError):
                duration = engine.config.scan_duration_seconds
        duration = max(5, min(60, duration))
        if engine.monitor:
            engine.monitor.pause()
        engine.shairport.pause_monitor()
        try:
            devices = engine.bt.scan(duration=duration)
        finally:
            if engine.monitor:
                engine.monitor.resume()
            engine.shairport.resume_monitor()
        return jsonify({
            "status": "ok",
            "duration": duration,
            "devices": [
                {
                    "mac": d.mac,
                    "name": d.name,
                    "rssi": d.rssi,
                    "paired": d.paired,
                    "trusted": d.trusted,
                    "connected": d.connected,
                }
                for d in devices
            ],
        })

    # --------------------------------------------------------------- pairing
    @app.route("/api/pair", methods=["POST"])
    def api_pair() -> "flask.Response":
        data = _json_body()
        mac = normalise_mac(data.get("mac"))
        raw_name = str(data.get("name", "")).strip()
        if not mac:
            return jsonify(_BAD_MAC), 400
        if not is_valid_display_name(raw_name):
            return jsonify(_BAD_NAME), 400
        name = sanitise_display_name(raw_name)
        if not name:
            return jsonify(_BAD_NAME), 400

        if not pair_lock.acquire(blocking=False):
            return jsonify({
                "status": "error",
                "message": "A pairing or connection operation is already in progress. Please wait for it to finish.",
            }), 409

        # Pause background monitors so they don't collide with manual pairing.
        if engine.monitor:
            engine.monitor.pause()
        engine.shairport.pause_monitor()

        try:
            pair_ok = engine.bt.pair(mac)
            trust_ok = engine.bt.trust(mac) if pair_ok else False
            connect_ok = engine.bt.connect(mac) if pair_ok else False
        finally:
            if engine.monitor:
                engine.monitor.resume()
            engine.shairport.resume_monitor()
            pair_lock.release()

        port = 0
        if pair_ok:
            index = len(engine.db.list_speakers())
            port = engine.config.airplay_port_base + index * 2
            engine.db.upsert(mac, name, paired=True, trusted=trust_ok, connected=connect_ok, port=port)
            spk = engine.db.get(mac)
            if spk and connect_ok and engine.pipewire is not None:
                engine.shairport.start_for_connected(mac)
            elif spk:
                engine.shairport.start_instance(spk, index)
        else:
            engine.db.upsert(mac, name, paired=False)

        return jsonify({
            "status": "ok" if pair_ok else "error",
            "paired": pair_ok,
            "trusted": trust_ok,
            "connected": connect_ok,
            "port": port,
            "message": "Device paired and AirPlay instance started" if pair_ok
                      else (engine.bt.last_pair_error or "Pairing failed — check diagnostics"),
        })

    # ----------------------------------------------------------- saved speakers
    @app.route("/api/speakers", methods=["GET"])
    def api_speakers() -> "flask.Response":
        speakers = []
        for spk in engine.db.list_speakers():
            bt_status = engine.bt.get_device_status(spk.mac)
            bt_connected = bt_status.get("connected", "no") == "yes"
            shairport_running = engine.shairport.is_running(spk.mac)
            # Check if a PipeWire A2DP sink exists for this device.
            has_sink = engine.pipewire.find_bluetooth_sink(spk.mac) is not None
            # AirPlay is considered "on" only if shairport is running AND
            # either the BT device is connected or a PipeWire sink exists.
            airplay_on = shairport_running and (bt_connected or has_sink)
            speakers.append({
                "mac": spk.mac,
                "name": spk.name,
                "paired": spk.paired,
                "trusted": spk.trusted,
                "connected": bt_connected,
                "streaming": spk.streaming,
                "rssi": spk.rssi,
                "last_seen": spk.last_seen,
                "port": spk.port,
                "shairport_running": shairport_running,
                "airplay_active": engine.shairport.pid_is_running(spk.mac),
                "airplay_on": airplay_on,
                "has_sink": has_sink,
                "bt_available": bt_status.get("available", "no") == "yes",
            })
        return jsonify({"status": "ok", "speakers": speakers})

    @app.route("/api/speakers/<mac>", methods=["PUT"])
    def api_edit_speaker(mac: str) -> "flask.Response":
        data = _json_body()
        mac = normalise_mac(mac)
        if not mac:
            return jsonify(_BAD_MAC), 400
        raw_name = str(data.get("name", "")).strip()
        if not is_valid_display_name(raw_name):
            return jsonify(_BAD_NAME), 400
        name = sanitise_display_name(raw_name)
        if not name:
            return jsonify(_BAD_NAME), 400
        spk = engine.db.update(mac, name=name)
        if spk is None:
            return jsonify({"status": "error", "message": "speaker not found"}), 404
        # Restart the shairport instance with the new name.
        index = engine.db.list_speakers().index(spk)
        engine.shairport.restart_instance(spk, index)
        return jsonify({"status": "ok", "speaker": spk.to_dict()})

    @app.route("/api/bluetooth/unpair", methods=["POST"])
    def api_bt_unpair() -> "flask.Response":
        data = _json_body()
        mac = normalise_mac(data.get("mac"))
        if not mac:
            return jsonify(_BAD_MAC), 400
        # Pause background monitors during manual operation.
        if engine.monitor:
            engine.monitor.pause()
        engine.shairport.pause_monitor()
        try:
            engine.shairport.stop_instance(mac)
            bt_ok = engine.bt.remove(mac)
            engine.db.delete(mac)
        finally:
            if engine.monitor:
                engine.monitor.resume()
            engine.shairport.resume_monitor()
        return jsonify({
            "status": "ok" if bt_ok else "error",
            "unpaired": bt_ok,
            "message": "Device unpaired and removed completely" if bt_ok
                      else "Removal may have partially failed — check diagnostics",
        })

    @app.route("/api/speakers/<mac>/test-connect", methods=["POST"])
    def api_test_connect(mac: str) -> "flask.Response":
        mac = normalise_mac(mac)
        if not mac:
            return jsonify(_BAD_MAC), 400
        spk = engine.db.get(mac)
        if spk is None:
            return jsonify({"status": "error", "message": "speaker not found"}), 404
        # Pause background monitors during manual test.
        if engine.monitor:
            engine.monitor.pause()
        engine.shairport.pause_monitor()
        try:
            ok = engine.bt.connect(mac)
            engine.db.update(mac, connected=ok)
            test_sound_ok = False
            if ok:
                sink_name = None
                if engine.pipewire is not None:
                    sink_name = engine.pipewire.wait_for_bluetooth_sink(
                        mac, timeout=15.0, interval=1.0,
                    )
                if sink_name is None and engine.pipewire is not None:
                    sink_name = engine.pipewire.find_bluetooth_sink(mac)
                if sink_name is not None:
                    test_sound_ok = engine.pipewire.play_test_sound(mac)
                else:
                    _LOG.warning("[Test] A2DP sink for %s never appeared — skipping test tone", mac)
        except Exception as exc:
            _LOG.exception("[Test] test-connect failed for %s", mac)
            return jsonify({"status": "error", "message": str(exc)}), 500
        finally:
            if engine.monitor:
                engine.monitor.resume()
            engine.shairport.resume_monitor()
        return jsonify({
            "status": "ok" if ok else "error",
            "connected": ok,
            "test_sound": test_sound_ok,
            "message": "Connection successful, test tone played" if test_sound_ok
                      else ("Connected but test tone failed" if ok else "Connection failed — check diagnostics"),
        })

    @app.route("/api/speakers/<mac>", methods=["DELETE"])
    def api_delete_speaker(mac: str) -> "flask.Response":
        mac = normalise_mac(mac)
        if not mac:
            return jsonify(_BAD_MAC), 400
        spk = engine.db.get(mac)
        if spk is None:
            return jsonify({"status": "error", "message": "speaker not found"}), 404
        if engine.monitor:
            engine.monitor.pause()
        engine.shairport.pause_monitor()
        try:
            engine.shairport.stop_instance(mac)
            engine.bt.remove(mac)
            engine.db.delete(mac)
        finally:
            if engine.monitor:
                engine.monitor.resume()
            engine.shairport.resume_monitor()
        return jsonify({"status": "ok", "message": "Speaker removed"})

    # --------------------------------------------------------------- diagnostics
    @app.route("/api/logs", methods=["GET"])
    def api_logs() -> "flask.Response":
        return jsonify({"status": "ok", "logs": get_log_buffer()})

    @app.route("/api/logs", methods=["DELETE"])
    def api_clear_logs() -> "flask.Response":
        clear_log_buffer()
        return jsonify({"status": "ok", "message": "Log buffer cleared"})

    @app.route("/api/log-level", methods=["GET"])
    def api_get_log_level() -> "flask.Response":
        return jsonify({"status": "ok", "level": get_log_level()})

    @app.route("/api/log-level", methods=["PUT"])
    def api_set_log_level() -> "flask.Response":
        data = _json_body()
        level = str(data.get("level", "INFO")).upper()
        if level not in ("INFO", "DEBUG", "TRACE"):
            return jsonify({"status": "error", "message": "Invalid log level"}), 400
        set_log_level(level)
        return jsonify({"status": "ok", "level": get_log_level()})

    @app.route("/api/bluetooth/status", methods=["GET"])
    def api_bt_status() -> "flask.Response":
        status = engine.bt.get_adapter_status()
        return jsonify({
            "status": "ok",
            "adapter": {
                "available": status.available,
                "powered": status.powered,
                "address": status.address,
                "name": status.name,
                "alias": status.alias,
                "discoverable": status.discoverable,
                "discovering": status.discovering,
            },
            "raw": engine.bt.get_adapter_status().__dict__,
        })

    @app.route("/api/bluetooth/discoverable", methods=["POST"])
    def api_bt_discoverable_start() -> "flask.Response":
        duration = _DISCOVERABLE_DURATION
        if request.is_json:
            try:
                duration = int((request.get_json(silent=True) or {}).get("duration", duration))
            except (TypeError, ValueError):
                duration = _DISCOVERABLE_DURATION
        ok = engine.bt.start_discoverable_mode(duration=duration)
        remaining = engine.bt.get_discoverable_remaining()
        return jsonify({
            "status": "ok" if ok else "error",
            "discoverable": ok,
            "duration": duration,
            "remaining": remaining,
            "message": "Pairing mode active — put your Echo into pairing mode now" if ok
                      else "Failed to enable pairing mode",
        })

    @app.route("/api/bluetooth/discoverable", methods=["DELETE"])
    def api_bt_discoverable_stop() -> "flask.Response":
        ok = engine.bt.stop_discoverable_mode()
        return jsonify({
            "status": "ok" if ok else "error",
            "discoverable": False,
            "remaining": 0,
            "message": "Pairing mode stopped" if ok else "Failed to stop pairing mode",
        })

    @app.route("/api/bluetooth/discoverable", methods=["GET"])
    def api_bt_discoverable_status() -> "flask.Response":
        active = engine.bt.is_discoverable_active()
        remaining = engine.bt.get_discoverable_remaining()
        return jsonify({
            "status": "ok",
            "discoverable": active,
            "remaining": remaining,
        })

    @app.route("/api/bluetooth/events", methods=["GET"])
    def api_bt_events() -> "flask.Response":
        events = engine.bt.get_events()
        return jsonify({
            "status": "ok",
            "events": [
                {
                    "timestamp": e.timestamp,
                    "mac": e.mac,
                    "action": e.action,
                    "success": e.success,
                    "detail": e.detail,
                    "error_code": e.error_code,
                }
                for e in events
            ],
        })

    @app.route("/api/audio/sinks", methods=["GET"])
    def api_sinks() -> "flask.Response":
        sinks = engine.pipewire.list_sinks()
        cards = engine.pipewire.list_cards()
        playback = engine.pipewire.list_playback()
        pw_status = engine.pipewire.get_status()
        return jsonify({
            "status": "ok",
            "pipewire": pw_status,
            "sinks": sinks,
            "cards": cards,
            "playback": playback,
        })

    @app.route("/api/daemons/restart", methods=["POST"])
    def api_restart_daemons() -> "flask.Response":
        engine.restart_daemons()
        return jsonify({"status": "ok", "message": "All Shairport and PipeWire daemons restarted"})

    @app.route("/api/speakers/<mac>/airplay/start", methods=["POST"])
    def api_airplay_start(mac: str) -> "flask.Response":
        """Manually start the AirPlay receiver for a speaker."""
        mac = normalise_mac(mac)
        if not mac:
            return jsonify(_BAD_MAC), 400
        spk = engine.db.get(mac)
        if spk is None:
            return jsonify({"status": "error", "message": "speaker not found"}), 404
        if engine.shairport.is_running(mac):
            return jsonify({"status": "ok", "message": "AirPlay already running", "already_running": True})
        if engine.monitor:
            engine.monitor.pause()
        engine.shairport.pause_monitor()
        try:
            if engine.pipewire is not None and engine.bt.is_device_connected(mac):
                ok = engine.shairport.start_for_connected(mac)
            else:
                index = engine.db.list_speakers().index(spk)
                ok = engine.shairport.start_instance(spk, index)
        finally:
            if engine.monitor:
                engine.monitor.resume()
            engine.shairport.resume_monitor()
        return jsonify({
            "status": "ok" if ok else "error",
            "message": "AirPlay receiver started" if ok
                      else "Failed to start — check that Bluetooth is connected and A2DP sink is ready",
        })

    @app.route("/api/speakers/<mac>/airplay/stop", methods=["POST"])
    def api_airplay_stop(mac: str) -> "flask.Response":
        """Manually stop the AirPlay receiver for a speaker."""
        mac = normalise_mac(mac)
        if not mac:
            return jsonify(_BAD_MAC), 400
        if not engine.shairport.is_running(mac):
            return jsonify({"status": "ok", "message": "AirPlay already stopped", "already_stopped": True})
        if engine.monitor:
            engine.monitor.pause()
        engine.shairport.pause_monitor()
        try:
            ok = engine.shairport.stop_instance(mac)
        finally:
            if engine.monitor:
                engine.monitor.resume()
            engine.shairport.resume_monitor()
        return jsonify({
            "status": "ok" if ok else "error",
            "message": "AirPlay receiver stopped" if ok else "Failed to stop",
        })

    @app.route("/api/system/info", methods=["GET"])
    def api_system_info() -> "flask.Response":
        return jsonify({
            "status": "ok",
            "config": {
                "log_level": engine.config.log_level,
                "airplay_port_base": engine.config.airplay_port_base,
                "audio_buffer_seconds": engine.config.audio_buffer_seconds,
                "bluetooth_retry_attempts": engine.config.bluetooth_retry_attempts,
                "scan_duration_seconds": engine.config.scan_duration_seconds,
            },
            "shairport_instances": engine.shairport.get_status(),
            "pipewire": engine.pipewire.get_status(),
        })

    @app.route("/api/diagnostics", methods=["GET"])
    def api_diagnostics() -> "flask.Response":
        """Granular diagnostics for every subsystem to pinpoint failures."""
        diag: dict = {"status": "ok", "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}

        # PipeWire subsystem
        pw = engine.pipewire
        diag["pipewire"] = {
            "running": pw.get_status(),
            "sinks_raw": pw.list_sinks()[:2000],
            "cards_raw": pw.list_cards()[:2000],
            "default_sink": pw.get_default_sink(),
            "volume": pw.get_volume(),
            "muted": pw.get_mute_state(),
        }

        # Shairport subsystem
        sh = engine.shairport
        sh_instances = []
        with sh._lock:
            for mac, proc in sh._processes.items():
                rc = proc.poll()
                sh_instances.append({
                    "mac": mac,
                    "pid": proc.pid,
                    "running": rc is None,
                    "exit_code": rc,
                })
            starting = list(sh._starting)
            sink_failed = list(sh._sink_failed)
            crash_counts = dict(sh._crash_counts)
        diag["shairport"] = {
            "instances": sh_instances,
            "starting": starting,
            "sink_failed": sink_failed,
            "crash_counts": crash_counts,
            "monitor_running": sh._monitor_thread is not None and sh._monitor_thread.is_alive(),
            "monitor_paused": not sh._monitor_pause.is_set(),
        }

        # Bluetooth subsystem
        bt = engine.bt
        bt_devices = []
        for spk in engine.db.list_speakers():
            status = bt.get_device_status(spk.mac)
            sink = pw.find_bluetooth_sink(spk.mac)
            bt_devices.append({
                "mac": spk.mac,
                "name": spk.name,
                "bt_connected": status.get("connected", "no") == "yes",
                "bt_paired": status.get("paired", "no") == "yes",
                "bt_trusted": status.get("trusted", "no") == "yes",
                "bt_available": status.get("available", "no") == "yes",
                "a2dp_sink": sink,
                "shairport_running": sh.is_running(spk.mac),
            })
        diag["bluetooth"] = {
            "adapter": bt.get_adapter_status().__dict__,
            "devices": bt_devices,
            "recent_events": [
                {
                    "timestamp": e.timestamp,
                    "mac": e.mac,
                    "action": e.action,
                    "success": e.success,
                    "detail": e.detail,
                    "error_code": e.error_code,
                }
                for e in bt.get_events()[-20:]
            ],
        }

        # Monitor subsystem
        mon = engine.monitor
        diag["monitor"] = {
            "running": mon is not None and mon.is_alive(),
            "paused": mon is not None and not mon._pause_event.is_set(),
            "interval": mon._interval if mon else None,
        }

        # Storage subsystem
        diag["storage"] = {
            "speaker_count": len(engine.db.list_speakers()),
            "speakers": [s.to_dict() for s in engine.db.list_speakers()],
        }

        return jsonify(diag)

    return app
