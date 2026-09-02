"""Add-on runtime configuration loaded from Home Assistant options."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

_LOG = logging.getLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "log_level": "INFO",
    "airplay_port_base": 5000,
    "audio_buffer_seconds": 3,
    "bluetooth_retry_attempts": 2,
    "scan_duration_seconds": 10,
    "speaker_db_path": "/data/options.json",
}


@dataclass
class AppConfig:
    """Resolved add-on configuration."""

    log_level: str = "INFO"
    airplay_port_base: int = 5000
    audio_buffer_seconds: int = 3
    bluetooth_retry_attempts: int = 2
    scan_duration_seconds: int = 10
    speaker_db_path: str = "/data/options.json"
    raw_options: dict[str, Any] = field(default_factory=dict)

    @property
    def data_dir(self) -> str:
        return os.path.dirname(self.speaker_db_path) or "/data"

    @property
    def shairport_conf_dir(self) -> str:
        return os.path.join(self.data_dir, "shairport-confs")

    @property
    def shairport_pid_dir(self) -> str:
        return os.path.join(self.data_dir, "shairport-pids")

    @property
    def pipewire_dir(self) -> str:
        return os.path.join(self.data_dir, "pipewire")

    @property
    def log_file(self) -> str:
        return os.path.join(self.data_dir, "bridge.log")


def load_config() -> AppConfig:
    """Load HA add-on options, falling back to defaults."""
    raw: dict[str, Any] = {}
    options_path = os.environ.get("OPTIONS_PATH", "/data/options.json")
    # HA writes the add-on options to /data/options.json on startup. We read our
    # own config keys from there if present; otherwise fall back to defaults.
    for path in (options_path, "/config/options.json"):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    raw.update(loaded)
                break
            except (OSError, json.JSONDecodeError) as exc:  # noqa: PERF203
                _LOG.warning("Could not read options from %s: %s", path, exc)

    merged = {**_DEFAULTS, **{k: v for k, v in raw.items() if k in _DEFAULTS}}
    cfg = AppConfig(
        log_level=str(merged.get("log_level", "INFO")).upper(),
        airplay_port_base=int(merged.get("airplay_port_base", 5000)),
        audio_buffer_seconds=int(merged.get("audio_buffer_seconds", 3)),
        bluetooth_retry_attempts=int(merged.get("bluetooth_retry_attempts", 2)),
        scan_duration_seconds=int(merged.get("scan_duration_seconds", 10)),
        speaker_db_path=str(merged.get("speaker_db_path", "/data/options.json")),
        raw_options=raw,
    )
    if cfg.log_level not in {"INFO", "DEBUG", "TRACE"}:
        cfg.log_level = "INFO"
    return cfg
