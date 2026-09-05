#!/usr/bin/env python3
"""Persistence and live availability adapter for the YI Add-on backend.

The proven YiAddonBackend remains responsible for runtime lifecycle, reprobe and
media publication. This subclass adds durable runtime intent plus the official
PPPP_CheckDevOnline availability path used by the YI Android client.

Availability material (DID/InitString) is transient in-memory state only. It is
never persisted under /data and never returned by the API.
"""

from __future__ import annotations

import threading
from typing import Any

from yi_addon_backend import CameraState, YiAddonBackend, _safe_error, _utc_now
from yi_online_status import AvailabilityRecord, YiOnlineStatusProbe
from yi_runtime_policy import YiRuntimePolicyStore


class YiPersistentAddonBackend(YiAddonBackend):
    """YiAddonBackend with durable intent and authoritative TNP availability."""

    def __init__(
        self,
        *,
        runtime_policy: YiRuntimePolicyStore,
        availability_probe: YiOnlineStatusProbe | None = None,
        availability_refresh_interval: float = 30.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.runtime_policy = runtime_policy
        self.availability_probe = availability_probe
        if self.availability_probe is None and self.lifecycle is not None:
            self.availability_probe = YiOnlineStatusProbe.from_lifecycle(self.lifecycle)
        self.availability_refresh_interval = max(float(availability_refresh_interval), 0.0)
        self._availability: dict[str, AvailabilityRecord] = {}
        self._availability_last_refresh_at: str | None = None
        self._availability_stop = threading.Event()
        self._availability_thread: threading.Thread | None = None
        self._policy_error: dict[str, str] | None = None
        self._restore_pending: set[str] = set()
        self._last_reconcile_at: str | None = None
        self._last_reconcile_restored = 0
        self._last_reconcile_failed = 0
        self._last_reconcile_offline_pending = 0
        self._last_reconcile_unknown_pending = 0
        try:
            self._restore_pending = set(self.runtime_policy.desired_running_ids())
        except (RuntimeError, ValueError, OSError):
            self._policy_error = {
                "category": "runtime_policy_error",
                "message": "Persistent runtime policy could not be read.",
            }

    def _policy_desired(self, stable_id: str) -> bool | None:
        try:
            return self.runtime_policy.desired_running(stable_id)
        except (RuntimeError, ValueError, OSError):
            return None

    def _persist_intent(self, stable_id: str, desired: bool) -> bool:
        try:
            self.runtime_policy.set_desired_running(stable_id, desired)
        except (RuntimeError, ValueError, OSError):
            with self._lock:
                self._policy_error = {
                    "category": "runtime_policy_error",
                    "message": "Persistent runtime policy could not be updated.",
                }
            return False
        with self._lock:
            self._policy_error = None
            if desired:
                self._restore_pending.add(stable_id)
            else:
                self._restore_pending.discard(stable_id)
        return True

    def _availability_record(self, stable_id: str) -> AvailabilityRecord:
        with self._lock:
            record = self._availability.get(stable_id)
        if record is not None:
            return record
        source = "not_configured" if self.availability_probe is None else "pppp_check_dev_online"
        return AvailabilityRecord.unknown(source=source, error=None)

    def _availability_fields(self, stable_id: str) -> dict[str, Any]:
        record = self._availability_record(stable_id)
        return {
            "availability_state": record.state,
            "availability_source": record.source,
            "last_online_at": record.last_online_at,
            "availability_error": record.error,
        }

    def _availability_summary(self) -> dict[str, Any]:
        with self._lock:
            records = list(self._availability.values())
            refreshed = self._availability_last_refresh_at
        return {
            "enabled": self.availability_probe is not None,
            "source": "pppp_check_dev_online" if self.availability_probe is not None else None,
            "online_count": sum(1 for item in records if item.state == "online"),
            "offline_count": sum(1 for item in records if item.state == "offline"),
            "unknown_count": sum(1 for item in records if item.state == "unknown"),
            "last_refresh_at": refreshed,
            "refresh_interval_seconds": self.availability_refresh_interval,
            "secrets_exposed": False,
        }

    def _ensure_availability_thread(self) -> None:
        if self.availability_probe is None or self.availability_refresh_interval <= 0:
            return
        with self._lock:
            if self._availability_thread is not None and self._availability_thread.is_alive():
                return
            self._availability_stop.clear()
            thread = threading.Thread(
                target=self._availability_loop,
                name="yi-online-status-refresh",
                daemon=True,
            )
            self._availability_thread = thread
        thread.start()

    def _availability_loop(self) -> None:
        while not self._availability_stop.wait(self.availability_refresh_interval):
            try:
                self.refresh_availability(stop_event=self._availability_stop)
            except Exception:
                # Availability failures must never crash the App. Existing
                # runtimes are left alone when state is unknown.
                continue

    def refresh_availability(self, *, stop_event: threading.Event | None = None) -> dict[str, Any]:
        if self.availability_probe is None:
            return self._availability_summary()
        results = self.availability_probe.refresh(stop_event)
        now = _utc_now()
        with self._lock:
            for stable_id, record in results.items():
                if stable_id in self._cameras:
                    self._availability[stable_id] = record
            self._availability_last_refresh_at = now
        if stop_event is not None and stop_event.is_set():
            return self._availability_summary()
        reconcile = self._reconcile_runtime_policy()
        payload = self._availability_summary()
        payload["runtime_restore"] = reconcile
        return payload

    def _reconcile_runtime_policy(self) -> dict[str, Any]:
        try:
            desired = set(self.runtime_policy.desired_running_ids())
        except (RuntimeError, ValueError, OSError):
            with self._lock:
                self._policy_error = {
                    "category": "runtime_policy_error",
                    "message": "Persistent runtime policy could not be read.",
                }
            return {
                "enabled": True,
                "desired_running_count": 0,
                "restored_count": 0,
                "pending_count": len(self._restore_pending),
                "offline_pending_count": 0,
                "unknown_pending_count": 0,
                "failed_count": 0,
                "secrets_exposed": False,
            }

        if self.lifecycle is None:
            with self._lock:
                self._restore_pending = set(desired)
            return {
                "enabled": True,
                "desired_running_count": len(desired),
                "restored_count": 0,
                "pending_count": len(desired),
                "offline_pending_count": 0,
                "unknown_pending_count": len(desired),
                "failed_count": 0,
                "secrets_exposed": False,
            }

        with self._lock:
            available = set(self._cameras)
        pending = desired - available
        restored = 0
        failed = 0
        offline_pending = 0
        unknown_pending = 0

        for stable_id in sorted(desired & available):
            availability = self._availability_record(stable_id).state
            runtime = self._runtime_for(stable_id) or {}

            if self.availability_probe is not None and availability == "offline":
                pending.add(stable_id)
                offline_pending += 1
                if runtime.get("desired_running") is True or runtime.get("process_alive") is True:
                    try:
                        self.lifecycle.stop(stable_id)
                    except (RuntimeError, ValueError, OSError):
                        failed += 1
                continue

            if self.availability_probe is not None and availability == "unknown":
                if runtime.get("desired_running") is True:
                    restored += 1
                else:
                    pending.add(stable_id)
                    unknown_pending += 1
                continue

            if runtime.get("desired_running") is True:
                restored += 1
                continue

            try:
                runtime = self.lifecycle.start(stable_id)
            except (RuntimeError, ValueError, OSError):
                pending.add(stable_id)
                failed += 1
                continue
            if runtime.get("desired_running") is True:
                restored += 1
            else:
                pending.add(stable_id)
                failed += 1

        with self._lock:
            self._restore_pending = pending
            self._last_reconcile_at = _utc_now()
            self._last_reconcile_restored = restored
            self._last_reconcile_failed = failed
            self._last_reconcile_offline_pending = offline_pending
            self._last_reconcile_unknown_pending = unknown_pending
            self._policy_error = None
        return {
            "enabled": True,
            "desired_running_count": len(desired),
            "restored_count": restored,
            "pending_count": len(pending),
            "offline_pending_count": offline_pending,
            "unknown_pending_count": unknown_pending,
            "failed_count": failed,
            "secrets_exposed": False,
        }

    def discover(self, *, fetch_tnp: bool = True, reason: str = "manual_discovery") -> dict[str, Any]:
        availability: dict[str, AvailabilityRecord] = {}
        try:
            devices = self.cloud_session.discover(fetch_tnp=fetch_tnp, reason=reason)
            if self.availability_probe is not None:
                availability = self.availability_probe.sync_and_probe(self.cloud_session.tnp_info, devices)
            else:
                availability = {
                    device.stable_id: AvailabilityRecord.unknown(source="not_configured")
                    for device in devices
                }
            snapshot = {
                device.stable_id: CameraState(
                    device=device,
                    capability=self._capability_for(device.stable_id),
                )
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
            self._availability = availability
            self._availability_last_refresh_at = now
            self._last_discovery_at = now
            self._last_error = None
            self._publisher_error = publisher_error
            self._discovery_generation += 1
            generation = self._discovery_generation

        restore = self._reconcile_runtime_policy()
        self._ensure_availability_thread()
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
            "availability": self._availability_summary(),
            "runtime_restore": restore,
            "secrets_exposed": False,
        }

    def _intent_write_error(self) -> tuple[int, dict[str, Any]]:
        return 500, {
            "ok": False,
            "error": {
                "code": "runtime_policy_write_failed",
                "message": "The requested runtime intent could not be persisted.",
            },
            "secrets_exposed": False,
        }

    def _offline_pending_response(self, stable_id: str, operation: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            self._restore_pending.add(stable_id)
        return 202, {
            "ok": True,
            "accepted": True,
            "operation": operation,
            "persisted_desired_running": True,
            "pending_reason": "camera_offline",
            "runtime": self._runtime_for(stable_id),
            "publication": self._publication_for(stable_id),
            **self._availability_fields(stable_id),
            "secrets_exposed": False,
        }

    def start_camera(self, stable_id: str) -> tuple[int, dict[str, Any]]:
        if not self._camera_exists(stable_id) or self.lifecycle is None:
            return super().start_camera(stable_id)
        if not self._persist_intent(stable_id, True):
            return self._intent_write_error()
        if self._availability_record(stable_id).state == "offline":
            return self._offline_pending_response(stable_id, "start")
        status, payload = super().start_camera(stable_id)
        payload["persisted_desired_running"] = True
        payload.update(self._availability_fields(stable_id))
        if status == 200:
            with self._lock:
                self._restore_pending.discard(stable_id)
        return status, payload

    def stop_camera(self, stable_id: str) -> tuple[int, dict[str, Any]]:
        if not self._camera_exists(stable_id) or self.lifecycle is None:
            return super().stop_camera(stable_id)
        if not self._persist_intent(stable_id, False):
            return self._intent_write_error()
        status, payload = super().stop_camera(stable_id)
        payload["persisted_desired_running"] = False
        payload.update(self._availability_fields(stable_id))
        return status, payload

    def restart_camera(self, stable_id: str) -> tuple[int, dict[str, Any]]:
        if not self._camera_exists(stable_id) or self.lifecycle is None:
            return super().restart_camera(stable_id)
        if not self._persist_intent(stable_id, True):
            return self._intent_write_error()
        if self._availability_record(stable_id).state == "offline":
            return self._offline_pending_response(stable_id, "restart")
        status, payload = super().restart_camera(stable_id)
        payload["persisted_desired_running"] = True
        payload.update(self._availability_fields(stable_id))
        if status == 200:
            with self._lock:
                self._restore_pending.discard(stable_id)
        return status, payload

    def reprobe_camera(self, stable_id: str) -> tuple[int, dict[str, Any]]:
        if self._camera_exists(stable_id) and self._availability_record(stable_id).state == "offline":
            return 409, {
                "ok": False,
                "error": {
                    "code": "camera_offline",
                    "message": "The camera is offline and cannot be reprobed.",
                },
                **self._availability_fields(stable_id),
                "secrets_exposed": False,
            }
        return super().reprobe_camera(stable_id)

    def _add_runtime_metadata(self, payload: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
        if payload is None:
            return None
        item = payload.get(key)
        if isinstance(item, dict):
            stable_id = item.get("stable_id")
            if isinstance(stable_id, str):
                item["persisted_desired_running"] = self._policy_desired(stable_id)
                item.update(self._availability_fields(stable_id))
        return payload

    def camera(self, stable_id: str) -> dict[str, Any] | None:
        return self._add_runtime_metadata(super().camera(stable_id), "camera")

    def camera_status(self, stable_id: str) -> dict[str, Any] | None:
        return self._add_runtime_metadata(super().camera_status(stable_id), "status")

    def cameras(self) -> dict[str, Any]:
        payload = super().cameras()
        for item in payload.get("cameras", []):
            if isinstance(item, dict) and isinstance(item.get("stable_id"), str):
                stable_id = item["stable_id"]
                item["persisted_desired_running"] = self._policy_desired(stable_id)
                item.update(self._availability_fields(stable_id))
        payload["availability"] = self._availability_summary()
        return payload

    def health(self) -> dict[str, Any]:
        payload = super().health()
        try:
            policy = self.runtime_policy.safe_status()
        except (RuntimeError, ValueError, OSError):
            policy = {
                "enabled": True,
                "schema_version": 1,
                "desired_running_count": 0,
                "updated_at": None,
                "secrets_exposed": False,
            }
        with self._lock:
            policy.update(
                {
                    "pending_restore_count": len(self._restore_pending),
                    "last_reconcile_at": self._last_reconcile_at,
                    "last_reconcile_restored": self._last_reconcile_restored,
                    "last_reconcile_failed": self._last_reconcile_failed,
                    "last_reconcile_offline_pending": self._last_reconcile_offline_pending,
                    "last_reconcile_unknown_pending": self._last_reconcile_unknown_pending,
                    "last_error": dict(self._policy_error) if self._policy_error is not None else None,
                }
            )
        payload["runtime_persistence"] = policy
        payload["availability"] = self._availability_summary()
        return payload

    def shutdown(self) -> None:
        self._availability_stop.set()
        thread = self._availability_thread
        if thread is not None and thread is not threading.current_thread():
            timeout = (self.availability_probe.timeout if self.availability_probe is not None else 1.0) + 5.0
            thread.join(timeout=timeout)
        if self.availability_probe is not None:
            self.availability_probe.clear()
        super().shutdown()
