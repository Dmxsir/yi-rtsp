#!/usr/bin/env python3
"""Secret-safe YI TNP availability using the official PPPP online check.

The current YI Android client uses PPPP_CheckDevOnline for TNP availability.
This module wraps the already-proven AArch64 worker under QEMU/Bionic and keeps
DID/InitString material only in process memory. Nothing secret is persisted or
returned by the safe API records.
"""

from __future__ import annotations

import re
import struct
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from yi_camera_manager import CameraDevice

SOURCE = "pppp_check_dev_online"
STABLE_ID_RE = re.compile(r"^[0-9a-f]{20}$")


def _field(value: str) -> bytes:
    raw = value.encode("utf-8")
    if not raw or len(raw) >= 4096 or b"\0" in raw:
        raise RuntimeError("online-check material has an invalid shape")
    return raw


def _payload(p2pid: str, server: str) -> bytes:
    did = _field(p2pid)
    init = _field(server)
    return b"YON1" + struct.pack(">II", len(did), len(init)) + did + init


def _safe_epoch(value: int) -> str | None:
    if value < 946684800 or value > 4102444800:
        return None
    return datetime.fromtimestamp(value, tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class AvailabilityRecord:
    state: str
    source: str
    last_online_at: str | None = None
    native_result: int | None = None
    error: str | None = None

    @classmethod
    def unknown(cls, source: str = SOURCE, error: str | None = None) -> "AvailabilityRecord":
        return cls(state="unknown", source=source, error=error)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "source": self.source,
            "last_online_at": self.last_online_at,
            "native_result": self.native_result,
            "error": self.error,
            "secrets_exposed": False,
        }


class YiOnlineStatusProbe:
    """Cache transient TNP check material and refresh availability without cloud login."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        worker: Path,
        guest_library: str = "/data/local/tmp/yi-online-status/libPPPP_API.so",
        qemu: str = "qemu-aarch64",
        timeout: float = 12.0,
    ) -> None:
        self.runtime_root = runtime_root.expanduser().resolve()
        self.worker = worker.expanduser().resolve()
        self.guest_library = guest_library
        self.qemu = qemu
        self.timeout = timeout
        self._lock = threading.RLock()
        self._materials: dict[str, tuple[str, str]] = {}
        self.validate()

    @classmethod
    def from_lifecycle(cls, lifecycle: Any, *, timeout: float = 12.0) -> "YiOnlineStatusProbe | None":
        config = getattr(lifecycle, "config", None)
        runtime_root = getattr(config, "runtime_root", None)
        qemu = getattr(config, "qemu", "qemu-aarch64")
        if not isinstance(runtime_root, Path):
            return None
        base = runtime_root / "data/local/tmp/yi-online-status"
        worker = base / "android_pppp_online_probe"
        library = base / "libPPPP_API.so"
        if not worker.is_file() or not library.is_file():
            return None
        try:
            return cls(runtime_root=runtime_root, worker=worker, qemu=qemu, timeout=timeout)
        except (RuntimeError, ValueError, OSError):
            return None

    def validate(self) -> None:
        if not self.runtime_root.is_dir():
            raise RuntimeError("online-status Bionic runtime is unavailable")
        if not self.worker.is_file():
            raise RuntimeError("online-status worker is unavailable")
        if not self.guest_library.startswith("/"):
            raise ValueError("guest_library must be an absolute guest path")
        host_library = self.runtime_root / self.guest_library.lstrip("/")
        if not host_library.is_file():
            raise RuntimeError("online-status PPPP library is unavailable")
        if self.timeout <= 0:
            raise ValueError("online-status timeout must be greater than zero")

    @staticmethod
    def _parse(raw: bytes) -> AvailabilityRecord:
        values: dict[str, str] = {}
        for line in raw.decode("utf-8", "replace").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
        state = values.get("device_online")
        if state not in {"online", "offline", "unknown"}:
            raise RuntimeError("native online worker returned no valid status")
        try:
            native_result = int(values.get("PPPP_CheckDevOnline_rc", ""))
            last_online_time = int(values.get("last_online_time", "0"))
        except ValueError as exc:
            raise RuntimeError("native online worker returned malformed diagnostics") from exc
        return AvailabilityRecord(
            state=state,
            source=SOURCE,
            last_online_at=_safe_epoch(last_online_time),
            native_result=native_result,
        )

    def _probe_material(self, p2pid: str, server: str) -> AvailabilityRecord:
        secrets = (p2pid.encode("utf-8"), server.encode("utf-8"))
        try:
            proc = subprocess.run(
                [
                    self.qemu,
                    "-L",
                    str(self.runtime_root),
                    "-E",
                    "LD_LIBRARY_PATH=/data/local/tmp/yi-online-status:/system/lib64",
                    str(self.worker),
                    self.guest_library,
                ],
                input=_payload(p2pid, server),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return AvailabilityRecord.unknown(error="online_check_timeout")
        except OSError:
            return AvailabilityRecord.unknown(error="online_check_failed")

        combined = proc.stdout + b"\n" + proc.stderr
        if any(secret and secret in combined for secret in secrets):
            return AvailabilityRecord.unknown(error="secret_output_rejected")
        if proc.returncode != 0:
            return AvailabilityRecord.unknown(error="online_check_failed")
        try:
            return self._parse(proc.stdout)
        except RuntimeError:
            return AvailabilityRecord.unknown(error="online_check_malformed")

    def sync_and_probe(
        self,
        tnp_info_for: Callable[[str], Mapping[str, Any]],
        devices: Iterable[CameraDevice],
    ) -> dict[str, AvailabilityRecord]:
        materials: dict[str, tuple[str, str]] = {}
        results: dict[str, AvailabilityRecord] = {}
        for device in devices:
            stable_id = device.stable_id
            if device.transport != "tnp" or not STABLE_ID_RE.fullmatch(stable_id):
                results[stable_id] = AvailabilityRecord.unknown(source="unsupported_transport")
                continue
            try:
                info = tnp_info_for(stable_id)  # engine-internal, never exposed
                p2pid = info.get("DID")
                server = info.get("InitString")
                if not isinstance(p2pid, str) or not p2pid or not isinstance(server, str) or not server:
                    raise RuntimeError("online-check material unavailable")
            except Exception:
                results[stable_id] = AvailabilityRecord.unknown(error="online_material_unavailable")
                continue
            materials[stable_id] = (p2pid, server)
            results[stable_id] = self._probe_material(p2pid, server)

        with self._lock:
            self._materials = materials
        return results

    def refresh(self, stop_event: threading.Event | None = None) -> dict[str, AvailabilityRecord]:
        with self._lock:
            materials = dict(self._materials)
        results: dict[str, AvailabilityRecord] = {}
        for stable_id, (p2pid, server) in materials.items():
            if stop_event is not None and stop_event.is_set():
                break
            results[stable_id] = self._probe_material(p2pid, server)
        return results

    def clear(self) -> None:
        with self._lock:
            self._materials.clear()

    def safe_status(self) -> dict[str, Any]:
        with self._lock:
            count = len(self._materials)
        return {
            "enabled": True,
            "source": SOURCE,
            "registered_camera_count": count,
            "secrets_exposed": False,
        }
