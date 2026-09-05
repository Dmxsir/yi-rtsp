#!/usr/bin/env python3
"""App-owned YI cloud session and private runtime-material broker."""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import struct
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

import yi_cloud_probe as cloud
from yi_camera_manager import CameraDevice, YiCameraManager
from yi_camera_runtime import RuntimeDescriptor, runtime_material_from_manager
from yi_tnp_oracle import CameraMaterial


SESSION_EXPIRED_CATEGORY = "session_expired"
MAX_BROKER_REQUEST = 4096
MAX_BROKER_RESPONSE = 64 * 1024
STABLE_ID_RE = re.compile(r"^[0-9a-f]{20}$")
_SAFE_REASONS = frozenset(
    {
        "account_replace",
        "initial_session",
        "initial_discovery",
        "ha_discovery",
        "manual_discovery",
        "runtime_material",
        "reprobe",
        "auth_expired",
        "availability_material",
    }
)
_T = TypeVar("_T")


def _safe_reason(reason: str) -> str:
    return reason if reason in _SAFE_REASONS else "other"


class YiCloudSession:
    """Single-flight owner of one authenticated account session and its caches."""

    def __init__(self, *, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._lock = threading.RLock()
        self._manager: YiCameraManager | None = None
        self._generation = 0
        self._cloud_login_attempt_total = 0
        self._cloud_login_success_total = 0
        self._cloud_session_reuse_total = 0
        self._cloud_relogin_total = 0
        self._last_login_reason: str | None = None

    def _login_manager(self, environment: Mapping[str, str], reason: str) -> YiCameraManager:
        self._cloud_login_attempt_total += 1
        self._last_login_reason = _safe_reason(reason)
        manager = YiCameraManager(timeout=self.timeout, environment=environment)
        try:
            manager.login()
        except Exception:
            manager.close()
            raise
        self._cloud_login_success_total += 1
        return manager

    def _ensure_locked(self, reason: str) -> tuple[YiCameraManager, int]:
        if self._manager is not None:
            self._cloud_session_reuse_total += 1
            return self._manager, self._generation
        manager = self._login_manager(os.environ, reason)
        self._manager = manager
        self._generation += 1
        return manager, self._generation

    def ensure_session(self, reason: str = "initial_session") -> tuple[YiCameraManager, int]:
        with self._lock:
            return self._ensure_locked(reason)

    def _invalidate_locked(self, manager: YiCameraManager, generation: int) -> None:
        if self._manager is manager and self._generation == generation:
            manager.close()
            self._manager = None

    def _run(self, reason: str, operation: Callable[[YiCameraManager], _T]) -> _T:
        with self._lock:
            manager, generation = self._ensure_locked(reason)
            try:
                return operation(manager)
            except cloud.YiCloudError as exc:
                if exc.category != SESSION_EXPIRED_CATEGORY:
                    raise

            self._invalidate_locked(manager, generation)
            self._cloud_relogin_total += 1
            retry_manager, retry_generation = self._ensure_locked("auth_expired")
            try:
                return operation(retry_manager)
            except cloud.YiCloudError as exc:
                if exc.category == SESSION_EXPIRED_CATEGORY:
                    self._invalidate_locked(retry_manager, retry_generation)
                raise

    def replace_credentials(
        self,
        environment: Mapping[str, str],
        commit: Callable[[], None],
    ) -> int:
        """Validate new credentials, commit them, then adopt that exact session."""
        with self._lock:
            candidate = self._login_manager(environment, "account_replace")
            try:
                candidate.refresh_devices()
                camera_count = candidate.camera_count
                commit()
            except Exception:
                candidate.close()
                raise
            previous = self._manager
            self._manager = candidate
            self._generation += 1
            if previous is not None:
                previous.close()
            return camera_count

    def discover(self, *, fetch_tnp: bool = True, reason: str = "manual_discovery") -> list[CameraDevice]:
        return self._run(
            reason,
            lambda manager: manager.discover(fetch_tnp=fetch_tnp, refresh=True),
        )

    def tnp_info(self, stable_id: str) -> dict[str, Any]:
        def get_info(manager: YiCameraManager) -> dict[str, Any]:
            if manager.camera_count == 0:
                manager.refresh_devices()
            return dict(manager._tnp_info(stable_id))

        return self._run("availability_material", get_info)

    def runtime_material(self, stable_id: str) -> tuple[CameraMaterial, RuntimeDescriptor]:
        def resolve(manager: YiCameraManager) -> tuple[CameraMaterial, RuntimeDescriptor]:
            if manager.camera_count == 0:
                manager.refresh_devices()
            return runtime_material_from_manager(manager, stable_id)

        return self._run("runtime_material", resolve)

    def safe_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session_available": self._manager is not None,
                "session_generation": self._generation,
                "cloud_login_attempt_total": self._cloud_login_attempt_total,
                "cloud_login_success_total": self._cloud_login_success_total,
                "cloud_session_reuse_total": self._cloud_session_reuse_total,
                "cloud_relogin_total": self._cloud_relogin_total,
                "last_cloud_login_reason": self._last_login_reason,
                "secrets_exposed": False,
            }

    def close(self) -> None:
        with self._lock:
            if self._manager is not None:
                self._manager.close()
                self._manager = None


