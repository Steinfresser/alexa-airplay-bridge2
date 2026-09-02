"""Input validation helpers.

Every value that arrives from an HTTP request and later reaches a filesystem
path, a shell command line or a generated configuration file must pass through
this module first.
"""

from __future__ import annotations

import re

# Canonical Bluetooth address form: AA:BB:CC:DD:EE:FF (uppercase hex).
_MAC_RE = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")

# Display names are allowlisted: letters (incl. accented), digits, spaces and a
# very small set of punctuation. Everything a shell or the shairport-sync
# config parser could interpret is excluded by construction.
_NAME_ALLOWED_RE = re.compile(r"^[\w \-\.\+\&\(\)']{1,48}$", re.UNICODE)

# Characters that must never survive into a generated config / hook command.
_NAME_STRIP_RE = re.compile(r"[\"'\\;$`\r\n\t{}<>|&*?!#%=:,/]")

MAX_NAME_LENGTH = 48


def normalise_mac(value: object) -> str:
    """Return the canonical uppercase form of a Bluetooth address, or ""."""
    text = str(value or "").strip().upper()
    if _MAC_RE.match(text):
        return text
    return ""


def is_valid_mac(value: object) -> bool:
    """True only for a well formed Bluetooth address."""
    return bool(normalise_mac(value))


def sanitise_display_name(value: object) -> str:
    """Strip every character that could escape a config string or shell word.

    Returns "" when nothing usable remains. This is the last line of defence:
    callers should also reject names that fail :func:`is_valid_display_name`.
    """
    text = str(value or "")
    text = _NAME_STRIP_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_NAME_LENGTH]


def is_valid_display_name(value: object) -> bool:
    """True when the name matches the allowlist and needs no stripping."""
    text = str(value or "").strip()
    if not text or len(text) > MAX_NAME_LENGTH:
        return False
    return bool(_NAME_ALLOWED_RE.match(text))
