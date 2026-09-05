#!/usr/bin/env python3
"""Secret-safe immutable stream identity helpers.

Display names are mutable user metadata and must never be used in media URLs.
All published stream identities derive only from the camera stable_id.
"""

from __future__ import annotations

import re

STABLE_ID_RE = re.compile(r"^[0-9a-f]{20}$")


def normalize_stable_id(value: str) -> str:
    key = value.strip().casefold()
    if not STABLE_ID_RE.fullmatch(key):
        raise ValueError("stable_id must be exactly 20 lowercase hexadecimal characters")
    return key


def media_stream_name(stable_id: str) -> str:
    """Return an immutable, URL-safe go2rtc/RTSP stream name."""
    key = normalize_stable_id(stable_id)
    return f"yi_{key[:12]}"


def media_rtsp_path(stable_id: str) -> str:
    return "/" + media_stream_name(stable_id)