def _material_payload(material: CameraMaterial, descriptor: RuntimeDescriptor) -> dict[str, Any]:
    return {
        "ok": True,
        "material": {
            "name": material.name,
            "raw_model": material.raw_model,
            "normalized_model": material.normalized_model,
            "cloud_uid": material.cloud_uid,
            "pppp_did": material.pppp_did,
            "server": material.server,
            "device_key": material.device_key,
            "password": material.password,
            "encrypted": material.encrypted,
            "wakeup": material.wakeup,
        },
        "descriptor": descriptor.safe_dict(),
    }


def _read_line(connection: socket.socket, limit: int) -> bytes:
    received = bytearray()
    while len(received) <= limit:
        chunk = connection.recv(min(4096, limit + 1 - len(received)))
        if not chunk:
            break
        received.extend(chunk)
        if b"\n" in chunk:
            break
    line, separator, trailing = bytes(received).partition(b"\n")
    if not separator or trailing or len(line) > limit:
        raise RuntimeError("invalid broker frame")
    return line


class YiMaterialBroker:
    """Private local socket that hands runtime material to scrubbed children."""

    def __init__(self, path: Path, cloud_session: YiCloudSession) -> None:
        self.path = path.expanduser().resolve()
        self.cloud_session = cloud_session
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._startup_failed = False

    def _peer_allowed(self, connection: socket.socket) -> bool:
        if not (
            hasattr(socket, "SO_PEERCRED")
            and hasattr(os, "getuid")
            and os.name == "posix"
        ):
            return True
        try:
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return uid == os.getuid()
        except OSError:
            return False

    def _handle(self, connection: socket.socket) -> None:
        material: CameraMaterial | None = None
        response = b""
        try:
            connection.settimeout(12.0)
            if not self._peer_allowed(connection):
                raise RuntimeError("peer rejected")
            request = json.loads(_read_line(connection, MAX_BROKER_REQUEST).decode("utf-8"))
            if not isinstance(request, dict) or set(request) != {"stable_id"}:
                raise RuntimeError("invalid broker request")
            stable_id = request.get("stable_id")
            if not isinstance(stable_id, str) or not STABLE_ID_RE.fullmatch(stable_id):
                raise RuntimeError("invalid stable id")
            material, descriptor = self.cloud_session.runtime_material(stable_id)
            response = json.dumps(
                _material_payload(material, descriptor),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            if len(response) > MAX_BROKER_RESPONSE:
                raise RuntimeError("broker response too large")
        except Exception:
            response = b'{"ok":false,"error":"material_unavailable"}\n'
        try:
            connection.sendall(response)
        except OSError:
            pass
        finally:
            if material is not None:
                material.clear()
            response = b""
            connection.close()

    def _serve(self) -> None:
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            self._startup_failed = True
            self._ready.set()
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            if os.path.lexists(self.path):
                mode = self.path.lstat().st_mode
                if not stat.S_ISSOCK(mode):
                    raise RuntimeError("broker path is not a socket")
                self.path.unlink()
            server = socket.socket(family, socket.SOCK_STREAM)
            self._server = server
            server.bind(str(self.path))
            os.chmod(self.path, 0o600)
            server.listen(16)
            server.settimeout(0.5)
            self._ready.set()
            while not self._stop.is_set():
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    continue
                threading.Thread(target=self._handle, args=(connection,), daemon=True).start()
        except Exception:
            self._startup_failed = True
            self._ready.set()
        finally:
            if self._server is not None:
                self._server.close()
                self._server = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._serve, name="yi-material-broker", daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0) or self._startup_failed:
            self.close()
            raise RuntimeError("The private runtime-material broker could not start.")

    def close(self) -> None:
        self._stop.set()
        server = self._server
        if server is not None:
            server.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        if os.path.lexists(self.path):
            try:
                if stat.S_ISSOCK(self.path.lstat().st_mode):
                    self.path.unlink()
            except OSError:
                pass


def request_runtime_material(
    path: Path,
    stable_id: str,
    *,
    timeout: float = 12.0,
) -> tuple[CameraMaterial, RuntimeDescriptor]:
    """Fetch one bounded material response from the App-owned broker."""
    if not STABLE_ID_RE.fullmatch(stable_id):
        raise RuntimeError("Invalid camera stable_id")
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        raise RuntimeError("Local runtime-material sockets are unavailable")
    connection = socket.socket(family, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(str(path))
        request = json.dumps({"stable_id": stable_id}, separators=(",", ":")).encode("utf-8") + b"\n"
        connection.sendall(request)
        payload = json.loads(_read_line(connection, MAX_BROKER_RESPONSE).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("Runtime material is unavailable") from exc
    finally:
        connection.close()
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("Runtime material is unavailable")
    material_values = payload.get("material")
    descriptor_values = payload.get("descriptor")
    if not isinstance(material_values, dict) or not isinstance(descriptor_values, dict):
        raise RuntimeError("Runtime material response is malformed")
    try:
        material = CameraMaterial(**material_values)
        descriptor = RuntimeDescriptor(**descriptor_values)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Runtime material response is malformed") from exc
    return material, descriptor
