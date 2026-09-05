#!/usr/bin/env python3
"""Reusable state core for the YI Home App/Add-on service.

The backend owns secret-safe camera discovery, capability state, per-camera
runtime lifecycle state and optional App-owned media publication state. HTTP
remains a separate layer in yi_addon_service.py.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import yi_cloud_probe as cloud
from yi_camera_manager import CameraDevice
from yi_cloud_session import YiCloudSession
from yi_capability_cache import CapabilityRecord, YiCapabilityCache
from yi_capability_probe_runtime import CapabilityProbeError, YiCapabilityProbe
from yi_media_publisher import YiGo2RTCPublisher
from yi_runtime_lifecycle import YiRuntimeLifecycleManager


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_error(exc: Exception) -> dict[str, str]:
    if isinstance(exc, cloud.YiCloudError):
        return {"category": exc.category, "message": exc.safe_message}
    if isinstance(exc, CapabilityProbeError):
        return {"category": exc.category, "message": exc.safe_message}
    if isinstance(exc, (TimeoutError, OSError)):
        return {"category": "transport_error", "message": "A backend transport operation failed."}
    return {"category": "backend_error", "message": "A backend operation failed."}


@dataclass(frozen=True)
class CameraState:
    device: CameraDevice
    capability: CapabilityRecord | None

    def safe_dict(
        self,
        runtime: dict[str, Any] | None = None,
        publication: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        item = self.device.safe_dict()
        item["capability"] = self.capability.safe_dict() if self.capability is not None else None
        item["capability_state"] = "proven" if self.capability is not None else "unprobed"
        if runtime is not None:
            item["runtime_state"] = runtime.get("runtime_state")
            item["runtime"] = runtime
        item["publication"] = publication
        return item

    def status_dict(
        self,
        runtime: dict[str, Any] | None = None,
        publication: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        capability = self.capability
        runtime_state = runtime.get("runtime_state") if runtime is not None else "not_managed"
        return {
            "stable_id": self.device.stable_id,
            "stream_id": self.device.stream_id,
            "name": self.device.name,
            "cloud_online_reported": self.device.cloud_online_reported,
            "transport": self.device.transport,
            "runtime_state": runtime_state,
            "runtime": runtime,
            "publication": publication,
            "capability_state": "proven" if capability is not None else "unprobed",
            "profile": capability.profile if capability is not None else None,
            "media": (
                {
                    "video": {
                        "codec": capability.video_codec,
                        "width": capability.video_width,
                        "height": capability.video_height,
                    },
                    "audio": {
                        "codec": capability.audio_codec,
                        "sample_rate": capability.audio_sample_rate,
                        "channels": capability.audio_channels,
                    },
                }
                if capability is not None
                else None
            ),
            "secrets_exposed": False,
        }


class YiAddonBackend:
    """Thread-safe secret-safe state core for the Add-on API."""

    API_VERSION = "v1"
    SERVICE_NAME = "yi-home-addon"

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        capability_cache: YiCapabilityCache | None = None,
        lifecycle: YiRuntimeLifecycleManager | None = None,
        capability_probe: YiCapabilityProbe | None = None,
        media_publisher: YiGo2RTCPublisher | None = None,
        cloud_session: YiCloudSession | None = None,
    ) -> None:
        self.timeout = timeout
        self.capability_cache = capability_cache or YiCapabilityCache()
        self.lifecycle = lifecycle
        self.capability_probe = capability_probe
        self.media_publisher = media_publisher
        self.cloud_session = cloud_session if cloud_session is not None else YiCloudSession(timeout=timeout)
        self._lock = threading.RLock()
        self._cameras: dict[str, CameraState] = {}
        self._last_discovery_at: str | None = None
        self._last_error: dict[str, str] | None = None
        self._publisher_error: dict[str, str] | None = None
        self._discovery_generation = 0
        self._camera_operation_locks: dict[str, threading.RLock] = {}

    def _capability_for(self, stable_id: str) -> CapabilityRecord | None:
        try:
            return self.capability_cache.get_success(stable_id)
        except (RuntimeError, ValueError, OSError):
            return None

    def _runtime_for(self, stable_id: str) -> dict[str, Any] | None:
        if self.lifecycle is None:
            return None
        try:
            return self.lifecycle.status(stable_id)
        except (RuntimeError, ValueError, OSError):
            return {
                "stable_id": stable_id,
                "runtime_state": "error",
                "desired_running": False,
                "process_alive": False,
                "pid": None,
                "last_error": "Runtime status is unavailable.",
                "secrets_exposed": False,
            }

    def _publication_for(self, stable_id: str) -> dict[str, Any] | None:
        if self.media_publisher is None:
            return None
        try:
            item = self.media_publisher.camera_endpoint(stable_id)
            item["publisher_ready"] = self.media_publisher.safe_status().get("ready", False)
            return item
        except (RuntimeError, ValueError, OSError):
            return {
                "configured": False,
                "publisher_ready": False,
                "error": "media_publication_unavailable",
                "secrets_exposed": False,
            }

    def _operation_lock(self, stable_id: str) -> threading.RLock:
        with self._lock:
            lock = self._camera_operation_locks.get(stable_id)
            if lock is None:
                lock = threading.RLock()
                self._camera_operation_locks[stable_id] = lock
            return lock

    def _resume_runtime_after_reprobe(self, stable_id: str, should_resume: bool) -> tuple[bool, dict[str, Any] | None]:
        if not should_resume or self.lifecycle is None:
            return False, self._runtime_for(stable_id)
        try:
            runtime = self.lifecycle.start(stable_id)
        except (RuntimeError, ValueError, OSError):
            return False, self._runtime_for(stable_id)
        return bool(runtime.get("desired_running")), runtime

    def discover(self, *, fetch_tnp: bool = True, reason: str = "manual_discovery") -> dict[str, Any]:
        try:
            devices = self.cloud_session.discover(fetch_tnp=fetch_tnp, reason=reason)
            snapshot = {
                device.stable_id: CameraState(device=device, capability=self._capability_for(device.stable_id))
                for device in devices
            }
        except Exception as exc:
            safe = _safe_error(exc)
            with self._lock:
                self._last_error = safe
            raise
        publisher_ready = False
        publisher_error: dict[str, str] | None = None
        if self.media_publisher is not None:
            try:
                publisher_ready = bool(self.media_publisher.sync_streams(snapshot.keys()).get("ready"))
            except (RuntimeError, ValueError, OSError):
                publisher_error = {
                    "category": "media_publisher_error",
                    "message": "The managed media publisher could not be synchronized.",
                }

        now = _utc_now()
        with self._lock:
            self._cameras = snapshot
            self._last_discovery_at = now
            self._last_error = None
            self._publisher_error = publisher_error
            self._discovery_generation += 1
            generation = self._discovery_generation

        return {
            "ok": True,
            "camera_count": len(snapshot),
            "probe_candidates": sum(1 for camera in snapshot.values() if camera.device.probe_candidate),
            "proven_capabilities": sum(1 for camera in snapshot.values() if camera.capability is not None),
            "discovered_at": now,
            "generation": generation,
            "runtime_lifecycle_ready": self.lifecycle is not None,
            "reprobe_ready": self.capability_probe is not None,
            "media_publisher_enabled": self.media_publisher is not None,
            "media_publisher_ready": publisher_ready,
            "media_publisher_error": publisher_error,
            "secrets_exposed": False,
        }

    def health(self) -> dict[str, Any]:
        with self._lock:
            camera_count = len(self._cameras)
            last_discovery_at = self._last_discovery_at
            generation = self._discovery_generation
            last_error = dict(self._last_error) if self._last_error is not None else None
            publisher_error = dict(self._publisher_error) if self._publisher_error is not None else None
        publisher = self.media_publisher.safe_status() if self.media_publisher is not None else None
        return {
            "ok": True,
            "service": self.SERVICE_NAME,
            "api_version": self.API_VERSION,
            "camera_count": camera_count,
            "last_discovery_at": last_discovery_at,
            "discovery_generation": generation,
            "last_error": last_error,
            "runtime_lifecycle_ready": self.lifecycle is not None,
            "reprobe_ready": self.capability_probe is not None,
            "managed_runtime_count": self.lifecycle.managed_count() if self.lifecycle is not None else 0,
            "media_publisher": publisher,
            "media_publisher_error": publisher_error,
            "cloud_session": self.cloud_session.safe_status(),
            "secrets_exposed": False,
        }

    def cameras(self) -> dict[str, Any]:
        with self._lock:
            states = list(self._cameras.values())
            last_discovery_at = self._last_discovery_at
        cameras = [
            state.safe_dict(
                self._runtime_for(state.device.stable_id),
                self._publication_for(state.device.stable_id),
            )
            for state in states
        ]
        return {
            "ok": True,
            "camera_count": len(cameras),
            "cameras": cameras,
            "last_discovery_at": last_discovery_at,
            "secrets_exposed": False,
        }

    def camera(self, stable_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._cameras.get(stable_id)
        if state is None:
            return None
        return {
            "ok": True,
            "camera": state.safe_dict(self._runtime_for(stable_id), self._publication_for(stable_id)),
            "secrets_exposed": False,
        }

    def camera_status(self, stable_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._cameras.get(stable_id)
        if state is None:
            return None
        return {
            "ok": True,
            "status": state.status_dict(self._runtime_for(stable_id), self._publication_for(stable_id)),
        }

    def _camera_exists(self, stable_id: str) -> bool:
        with self._lock:
            return stable_id in self._cameras

    def _runtime_operation(self, stable_id: str, operation: str) -> tuple[int, dict[str, Any]]:
        if not self._camera_exists(stable_id):
            return 404, {
                "ok": False,
                "error": {"code": "camera_not_found", "message": "Unknown camera stable_id."},
                "secrets_exposed": False,
            }
        if self.lifecycle is None:
            return 409, {
                "ok": False,
                "error": {
                    "code": "runtime_lifecycle_not_ready",
                    "message": "The camera runtime lifecycle manager is not enabled.",
                },
                "secrets_exposed": False,
            }

        with self._operation_lock(stable_id):
            try:
                if operation == "start":
                    runtime = self.lifecycle.start(stable_id)
                elif operation == "stop":
                    runtime = self.lifecycle.stop(stable_id)
                elif operation == "restart":
                    runtime = self.lifecycle.restart(stable_id)
                else:
                    raise ValueError("unsupported runtime operation")
            except (RuntimeError, ValueError, OSError):
                return 500, {
                    "ok": False,
                    "error": {
                        "code": "runtime_operation_failed",
                        "message": f"Camera {operation} operation failed.",
                    },
                    "secrets_exposed": False,
                }
        return 200, {
            "ok": True,
            "operation": operation,
            "runtime": runtime,
            "publication": self._publication_for(stable_id),
            "secrets_exposed": False,
        }

    def start_camera(self, stable_id: str) -> tuple[int, dict[str, Any]]:
        return self._runtime_operation(stable_id, "start")

    def stop_camera(self, stable_id: str) -> tuple[int, dict[str, Any]]:
        return self._runtime_operation(stable_id, "stop")

    def restart_camera(self, stable_id: str) -> tuple[int, dict[str, Any]]:
        return self._runtime_operation(stable_id, "restart")

    def reprobe_camera(self, stable_id: str) -> tuple[int, dict[str, Any]]:
        if not self._camera_exists(stable_id):
            return 404, {
                "ok": False,
                "error": {"code": "camera_not_found", "message": "Unknown camera stable_id."},
                "secrets_exposed": False,
            }
        if self.capability_probe is None:
            return 409, {
                "ok": False,
                "error": {
                    "code": "reprobe_not_ready",
                    "message": "The live capability probe is not enabled.",
                },
                "secrets_exposed": False,
            }

        with self._operation_lock(stable_id):
            runtime_before = self._runtime_for(stable_id)
            resume_runtime = bool(runtime_before and runtime_before.get("desired_running"))
            if resume_runtime and self.lifecycle is not None:
                try:
                    self.lifecycle.stop(stable_id)
                except (RuntimeError, ValueError, OSError):
                    return 500, {
                        "ok": False,
                        "error": {
                            "code": "reprobe_prepare_failed",
                            "message": "The camera runtime could not be isolated for reprobe.",
                        },
                        "secrets_exposed": False,
                    }

            try:
                result = self.capability_probe.probe(stable_id, session_handoff=resume_runtime)
            except CapabilityProbeError as exc:
                resumed_ok, resumed = self._resume_runtime_after_reprobe(stable_id, resume_runtime)
                return 502, {
                    "ok": False,
                    "operation": "reprobe",
                    "error": {"code": exc.category, "message": exc.safe_message},
                    "runtime_was_running": resume_runtime,
                    "runtime_resumed": resumed_ok,
                    "runtime": resumed,
                    "publication": self._publication_for(stable_id),
                    "secrets_exposed": False,
                }
            except (RuntimeError, ValueError, OSError):
                resumed_ok, resumed = self._resume_runtime_after_reprobe(stable_id, resume_runtime)
                return 500, {
                    "ok": False,
                    "operation": "reprobe",
                    "error": {
                        "code": "reprobe_failed",
                        "message": "The live camera capability reprobe failed.",
                    },
                    "runtime_was_running": resume_runtime,
                    "runtime_resumed": resumed_ok,
                    "runtime": resumed,
                    "publication": self._publication_for(stable_id),
                    "secrets_exposed": False,
                }

            with self._lock:
                state = self._cameras.get(stable_id)
                if state is not None:
                    self._cameras[stable_id] = CameraState(device=state.device, capability=result.capability)

            resumed_ok, resumed = self._resume_runtime_after_reprobe(stable_id, resume_runtime)
            if resume_runtime and not resumed_ok:
                return 500, {
                    "ok": False,
                    "operation": "reprobe",
                    "probe": result.safe_dict(),
                    "error": {
                        "code": "runtime_resume_failed",
                        "message": "The capability probe passed but the camera runtime could not be resumed.",
                    },
                    "runtime_was_running": True,
                    "runtime_resumed": False,
                    "runtime": resumed,
                    "publication": self._publication_for(stable_id),
                    "secrets_exposed": False,
                }

            return 200, {
                "ok": True,
                "operation": "reprobe",
                "probe": result.safe_dict(),
                "runtime_was_running": resume_runtime,
                "runtime_resumed": resumed_ok if resume_runtime else False,
                "runtime": resumed,
                "publication": self._publication_for(stable_id),
                "secrets_exposed": False,
            }

    def shutdown(self) -> None:
        if self.capability_probe is not None:
            self.capability_probe.shutdown()
        if self.lifecycle is not None:
            self.lifecycle.shutdown_all()
        if self.media_publisher is not None:
            self.media_publisher.stop()
        self.cloud_session.close()
