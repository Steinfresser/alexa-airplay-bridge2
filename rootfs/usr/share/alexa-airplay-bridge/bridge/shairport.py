"""Dynamic Shairport-Sync multi-instance manager with lifecycle monitoring.

Generates an isolated ``shairport-sync.conf`` and spawns a dedicated
``shairport-sync`` process per saved speaker. Each instance advertises its own
custom AirPlay name so iOS sees every speaker permanently.

A background monitor thread watches each Shairport process and the Bluetooth
connection state. When Bluetooth disconnects, the Shairport instance is stopped.
When Bluetooth reconnects and a PipeWire A2DP sink exists, Shairport is restarted.

The ``run_this_before_entering_active_state`` hook is configured to call back
into ``audio_hook.sh`` which performs the D-Bus Bluetooth switch and PipeWire
audio routing.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Optional

from .storage import Speaker
from .validation import normalise_mac, sanitise_display_name

if TYPE_CHECKING:
    from .bluetooth import BluetoothManager
    from .pipewire import PipeWireManager

_LOG = logging.getLogger(__name__)

_HOOK_SCRIPT = "/usr/share/alexa-airplay-bridge/audio_hook.sh"
_SWITCH_SCRIPT = "/usr/share/alexa-airplay-bridge/bt_switch.sh"
_MONITOR_INTERVAL = 10  # seconds between lifecycle checks


class ShairportManager:
    """Manages one shairport-sync process per saved speaker with lifecycle monitoring."""

    def __init__(
        self,
        conf_dir: str,
        pid_dir: str,
        port_base: int,
        buffer_seconds: int,
        runtime_dir: str,
    ) -> None:
        self._conf_dir = conf_dir
        self._pid_dir = pid_dir
        self._port_base = port_base
        self._buffer_seconds = buffer_seconds
        self._runtime_dir = runtime_dir
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen] = {}
        self._speakers: dict[str, tuple[Speaker, int]] = {}
        self._bt: Optional[BluetoothManager] = None
        self._pipewire: Optional[PipeWireManager] = None
        self._db: Optional[object] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()
        self._monitor_pause = threading.Event()
        self._monitor_pause.set()  # not paused by default
        self._crash_counts: dict[str, int] = {}
        self._crash_backoff_until: dict[str, float] = {}
        self._starting: set[str] = set()
        self._sink_failed: set[str] = set()
        self._now_playing: dict[str, dict[str, str]] = {}
        self._metadata_threads: dict[str, threading.Thread] = {}
        self._avrcp_listening = False

    def set_dependencies(self, bt: "BluetoothManager", pipewire: "PipeWireManager") -> None:
        """Inject Bluetooth and PipeWire managers for lifecycle monitoring."""
        self._bt = bt
        self._pipewire = pipewire

    def set_database(self, db: object) -> None:
        """Provide the saved-speaker database for reconnect recovery."""
        self._db = db

    def get_now_playing(self, mac: str) -> dict[str, str]:
        """Return cached metadata for the active stream on a speaker."""
        mac = normalise_mac(mac)
        if not mac:
            return {}
        with self._lock:
            return dict(self._now_playing.get(mac, {}))

    def get_all_now_playing(self) -> list[dict[str, str]]:
        """Return cached metadata for all speakers with active streams."""
        with self._lock:
            return [
                {"mac": mac, **meta}
                for mac, meta in self._now_playing.items()
                if meta
            ]

    def _metadata_pipe_path(self, mac: str) -> str:
        """Return the FIFO path for shairport-sync metadata output."""
        return os.path.join(self._runtime_dir, f"metadata-{mac.replace(':', '')}.fifo")

    def _start_metadata_reader(self, mac: str) -> None:
        """Start a background thread reading shairport-sync metadata pipe.

        Shairport-sync writes metadata as line-based key=value pairs to a
        named pipe. We parse track title, artist, album, and cover art
        and cache them for the Now Playing display.
        """
        pipe_path = self._metadata_pipe_path(mac)
        try:
            if os.path.exists(pipe_path):
                os.remove(pipe_path)
            os.makedirs(os.path.dirname(pipe_path), exist_ok=True)
            os.mkfifo(pipe_path)
        except OSError as exc:
            _LOG.debug("[Shairport] Could not create metadata pipe for %s: %s", mac, exc)
            return

        def _reader() -> None:
            try:
                with open(pipe_path, "r", encoding="utf-8", errors="replace") as fh:
                    title = artist = album = cover = ""
                    for line in iter(fh.readline, ""):
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("item_type="):
                            continue
                        if line.startswith("item_value="):
                            value = line[len("item_value="):].strip().strip('"')
                            if not value:
                                continue
                            # Heuristic: shairport sends metadata in chunks;
                            # we collect the most recent non-empty values.
                            if not title:
                                title = value
                            elif not artist:
                                artist = value
                            elif not album:
                                album = value
                            elif not cover:
                                cover = value
                            with self._lock:
                                self._now_playing[mac] = {
                                    "title": title,
                                    "artist": artist,
                                    "album": album,
                                    "cover_art": cover,
                                    "status": "playing",
                                }
                            # Forward to Bluetooth MPRIS/AVRCP if available.
                            if self._bt is not None:
                                try:
                                    self._bt.update_mpris_metadata(mac, title, artist, album)
                                except Exception:  # noqa: BLE001
                                    pass
                            # Reset for next track.
                            if title and artist and album:
                                title = artist = album = cover = ""
            except OSError:
                pass
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("[Shairport] Metadata reader for %s stopped: %s", mac, exc)

        thread = threading.Thread(target=_reader, daemon=True, name=f"metadata-{mac.replace(':', '')}")
        thread.start()
        with self._lock:
            self._metadata_threads[mac] = thread

    def _stop_metadata_reader(self, mac: str) -> None:
        """Stop the metadata reader thread and clean up the pipe."""
        with self._lock:
            self._now_playing.pop(mac, None)
        pipe_path = self._metadata_pipe_path(mac)
        try:
            if os.path.exists(pipe_path):
                os.remove(pipe_path)
        except OSError:
            pass

    def _ensure_dirs(self) -> None:
        os.makedirs(self._conf_dir, exist_ok=True)
        os.makedirs(self._pid_dir, exist_ok=True)

    def _conf_path(self, mac: str) -> str:
        """Config path for a speaker.

        Only a canonical Bluetooth address may reach the filesystem here; any
        other value would let a caller escape ``self._conf_dir``.
        """
        canonical = normalise_mac(mac)
        if not canonical:
            raise ValueError(f"refusing to build a config path for invalid address: {mac!r}")
        return os.path.join(self._conf_dir, f"shairport-{canonical.replace(':', '')}.conf")

    def _port_for(self, index: int) -> int:
        return self._port_base + index * 2

    def generate_conf(self, speaker: Speaker, index: int, sink_name: str | None = None) -> int:
        """Write a shairport-sync.conf for the speaker, return its port.

        If ``sink_name`` is provided, it is written as the PA ``device`` so
        shairport-sync outputs directly to the PipeWire A2DP sink.
        """
        port = self._port_for(index)
        runtime_dir = self._runtime_dir
        safe_name = sanitise_display_name(speaker.name) or "AirPlay Speaker"
        safe_mac = normalise_mac(speaker.mac)
        if not safe_mac:
            raise ValueError(f"refusing to generate a config for invalid address: {speaker.mac!r}")

        conf = f"""# Auto-generated by AirPlay to Bluetooth Bridge
