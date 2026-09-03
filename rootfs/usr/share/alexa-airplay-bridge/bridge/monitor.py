"""Background availability monitor that polls saved MACs and refreshes status."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bluetooth import BluetoothManager
    from .pipewire import PipeWireManager
    from .shairport import ShairportManager
    from .storage import SpeakerDB

_LOG = logging.getLogger(__name__)


class AvailabilityMonitor(threading.Thread):
    """Periodically pings saved MACs to update connection/availability status."""

    def __init__(
        self,
        db: "SpeakerDB",
        bt: "BluetoothManager",
        shairport: "ShairportManager",
        interval: int = 15,
        pipewire: "PipeWireManager | None" = None,
    ) -> None:
        super().__init__(daemon=True, name="AvailabilityMonitor")
        self._db = db
        self._bt = bt
        self._shairport = shairport
        self._pipewire = pipewire
        self._interval = max(5, interval)
        self._stop = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # not paused by default

    def pause(self) -> None:
        """Pause the monitor so manual operations don't collide with it."""
        self._pause_event.clear()
        _LOG.info("[Monitor] Paused for manual interaction")

    def resume(self) -> None:
        """Resume the monitor after a manual operation completes."""
        self._pause_event.set()
        _LOG.info("[Monitor] Resumed")

    def run(self) -> None:
        _LOG.info("[Monitor] Availability monitor started (interval=%ds)", self._interval)
        while not self._stop.is_set():
            self._pause_event.wait()
            if self._stop.is_set():
                break
            try:
                # Periodic sink watchdog logging.
                if self._pipewire is not None:
                    sinks = self._pipewire.list_sinks().strip()
                    default_sink = self._pipewire.get_default_sink()
                    _LOG.info("[Monitor] [WATCHDOG] default_sink=%s | sinks:\n%s",
                              default_sink, sinks if sinks else "(none)")
                    for mac_k, kp in list(self._pipewire._keepalive_procs.items()):
                        alive = kp.poll() is None
                        _LOG.info("[Monitor] [WATCHDOG] keepalive %s: pid=%d alive=%s",
                                  mac_k, kp.pid, alive)

                    self._pipewire.check_host_audio_conflict()

                for spk in self._db.list_speakers():
                    status = self._bt.get_device_status(spk.mac)
                    connected = status.get("connected") == "yes"
                    shairport_running = self._shairport.is_running(spk.mac)
                    bt_sink = self._pipewire.find_bluetooth_sink(spk.mac) if self._pipewire else None
                    _LOG.info("[Monitor] [WATCHDOG] %s (%s): bt=%s shairport=%s sink=%s",
                              spk.name, spk.mac, "conn" if connected else "disc",
                              "running" if shairport_running else "stopped",
                              bt_sink or "NONE")
                    if connected != spk.connected:
                        _LOG.info("[Monitor] %s connection state: %s -> %s",
                                  spk.mac, spk.connected, connected)
                        self._db.update(spk.mac, connected=connected)
                        if connected:
                            self._shairport.start_for_connected(spk.mac)
                        else:
                            self._shairport.stop_for_disconnected(spk.mac)
                    if connected:
                        self._db.update(spk.mac, last_seen=time.strftime("%Y-%m-%d %H:%M:%S"))
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("[Monitor] poll error: %s", exc)
            self._stop.wait(self._interval)
        _LOG.info("[Monitor] Availability monitor stopped")

    def stop(self) -> None:
        self._stop.set()
        self._pause_event.set()
