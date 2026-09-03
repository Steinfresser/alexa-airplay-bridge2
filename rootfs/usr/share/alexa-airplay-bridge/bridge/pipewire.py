"""PipeWire / PulseAudio daemon management and A2DP sink routing."""

from __future__ import annotations

import logging
import math
import os
import struct
import subprocess
import tempfile
import threading
import time
from typing import Callable, Optional

_LOG = logging.getLogger(__name__)

# Path to the ALSA test sound file; may not exist in minimal containers.
_TEST_WAV_PATH = "/usr/share/sounds/alsa/test.wav"


class PipeWireManager:
    """Starts and supervises PipeWire + WirePlumber + pipewire-pulse."""

    def __init__(self, runtime_dir: str) -> None:
        self._runtime_dir = runtime_dir
        self._lock = threading.RLock()
        self._pipewire_proc: Optional[subprocess.Popen] = None
        self._pulse_proc: Optional[subprocess.Popen] = None
        self._wireplumber_proc: Optional[subprocess.Popen] = None
        self._keepalive_procs: dict[str, subprocess.Popen] = {}

    @property
    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["XDG_RUNTIME_DIR"] = self._runtime_dir
        env["PIPEWIRE_RUNTIME_DIR"] = self._runtime_dir
        env["PULSE_RUNTIME_PATH"] = os.path.join(self._runtime_dir, "pulse")
        env["DBUS_SESSION_BUS_ADDRESS"] = os.environ.get(
            "DBUS_SESSION_BUS_ADDRESS",
            f"unix:path={self._runtime_dir}/bus",
        )
        return env

    def start(self) -> bool:
        with self._lock:
            os.makedirs(self._runtime_dir, exist_ok=True)
            os.makedirs(os.path.join(self._runtime_dir, "pulse"), exist_ok=True)

            log_fd = self._open_daemon_log()
            if not self._is_running("pipewire"):
                _LOG.info("[PipeWire] Starting pipewire daemon")
                self._pipewire_proc = subprocess.Popen(  # noqa: S603
                    ["pipewire"],
                    stdout=log_fd, stderr=log_fd,
                    env=self.env,
                )
            if not self._is_running("wireplumber"):
                _LOG.info("[PipeWire] Starting wireplumber")
                self._wireplumber_proc = subprocess.Popen(  # noqa: S603
                    ["wireplumber"],
                    stdout=log_fd, stderr=log_fd,
                    env=self.env,
                )
            if not self._is_running("pipewire-pulse"):
                _LOG.info("[PipeWire] Starting pipewire-pulse")
                self._pulse_proc = subprocess.Popen(  # noqa: S603
                    ["pipewire-pulse"],
                    stdout=log_fd, stderr=log_fd,
                    env=self.env,
                )

            time.sleep(1.5)
            ok = self._is_running("pipewire") and self._is_running("pipewire-pulse")
            if ok:
                _LOG.info("[PipeWire] Daemons started successfully")
            else:
                _LOG.error("[PipeWire] Failed to start all daemons")
            return ok

    def _open_daemon_log(self) -> int:
        """Open a log file for PipeWire/WirePlumber daemon output.

        Using a file (not subprocess.PIPE) is critical: if stderr goes to a
        pipe that nobody reads, the 64KB kernel pipe buffer fills up and the
        daemon blocks on write — freezing all audio processing.
        """
        log_path = os.path.join(self._runtime_dir, "pipewire-daemons.log")
        try:
            return os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        except OSError:
            return subprocess.DEVNULL  # type: ignore[return-value]

    def dump_diagnostics(self) -> str:
        """Dump PipeWire/WirePlumber state for debugging sink issues."""
        lines: list[str] = []
        for cmd in (
            ["pactl", "list", "sinks", "short"],
            ["pactl", "list", "cards", "short"],
            ["pactl", "get-default-sink"],
            ["pw-cli", "list-objects", "Node"],
        ):
            code, out = self._run_cmd_env(cmd, timeout=5)
            lines.append(f"$ {' '.join(cmd)} (rc={code})")
            lines.append(out.strip()[:500])
            lines.append("")
        # Tail the daemon log for recent WirePlumber errors.
        log_path = os.path.join(self._runtime_dir, "pipewire-daemons.log")
        if os.path.isfile(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                # Last 2000 chars.
                if len(content) > 2000:
                    content = content[-2000:]
                lines.append("--- pipewire-daemons.log (tail) ---")
                lines.append(content)
            except OSError:
                pass
        return "\n".join(lines)

    def stop(self) -> None:
        self.stop_all_keepalives()
        with self._lock:
            for name, proc in (
                ("pipewire-pulse", self._pulse_proc),
                ("wireplumber", self._wireplumber_proc),
                ("pipewire", self._pipewire_proc),
            ):
                if proc and proc.poll() is None:
                    _LOG.info("[PipeWire] Stopping %s", name)
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            self._kill_all("pipewire")
            self._kill_all("pipewire-pulse")
            self._kill_all("wireplumber")
            self._pipewire_proc = None
            self._pulse_proc = None
            self._wireplumber_proc = None

    def restart(self) -> bool:
        self.stop()
        time.sleep(1)
        return self.start()

    @staticmethod
    def _is_running(name: str) -> bool:
        code, _ = PipeWireManager._run_cmd(["pgrep", "-x", name])
        return code == 0

    @staticmethod
    def _kill_all(name: str) -> None:
        subprocess.run(["pkill", "-x", name], capture_output=True)  # noqa: S603

    @staticmethod
    def _run_cmd(cmd: list[str], timeout: int = 8) -> tuple[int, str]:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)  # noqa: S603
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:  # noqa: BLE001
            return 1, str(exc)

    def _run_cmd_env(self, cmd: list[str], timeout: int = 8, env: dict[str, str] | None = None) -> tuple[int, str]:
        """Run a command with PipeWire/PulseAudio env vars."""
        run_env = env if env else self.env
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=run_env)  # noqa: S603
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:  # noqa: BLE001
            return 1, str(exc)

    # -- sink inspection ------------------------------------------------------

    def list_sinks(self) -> str:
        code, out = self._run_cmd_env(["pactl", "list", "sinks", "short"])
        if code != 0:
            _LOG.debug("[PipeWire] pactl list sinks with env failed: %s", out.strip()[:200])
            code, out = self._run_cmd(["pactl", "list", "sinks", "short"])
        return out

    def list_cards(self) -> str:
        code, out = self._run_cmd_env(["pactl", "list", "cards", "short"])
        if code != 0:
            code, out = self._run_cmd(["pactl", "list", "cards", "short"])
        return out

    def set_card_profile(self, mac: str, profile: str = "a2dp_sink", retries: int = 5) -> bool:
        """Set the BlueZ card profile to A2DP sink so WirePlumber creates the sink node.

        After a Bluetooth device connects, the card may not appear immediately
        in pactl. WirePlumber only creates the ``bluez_sink.*.a2dp_sink``
        node when the card profile is set to ``a2dp_sink``. This method retries
        up to ``retries`` times with a 1s delay, giving WirePlumber time to
        register the card after BlueZ completes the connection.

        Some BlueZ/PipeWire versions use different profile names, so we try
        ``a2dp_sink`` first, then ``a2dp``, then ``headset_head_unit``.
        """
        mac_clean = mac.upper().replace(":", "_")
        profile_candidates = [profile, "a2dp", "headset_head_unit"]
        for attempt in range(1, retries + 1):
            cards_out = self.list_cards()
            card_name = None
            for line in cards_out.splitlines():
                line_lower = line.lower()
                if mac_clean.lower() in line_lower or mac.upper().lower() in line_lower:
                    parts = line.strip().split()
                    if parts:
                        card_name = parts[1] if len(parts) >= 2 else parts[0]
                        break
            if card_name is None:
                _LOG.debug("[PipeWire] No card found for %s (attempt %d/%d)", mac, attempt, retries)
                if attempt < retries:
                    time.sleep(1.0)
                continue
            for prof in profile_candidates:
                code, out = self._run_cmd_env(["pactl", "set-card-profile", card_name, prof])
                if code == 0:
                    _LOG.info("[PipeWire] Set card profile %s -> %s (attempt %d)", card_name, prof, attempt)
                    return True
            _LOG.debug("[PipeWire] set-card-profile %s failed (attempt %d): %s",
                       card_name, attempt, out.strip()[:200])
            if attempt < retries:
                time.sleep(1.0)
        if retries <= 1:
            _LOG.debug("[PipeWire] No card/profile for %s (single attempt, will retry in polling loop)", mac)
        else:
            _LOG.warning("[PipeWire] Could not set card profile for %s after %d attempts", mac, retries)
        return False

    def list_playback(self) -> str:
        code, out = self._run_cmd_env(["pactl", "list", "playback"])
        if code != 0:
            code, out = self._run_cmd(["pactl", "list", "playback"])
        return out

    def get_status(self) -> dict[str, str]:
        return {
            "pipewire_running": "yes" if self._is_running("pipewire") else "no",
            "pipewire_pulse_running": "yes" if self._is_running("pipewire-pulse") else "no",
            "wireplumber_running": "yes" if self._is_running("wireplumber") else "no",
            "runtime_dir": self._runtime_dir,
        }

    def get_volume(self) -> Optional[int]:
        """Return the default sink volume as a percentage, or None."""
        code, out = self._run_cmd_env(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
        if code == 0:
            for line in out.splitlines():
                if "volume:" in line.lower():
                    for part in line.split():
                        if part.endswith("%"):
                            try:
                                return int(part.rstrip("%"))
                            except ValueError:
                                continue
        return None

    def get_mute_state(self) -> Optional[bool]:
        """Return True if the default sink is muted, False if not, None if unknown."""
        code, out = self._run_cmd_env(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
        if code == 0:
            return "yes" in out.lower()
        return None

    def set_default_sink(self, sink_name: str) -> bool:
        """Set the PipeWire/PulseAudio default sink to the given A2DP sink.

        Also unmutes and sets volume to 100% — PipeWire sometimes creates
        Bluetooth A2DP sinks muted or at 0 volume.
        """
        code, out = self._run_cmd_env(["pactl", "set-default-sink", sink_name])
        if code != 0:
            _LOG.warning("[PipeWire] set-default-sink %s failed: %s", sink_name, out)
        else:
            _LOG.info("[PipeWire] Default sink set to %s", sink_name)
        # Unmute and set volume to 100%.
        self._run_cmd_env(["pactl", "set-sink-mute", sink_name, "0"])
        self._run_cmd_env(["pactl", "set-sink-volume", sink_name, "100%"])
        vol_code, vol_out = self._run_cmd_env(["pactl", "get-sink-volume", sink_name])
        if vol_code == 0:
            _LOG.info("[PipeWire] Sink %s unmuted, volume: %s", sink_name,
                      " ".join(l.strip() for l in vol_out.splitlines() if "volume" in l.lower()))
        return code == 0

    def route_to_sink(self, sink_name: str) -> bool:
        """Move all active playback streams to the given A2DP sink."""
        return self.set_default_sink(sink_name)

    def wait_for_bluetooth_sink(self, mac: str, timeout: float = 30.0, interval: float = 1.0, on_found: Optional[Callable[[str], None]] = None) -> Optional[str]:
        """Poll for a Bluetooth A2DP sink to appear in PipeWire.

        After a Bluetooth device connects, PipeWire/WirePlumber needs several
        seconds to negotiate the A2DP profile and create the sink node. This
        method polls every ``interval`` seconds for up to ``timeout`` seconds.

        If the BlueZ card exists but the sink node hasn't appeared after
        half the timeout, a forced Disconnect/Connect cycle is triggered to
        release any stale transport held by a competing sound server and let
        WirePlumber acquire it cleanly.

        If ``on_found`` is provided, it is called with the sink name as soon
        as the sink is discovered — typically used to regenerate the Shairport
        config and restart the instance.
        """
        deadline = time.time() + timeout
        attempt = 0
        forced_reconnect = False
        while time.time() < deadline:
            attempt += 1
            sink = self.find_bluetooth_sink(mac)
            if sink:
                _LOG.info("[PipeWire] A2DP sink %s appeared after %d attempt(s) for %s", sink, attempt, mac)
                self.set_default_sink(sink)
                self._start_sink_keepalive(sink, mac)
                _LOG.info("[PipeWire] Waiting 2s for A2DP transport to settle for %s", mac)
                time.sleep(2.0)
                self._force_transport(sink)
                if on_found is not None:
                    try:
                        on_found(sink)
                    except Exception as exc:  # noqa: BLE001
                        _LOG.warning("[PipeWire] on_found callback error: %s", exc)
                return sink
            self.set_card_profile(mac, "a2dp_sink", retries=1)

            # Recovery: if we're past half the timeout and the card exists
            # but no sink has appeared, force a disconnect/reconnect to
            # release a stale transport that a competing sound server may
            # be holding.
            elapsed = time.time() - (deadline - timeout)
            if not forced_reconnect and elapsed > timeout * 0.5:
                cards_out = self.list_cards()
                mac_clean = mac.upper().replace(":", "_")
                card_exists = any(mac_clean.lower() in line.lower() for line in cards_out.splitlines())
                if card_exists:
                    _LOG.warning("[PipeWire] Card exists but no sink after %.0fs — forcing BT reconnect for %s", elapsed, mac)
                    forced_reconnect = True
                    self._force_bt_reconnect(mac)
                    continue

            remaining = deadline - time.time()
            if remaining > interval:
                time.sleep(interval)
        _LOG.warning("[PipeWire] A2DP sink for %s did not appear within %.1fs", mac, timeout)
        _LOG.warning("[PipeWire] Diagnostics for %s:\n%s", mac, self.dump_diagnostics())
        return None

    def _force_bt_reconnect(self, mac: str) -> None:
        """Force a Bluetooth disconnect/reconnect to release stale A2DP transport.

        When the BlueZ card exists but no sink node appears, the A2DP transport
        is often held by a stale connection or a competing sound server. A full
        Disconnect/Connect cycle on the BlueZ device releases the transport and
        gives WirePlumber a clean chance to acquire it.
        """
        _LOG.info("[PipeWire] Forcing BT disconnect/reconnect for %s", mac)
        self._run_cmd_env(["bluetoothctl", "disconnect", mac], timeout=10)
        time.sleep(2.0)
        self._run_cmd_env(["bluetoothctl", "connect", mac], timeout=15)
        time.sleep(2.0)
        self.set_card_profile(mac, "a2dp_sink", retries=3)

    def find_bluetooth_sink(self, mac: str) -> Optional[str]:
        """Find the PipeWire/Pulse sink name for a connected Bluetooth MAC.

        Matches any sink line containing the MAC address in any common format:
        - ``08_12_A5_72_A6_A9`` (underscore, upper or lower)
        - ``08.12.A5.72.A6.A9`` (dot, upper or lower)
        - ``08:12:A5:72:A6:A9`` (colon, upper or lower)
        Falls back to ``pw-cli`` introspection if ``pactl`` yields nothing.
        """
        mac_underscore = mac.upper().replace(":", "_")
        mac_dots = mac.upper().replace(":", ".")
        mac_colon = mac.upper()
        candidates = [
            mac_underscore, mac_underscore.lower(),
            mac_dots, mac_dots.lower(),
            mac_colon, mac_colon.lower(),
        ]

        out = self.list_sinks()
        _LOG.debug("[PipeWire] Searching for BT sink for %s in %d sink line(s)", mac, len(out.splitlines()))
        for line in out.splitlines():
            line_stripped = line.strip()
            if not line_stripped:
                continue
            # Case-insensitive match by MAC address in any format.
            line_lower = line_stripped.lower()
            for cand in candidates:
                if cand and cand.lower() in line_lower:
                    parts = line_stripped.split()
                    if len(parts) >= 2:
                        sink_name = parts[1]
                        _LOG.debug("[PipeWire] Found sink for %s: %s", mac, sink_name)
                        return sink_name
                    break

        # Fallback: pw-cli list-objects Node
        code, pw_out = self._run_cmd_env(["pw-cli", "list-objects", "Node"])
        if code == 0:
            for line in pw_out.splitlines():
                line_lower = line.lower()
                for cand in candidates:
                    if cand and cand.lower() in line_lower and "bluez" in line_lower:
                        # pw-cli list-objects output format: "  id  type  name"
                        parts = line.split()
                        if len(parts) >= 3:
                            sink_name = parts[-1]
                            _LOG.debug("[PipeWire] pw-cli found sink for %s: %s", mac, sink_name)
                            return sink_name

        # Second fallback: pw-cli dump short
        code, pw_out = self._run_cmd_env(["pw-cli", "dump", "short"])
        if code == 0:
            for line in pw_out.splitlines():
                line_lower = line.lower()
                for cand in candidates:
                    if cand and cand.lower() in line_lower and "bluez" in line_lower:
                        parts = line.split()
                        if len(parts) >= 2:
                            _LOG.debug("[PipeWire] pw-cli dump found sink for %s: %s", mac, parts[1])
                            return parts[1]

        _LOG.debug("[PipeWire] No A2DP sink found for %s", mac)
        return None

    def get_default_sink(self) -> Optional[str]:
        """Return the current default sink name, or None."""
        code, out = self._run_cmd_env(["pactl", "get-default-sink"])
        if code == 0:
            name = out.strip()
            if name:
                return name
        return None

    def _generate_test_wav(self, path: str, duration: float = 3.0, freq: float = 440.0, sample_rate: int = 44100) -> bool:
        """Generate a short sine-wave WAV file at ``path``.

        Used as a fallback when the ALSA test sound file is missing.
        """
        try:
            num_samples = int(duration * sample_rate)
            with open(path, "wb") as f:
                # WAV header (44 bytes)
                data_size = num_samples * 2  # 16-bit mono
                f.write(b"RIFF")
                f.write(struct.pack("<I", 36 + data_size))
                f.write(b"WAVE")
                f.write(b"fmt ")
                f.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16))
                f.write(b"data")
                f.write(struct.pack("<I", data_size))
                for i in range(num_samples):
                    sample = int(32767 * 0.5 * math.sin(2 * math.pi * freq * i / sample_rate))
                    f.write(struct.pack("<h", sample))
            return True
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("[PipeWire] Failed to generate test WAV: %s", exc)
            return False

    def _get_test_wav(self, duration: float = 3.0) -> str:
        """Return a path to a test WAV file, generating one if the stock file is missing."""
        if os.path.isfile(_TEST_WAV_PATH):
            return _TEST_WAV_PATH
        # Generate a temporary WAV file.
        tmp_path = os.path.join(tempfile.gettempdir(), "bridge_test_tone.wav")
        if os.path.isfile(tmp_path):
            return tmp_path
        if self._generate_test_wav(tmp_path, duration=duration):
            _LOG.info("[PipeWire] Generated test tone at %s", tmp_path)
            return tmp_path
        return _TEST_WAV_PATH  # last resort — let the player report the error

    def _start_sink_keepalive(self, sink: str, mac: str) -> None:
        """Start a background process that holds the A2DP sink open.

        WirePlumber suspends and eventually removes idle Bluetooth nodes.
        A silent pacat stream keeps the sink active so shairport-sync can
        write audio to it at any time without the node vanishing.
        """
        self._stop_sink_keepalive(mac)
        try:
            devzero = open("/dev/zero", "rb")  # noqa: SIM115, PTH123
            proc = subprocess.Popen(  # noqa: S603
                ["pacat", "--playback", "--device", sink,
                 "--rate=44100", "--channels=2", "--format=s16le",
                 "--volume=0", "--latency-msec=1000"],
                stdin=devzero,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=self.env,
            )
            self._keepalive_procs[mac] = proc

            def _monitor_keepalive(p: subprocess.Popen, m: str, s: str, fh: object) -> None:
                rc = p.wait()
                stderr_out = ""
                try:
                    stderr_out = p.stderr.read().decode("utf-8", errors="replace")[:500] if p.stderr else ""
                except Exception:  # noqa: BLE001
                    pass
                try:
                    fh.close()  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    pass
                if rc != 0:
                    _LOG.warning("[PipeWire] Sink keepalive for %s exited (rc=%d, sink=%s): %s",
                                 m, rc, s, stderr_out)
                else:
                    _LOG.info("[PipeWire] Sink keepalive for %s ended normally", m)

            threading.Thread(
                target=_monitor_keepalive, args=(proc, mac, sink, devzero),
                daemon=True, name=f"keepalive-mon-{mac.replace(':', '')}",
            ).start()

            _LOG.info("[PipeWire] Sink keepalive started for %s (pid=%d, sink=%s)", mac, proc.pid, sink)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("[PipeWire] Failed to start sink keepalive for %s: %s", mac, exc)

    def _stop_sink_keepalive(self, mac: str) -> None:
        """Stop the keepalive process for a speaker."""
        proc = self._keepalive_procs.pop(mac, None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            _LOG.info("[PipeWire] Sink keepalive stopped for %s", mac)

    def stop_all_keepalives(self) -> None:
        """Stop all keepalive processes (called during shutdown)."""
        for mac in list(self._keepalive_procs):
            self._stop_sink_keepalive(mac)

    def _force_transport(self, sink: str) -> None:
        """Force the A2DP transport to (re)acquire by suspending and resuming the sink.

        PipeWire can leave a Bluetooth A2DP sink in SUSPENDED state where the
        transport isn't actually acquired. Audio written to the sink disappears
        silently. A suspend/resume cycle forces WirePlumber to re-acquire the
        Bluetooth transport before we attempt to play audio.
        """
        self._run_cmd_env(["pactl", "suspend-sink", sink, "1"])
        time.sleep(0.5)
        self._run_cmd_env(["pactl", "suspend-sink", sink, "0"])
        time.sleep(1.0)

    def play_test_sound(self, mac: str) -> bool:
        """Play a short test tone to the Bluetooth sink (or default sink).

        Forces the A2DP transport to (re)acquire before playing, since
        PipeWire can leave Bluetooth sinks in SUSPENDED state where audio
        is silently discarded. Uses a 5-second tone to give the transport
        time to settle after the suspend/resume cycle.
        """
        sink = self.find_bluetooth_sink(mac)
        if sink is None:
            _LOG.warning("[PipeWire] BT sink not found for %s — dumping diagnostics", mac)
            _LOG.warning("[PipeWire] Diagnostics:\n%s", self.dump_diagnostics())
            sink = self.get_default_sink()
        if sink is None or sink == "auto_null":
            _LOG.error("[PipeWire] No usable sink for test sound to %s (sink=%s). "
                       "The A2DP node has likely been removed by WirePlumber.",
                       mac, sink)
            return False

        # Force the A2DP transport to acquire before doing anything else.
        _LOG.info("[PipeWire] Forcing A2DP transport for sink %s", sink)
        self._force_transport(sink)

        # Unmute and set volume to 100% before playing.
        self._run_cmd_env(["pactl", "set-sink-mute", sink, "0"])
        self._run_cmd_env(["pactl", "set-sink-volume", sink, "100%"])

        # Log current volume/mute state for diagnostics.
        _, vol_out = self._run_cmd_env(["pactl", "get-sink-volume", sink])
        _, mute_out = self._run_cmd_env(["pactl", "get-sink-mute", sink])
        _LOG.info("[PipeWire] Sink %s volume: %s | mute: %s", sink,
                  " ".join(l.strip() for l in vol_out.splitlines() if "volume" in l.lower()),
                  " ".join(l.strip() for l in mute_out.splitlines() if "mute" in l.lower()))

        # Generate a 5-second tone — the first 1-2 seconds may be consumed by
        # the A2DP transport handshake, so a longer tone ensures audible output.
        test_file = self._get_test_wav(duration=5.0)
        pulse_server = f"unix:{self._runtime_dir}/pulse/native"

        _LOG.info("[PipeWire] Playing test sound to %s (sink=%s, file=%s)", mac, sink, test_file)

        pw_env = self.env
        pw_env["PULSE_SERVER"] = pulse_server
        pw_env["PIPEWIRE_NODE"] = sink

        # Log sink state right before playback.
        code_state, state_out = self._run_cmd_env(
            ["pactl", "list", "sinks", "short"], env=pw_env)
        _LOG.info("[PipeWire] Sinks before test tone (rc=%d):\n%s", code_state, state_out.strip())

        attempts = [
            ["paplay", "--server", pulse_server, "--device", sink, test_file],
            ["paplay", "--device", sink, test_file],
            ["pw-play", "--target", sink, test_file],
            ["aplay", "-D", "default", test_file],
        ]

        for cmd in attempts:
            _LOG.info("[PipeWire] Test tone attempt: %s", " ".join(cmd))
            code, out = self._run_cmd_env(cmd, timeout=10, env=pw_env)
            if code == 0:
                _LOG.info("[PipeWire] Test sound played to %s via %s (cmd=%s)", mac, sink, cmd[0])
                return True
            _LOG.warning("[PipeWire] Test sound attempt failed (rc=%d, cmd=%s): %s",
                         code, cmd[0], out.strip()[:300])

        _LOG.error("[PipeWire] All test sound attempts failed for %s (sink=%s)", mac, sink)
        return False


