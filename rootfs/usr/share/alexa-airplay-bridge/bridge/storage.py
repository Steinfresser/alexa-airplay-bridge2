"""Persistent JSON speaker database backed by /data/options.json."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

_LOG = logging.getLogger(__name__)


@dataclass
class Speaker:
    """A saved Bluetooth speaker with an associated AirPlay receiver."""

    mac: str
    name: str  # custom AirPlay display name
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    streaming: bool = False
    rssi: int = 0
    last_seen: str = ""
    port: int = 0  # shairport-sync port
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SpeakerDB:
    """Thread-safe persistent speaker store."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._speakers: dict[str, Speaker] = {}
        self._lock = threading.RLock()
        self._ensure_dir()
        self.load()

    def _ensure_dir(self) -> None:
        d = os.path.dirname(self._path)
        if d:
            os.makedirs(d, exist_ok=True)

    def load(self) -> None:
        with self._lock:
            if not os.path.exists(self._path):
                self._speakers = {}
                self._save_locked()
                return
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                speakers: dict[str, Speaker] = {}
                if isinstance(raw, dict):
                    spk_list = raw.get("speakers", [])
                elif isinstance(raw, list):
                    spk_list = raw
                else:
                    spk_list = []
                for item in spk_list:
                    if not isinstance(item, dict):
                        continue
                    mac = str(item.get("mac", "")).upper()
                    if not mac:
                        continue
                    speakers[mac] = Speaker(
                        mac=mac,
                        name=str(item.get("name", mac)),
                        paired=bool(item.get("paired", False)),
                        trusted=bool(item.get("trusted", False)),
                        connected=bool(item.get("connected", False)),
                        streaming=bool(item.get("streaming", False)),
                        rssi=int(item.get("rssi", 0)),
                        last_seen=str(item.get("last_seen", "")),
                        port=int(item.get("port", 0)),
                        created_at=float(item.get("created_at", time.time())),
                    )
                self._speakers = speakers
                _LOG.info("Loaded %d speaker(s) from %s", len(speakers), self._path)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                _LOG.error("Failed to load speaker DB from %s: %s", self._path, exc)
                self._speakers = {}

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        data = {
            "speakers": [s.to_dict() for s in self._speakers.values()],
        }
        tmp = f"{self._path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except OSError as exc:
            _LOG.error("Failed to save speaker DB to %s: %s", self._path, exc)

    # -- CRUD -----------------------------------------------------------------

    def list_speakers(self) -> list[Speaker]:
        with self._lock:
            return list(self._speakers.values())

    def get(self, mac: str) -> Speaker | None:
        with self._lock:
            return self._speakers.get(mac.upper())

    def upsert(self, mac: str, name: str, **kwargs: Any) -> Speaker:
        mac = mac.upper()
        with self._lock:
            spk = self._speakers.get(mac)
            if spk is None:
                spk = Speaker(mac=mac, name=name)
                self._speakers[mac] = spk
            else:
                spk.name = name
            for k, v in kwargs.items():
                if hasattr(spk, k):
                    setattr(spk, k, v)
            self._save_locked()
            return spk

    def update(self, mac: str, **kwargs: Any) -> Speaker | None:
        mac = mac.upper()
        with self._lock:
            spk = self._speakers.get(mac)
            if spk is None:
                return None
            for k, v in kwargs.items():
                if hasattr(spk, k):
                    setattr(spk, k, v)
            self._save_locked()
            return spk

    def delete(self, mac: str) -> bool:
        mac = mac.upper()
        with self._lock:
            if mac in self._speakers:
                del self._speakers[mac]
                self._save_locked()
                return True
            return False

    def set_connected(self, mac: str, connected: bool) -> None:
        self.update(mac, connected=connected)

    def set_streaming(self, mac: str, streaming: bool) -> None:
        self.update(mac, streaming=streaming)
