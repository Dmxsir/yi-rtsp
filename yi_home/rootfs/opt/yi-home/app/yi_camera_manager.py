#!/usr/bin/env python3
"""Generic YI account camera discovery and material manager.

Phase 6 removes the Phase 3 single-camera assumptions from discovery. It logs
in once, enumerates every camera returned by /v4/devices/list, assigns a stable
secret-safe identifier derived from the cloud UID, and can validate TNP
connection material for every eligible camera without opening a PPPP session.

No camera password, UID, DID, InitString, license, token or token secret is
printed by this module. Runtime streaming/probing is intentionally a separate
step so discovery cannot disturb currently running production relays.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import yi_cloud_probe as cloud
import yi_tnp_oracle as oracle
from yi_stream_identity import media_stream_name


def _required_env(name: str, environment: Mapping[str, str] | None = None) -> str:
    source = os.environ if environment is None else environment
    value = source.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


def _stable_id(uid: str) -> str:
    """Return a deterministic identifier without exposing the YI cloud UID."""
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class CameraDevice:
    stable_id: str
    stream_id: str
    name: str
    raw_model: str
    normalized_model: str
    p2p_type: int
    transport: str
    cloud_online_reported: bool
    encrypted: bool | None
    wakeup: bool
    has_pincode: bool
    credential_status: str
    tnp_material_ready: bool
    tnp_material_status: str
    probe_candidate: bool

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


class YiCameraManager:
    """One authenticated YI account session with secret-safe camera inventory."""

    def __init__(self, timeout: float = 10.0, *, environment: Mapping[str, str] | None = None) -> None:
        self.timeout = timeout
        self._environment = dict(os.environ if environment is None else environment)
        self.region = self._environment.get("YI_REGION", "eu").casefold()
        if self.region not in cloud.GATEWAY_HOSTS:
            raise RuntimeError(f"Unsupported YI_REGION: {self.region!r}")
        self.country = _required_env("YI_COUNTRY", self._environment).upper()
        self.host = cloud.GATEWAY_HOSTS[self.region]
        self.headers = cloud.request_headers(
            self.country,
            _required_env("YI_DEVICE_MODEL", self._environment),
            _required_env("YI_ANDROID_VERSION", self._environment),
            _required_env("YI_LANGUAGE", self._environment),
        )
        self._user_id = ""
        self._token = ""
        self._token_secret = ""
        self._cameras: dict[str, dict[str, Any]] = {}
        self._tnp_cache: dict[str, dict[str, Any]] = {}
        self.request_diagnostics: list[dict[str, Any]] = []

    def close(self) -> None:
        self._user_id = self._token = self._token_secret = ""
        self._cameras.clear()
        self._tnp_cache.clear()
        self._environment.clear()

    @property
    def authenticated(self) -> bool:
        return bool(self._user_id and self._token and self._token_secret)

    @property
    def camera_count(self) -> int:
        return len(self._cameras)

    def login(self) -> None:
        account = _required_env("YI_ACCOUNT", self._environment)
        account_password = _required_env("YI_PASSWORD", self._environment)
        login, diag = cloud.get_json(
            self.host,
            "/v4/users/login",
            cloud.login_params(
                self.region,
                account,
                account_password,
                _required_env("YI_DEVICE_BRAND", self._environment),
                _required_env("YI_DEVICE_MODEL", self._environment),
                _required_env("YI_ANDROID_VERSION", self._environment),
            ),
            self.headers,
            self.timeout,
            auth_request=True,
        )
        self.request_diagnostics.append(diag)
        data = login.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected login schema")
        user_id = cloud._user_id(data.get("userid"))
        token = data.get("token")
        token_secret = data.get("token_secret")
        if user_id is None or not isinstance(token, str) or not token or not isinstance(token_secret, str) or not token_secret:
            raise RuntimeError("Required live authentication state is unavailable")
        self._user_id = user_id
        self._token = token
        self._token_secret = token_secret

    def refresh_devices(self) -> None:
        if not self.authenticated:
            raise RuntimeError("A YI cloud session is required before camera discovery")
        devices, diag = cloud.get_json(
            self.host,
            "/v4/devices/list",
            cloud.device_list_params(self._user_id, self._token, self._token_secret),
            self.headers,
            self.timeout,
        )
        self.request_diagnostics.append(diag)
        cameras = devices.get("data")
        if not isinstance(cameras, list) or not all(isinstance(item, dict) for item in cameras):
            raise RuntimeError("Unexpected devices/list schema")

        seen: set[str] = set()
        self._cameras.clear()
        self._tnp_cache.clear()
        for item in cameras:
            uid = item.get("uid")
            if not isinstance(uid, str) or not uid:
                continue
            stable = _stable_id(uid)
            if stable in seen:
                raise RuntimeError("Stable camera identifier collision")
            seen.add(stable)
            self._cameras[stable] = dict(item)

    def login_and_list(self) -> None:
        self.login()
        self.refresh_devices()

    def _tnp_info(self, stable_id: str) -> dict[str, Any]:
        cached = self._tnp_cache.get(stable_id)
        if cached is not None:
            return cached
        camera = self._cameras[stable_id]
        uid = camera.get("uid")
        if not isinstance(uid, str) or not uid:
            raise RuntimeError("Camera has no usable UID")
        response, diag = cloud.get_json(
            self.host,
            "/v4/tnp/device_info",
            cloud.tnp_device_info_params(self._user_id, uid, self._token, self._token_secret),
            self.headers,
            self.timeout,
        )
        self.request_diagnostics.append(diag)
        data = response.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected tnp/device_info schema")
        self._tnp_cache[stable_id] = data
        return data

    @staticmethod
    def _credential_state(camera: Mapping[str, Any]) -> tuple[str, bool]:
        if camera.get("hasPincode") is True:
            return "pin_required", False
        uid = camera.get("uid")
        encrypted_password = camera.get("password")
        if not isinstance(uid, str) or not uid:
            return "uid_missing", False
        if encrypted_password == "":
            return "apk_default_unverified", False
        if not isinstance(encrypted_password, str) or not encrypted_password:
            return "camera_password_missing", False
        try:
            plaintext = cloud._decrypt_camera_password(uid, encrypted_password)
            usable = bool(plaintext)
            plaintext = ""
            return ("decrypted" if usable else "decrypted_empty"), usable
        except (ValueError, UnicodeDecodeError):
            return "decrypt_failed", False

    def _device(self, stable_id: str, *, fetch_tnp: bool) -> CameraDevice:
        camera = self._cameras[stable_id]
        name = str(camera.get("name", ""))
        raw_model = str(camera.get("model", ""))
        normalized = cloud.normalize_model(camera.get("model"), camera.get("did"))
        p2p_type = camera.get("type") if isinstance(camera.get("type"), int) else -1
        transport = {0: "tutk", 1: "langtao", 2: "tnp"}.get(p2p_type, "unknown")
        has_pincode = camera.get("hasPincode") is True
        credential_status, credential_usable = self._credential_state(camera)

        encrypted: bool | None = None
        wakeup = False
        try:
            ipc = cloud._ipc_params(camera)
            wakeup = ipc.get("wakeup") is True
            if "p2p_encrypt" in ipc:
                encrypted = cloud._p2p_encrypt(ipc.get("p2p_encrypt"))
        except cloud.YiCloudError:
            pass

        tnp_ready = False
        tnp_status = "not_requested"
        if fetch_tnp:
            if p2p_type != 2:
                tnp_status = "not_tnp_transport"
            elif not credential_usable:
                tnp_status = f"credential_{credential_status}"
            else:
                try:
                    info = self._tnp_info(stable_id)
                    did = info.get("DID")
                    init = info.get("InitString")
                    license_value = info.get("License")
                    device_key = license_value.split(":", 1)[0] if isinstance(license_value, str) else ""
                    tnp_ready = all(
                        isinstance(value, str) and bool(value)
                        for value in (did, init, license_value, device_key)
                    )
                    tnp_status = "ready" if tnp_ready else "missing_fields"
                except cloud.YiCloudError as exc:
                    if exc.category == "session_expired":
                        raise
                    tnp_status = "device_info_failed"
                except RuntimeError:
                    tnp_status = "device_info_failed"

        probe_candidate = p2p_type == 2 and credential_usable and (tnp_ready if fetch_tnp else True)
        return CameraDevice(
            stable_id=stable_id,
            stream_id=media_stream_name(stable_id),
            name=name,
            raw_model=raw_model,
            normalized_model=normalized,
            p2p_type=p2p_type,
            transport=transport,
            cloud_online_reported=camera.get("online") is True,
            encrypted=encrypted,
            wakeup=wakeup,
            has_pincode=has_pincode,
            credential_status=credential_status,
            tnp_material_ready=tnp_ready,
            tnp_material_status=tnp_status,
            probe_candidate=probe_candidate,
        )

    def discover(self, *, fetch_tnp: bool = False, refresh: bool = False) -> list[CameraDevice]:
        if not self.authenticated:
            self.login()
        if refresh or not self._cameras:
            self.refresh_devices()
        return [self._device(stable_id, fetch_tnp=fetch_tnp) for stable_id in self._cameras]

    def material_for(self, stable_id: str) -> oracle.CameraMaterial:
        """Return secret-bearing runtime material for one discovered TNP camera."""
        if stable_id not in self._cameras:
            raise KeyError(stable_id)
        camera = self._cameras[stable_id]
        if camera.get("type") != 2:
            raise RuntimeError("Camera is not a TNP transport device")
        if camera.get("hasPincode") is True:
            raise RuntimeError("Camera requires PIN-gated credential retrieval")
        uid = camera.get("uid")
        encrypted_password = camera.get("password")
        if not isinstance(uid, str) or not uid:
            raise RuntimeError("Camera UID is unavailable")
        if not isinstance(encrypted_password, str) or not encrypted_password:
            raise RuntimeError("Encrypted camera password is unavailable")
        password = cloud._decrypt_camera_password(uid, encrypted_password)
        ipc = cloud._ipc_params(camera)
        encrypted = cloud._p2p_encrypt(ipc.get("p2p_encrypt"))
        info = self._tnp_info(stable_id)
        did = info.get("DID")
        server = info.get("InitString")
        license_value = info.get("License")
        if not all(isinstance(value, str) and value for value in (did, server, license_value, password)):
            raise RuntimeError("Camera is missing required TNP connection material")
        device_key = license_value.split(":", 1)[0]
        if not device_key:
            raise RuntimeError("TNP license has no device key")
        return oracle.CameraMaterial(
            name=str(camera.get("name", "")),
            raw_model=str(camera.get("model", "")),
            normalized_model=cloud.normalize_model(camera.get("model"), camera.get("did")),
            cloud_uid=uid,
            pppp_did=did,
            server=server,
            device_key=device_key,
            password=password,
            encrypted=encrypted,
            wakeup=ipc.get("wakeup") is True,
        )


def _safe_request_summary(diag: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: diag.get(key)
        for key in ("endpoint", "http_status", "yi_code", "ok", "error_category")
        if key in diag
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Generic secret-safe YI camera manager")
    p.add_argument("command", choices=("discover",))
    p.add_argument("--env-file", type=Path, required=True)
    p.add_argument("--with-tnp", action="store_true", help="validate /v4/tnp/device_info for each eligible TNP camera")
    p.add_argument("--timeout", type=float, default=10.0)
    args = p.parse_args()

    if not args.env_file.is_file():
        raise SystemExit(f"env file not found: {args.env_file}")
    oracle.load_env_file(args.env_file)

    manager = YiCameraManager(timeout=args.timeout)
    try:
        devices = manager.discover(fetch_tnp=args.with_tnp)
        result = {
            "ok": True,
            "phase": "6A",
            "region": manager.region,
            "country": manager.country,
            "camera_count": len(devices),
            "tnp_probe_candidates": sum(1 for device in devices if device.probe_candidate),
            "cameras": [device.safe_dict() for device in devices],
            "requests": [_safe_request_summary(item) for item in manager.request_diagnostics],
            "secrets_exposed": False,
            "production_modified": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
