"""Bluetooth manager: native BlueZ D-Bus agent, scanning, pairing, connecting.

Uses ``dbus-next`` for all BlueZ interactions — no ``bluetoothctl`` subprocess
calls for agent management. A persistent ``org.bluez.Agent1`` agent is
registered at startup with capability ``NoInputNoOutput`` to auto-accept
incoming pairing requests.

Key design decisions:
- Scanning is done via ``org.bluez.Adapter1.StartDiscovery`` + polling
  ``org.bluez.Device1`` properties on the managed objects.
- ``Pair()`` and ``Connect()`` always call ``StopDiscovery()`` first, because
  an active scan interferes with BlueZ link-key establishment.
- ``Trusted`` is set to ``True`` on the Device1 object via D-Bus before
  connecting.
- The A2DP sink UUID is connected explicitly after pairing.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

_LOG = logging.getLogger(__name__)

_MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
_DISCOVERABLE_DURATION = 180  # 3 minutes
_A2DP_UUID = "0000110b-0000-1000-8000-00805f9b34fb"

# BlueZ D-Bus constants
_BLUEZ_BUS = "org.bluez"
_BLUEZ_PATH = "/org/bluez"
_AGENT_PATH = "/org/bluez/agent"
_AGENT_IFACE = "org.bluez.Agent1"
_ADAPTER_IFACE = "org.bluez.Adapter1"
_DEVICE_IFACE = "org.bluez.Device1"
_PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
_OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
_AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"


@dataclass
class DiscoveredDevice:
    mac: str
    name: str
    rssi: int = 0
    paired: bool = False
    trusted: bool = False
    connected: bool = False


@dataclass
class AdapterStatus:
    powered: bool = False
    address: str = ""
    name: str = ""
    alias: str = ""
    discoverable: bool = False
    discovering: bool = False
    available: bool = False


@dataclass
class BtEvent:
    """A Bluetooth handshake event surfaced in the diagnostics tab."""

    timestamp: str
    mac: str
    action: str  # connect / disconnect / pair / trust / error
    success: bool
    detail: str = ""
    error_code: str = ""


# ---------------------------------------------------------------------------
# D-Bus imports (deferred so the module loads even if dbus-next is absent)
# ---------------------------------------------------------------------------
_dbus_ok = False
try:
    from dbus_next import Message, MessageType, Variant, BusType  # type: ignore[import-untyped]
    from dbus_next.aio import MessageBus  # type: ignore[import-untyped]
    from dbus_next.constants import PropertyAccess  # type: ignore[import-untyped]
    from dbus_next.service import ServiceInterface, method  # type: ignore[import-untyped]
    _dbus_ok = True
except Exception:  # noqa: BLE001
    ServiceInterface = object  # type: ignore[assignment,misc]

    def method(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return lambda func: func

    _LOG.warning("[BT] dbus-next not available, falling back to bluetoothctl")


def _unwrap(val: object) -> object:
    """Safely unwrap a dbus-next Variant to its inner value."""
    if hasattr(val, "value"):
        return val.value
    return val


def _mac_to_path(mac: str) -> str:
    """Convert a MAC address to a BlueZ device object path on hci0."""
    return f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"


def _path_to_mac(path: str) -> str:
    """Extract a MAC address from a BlueZ device object path."""
    m = _MAC_RE.search(path)
    return m.group(1).upper() if m else ""


# ---------------------------------------------------------------------------
# Native BlueZ D-Bus Agent
# ---------------------------------------------------------------------------
class BluezAgent:
    """Native ``org.bluez.Agent1`` implementation that auto-accepts all pairing.

    All callbacks return success immediately without throwing D-Bus errors,
    so incoming pairing requests from Echo devices are accepted without
    user interaction. The agent is registered via
    ``AgentManager1.RegisterAgent`` and ``RequestDefaultAgent`` — no
    ``bluetoothctl`` subprocess calls.
    """

    CAPABILITY = "NoInputNoOutput"

    def __init__(self) -> None:
        self._registered = False
        self._bus: Optional[object] = None
        self._exported = False

    def set_bus(self, bus: object) -> None:
        self._bus = bus

    async def register(self, bus: object) -> bool:
        """Register this agent on the bus and request default-agent status."""
        self._bus = bus
        try:
            # Export the agent object so BlueZ can call our methods.
            if not self._exported:
                bus.export(_AGENT_PATH, _AgentService())
                self._exported = True
                _LOG.info("[BT Agent] Exported agent at %s", _AGENT_PATH)

            # Call AgentManager1.RegisterAgent
            reply = await bus.call(
                Message(
                    destination=_BLUEZ_BUS,
                    path=_BLUEZ_PATH,
                    interface=_AGENT_MANAGER_IFACE,
                    member="RegisterAgent",
                    signature="os",
                    body=[_AGENT_PATH, self.CAPABILITY],
                )
            )
            if reply.message_type == MessageType.ERROR:
                _LOG.warning("[BT Agent] RegisterAgent failed: %s",
                             reply.error_name or "unknown")
                return False

            _LOG.info("[BT Agent] RegisterAgent OK (capability=%s)", self.CAPABILITY)

            # Call RequestDefaultAgent
            reply = await bus.call(
                Message(
                    destination=_BLUEZ_BUS,
                    path=_BLUEZ_PATH,
                    interface=_AGENT_MANAGER_IFACE,
                    member="RequestDefaultAgent",
                    signature="o",
                    body=[_AGENT_PATH],
                )
            )
            if reply.message_type == MessageType.ERROR:
                _LOG.warning("[BT Agent] RequestDefaultAgent failed: %s",
                             reply.error_name or "unknown")
                # Agent is still registered, just not default — that's OK.
            else:
                _LOG.info("[BT Agent] RequestDefaultAgent OK")

            self._registered = True
            return True
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("[BT Agent] Registration error: %s", exc)
            return False

    async def unregister(self, bus: object) -> None:
        """Unregister the agent from BlueZ."""
        if not self._registered:
            return
        try:
            await bus.call(
                Message(
                    destination=_BLUEZ_BUS,
                    path=_BLUEZ_PATH,
                    interface=_AGENT_MANAGER_IFACE,
                    member="UnregisterAgent",
                    signature="o",
                    body=[_AGENT_PATH],
                )
            )
        except Exception:  # noqa: BLE001
            pass
        self._registered = False
        _LOG.info("[BT Agent] Unregistered")

    @property
    def is_registered(self) -> bool:
        return self._registered


class _AgentService(ServiceInterface if _dbus_ok else object):  # type: ignore[misc, valid-type]
    """Service object implementing ``org.bluez.Agent1`` interface methods.

    Inherits from ``dbus_next.service.ServiceInterface`` so that BlueZ
    accepts the registration without throwing ``interface must be a
    ServiceInterface``. Each method returns success immediately so that
    pairing is auto-accepted with no user interaction.
    """

    def __init__(self) -> None:
        if _dbus_ok:
            super().__init__('org.bluez.Agent1')

    @method(name="Release")
    def Release(self) -> '':
        _LOG.debug("[BT Agent] Release called")

    @method(name="RequestPinCode")
    def RequestPinCode(self, device: 'o') -> 's':
        _LOG.info("[BT Agent] RequestPinCode for %s — auto-accept", device)
        return "0000"

    @method(name="DisplayPinCode")
    def DisplayPinCode(self, device: 'o', pincode: 's') -> '':
        _LOG.info("[BT Agent] DisplayPinCode for %s: %s", device, pincode)

    @method(name="RequestPasskey")
    def RequestPasskey(self, device: 'o') -> 'u':
        _LOG.info("[BT Agent] RequestPasskey for %s — auto-accept", device)
        return 0

    @method(name="DisplayPasskey")
    def DisplayPasskey(self, device: 'o', passkey: 'u', entered: 'q') -> '':
        _LOG.info("[BT Agent] DisplayPasskey for %s: %d (%d entered)", device, passkey, entered)

    @method(name="RequestConfirmation")
    def RequestConfirmation(self, device: 'o', passkey: 'u') -> '':
        _LOG.info("[BT Agent] RequestConfirmation for %s passkey %d — auto-accept", device, passkey)

    @method(name="RequestAuthorization")
    def RequestAuthorization(self, device: 'o') -> '':
        _LOG.info("[BT Agent] RequestAuthorization for %s — auto-accept", device)

    @method(name="AuthorizeService")
    def AuthorizeService(self, device: 'o', uuid: 's') -> '':
        _LOG.info("[BT Agent] AuthorizeService for %s uuid %s — auto-accept", device, uuid)

    @method(name="Cancel")
    def Cancel(self) -> '':
        _LOG.debug("[BT Agent] Cancel called")


# ---------------------------------------------------------------------------
# Bluetooth Manager
# ---------------------------------------------------------------------------
class BluetoothManager:
    """Thread-safe wrapper around BlueZ D-Bus with bluetoothctl fallback."""

    def __init__(self, retry_attempts: int = 2) -> None:
        self._lock = threading.RLock()
        self._retry_attempts = max(0, retry_attempts)
        self._events: list[BtEvent] = []
        self._scan_results: dict[str, DiscoveredDevice] = {}
        self._scan_thread: Optional[threading.Thread] = None
        self._connected_mac: Optional[str] = None
        self._agent: Optional[BluezAgent] = None
        self._discoverable_timer: Optional[threading.Timer] = None
        self._discoverable_active = False
        self._discoverable_started_at: float = 0.0
        self._discoverable_duration: int = _DISCOVERABLE_DURATION
        self._bus: Optional[object] = None
        self._bus_thread: Optional[threading.Thread] = None
        self._bus_loop: Optional[object] = None
        self._dbus_lock = threading.RLock()
        self._bt_action_lock = threading.RLock()
        self._last_pair_error = ""
        self._shairport_mgr: Optional[object] = None

    # -- subprocess helper (fallback) ----------------------------------------

    @staticmethod
    def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str]:
        try:
            proc = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except FileNotFoundError:
            return 127, f"command not found: {cmd[0]}"
        except subprocess.TimeoutExpired:
            return 124, f"timeout after {timeout}s"
        except Exception as exc:  # noqa: BLE001
            return 1, str(exc)

    def _btctl(self, *args: str, timeout: int = 15) -> tuple[int, str]:
        return self._run(["bluetoothctl", *args], timeout=timeout)

    # -- D-Bus bus management -------------------------------------------------

    def init_dbus(self) -> bool:
        """Start the D-Bus event loop and register the pairing agent.

        Called from ``BridgeEngine.start()`` during startup. If dbus-next
        is unavailable, falls back to bluetoothctl (with reduced
        reliability for incoming pairing).
        """
        if not _dbus_ok:
            _LOG.warning("[BT] dbus-next unavailable — using bluetoothctl fallback")
            return False

        try:
            import asyncio

            def _run_bus() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._bus_loop = loop
                try:
                    loop.run_until_complete(self._setup_dbus())
                    loop.run_forever()
                except Exception as exc:  # noqa: BLE001
                    _LOG.error("[BT] D-Bus event loop error: %s", exc)

            self._bus_thread = threading.Thread(target=_run_bus, daemon=True, name="bt-dbus")
            self._bus_thread.start()
            # Give the bus a moment to connect
            time.sleep(1.0)
            return True
        except Exception as exc:  # noqa: BLE001
            _LOG.error("[BT] Failed to init D-Bus: %s", exc)
            return False

    async def _setup_dbus(self) -> None:
        """Connect to the system bus, register the agent, store the bus handle."""
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception:  # noqa: BLE001
            try:
                bus = await MessageBus(bus_type=BusType.SESSION).connect()
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("[BT] Could not connect to any D-Bus bus: %s", exc)
                return

        self._bus = bus
        _LOG.info("[BT] Connected to D-Bus, registering agent…")

        self._agent = BluezAgent()
        ok = await self._agent.register(bus)
        if ok:
            _LOG.info("[BT] Pairing agent registered and ready")
        else:
            _LOG.warning("[BT] Agent registration failed — incoming pairing may not work")

    # -- events ---------------------------------------------------------------

    def get_events(self) -> list[BtEvent]:
        with self._lock:
            return list(self._events[-200:])

    def _record(self, mac: str, action: str, success: bool, detail: str = "", error_code: str = "") -> None:
        evt = BtEvent(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            mac=mac,
            action=action,
            success=success,
            detail=detail,
            error_code=error_code,
        )
        with self._lock:
            self._events.append(evt)
        level = logging.INFO if success else logging.WARNING
        _LOG.log(level, "[BT] %s %s -> %s%s", action, mac, "OK" if success else "FAIL",
                 f" ({error_code})" if error_code else "")

    # -- D-Bus property helpers -----------------------------------------------

    def _dbus_get(self, path: str, interface: str, prop: str) -> object:
        """Synchronously get a D-Bus property (via the event loop)."""
        if not _dbus_ok or self._bus is None or self._bus_loop is None:
            return None
        import asyncio

        async def _get() -> object:
            reply = await self._bus.call(
                Message(
                    destination=_BLUEZ_BUS,
                    path=path,
                    interface=_PROPERTIES_IFACE,
                    member="Get",
                    signature="ss",
                    body=[interface, prop],
                )
            )
            if reply.message_type == MessageType.ERROR:
                return None
            if reply.body and isinstance(reply.body[0], Variant):
                return reply.body[0].value
            return reply.body[0] if reply.body else None

        fut = asyncio.run_coroutine_threadsafe(_get(), self._bus_loop)
        try:
            return fut.result(timeout=5)
        except Exception:  # noqa: BLE001
            return None

    def _dbus_set(self, path: str, interface: str, prop: str, value: object) -> bool:
        """Synchronously set a D-Bus property."""
        if not _dbus_ok or self._bus is None or self._bus_loop is None:
            return False
        import asyncio

        async def _set() -> bool:
            reply = await self._bus.call(
                Message(
                    destination=_BLUEZ_BUS,
                    path=path,
                    interface=_PROPERTIES_IFACE,
                    member="Set",
                    signature="ssv",
                    body=[interface, prop, Variant("s" if isinstance(value, str) else "b", value)],
                )
            )
            return reply.message_type != MessageType.ERROR

        fut = asyncio.run_coroutine_threadsafe(_set(), self._bus_loop)
        try:
            return fut.result(timeout=5)
        except Exception:  # noqa: BLE001
            return False

    def _dbus_call(self, path: str, interface: str, member: str,
                   signature: str = "", body: list | None = None, timeout: int = 30) -> tuple[bool, str]:
        """Synchronously call a D-Bus method, returning (success, error_name)."""
        if not _dbus_ok or self._bus is None or self._bus_loop is None:
            return False, "no-dbus"
        import asyncio

        async def _call() -> tuple[bool, str]:
            reply = await self._bus.call(
                Message(
                    destination=_BLUEZ_BUS,
                    path=path,
                    interface=interface,
                    member=member,
                    signature=signature,
                    body=body or [],
                )
            )
            if reply.message_type == MessageType.ERROR:
                return False, reply.error_name or "unknown-error"
            return True, ""

        fut = asyncio.run_coroutine_threadsafe(_call(), self._bus_loop)
        try:
            return fut.result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def _dbus_get_managed_objects(self) -> dict:
        """Get all managed objects from BlueZ ObjectManager."""
        if not _dbus_ok or self._bus is None or self._bus_loop is None:
            return {}
        import asyncio

        async def _get() -> dict:
            reply = await self._bus.call(
                Message(
                    destination=_BLUEZ_BUS,
                    path="/",
                    interface=_OBJECT_MANAGER_IFACE,
                    member="GetManagedObjects",
                )
            )
            if reply.message_type == MessageType.ERROR:
                return {}
            if not reply.body:
                return {}
            return reply.body[0]

        fut = asyncio.run_coroutine_threadsafe(_get(), self._bus_loop)
        try:
            return fut.result(timeout=5)
        except Exception:  # noqa: BLE001
            return {}

    # -- adapter ---------------------------------------------------------------

    def ensure_adapter(self) -> AdapterStatus:
        status = self.get_adapter_status()
        if not status.available:
            _LOG.warning("[BT] No Bluetooth adapter available")
            return status
        if not status.powered:
            _LOG.info("[BT] Adapter not powered, powering on")
            self._set_adapter_property("Powered", True)
            time.sleep(0.5)
            status = self.get_adapter_status()
        return status

    def _set_adapter_property(self, prop: str, value: object) -> bool:
        """Set an adapter property, trying D-Bus first, then bluetoothctl."""
        if _dbus_ok and self._bus is not None:
            ok = self._dbus_set("/org/bluez/hci0", _ADAPTER_IFACE, prop, value)
            if ok:
                return True
        # Fallback to bluetoothctl
        if prop == "Powered":
            code, _ = self._btctl("power", "on" if value else "off")
            return code == 0
        if prop == "Discoverable":
            code, _ = self._btctl("discoverable", "on" if value else "off")
            return code == 0
        if prop == "Pairable":
            code, _ = self._btctl("pairable", "on" if value else "off")
            return code == 0
        return False

    def get_adapter_status(self) -> AdapterStatus:
        """Get adapter status, trying D-Bus first, then bluetoothctl."""
        if _dbus_ok and self._bus is not None:
            return self._get_adapter_status_dbus()
        return self._get_adapter_status_btctl()

    def _get_adapter_status_dbus(self) -> AdapterStatus:
        status = AdapterStatus()
        powered = self._dbus_get("/org/bluez/hci0", _ADAPTER_IFACE, "Powered")
        if powered is None:
            return self._get_adapter_status_btctl()
        status.available = True
        status.powered = bool(powered)
        status.address = str(self._dbus_get("/org/bluez/hci0", _ADAPTER_IFACE, "Address") or "")
        status.name = str(self._dbus_get("/org/bluez/hci0", _ADAPTER_IFACE, "Name") or "")
        status.alias = str(self._dbus_get("/org/bluez/hci0", _ADAPTER_IFACE, "Alias") or "")
        status.discoverable = bool(self._dbus_get("/org/bluez/hci0", _ADAPTER_IFACE, "Discoverable"))
        status.discovering = bool(self._dbus_get("/org/bluez/hci0", _ADAPTER_IFACE, "Discovering"))
        return status

    def _get_adapter_status_btctl(self) -> AdapterStatus:
        code, out = self._btctl("show")
        status = AdapterStatus()
        if code != 0 or not out.strip():
            return status
        status.available = True
        for line in out.splitlines():
            low = line.strip().lower()
            if "powered:" in low:
                status.powered = "yes" in low
            elif "discoverable:" in low:
                status.discoverable = "yes" in low
            elif "discovering:" in low:
                status.discovering = "yes" in low
            elif low.startswith("controller"):
                parts = line.strip().split()
                if len(parts) >= 2:
                    status.address = parts[1]
            elif low.startswith("alias:"):
                status.alias = line.strip().split(":", 1)[1].strip()
            elif low.startswith("name:"):
                status.name = line.strip().split(":", 1)[1].strip()
        return status

    # -- discoverable & pairable mode ----------------------------------------

    def start_discoverable_mode(self, duration: int = _DISCOVERABLE_DURATION) -> bool:
        """Enable ``Discoverable`` + ``Pairable`` for ``duration`` seconds."""
        with self._lock:
            if self._discoverable_active:
                _LOG.info("[BT] Discoverable mode already active, resetting timer")
                self._cancel_timer()

            self._discoverable_duration = max(10, min(600, duration))
            self._discoverable_started_at = time.time()
            self._discoverable_active = True

            # Power on the adapter first.
            self._set_adapter_property("Powered", True)
            time.sleep(0.3)

            # Enable discoverable and pairable via D-Bus.
            self._set_adapter_property("Discoverable", True)
            self._set_adapter_property("Pairable", True)
            _LOG.info("[BT] Discoverable & Pairable ON for %ds", self._discoverable_duration)

            # The agent is registered at startup, so it's already active.

            # Start the expiry timer.
            self._discoverable_timer = threading.Timer(
                self._discoverable_duration, self._expire_discoverable
            )
            self._discoverable_timer.daemon = True
            self._discoverable_timer.start()

            self._record(
                "system", "discoverable_on", True,
                f"Discoverable & Pairable for {self._discoverable_duration}s",
            )
            return True

    def stop_discoverable_mode(self) -> bool:
        """Manually stop discoverable mode before the timer expires."""
        with self._lock:
            self._cancel_timer()
            self._do_disable_discoverable()
            self._discoverable_active = False
        self._record("system", "discoverable_off", True, "Discoverable mode stopped manually")
        return True

    def is_discoverable_active(self) -> bool:
        with self._lock:
            if not self._discoverable_active:
                return False
            elapsed = time.time() - self._discoverable_started_at
            return elapsed < self._discoverable_duration

    def get_discoverable_remaining(self) -> int:
        """Seconds remaining in the current discoverable window (0 if inactive)."""
        with self._lock:
            if not self._discoverable_active:
                return 0
            elapsed = int(time.time() - self._discoverable_started_at)
            remaining = self._discoverable_duration - elapsed
            return max(0, remaining)

    def _expire_discoverable(self) -> None:
        with self._lock:
            self._do_disable_discoverable()
            self._discoverable_active = False
        self._record("system", "discoverable_off", True, "Discoverable mode expired (timer)")
        _LOG.info("[BT] Discoverable mode expired — reverting")

    def _do_disable_discoverable(self) -> None:
        """Revert Discoverable/Pairable (caller holds lock)."""
        self._set_adapter_property("Discoverable", False)
        self._set_adapter_property("Pairable", False)

    def _cancel_timer(self) -> None:
        if self._discoverable_timer is not None:
            self._discoverable_timer.cancel()
            self._discoverable_timer = None

    # -- scanning -------------------------------------------------------------

    def scan(self, duration: int = 10) -> list[DiscoveredDevice]:
        """Scan for ``duration`` seconds, returning discovered devices."""
        with self._lock:
            self._scan_results.clear()

        _LOG.info("[BT] Starting scan for %ds", duration)

        with self._dbus_lock:
            if _dbus_ok and self._bus is not None:
                self._scan_dbus(duration)
            else:
                self._scan_btctl(duration)

        _LOG.info("[BT] Scan complete, found %d device(s)", len(self._scan_results))

        # Resolve full device info for all discovered devices.
        self._resolve_all_devices()

        with self._lock:
            return list(self._scan_results.values())

    def _scan_dbus(self, duration: int) -> None:
        """Scan using D-Bus StartDiscovery + polling GetManagedObjects."""
        # Start discovery
        ok, err = self._dbus_call("/org/bluez/hci0", _ADAPTER_IFACE, "StartDiscovery")
        if not ok:
            _LOG.warning("[BT] StartDiscovery failed: %s", err)
            return

        deadline = time.time() + duration
        while time.time() < deadline:
            time.sleep(1.0)
            self._poll_managed_objects()

        # Stop discovery
        self._dbus_call("/org/bluez/hci0", _ADAPTER_IFACE, "StopDiscovery")

    def _poll_managed_objects(self) -> None:
        """Poll GetManagedObjects and update scan results with device properties."""
        objects = self._dbus_get_managed_objects()
        if not objects:
            return

        for path, ifaces in objects.items():
            if _DEVICE_IFACE not in ifaces:
                continue
            props = ifaces[_DEVICE_IFACE]
            mac = _path_to_mac(path)
            if not mac:
                addr = _unwrap(props.get("Address"))
                mac = str(addr) if addr else ""
            if not mac:
                continue

            alias_raw = _unwrap(props.get("Alias"))
            name_raw = _unwrap(props.get("Name"))
            name = str(alias_raw or name_raw or "")
            rssi_raw = _unwrap(props.get("RSSI"))
            rssi = int(rssi_raw) if rssi_raw is not None else 0
            paired = bool(_unwrap(props.get("Paired")))
            trusted = bool(_unwrap(props.get("Trusted")))
            connected = bool(_unwrap(props.get("Connected")))

            with self._lock:
                dev = self._scan_results.get(mac)
                if dev is None:
                    dev = DiscoveredDevice(mac=mac, name=name or mac)
                    self._scan_results[mac] = dev
                if name and name != mac:
                    dev.name = name
                if rssi:
                    dev.rssi = rssi
                dev.paired = paired
                dev.trusted = trusted
                dev.connected = connected

    def _scan_btctl(self, duration: int) -> None:
        """Fallback scan using bluetoothctl subprocess."""
        proc = subprocess.Popen(  # noqa: S603
            ["bluetoothctl", "--timeout", str(duration), "scan", "on"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        deadline = time.time() + duration + 2
        while proc.poll() is None and time.time() < deadline:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                time.sleep(0.1)
                continue
            self._parse_scan_line(line.rstrip())

        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

        self._btctl("scan", "off")

    def _parse_scan_line(self, line: str) -> None:
        low = line.lower()
        mac_match = _MAC_RE.search(line)
        if not mac_match:
            return
        mac = mac_match.group(1).upper()

        name = ""
        name_match = re.search(r"Device\s+[0-9A-Fa-f:]+\s+(.+)", line)
        if name_match:
            name = name_match.group(1).strip()

        rssi = 0
        rssi_match = re.search(r"RSSI:\s*(-?\d+)", line, re.IGNORECASE)
        if rssi_match:
            rssi = int(rssi_match.group(1))
        elif "rssi" in low:
            nums = re.findall(r"-?\d+", line)
            if nums:
                rssi = int(nums[-1])

        with self._lock:
            dev = self._scan_results.get(mac)
            if dev is None:
                dev = DiscoveredDevice(mac=mac, name=name or mac)
                self._scan_results[mac] = dev
            if name and name != mac:
                dev.name = name
            if rssi:
                dev.rssi = rssi

    def _resolve_all_devices(self) -> None:
        """Query full device properties for all scan results via D-Bus."""
        if _dbus_ok and self._bus is not None:
            self._poll_managed_objects()
            return

        # Fallback: use bluetoothctl info per device
        with self._lock:
            macs = list(self._scan_results.keys())
        for mac in macs:
            info = self.info(mac)
            if info and not info.startswith("Error:"):
                resolved_name = self._extract_device_name(info)
                resolved_rssi = self._extract_rssi(info)
                with self._lock:
                    dev = self._scan_results.get(mac)
                    if dev is not None:
                        if resolved_name and resolved_name != mac:
                            dev.name = resolved_name
                        if resolved_rssi:
                            dev.rssi = resolved_rssi
                        dev.paired = "Paired: yes" in info
                        dev.trusted = "Trusted: yes" in info
                        dev.connected = "Connected: yes" in info

    # -- pairing / trusting / connecting --------------------------------------

    def _stop_discovery(self) -> None:
        """Stop any active scan — required before Pair/Connect."""
        if _dbus_ok and self._bus is not None:
            self._dbus_call("/org/bluez/hci0", _ADAPTER_IFACE, "StopDiscovery", timeout=5)
        else:
            self._btctl("scan", "off", timeout=5)
        time.sleep(0.5)

    def pair(self, mac: str) -> bool:
        """Pair with a device, stopping discovery first.

        Acquires ``_bt_action_lock`` so the background monitor cannot
        race with user-initiated pair/connect/disconnect calls that
        resolve, create, or remove device paths.
        If Pair() fails with ConnectionAttemptFailed, ``last_pair_error``
        is set to a user-friendly message instead of spamming retries.
        """
        mac = mac.upper()
        self._last_pair_error = ""
        with self._bt_action_lock:
            with self._dbus_lock:
                _LOG.info("[BT] Stopping discovery before pairing %s", mac)
                self._stop_discovery()

                if _dbus_ok and self._bus is not None:
                    return self._pair_dbus(mac)
                return self._pair_btctl(mac)

    def _is_paired_dbus(self, mac: str) -> bool:
        """Check if a device is already paired in BlueZ via D-Bus."""
        path = _mac_to_path(mac)
        paired = self._dbus_get(path, _DEVICE_IFACE, "Paired")
        return bool(paired)

    def _resolve_device_path(self, mac: str) -> Optional[str]:
        """Dynamically resolve the D-Bus object path for a MAC.

        Always queries BlueZ GetManagedObjects fresh — never caches.
        Returns None if the device object does not currently exist.
        """
        if not _dbus_ok or self._bus is None:
            return _mac_to_path(mac)
        objects = self._dbus_get_managed_objects()
        expected = _mac_to_path(mac.upper())
        for path in objects:
            if path == expected:
                return path
        return None

    @property
    def last_pair_error(self) -> str:
        """User-facing error message from the most recent pair() call."""
        return self._last_pair_error

    def _device_path_exists(self, mac: str) -> bool:
        """Check if the device object still exists in BlueZ D-Bus.

        After ``RemoveDevice``, the object path disappears from the
        ObjectManager. We must never call ``Pair()`` on a stale path.
        """
        if not _dbus_ok or self._bus is None:
            return True  # can't check — assume it exists
        objects = self._dbus_get_managed_objects()
        expected = _mac_to_path(mac)
        return any(p == expected for p in objects)

    def _rediscover_device_path(self, mac: str, timeout: float = 15.0) -> bool:
        """Trigger a brief discovery to make BlueZ re-create the device object.

        After ``RemoveDevice``, the D-Bus object for a MAC disappears. A
        short ``StartDiscovery`` cycle forces BlueZ to re-scan and re-create
        the object so ``Pair()`` can be called on a fresh, valid path.
        """
        if not _dbus_ok or self._bus is None:
            return True  # fallback mode — nothing to rediscover

        if self._device_path_exists(mac):
            return True

        _LOG.info("[BT] Device path for %s missing — rediscovering via scan", mac)
        ok, err = self._dbus_call("/org/bluez/hci0", _ADAPTER_IFACE, "StartDiscovery", timeout=5)
        if not ok:
            _LOG.warning("[BT] StartDiscovery for rediscovery failed: %s", err)
            return False

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1.0)
            if self._device_path_exists(mac):
                self._dbus_call("/org/bluez/hci0", _ADAPTER_IFACE, "StopDiscovery", timeout=5)
                _LOG.info("[BT] Device path for %s rediscovered", mac)
                return True

        self._dbus_call("/org/bluez/hci0", _ADAPTER_IFACE, "StopDiscovery", timeout=5)
        _LOG.warning("[BT] Could not rediscover device path for %s within %.1fs", mac, timeout)
        return False

    def _remove_device_from_adapter(self, mac: str) -> bool:
        """Remove a device completely from BlueZ via Adapter1.RemoveDevice."""
        path = _mac_to_path(mac)
        ok, err = self._dbus_call(
            "/org/bluez/hci0", _ADAPTER_IFACE, "RemoveDevice",
            signature="o", body=[path], timeout=10,
        )
        if ok:
            _LOG.info("[BT] RemoveDevice %s -> OK", mac)
        else:
            _LOG.warning("[BT] RemoveDevice %s -> FAIL: %s (trying fallback)", mac, err)
            # Fallback: use the Device1.Remove method
            self._btctl("disconnect", mac, timeout=10)
            self._btctl("untrust", mac, timeout=10)
            code, out = self._btctl("remove", mac, timeout=10)
            ok = code == 0 or "not available" in out.lower()
        self._record(mac, "remove", ok, "RemoveDevice (pre-pair cleanup)")
        return ok

    def _pair_dbus(self, mac: str) -> bool:
        """Pair via D-Bus Device1.Pair() with dynamic path resolution.

        Always resolves the device path via GetManagedObjects before
        calling Pair(). On UnknownObject the cached reference is cleared
        and we stop — never retry on a missing path. On
        ConnectionAttemptFailed we set a user-friendly error message
        instead of spamming retries.
        """
        if self._is_paired_dbus(mac):
            # If already paired AND connected, don't remove — removing would
            # disconnect a working link. Just trust and connect directly.
            path_check = self._resolve_device_path(mac)
            connected = False
            if path_check is not None:
                connected = bool(self._dbus_get(path_check, _DEVICE_IFACE, "Connected"))
            if connected:
                _LOG.info("[BT] %s already paired and connected — skipping re-pair", mac)
                self._record(mac, "pair", True, "Already paired and connected")
                return True
            _LOG.info("[BT] %s already paired but not connected — removing before re-pair", mac)
            self._remove_device_from_adapter(mac)
            time.sleep(3.0)

        path = self._resolve_device_path(mac)
        if path is None:
            _LOG.info("[BT] Device path for %s not found, rediscovering before pair", mac)
            if not self._rediscover_device_path(mac):
                self._last_pair_error = "Pairing timed out. Please say 'Alexa, pair Bluetooth' and try again."
                self._record(mac, "pair", False, "Device path not found after rediscovery", "UnknownObject")
                return False
            path = self._resolve_device_path(mac)
            if path is None:
                self._last_pair_error = "Pairing timed out. Please say 'Alexa, pair Bluetooth' and try again."
                self._record(mac, "pair", False, "Device path not found", "UnknownObject")
                return False

        # Log device state before pairing for diagnostics.
        paired = self._dbus_get(path, _DEVICE_IFACE, "Paired")
        connected = self._dbus_get(path, _DEVICE_IFACE, "Connected")
        rssi = self._dbus_get(path, _DEVICE_IFACE, "RSSI")
        _LOG.info("[BT] Pairing %s — device state: paired=%s connected=%s rssi=%s",
                  mac, bool(paired) if paired is not None else "?",
                  bool(connected) if connected is not None else "?",
                  rssi if rssi is not None else "?")

        ok, err = self._dbus_call(path, _DEVICE_IFACE, "Pair", timeout=30)
        if not ok and "AlreadyExists" in (err or ""):
            _LOG.info("[BT] Pair %s -> AlreadyExists, removing and retrying", mac)
            self._remove_device_from_adapter(mac)
            time.sleep(3.0)
            path = self._resolve_device_path(mac)
            if path is None:
                self._last_pair_error = "Pairing timed out. Please say 'Alexa, pair Bluetooth' and try again."
                self._record(mac, "pair", False, "Device path not found after removal", "UnknownObject")
                return False
            ok, err = self._dbus_call(path, _DEVICE_IFACE, "Pair", timeout=30)

        # Retry once on ConnectionAttemptFailed after a brief wait —
        # the device may not have been ready for pairing.
        if not ok and "ConnectionAttemptFailed" in (err or ""):
            _LOG.info("[BT] Pair %s -> ConnectionAttemptFailed, waiting 2s and retrying", mac)
            time.sleep(2.0)
            # Re-resolve path in case it changed.
            path = self._resolve_device_path(mac)
            if path is not None:
                ok, err = self._dbus_call(path, _DEVICE_IFACE, "Pair", timeout=30)

        if not ok:
            if "UnknownObject" in (err or ""):
                self._last_pair_error = "Pairing timed out. Please say 'Alexa, pair Bluetooth' and try again."
                self._record(mac, "pair", False, "Device object disappeared", "UnknownObject")
                return False
            if "ConnectionAttemptFailed" in (err or ""):
                self._last_pair_error = "Pairing timed out. Please say 'Alexa, pair Bluetooth' and try again."
                self._record(mac, "pair", False, "Connection attempt failed", "ConnectionAttemptFailed")
                return False

        error_code = err if not ok else ""
        self._record(mac, "pair", ok, f"D-Bus Pair {'OK' if ok else 'FAIL: ' + err}", error_code)
        return ok

    def _pair_btctl(self, mac: str) -> bool:
        """Pair via bluetoothctl (fallback)."""
        code, out = self._btctl("pair", mac, timeout=30)
        ok = code == 0 and "Pairing successful" in out
        self._record(mac, "pair", ok, out.strip()[:200], "" if ok else self._extract_error(out))
        return ok

    def trust(self, mac: str) -> bool:
        """Set Trusted=True on the device via D-Bus."""
        mac = mac.upper()
        if _dbus_ok and self._bus is not None:
            path = _mac_to_path(mac)
            ok = self._dbus_set(path, _DEVICE_IFACE, "Trusted", True)
            self._record(mac, "trust", ok, f"D-Bus Set Trusted=True {'OK' if ok else 'FAIL'}")
            return ok
        # Fallback
        code, out = self._btctl("trust", mac, timeout=10)
        ok = code == 0
        self._record(mac, "trust", ok, out.strip()[:200])
        return ok

    def connect(self, mac: str) -> bool:
        """Connect to a device, stopping discovery first, then connect A2DP.

        Acquires ``_bt_action_lock`` so the background monitor cannot issue a
        conflicting Connect() or race with pair/disconnect on device paths.
        Does NOT spam retries on
        ConnectionAttemptFailed — returns False immediately.
        """
        mac = mac.upper()
        with self._bt_action_lock:
            with self._dbus_lock:
                _LOG.info("[BT] Stopping discovery before connecting %s", mac)
                self._stop_discovery()

                if _dbus_ok and self._bus is not None:
                    ok = self._connect_dbus(mac)
                else:
                    ok = self._connect_btctl(mac)
                if ok:
                    self._record(mac, "connect", True, "Connection successful")
                    with self._lock:
                        self._connected_mac = mac
                    # Connect the A2DP profile explicitly. BlueZ's Connect()
                    # connects the base ACL link but does not always auto-connect
                    # the A2DP audio profile — especially on freshly-paired Echo
                    # devices. If the profile is already connected, BlueZ returns
                    # org.bluez.Error.Failed, which we treat as success.
                    time.sleep(1.5)
                    self._connect_a2dp(mac)
                    return True
                self._record(mac, "connect", False, "Connection attempt failed", "ConnectionAttemptFailed")
                return False

    def _connect_dbus(self, mac: str) -> bool:
        """Connect via D-Bus Device1.Connect() with dynamic path resolution.

        If the device is already Connected (including A2DP), skips the
        D-Bus Connect() call to prevent org.bluez.Error.Failed.
        If the device path is not found, attempts rediscovery before giving up.
        Handles InProgress by waiting 1.5s and retrying once.
        """
        path = self._resolve_device_path(mac)
        if path is None:
            _LOG.info("[BT] D-Bus Connect: device path for %s not found, rediscovering", mac)
            if not self._rediscover_device_path(mac):
                _LOG.warning("[BT] D-Bus Connect: could not rediscover device path for %s", mac)
                return False
            path = self._resolve_device_path(mac)
            if path is None:
                _LOG.warning("[BT] D-Bus Connect: device path for %s still not found after rediscovery", mac)
                return False

        # Skip Connect() if already Connected — calling Connect on an
        # already-connected device raises org.bluez.Error.Failed.
        already_connected = self._dbus_get(path, _DEVICE_IFACE, "Connected")
        if already_connected is not None and bool(already_connected):
            _LOG.info("[BT] %s already Connected — skipping D-Bus Connect()", mac)
            return True

        ok, err = self._dbus_call(path, _DEVICE_IFACE, "Connect", timeout=20)
        if not ok and "InProgress" in (err or ""):
            _LOG.info("[BT] Connect %s -> InProgress, waiting 1.5s and retrying", mac)
            time.sleep(1.5)
            ok, err = self._dbus_call(path, _DEVICE_IFACE, "Connect", timeout=20)
        if not ok:
            _LOG.warning("[BT] D-Bus Connect failed: %s", err)
        return ok

    def _connect_btctl(self, mac: str) -> bool:
        """Connect via bluetoothctl (fallback)."""
        code, out = self._btctl("connect", mac, timeout=20)
        return code == 0 and "Connection successful" in out

    def _connect_a2dp(self, mac: str) -> None:
        """Connect the A2DP sink profile explicitly via D-Bus.

        BlueZ's Connect() establishes the base ACL link but does not always
        auto-connect the A2DP audio profile — especially on freshly-paired Echo
        devices. Without this call, no bluez_sink node appears in PipeWire and
        audio never reaches the speaker.

        If the profile is already connected (common when Connect() did
        auto-connect it), BlueZ returns org.bluez.Error.Failed. We treat this
        as success since the desired state (A2DP connected) is already true.

        However, if the transport was acquired by a competing sound server
        (e.g. pulseaudio-bluez), the profile appears "connected" but
        WirePlumber never creates the sink node. In that case we force a
        Disconnect/Connect cycle to release the stale transport and let
        WirePlumber acquire it cleanly.

        Retries up to 3 times with a 2s delay for transient failures.
        """
        if not _dbus_ok or self._bus is None:
            return
        path = _mac_to_path(mac)
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            ok, err = self._dbus_call(
                path, _DEVICE_IFACE, "ConnectProfile",
                signature="s", body=[_A2DP_UUID], timeout=15,
            )
            if ok:
                _LOG.info("[BT] A2DP profile connected for %s (attempt %d)", mac, attempt)
                return
            # org.bluez.Error.Failed usually means the profile is already
            # connected — but if WirePlumber hasn't created the sink, the
            # transport may be held by a stale/competing sound server.
            # Force a disconnect/reconnect to release it.
            if "Error.Failed" in (err or ""):
                _LOG.info("[BT] A2DP profile already connected for %s (ConnectProfile returned Error.Failed)", mac)
                _LOG.info("[BT] Forcing A2DP disconnect/reconnect to release stale transport for %s", mac)
                self._dbus_call(path, _DEVICE_IFACE, "DisconnectProfile",
                                signature="s", body=[_A2DP_UUID], timeout=10)
                time.sleep(1.5)
                ok2, err2 = self._dbus_call(
                    path, _DEVICE_IFACE, "ConnectProfile",
                    signature="s", body=[_A2DP_UUID], timeout=15,
                )
                if ok2:
                    _LOG.info("[BT] A2DP profile re-connected for %s after stale transport release", mac)
                    return
                _LOG.warning("[BT] A2DP reconnect failed for %s: %s — will retry via sink polling", mac, err2)
                return
            _LOG.warning("[BT] A2DP ConnectProfile attempt %d/%d failed for %s: %s",
                         attempt, max_attempts, mac, err)
            if attempt < max_attempts:
                time.sleep(2.0)
        _LOG.warning("[BT] A2DP ConnectProfile failed for %s after %d attempts (non-fatal, will retry via sink polling)",
                     mac, max_attempts)

    def disconnect(self, mac: str) -> bool:
        mac = mac.upper()
        with self._bt_action_lock:
            with self._dbus_lock:
                if _dbus_ok and self._bus is not None:
                    path = self._resolve_device_path(mac)
                    if path is None:
                        _LOG.info("[BT] Disconnect: device %s not in BlueZ (already removed)", mac)
                        ok = True
                    else:
                        ok, err = self._dbus_call(path, _DEVICE_IFACE, "Disconnect", timeout=15)
                        if not ok and "not connected" not in (err or "").lower():
                            _LOG.warning("[BT] D-Bus Disconnect failed: %s", err)
                        if not ok and "UnknownObject" in (err or ""):
                            _LOG.info("[BT] Disconnect: device %s disappeared", mac)
                            ok = True
                else:
                    code, out = self._btctl("disconnect", mac, timeout=15)
                    ok = code == 0 or "not connected" in out.lower()
            self._record(mac, "disconnect", ok, "Disconnect")
            with self._lock:
                if self._connected_mac == mac:
                    self._connected_mac = None
        return ok

    def remove(self, mac: str) -> bool:
        """Remove a device completely from BlueZ (disconnect + untrust + remove).

        After removal, the D-Bus object path for this MAC is invalidated.
        Any subsequent ``Pair()`` call must rediscover the path first.
        """
        mac = mac.upper()
        with self._bt_action_lock:
            with self._dbus_lock:
                self.disconnect(mac)
                if _dbus_ok and self._bus is not None:
                    if self._device_path_exists(mac):
                        ok = self._remove_device_from_adapter(mac)
                    else:
                        _LOG.info("[BT] Device %s already removed from BlueZ", mac)
                        ok = True
                else:
                    self._btctl("untrust", mac, timeout=10)
                    code, out = self._btctl("remove", mac, timeout=10)
                    ok = code == 0 or "not available" in out.lower()
            with self._lock:
                if self._connected_mac == mac:
                    self._connected_mac = None
        self._record(mac, "remove", ok, "Remove")
        return ok

    def info(self, mac: str) -> str:
        """Get device info (bluetoothctl fallback text format)."""
        code, out = self._btctl("info", mac)
        return out if code == 0 else f"Error: {out}"

    def get_connected_mac(self) -> Optional[str]:
        with self._lock:
            return self._connected_mac

    def disconnect_all(self) -> int:
        """Disconnect all currently connected Bluetooth devices.

        Called at startup before PipeWire/WirePlumber starts to clear stale
        profile registrations from a previous container session.  Without
        this, WirePlumber's RegisterProfile() fails with org.bluez.Error.
        NotPermitted on devices that are still connected, and no A2DP sink
        is ever created.

        Returns the number of devices disconnected.
        """
        disconnected = 0
        if _dbus_ok and self._bus is not None:
            objects = self._dbus_get_managed_objects()
            for path, ifaces in objects.items():
                if _DEVICE_IFACE not in ifaces:
                    continue
                props = ifaces[_DEVICE_IFACE]
                connected = bool(_unwrap(props.get("Connected")))
                if not connected:
                    continue
                mac = _path_to_mac(path)
                if not mac:
                    addr = _unwrap(props.get("Address"))
                    mac = str(addr) if addr else ""
                if not mac:
                    continue
                _LOG.info("[BT] Startup cleanup: disconnecting stale session for %s", mac)
                if self.disconnect(mac):
                    disconnected += 1
                time.sleep(0.5)
        else:
            code, out = self._btctl("devices", timeout=8)
            if code == 0:
                for line in out.splitlines():
                    mac_match = _MAC_RE.search(line)
                    if not mac_match:
                        continue
                    mac = mac_match.group(1).upper()
                    info = self.info(mac)
                    if "Connected: yes" in info:
                        _LOG.info("[BT] Startup cleanup: disconnecting stale session for %s", mac)
                        if self.disconnect(mac):
                            disconnected += 1
                        time.sleep(0.5)
        if disconnected:
            _LOG.info("[BT] Startup cleanup: disconnected %d stale device(s)", disconnected)
            time.sleep(2.0)
        return disconnected

    def set_connected_mac(self, mac: Optional[str]) -> None:
        with self._lock:
            self._connected_mac = mac.upper() if mac else None

    def is_device_connected(self, mac: str) -> bool:
        """Check if a specific device is currently connected via D-Bus."""
        mac = mac.upper()
        if _dbus_ok and self._bus is not None:
            path = _mac_to_path(mac)
            connected = self._dbus_get(path, _DEVICE_IFACE, "Connected")
            return bool(connected)
        # Fallback
        code, out = self._btctl("info", mac, timeout=8)
        return code == 0 and "Connected: yes" in out

    # -- availability polling -------------------------------------------------

    def is_available(self, mac: str) -> bool:
        """Check if a device is available (connected, paired, or trusted)."""
        if _dbus_ok and self._bus is not None:
            path = _mac_to_path(mac.upper())
            connected = self._dbus_get(path, _DEVICE_IFACE, "Connected")
            paired = self._dbus_get(path, _DEVICE_IFACE, "Paired")
            trusted = self._dbus_get(path, _DEVICE_IFACE, "Trusted")
            return bool(connected or paired or trusted)
        # Fallback
        code, out = self._btctl("info", mac.upper(), timeout=8)
        if code != 0:
            return False
        return "Connected: yes" in out or "Paired: yes" in out or "Trusted: yes" in out

    def get_device_status(self, mac: str) -> dict[str, str]:
        """Get device status as a dict."""
        if _dbus_ok and self._bus is not None:
            return self._get_device_status_dbus(mac.upper())
        return self._get_device_status_btctl(mac.upper())

    def _get_device_status_dbus(self, mac: str) -> dict[str, str]:
        path = _mac_to_path(mac)
        status: dict[str, str] = {"available": "no"}
        connected = self._dbus_get(path, _DEVICE_IFACE, "Connected")
        if connected is None:
            # Device not in BlueZ — fall back to bluetoothctl
            return self._get_device_status_btctl(mac)
        status["available"] = "yes"
        status["connected"] = "yes" if bool(connected) else "no"
        paired = self._dbus_get(path, _DEVICE_IFACE, "Paired")
        status["paired"] = "yes" if bool(paired) else "no"
        trusted = self._dbus_get(path, _DEVICE_IFACE, "Trusted")
        status["trusted"] = "yes" if bool(trusted) else "no"
        name = self._dbus_get(path, _DEVICE_IFACE, "Alias")
        if name:
            status["name"] = str(name)
        return status

    def _get_device_status_btctl(self, mac: str) -> dict[str, str]:
        code, out = self._btctl("info", mac, timeout=8)
        status: dict[str, str] = {"available": "no"}
        if code != 0:
            return status
        status["available"] = "yes"
        for line in out.splitlines():
            low = line.strip().lower()
            if low.startswith("connected:"):
                status["connected"] = "yes" if "yes" in low else "no"
            elif low.startswith("paired:"):
                status["paired"] = "yes" if "yes" in low else "no"
            elif low.startswith("trusted:"):
                status["trusted"] = "yes" if "yes" in low else "no"
            elif low.startswith("name:"):
                status["name"] = line.strip().split(":", 1)[1].strip()
        return status

    # -- static helpers -------------------------------------------------------

    @staticmethod
    def _extract_device_name(info_text: str) -> str:
        """Extract the Alias or Name from ``bluetoothctl info`` output."""
        for line in info_text.splitlines():
            stripped = line.strip()
            low = stripped.lower()
            if low.startswith("alias:"):
                return stripped.split(":", 1)[1].strip()
        for line in info_text.splitlines():
            stripped = line.strip()
            low = stripped.lower()
            if low.startswith("name:"):
                return stripped.split(":", 1)[1].strip()
        return ""

    @staticmethod
    def _extract_rssi(info_text: str) -> int:
        """Extract RSSI from ``bluetoothctl info`` output."""
        for line in info_text.splitlines():
            low = line.strip().lower()
            if "rssi" in low:
                nums = re.findall(r"-?\d+", line)
                if nums:
                    try:
                        return int(nums[-1])
                    except ValueError:
                        pass
        return 0

    @staticmethod
    def _extract_error(output: str) -> str:
        for marker in ("Org.bluez.Error.", "org.bluez.Error.", "Connection Refused", "Failed", "not available", "No reply"):
            if marker in output:
                idx = output.find(marker)
                return output[idx : idx + 80].strip()
        return "Unknown error"

    # -- MPRIS / AVRCP metadata forwarding -------------------------------------

    def update_mpris_metadata(self, mac: str, title: str, artist: str, album: str) -> bool:
        """Push track metadata to BlueZ via the MediaItem1 interface.

        BlueZ exposes per-device media player objects when A2DP + AVRCP are
        connected. We set the Title, Artist, and Album properties so the
        Echo Show screen displays the currently playing song.
        """
        if not _dbus_ok or self._bus is None or self._bus_loop is None:
            return False
        mac = mac.upper()
        # Find the media player object path for this device.
        player_path = self._find_media_player_path(mac)
        if not player_path:
            _LOG.debug("[BT] No media player path for %s — AVRCP not connected", mac)
            return False

        _LOG.info("[BT] Updating MPRIS metadata for %s: %s - %s - %s", mac, title, artist, album)
        # Set metadata properties via the org.freedesktop.DBus.Properties interface.
        for prop, value in (("Title", title), ("Artist", artist), ("Album", album)):
            self._dbus_set(player_path, "org.bluez.MediaItem1", prop, value)
        return True

    def _find_media_player_path(self, mac: str) -> Optional[str]:
        """Find the BlueZ media player object path for a connected device."""
        objects = self._dbus_get_managed_objects()
        if not objects:
            return None
        dev_path_prefix = _mac_to_path(mac)
        for path, ifaces in objects.items():
            if "org.bluez.MediaPlayer1" in ifaces and path.startswith(dev_path_prefix):
                return path
        return None

    # -- AVRCP control signal backchannel -------------------------------------

    def start_avrcp_listener(self, shairport_mgr: object) -> bool:
        """Start listening for AVRCP control signals from connected Echo devices.

        When the Echo Show sends Next/Previous/Play/Pause via Bluetooth AVRCP,
        BlueZ emits property changes on the MediaPlayer1 interface. This
        listener forwards those commands to shairport-sync to control the
        connected Apple device.
        """
        if not _dbus_ok or self._bus is None or self._bus_loop is None:
            _LOG.debug("[BT] Cannot start AVRCP listener — no D-Bus")
            return False

        import asyncio

        async def _setup_listener() -> None:
            try:
                # Add a match rule for property changes on MediaPlayer1 interfaces.
                await self._bus.call(
                    Message(
                        destination="org.freedesktop.DBus",
                        path="/org/freedesktop/DBus",
                        interface="org.freedesktop.DBus",
                        member="AddMatch",
                        signature="s",
                        body=["type='signal',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',arg0='org.bluez.MediaPlayer1'"],
                    )
                )
                _LOG.info("[BT] AVRCP listener registered for MediaPlayer1 property changes")
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("[BT] AVRCP listener setup failed: %s", exc)

        # Store the shairport manager reference for command forwarding.
        self._shairport_mgr = shairport_mgr
        fut = asyncio.run_coroutine_threadsafe(_setup_listener(), self._bus_loop)
        try:
            fut.result(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        return True
