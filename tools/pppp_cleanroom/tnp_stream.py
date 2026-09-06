#!/usr/bin/env python3
"""Bounded TNP v2 unit reconstruction over one clean PPPP byte stream."""
from __future__ import annotations

import time
from typing import Protocol


class ChannelReader(Protocol):
    def read_channel(self, channel: int, max_bytes: int, timeout: float) -> bytes: ...


class TnpStreamError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class TnpUnitReader:
    """Stateful reader that retains partial headers/bodies across timeouts."""

    def __init__(self, channel: int, max_record_bytes: int) -> None:
        if channel not in (1, 2, 3) or max_record_bytes < 32:
            raise ValueError("invalid media TNP reader limits")
        self.channel = channel
        self.max_record_bytes = max_record_bytes
        self._buffer = bytearray()
        self._expected = 8

    def _validate_header(self) -> None:
        version, io_type = self._buffer[0], self._buffer[1]
        body_size = int.from_bytes(self._buffer[4:8], "big")
        total = 8 + body_size
        expected_io_type = 2 if self.channel == 1 else 1
        if version < 2 or io_type != expected_io_type:
            category = "AUDIO_TNP_INVALID" if self.channel == 1 else "VIDEO_TNP_INVALID"
            raise TnpStreamError(category)
        if body_size < 24 or total > self.max_record_bytes:
            raise TnpStreamError("TNP_MEDIA_LENGTH_INVALID")
        self._expected = total

    def read_one(self, session: ChannelReader, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self._buffer) < self._expected:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TnpStreamError("MEDIA_CHANNEL_TIMEOUT")
            try:
                chunk = session.read_channel(
                    self.channel,
                    self._expected - len(self._buffer),
                    remaining,
                )
            except RuntimeError as exc:
                category = getattr(exc, "category", None)
                if category == "MEDIA_CHANNEL_TIMEOUT":
                    raise TnpStreamError(category) from exc
                raise
            self._buffer.extend(chunk)
            if self._expected == 8 and len(self._buffer) == 8:
                self._validate_header()

        unit = bytes(self._buffer[: self._expected])
        del self._buffer[: self._expected]
        self._expected = 8
        return unit

    @property
    def partial_bytes(self) -> int:
        return len(self._buffer)
