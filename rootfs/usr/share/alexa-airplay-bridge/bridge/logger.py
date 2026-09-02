"""Centralized logging with a ring-buffered memory tail for the Web UI."""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from datetime import datetime
from typing import Optional

_LOCK = threading.Lock()
_BUFFER: deque[str] = deque(maxlen=2000)
_CURRENT_LEVEL = logging.INFO

# TRACE is a custom level below DEBUG for very verbose output.
TRACE = 5
logging.addLevelName(TRACE, "TRACE")


class _BufferHandler(logging.Handler):
    """Pushes formatted records into the in-memory ring buffer."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001
            msg = record.getMessage()
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"{ts} {msg}"
        with _LOCK:
            _BUFFER.append(line)


_HANDLER = _BufferHandler()


def get_log_buffer() -> list[str]:
    """Return a snapshot of the current log buffer."""
    with _LOCK:
        return list(_BUFFER)


def clear_log_buffer() -> None:
    with _LOCK:
        _BUFFER.clear()


def set_log_level(level: str) -> None:
    """Set the global log level (INFO/DEBUG/TRACE)."""
    global _CURRENT_LEVEL
    name = (level or "INFO").upper()
    if name == "TRACE":
        _CURRENT_LEVEL = TRACE
    elif name == "DEBUG":
        _CURRENT_LEVEL = logging.DEBUG
    else:
        _CURRENT_LEVEL = logging.INFO
    logging.getLogger().setLevel(_CURRENT_LEVEL)
    _HANDLER.setLevel(_CURRENT_LEVEL)


def get_log_level() -> str:
    if _CURRENT_LEVEL == TRACE:
        return "TRACE"
    if _CURRENT_LEVEL == logging.DEBUG:
        return "DEBUG"
    return "INFO"


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    """Configure root logging with console + buffer (+ optional file)."""
    set_log_level(level)
    root = logging.getLogger()
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    console.setLevel(_CURRENT_LEVEL)
    root.addHandler(console)
    root.addHandler(_HANDLER)

    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s"
                )
            )
            fh.setLevel(_CURRENT_LEVEL)
            root.addHandler(fh)
        except OSError:
            pass

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