# Speaker: {safe_name} ({safe_mac})
general = {{
    name = "{safe_name}";
    port = {port};
    udp_port_base = {port};
    udp_port_range = 10;
    bitrate = 320;
    ignore_volume_control = "no";
    volume_max_db = "0";
    dbus_service_bus = "session";
    mpris_service_bus = "session";
}};

diagnostics = {{
    log_verbosity = 3;
    statistics = "yes";
}};

alsa = {{
    audio_backend_latency = {self._buffer_seconds};
}};

metadata = {{
    enabled = "yes";
    include_cover_art = "yes";
}};

sessioncontrol = {{
    run_this_before_entering_active_state = "{_HOOK_SCRIPT} \\"{safe_name}\\" \\"{safe_mac}\\" enter";
    run_this_after_exiting_active_state = "{_HOOK_SCRIPT} \\"{safe_name}\\" \\"{safe_mac}\\" exit";
    wait_for_completion = "yes";
}};
"""
        self._ensure_dirs()
        path = self._conf_path(safe_mac)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(conf)

        _LOG.info("[Shairport] Wrote config for %s -> %s (port %d, sink=%s)",
                  safe_name, path, port, sink_name or "default")
        return port

    def start_instance(self, speaker: Speaker, index: int, sink_name: str | None = None) -> bool:
        mac = normalise_mac(speaker.mac)
        if not mac:
            _LOG.error("[Shairport] Refusing to start instance for invalid address %r", speaker.mac)
            return False
        with self._lock:
            existing = self._processes.get(mac)
            if existing and existing.poll() is None:
                _LOG.debug("[Shairport] %s already running", speaker.name)
                return True

            # Crash-loop protection: if this instance has crashed repeatedly,
            # enforce an exponential backoff window before allowing restart.
            backoff_until = self._crash_backoff_until.get(mac, 0)
            if time.time() < backoff_until:
                _LOG.warning("[Shairport] %s in crash backoff, skipping restart", speaker.name)
                return False

            self._speakers[mac] = (speaker, index)

            # Detect the A2DP sink if not explicitly provided.
            if sink_name is None and self._pipewire is not None:
                sink_name = self._pipewire.find_bluetooth_sink(mac)
            if sink_name is None:
                # Check if BT is connected. If so, don't start with auto_null —
                # the sink polling thread will start shairport once the A2DP
                # sink appears. If BT is not connected (manual start), use PA default.
                if self._bt is not None and self._bt.is_device_connected(mac):
                    _LOG.info("[Shairport] BT connected but no A2DP sink for %s — deferring start until sink appears", mac)
                    return False
                _LOG.info("[Shairport] No A2DP sink for %s (BT not connected) — using PA default output", mac)
            else:
                _LOG.info("[Shairport] Using A2DP sink %s for %s", sink_name, mac)

            port = self.generate_conf(speaker, index, sink_name=sink_name)
            conf_path = self._conf_path(mac)
            _LOG.info("[Shairport] Starting shairport-sync for %s: conf=%s port=%d sink=%s",
                      mac, conf_path, port, sink_name or "default")
            try:
                env = os.environ.copy()
                env["PULSE_SERVER"] = f"unix:{self._runtime_dir}/pulse/native"
                env["XDG_RUNTIME_DIR"] = self._runtime_dir
                env["PIPEWIRE_RUNTIME_DIR"] = self._runtime_dir
                env["PULSE_RUNTIME_PATH"] = os.path.join(self._runtime_dir, "pulse")
                env["DBUS_SESSION_BUS_ADDRESS"] = os.environ.get(
                    "DBUS_SESSION_BUS_ADDRESS",
                    f"unix:path={self._runtime_dir}/bus",
                )

                proc = subprocess.Popen(  # noqa: S603
                    [
                        "shairport-sync",
                        "-c", conf_path,
                        "-v",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
                self._processes[mac] = proc
                _LOG.info("[Shairport] Started instance for %s (%s) on port %d pid=%d sink=%s",
                          speaker.name, mac, port, proc.pid, sink_name or "default")
                # Start a background reader thread to log stdout/stderr in real-time.
                reader = threading.Thread(
                    target=self._log_process_output, args=(mac, proc), daemon=True,
                    name=f"shairport-stdout-{mac.replace(':', '')}",
                )
                reader.start()
                # Start metadata pipe reader for Now Playing + MPRIS passthrough.
                self._start_metadata_reader(mac)

                # Post-start health check: wait 2s and verify the process
                # hasn't already exited.  If it crashed, log the exit code.
                def _health_check() -> None:
                    time.sleep(2)
                    rc = proc.poll()
                    if rc is not None:
                        _LOG.error("[Shairport] Process for %s exited immediately (rc=%d) — check config and audio backend",
                                   mac, rc)
                    else:
                        _LOG.info("[Shairport] Process for %s is healthy (pid=%d still running after 2s)",
                                  mac, proc.pid)

                health_thread = threading.Thread(target=_health_check, daemon=True,
                                                  name=f"health-{mac.replace(':', '')}")
                health_thread.start()
                return True
            except FileNotFoundError:
                _LOG.error("[Shairport] shairport-sync binary not found")
                return False
            except OSError as exc:
                _LOG.error("[Shairport] Failed to start instance for %s: %s", speaker.name, exc)
                return False

    def stop_instance(self, mac: str) -> bool:
        mac = normalise_mac(mac)
        if not mac:
            _LOG.error("[Shairport] Refusing to stop instance for invalid address")
            return False
        with self._lock:
            proc = self._processes.get(mac)
            if proc and proc.poll() is None:
                _LOG.info("[Shairport] Stopping instance for %s", mac)
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            elif proc is not None:
                # Process already exited — check if it crashed.
                self._check_crash(mac, proc)
            self._processes.pop(mac, None)
            self._stop_metadata_reader(mac)
            # Keep speaker metadata so a Bluetooth reconnect can restart it.
            conf_path = self._conf_path(mac)
            subprocess.run(  # noqa: S603
                ["pkill", "-f", conf_path],
                capture_output=True,
            )
            return True

    def restart_instance(self, speaker: Speaker, index: int) -> bool:
        self.stop_instance(speaker.mac)
        time.sleep(0.5)
        return self.start_instance(speaker, index)  # noqa: sink auto-detected

    def start_all(self, speakers: list[Speaker]) -> None:
        # Register all saved speakers first, including currently disconnected
        # ones, so the lifecycle monitor can restart them after reconnect.
        with self._lock:
            self._speakers = {
                normalise_mac(spk.mac): (spk, i)
                for i, spk in enumerate(speakers)
                if normalise_mac(spk.mac)
            }
        for i, spk in enumerate(speakers):
            mac = normalise_mac(spk.mac)
            if not mac:
                continue
            if self._bt is not None and not self._bt.is_device_connected(mac):
                continue
            self.start_for_connected(mac) if self._pipewire is not None else self.start_instance(spk, i)

    def stop_all(self) -> None:
        with self._lock:
            for mac in list(self._processes.keys()):
                self.stop_instance(mac)
            subprocess.run(["pkill", "-x", "shairport-sync"], capture_output=True)  # noqa: S603

    def restart_all(self, speakers: list[Speaker]) -> None:
        self.stop_all()
        time.sleep(1)
        self.start_all(speakers)

    def get_status(self) -> list[dict[str, str]]:
        with self._lock:
            result = []
            for mac, proc in self._processes.items():
                running = proc.poll() is None
                result.append({
                    "mac": mac,
                    "pid": str(proc.pid),
                    "running": "yes" if running else "no",
                })
            return result

    def is_running(self, mac: str) -> bool:
        mac = normalise_mac(mac)
        if not mac:
            return False
        with self._lock:
            proc = self._processes.get(mac)
            return proc is not None and proc.poll() is None

    def pid_is_running(self, mac: str) -> bool:
        """Check if the Shairport process for a MAC is actually running (alias for is_running)."""
        return self.is_running(mac)

    # -- lifecycle management ------------------------------------------------

    @staticmethod
    def _log_process_output(mac: str, proc: subprocess.Popen) -> None:
        """Read stdout/stderr from a shairport-sync process and log it.

        Runs in a daemon thread so output appears in the system log in
        real-time, not only when the process exits.
        """
        if proc.stdout is None:
            return
        try:
            for line in iter(proc.stdout.readline, b""):
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                line = line.rstrip()
                if line:
                    _LOG.info("[Shairport %s] %s", mac, line)
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("[Shairport %s] stdout reader stopped: %s", mac, exc)

    def _check_crash(self, mac: str, proc: subprocess.Popen) -> None:
        """Check if a terminated shairport-sync process crashed.

        If the process exited with a non-zero return code, increments the
        crash counter and sets an exponential backoff window. After 3
        consecutive crashes, the instance is blocked from restarting
        until the backoff expires.
        """
        rc = proc.poll()
        if rc is None or rc == 0:
            self._crash_counts.pop(mac, None)
            self._crash_backoff_until.pop(mac, None)
            return

        crashes = self._crash_counts.get(mac, 0) + 1
        self._crash_counts[mac] = crashes

        conf_path = self._conf_path(mac)

        if crashes >= 3:
            backoff = min(300, 30 * (2 ** (crashes - 3)))
            self._crash_backoff_until[mac] = time.time() + backoff
            if crashes <= 5:
                _LOG.error("[Shairport] %s crashed %d times (rc=%d). Backoff %ds. Config: %s",
                          mac, crashes, rc, backoff, conf_path)
            elif crashes == 6:
                _LOG.error("[Shairport] %s crashed %d times — suppressing further logs until BT reconnect. Config: %s",
                          mac, crashes, conf_path)
        else:
            _LOG.warning("[Shairport] %s exited with rc=%d (crash %d/3). Config: %s",
                         mac, rc, crashes, conf_path)

    def start_for_connected(self, mac: str) -> bool:
        """Start Shairport instance if BT is connected, waiting for A2DP sink.

        Called by the AvailabilityMonitor when a device transitions to connected.
        Forces the BlueZ card profile to a2dp_sink, then waits for the A2DP
        sink to appear in PipeWire before starting shairport-sync. This prevents
        audio from routing to the auto_null dummy sink.
        """
        mac = normalise_mac(mac)
        if not mac:
            return False

        with self._lock:
            if self.is_running(mac):
                return True
            if mac in self._starting:
                _LOG.debug("[Shairport] Start already in progress for %s", mac)
                return True
            if mac in self._sink_failed:
                _LOG.debug("[Shairport] Skipping %s — A2DP sink polling failed, will retry after BT reconnect", mac)
                return False
            if self._crash_counts.get(mac, 0) > 5:
                return False
            self._starting.add(mac)
            entry = self._speakers.get(mac)
        if entry is None and self._db is not None:
            speaker = self._db.get(mac)  # type: ignore[attr-defined]
            if speaker is not None:
                saved = self._db.list_speakers()  # type: ignore[attr-defined]
                index = next((i for i, item in enumerate(saved) if item.mac == mac), 0)
                with self._lock:
                    self._speakers[mac] = (speaker, index)
                entry = (speaker, index)
        try:
            if entry is None:
                _LOG.warning("[Shairport] No speaker entry for %s, cannot auto-start", mac)
                return False

            speaker, index = entry

            # Force the BlueZ card profile to a2dp_sink so WirePlumber
            # creates the bluez_sink node. Use retries=1 here — the
            # wait_for_bluetooth_sink polling loop provides additional retries.
            if self._pipewire is not None:
                self._pipewire.set_card_profile(mac, "a2dp_sink", retries=1)

            sink_name = None
            if self._pipewire is not None:
                sink_name = self._pipewire.find_bluetooth_sink(mac)

            if sink_name:
                _LOG.info("[Shairport] BT connected + sink ready (%s) — starting instance for %s", sink_name, mac)
                return self.start_instance(speaker, index, sink_name=sink_name)

            # No sink yet — poll in background, do NOT start with auto_null.
            # The _starting flag stays set until the poll thread finishes.
            _LOG.info("[Shairport] BT connected but no A2DP sink yet — waiting for sink before starting %s", mac)
            thread = threading.Thread(
                target=self._poll_for_sink_and_start, args=(mac,), daemon=True,
                name=f"sink-poll-{mac.replace(':', '')}",
            )
            thread.start()
            return True
        except Exception:
            with self._lock:
                self._starting.discard(mac)
            raise

    def _poll_for_sink_and_start(self, mac: str) -> None:
        """Background thread: poll for A2DP sink, then start Shairport.

        Unlike the old behavior, shairport-sync is NOT started with the PA
        default sink while waiting. It only starts once the real A2DP sink
        appears, preventing audio from routing to auto_null.

        The ``_starting`` flag is held for the entire duration of the poll
        to prevent concurrent start attempts.
        """
        if self._pipewire is None:
            with self._lock:
                self._starting.discard(mac)
            return

        def on_sink_found(sink_name: str) -> None:
            _LOG.info("[Shairport] A2DP sink %s appeared for %s — starting instance", sink_name, mac)
            with self._lock:
                entry = self._speakers.get(mac)
                self._crash_counts.pop(mac, None)
                self._crash_backoff_until.pop(mac, None)
                # If an instance is already running with the wrong sink, stop it
                # so it can be restarted with the dedicated BT sink.
                old_proc = self._processes.get(mac)
            if old_proc is not None and old_proc.poll() is None:
                _LOG.info("[Shairport] Stopping existing instance for %s to reload with sink %s", mac, sink_name)
                self.stop_instance(mac)
                time.sleep(0.5)
            if entry is None:
                _LOG.warning("[Shairport] Cannot start %s — no speaker entry", mac)
                return
            speaker, index = entry
            self.start_instance(speaker, index, sink_name=sink_name)
            # Start AVRCP backchannel listener if not already active.
            if self._bt is not None and not self._avrcp_listening:
                try:
                    self._bt.start_avrcp_listener(self)
                    self._avrcp_listening = True
                except Exception as exc:  # noqa: BLE001
                    _LOG.debug("[Shairport] AVRCP listener start failed: %s", exc)

        try:
            sink = self._pipewire.wait_for_bluetooth_sink(mac, timeout=45.0, interval=1.0, on_found=on_sink_found)
            if sink is None:
                # Retry profile switch once more before giving up.
                _LOG.info("[Shairport] No sink after 45s — retrying profile switch for %s", mac)
                if self._pipewire is not None:
                    self._pipewire.set_card_profile(mac, "a2dp_sink", retries=2)
                sink = self._pipewire.wait_for_bluetooth_sink(mac, timeout=15.0, interval=1.0, on_found=on_sink_found)
            if sink is None:
                with self._lock:
                    self._sink_failed.add(mac)
                _LOG.warning("[Shairport] A2DP sink for %s never appeared — deferring until BT reconnect", mac)
        finally:
            with self._lock:
                self._starting.discard(mac)

    _DISCONNECT_DEBOUNCE = 5  # seconds to wait before acting on a BT disconnect

    def stop_for_disconnected(self, mac: str) -> bool:
        """Stop Shairport instance when BT disconnects.

        Uses a debounce: waits ``_DISCONNECT_DEBOUNCE`` seconds, then re-checks
        whether BT is still disconnected. This prevents AirPlay from being
        killed during transient BT link drops (which are normal during
        pairing, profile switches, and Echo reconnections).
        """
        mac = normalise_mac(mac)
        if not mac:
            return False

        if not self.is_running(mac):
            with self._lock:
                self._crash_counts.pop(mac, None)
                self._crash_backoff_until.pop(mac, None)
                self._starting.discard(mac)
                self._sink_failed.discard(mac)
                self._now_playing.pop(mac, None)
            return True

        _LOG.info("[Shairport] BT disconnect detected for %s — waiting %ds before stopping (debounce)",
                   mac, self._DISCONNECT_DEBOUNCE)

        def _debounced_stop() -> None:
            time.sleep(self._DISCONNECT_DEBOUNCE)
            # Re-check: is BT still disconnected?
            if self._bt is not None and self._bt.is_device_connected(mac):
                _LOG.info("[Shairport] BT reconnected within debounce for %s — keeping AirPlay running", mac)
                return
            if self._bt is not None:
                status = self._bt.get_device_status(mac)
                if status.get("connected") == "yes":
                    _LOG.info("[Shairport] BT reconnected (status check) for %s — keeping AirPlay running", mac)
                    return
            # BT is still gone — stop AirPlay.
            with self._lock:
                self._crash_counts.pop(mac, None)
                self._crash_backoff_until.pop(mac, None)
                self._starting.discard(mac)
                self._sink_failed.discard(mac)
                self._now_playing.pop(mac, None)
            if self.is_running(mac):
                _LOG.info("[Shairport] BT still disconnected after debounce — stopping instance for %s", mac)
                self.stop_instance(mac)

        thread = threading.Thread(target=_debounced_stop, daemon=True,
                                  name=f"debounce-stop-{mac.replace(':', '')}")
        thread.start()
        return True

    def handle_avrcp_command(self, command: str, mac: str | None = None) -> bool:
        """Handle an AVRCP control command from the Echo Show backchannel.

        Forwards Next/Previous/Play/Pause commands to shairport-sync via D-Bus.
        Shairport-sync exposes a D-Bus interface at org.gnome.ShairportSync
        on the session bus when built with --with-dbus-interface.
        """
        command = command.lower().strip()
        _LOG.info("[Shairport] AVRCP command received: %s (mac=%s)", command, mac or "all")
        if command not in ("next", "previous", "play", "pause", "stop"):
            _LOG.debug("[Shairport] Unknown AVRCP command: %s", command)
            return False
        # Map AVRCP commands to shairport-sync D-Bus method calls.
        method_map = {
            "next": "Next",
            "previous": "Previous",
            "play": "Play",
            "pause": "Pause",
            "stop": "Stop",
        }
        method = method_map.get(command)
        if not method:
            return False
        # Try to send the command via shairport-sync's D-Bus interface.
        try:
            import asyncio
            from dbus_next import Message, MessageType, BusType  # type: ignore[import-untyped]
            from dbus_next.aio import MessageBus  # type: ignore[import-untyped]

            async def _send() -> bool:
                try:
                    bus = await MessageBus(bus_type=BusType.SESSION).connect()
                    reply = await bus.call(
                        Message(
                            destination="org.gnome.ShairportSync",
                            path="/",
                            interface="org.gnome.ShairportSync",
                            member=method,
                        )
                    )
                    return reply.message_type != MessageType.ERROR
                except Exception:  # noqa: BLE001
                    return False

            try:
                loop = asyncio.get_event_loop()
                fut = asyncio.run_coroutine_threadsafe(_send(), loop)
                return fut.result(timeout=3)
            except RuntimeError:
                # No running loop — create a temporary one.
                return asyncio.run(_send())
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("[Shairport] D-Bus command %s failed: %s", method, exc)
            return False

    def start_monitor(self) -> None:
        """Start the background lifecycle monitor thread."""
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="shairport-monitor"
        )
        self._monitor_thread.start()
        _LOG.info("[Shairport] Lifecycle monitor started (interval=%ds)", _MONITOR_INTERVAL)

    def stop_monitor(self) -> None:
        """Stop the background lifecycle monitor thread."""
        self._monitor_stop.set()
        self._monitor_pause.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5)
            self._monitor_thread = None
        _LOG.info("[Shairport] Lifecycle monitor stopped")

    def pause_monitor(self) -> None:
        """Pause the lifecycle monitor during manual operations."""
        self._monitor_pause.clear()
        _LOG.info("[Shairport] Lifecycle monitor paused")

    def resume_monitor(self) -> None:
        """Resume the lifecycle monitor after manual operations."""
        self._monitor_pause.set()
        _LOG.info("[Shairport] Lifecycle monitor resumed")

    def _monitor_loop(self) -> None:
        """Background loop that syncs Shairport lifecycle with BT connection state."""
        while not self._monitor_stop.is_set():
            self._monitor_pause.wait()
            if self._monitor_stop.is_set():
                break
            try:
                self._check_all_lifecycles()
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("[Shairport] Monitor loop error: %s", exc)
            self._monitor_stop.wait(_MONITOR_INTERVAL)

    def _check_all_lifecycles(self) -> None:
        """Check each managed speaker for crashed shairport-sync processes.

        BT connection state transitions are handled exclusively by the
        AvailabilityMonitor, which calls start_for_connected/stop_for_disconnected.
        This loop only detects crashed processes so they can be cleaned up
        and crash counters updated — it does NOT start/stop based on BT state.
        """
        if self._bt is None:
            return

        with self._lock:
            managed_macs = list(self._speakers.keys())
        if self._db is not None:
            for speaker in self._db.list_speakers():  # type: ignore[attr-defined]
                mac = normalise_mac(speaker.mac)
                if mac and mac not in managed_macs:
                    with self._lock:
                        saved_speakers = self._db.list_speakers()  # type: ignore[attr-defined]
                    index = next((i for i, item in enumerate(saved_speakers) if item.mac == mac), 0)
                    self._speakers[mac] = (speaker, index)
                    managed_macs.append(mac)

        for mac in managed_macs:
            with self._lock:
                old_proc = self._processes.get(mac)
            if old_proc is not None and old_proc.poll() is not None:
                _LOG.info("[Shairport] Detected crashed process for %s", mac)
                self._check_crash(mac, old_proc)
                # If BT is still connected, attempt restart.
                if self._bt.is_device_connected(mac):
                    self.start_for_connected(mac)
