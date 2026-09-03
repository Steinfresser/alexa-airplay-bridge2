#!/usr/bin/env python3
"""AirPlay to Bluetooth Bridge — main control daemon.

Orchestrates startup, persistence, D-Bus Bluetooth, PipeWire, and the
multi-instance Shairport-Sync AirPlay pipeline, then launches the embedded
Flask Web UI for Home Assistant Ingress.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from typing import Optional

# Ensure our package is importable when run directly.
_BASE = os.path.dirname(os.path.abspath(__file__))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from bridge.config import AppConfig, load_config  # noqa: E402
from bridge.logger import get_log_level, setup_logging  # noqa: E402
from bridge.bluetooth import BluetoothManager  # noqa: E402
from bridge.monitor import AvailabilityMonitor  # noqa: E402
from bridge.pipewire import PipeWireManager  # noqa: E402
from bridge.shairport import ShairportManager  # noqa: E402
from bridge.storage import SpeakerDB  # noqa: E402

_LOG = logging.getLogger("run")


class BridgeEngine:
    """Top-level orchestrator wiring all subsystems together."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        os.makedirs(config.data_dir, exist_ok=True)
        os.makedirs(config.shairport_conf_dir, exist_ok=True)
        os.makedirs(config.shairport_pid_dir, exist_ok=True)
        os.makedirs(config.pipewire_dir, exist_ok=True)

        self.db = SpeakerDB(config.speaker_db_path)
        self.bt = BluetoothManager(retry_attempts=config.bluetooth_retry_attempts)
        self.pipewire = PipeWireManager(config.pipewire_dir)
        self.shairport = ShairportManager(
            conf_dir=config.shairport_conf_dir,
            pid_dir=config.shairport_pid_dir,
            port_base=config.airplay_port_base,
            buffer_seconds=config.audio_buffer_seconds,
            runtime_dir=config.pipewire_dir,
        )
        self.shairport.set_dependencies(self.bt, self.pipewire)
        self.shairport.set_database(self.db)
        self.monitor: Optional[AvailabilityMonitor] = None
        self._stop = threading.Event()

    # -- startup --------------------------------------------------------------

    def ensure_dbus(self) -> bool:
        """Ensure the host D-Bus system bus socket is accessible.

        Returns True if the system bus is available. If the socket is not
        yet present, waits up to 10 seconds for it to appear (non-fatal
        delay during container startup). Falls back to a session bus.
        """
        socket_path = "/var/run/dbus/system_bus_socket"

        # Wait up to 10 seconds for the socket to appear (HA supervisor
        # may still be setting up the D-Bus socket when the add-on starts).
        deadline = time.time() + 10
        while not os.path.exists(socket_path) and time.time() < deadline:
            _LOG.info("[D-Bus] Waiting for system bus socket at %s…", socket_path)
            time.sleep(1)

        if os.path.exists(socket_path):
            _LOG.info("[D-Bus] System bus socket found at %s", socket_path)
            return True

        _LOG.warning("[D-Bus] System bus socket not found after waiting — trying session bus fallback")

        # Only start a new session bus if DBUS_SESSION_BUS_ADDRESS is not
        # already set (the entrypoint may have started one for Avahi).
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            env = os.environ.copy()
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={self.config.pipewire_dir}/bus"
            try:
                subprocess.run(  # noqa: S603
                    ["dbus-daemon", "--session", "--fork", "--print-address=1",
                     f"--address=unix:path={self.config.pipewire_dir}/bus"],
                    capture_output=True, timeout=5, env=env,
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("[D-Bus] Could not start session bus: %s", exc)

        if os.path.exists(socket_path):
            return True

        _LOG.warning("[D-Bus] No D-Bus bus available — running in degraded mode")
        return False

    def start(self) -> bool:
        _LOG.info("=== AirPlay to Bluetooth Bridge starting ===")
        try:
            self.ensure_dbus()

            # Initialize native D-Bus connection and register the pairing agent.
            self.bt.init_dbus()

            # Bluetooth adapter.
            adapter = self.bt.ensure_adapter()
            if not adapter.available:
                _LOG.warning("[Startup] No Bluetooth adapter detected — pairing will fail until one is available")

            # Disconnect stale Bluetooth sessions before starting PipeWire.
            # Without this, WirePlumber's RegisterProfile() fails on devices
            # that are still connected from a previous container session.
            self.bt.disconnect_all()

            # PipeWire / PulseAudio.
            self.pipewire.start()

# shairport-sync (Alpine package) uses Avahi for mDNS/Bonjour — started by entrypoint.sh.
            # Shairport-Sync instances.
            speakers = self.db.list_speakers()
            if speakers:
                _LOG.info("[Startup] Starting %d Shairport-Sync instance(s)", len(speakers))
                self.shairport.start_all(speakers)
            else:
                _LOG.info("[Startup] No saved speakers — start pairing via the Web UI")

            # Availability monitor (drives Shairport lifecycle on BT transitions).
            self.monitor = AvailabilityMonitor(self.db, self.bt, self.shairport, interval=15, pipewire=self.pipewire)
            self.monitor.start()
            # Shairport lifecycle monitor (watches process + BT state).
            self.shairport.start_monitor()

            _LOG.info("=== Bridge started — log level: %s ===", get_log_level())
            return True
        except Exception as exc:  # noqa: BLE001
            _LOG.error("[Startup] FATAL: %s", exc)
            _LOG.error("[Startup] Traceback:\n%s", traceback.format_exc())
            return False

    def stop(self) -> None:
        _LOG.info("=== AirPlay to Bluetooth Bridge shutting down ===")
        self._stop.set()
        if self.monitor:
            self.monitor.stop()
        self.shairport.stop_monitor()
        self.shairport.stop_all()
        self.pipewire.stop()
        _LOG.info("=== Shutdown complete ===")

    def restart_daemons(self) -> bool:
        """Kill and restart all Shairport + PipeWire instances."""
        _LOG.info("[Engine] Force restart daemons requested")
        self.shairport.stop_all()
        self.pipewire.restart()
        time.sleep(1)
        speakers = self.db.list_speakers()
        self.shairport.start_all(speakers)
        return True


def main() -> None:
    try:
        config = load_config()
        setup_logging(level=config.log_level, log_file=config.log_file)
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] Failed to load configuration: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    try:
        engine = BridgeEngine(config)
    except Exception as exc:  # noqa: BLE001
        _LOG.error("[FATAL] Failed to initialise engine: %s", exc)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    def handle_signal(signum: int, _frame: object) -> None:
        _LOG.info("Received signal %d, shutting down", signum)
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        if not engine.start():
            _LOG.error("[FATAL] Engine startup failed — see traceback above")
            sys.exit(1)

        # Import and run the Flask app (Ingress Web UI).
        from bridge.app import create_app

        app = create_app(engine)
        port = int(os.environ.get("INGRESS_PORT", "8099"))
        _LOG.info("[Web UI] Starting Flask on port %d", port)
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except Exception as exc:  # noqa: BLE001
        _LOG.error("[FATAL] Runtime error: %s", exc)
        _LOG.error("[FATAL] Traceback:\n%s", traceback.format_exc())
        engine.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
