#!/usr/bin/env python3
"""Secret-safe host coordinator for Phase 3E TNP transport over Bionic PPPP.

This phase proves PPPP_Write/PPPP_Read on channel 0 without ADB or a phone.
It preserves the already-proven uninterrupted TNP startup burst
4881 -> 9029 -> 768, verifies the first 4882 authentication response, sends
STOP_LIVE (767), and then closes the PPPP session.

DID, InitString, device key, camera password, per-command nonce/auth material,
and the binary TNP units are passed to the QEMU child only through stdin and
are never printed in normal diagnostics.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import secrets
import string
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yi_cloud_probe as cloud
import yi_tnp_oracle as oracle

TARGET = "מחסן"
RAW_MODEL = "83"
NORMALIZED_MODEL = "y291ga"
TNP_VERSION = 2
CONNECTION_FLAG = 0x4B
SET_RESOLUTION = 4881
START_REALTIME = 9029
START_AUDIO = 768
STOP_LIVE = 767
RESOLUTION = 1
START_USE_COUNT = 2
NONCE_ALPHABET = string.ascii_letters + string.digits


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


def _fresh_exact_target(timeout: float) -> tuple[oracle.CameraMaterial, dict[str, Any]]:
    """Fetch fresh TNP material for exactly the approved warehouse camera.

    Cloud `online` is recorded only as a reported/stale-prone hint. Runtime
    reachability is decided by PPPP_Connect/PPPP_Check/PPPP_Read, not by that
    cloud flag.
    """

    region = os.getenv("YI_REGION", "eu").casefold()
    country = _required_env("YI_COUNTRY").upper()
    if region != "eu" or country != "IL":
        raise RuntimeError("Phase 3E is restricted to the configured EU/IL account")

    host = cloud.GATEWAY_HOSTS[region]
    headers = cloud.request_headers(
        country,
        _required_env("YI_DEVICE_MODEL"),
        _required_env("YI_ANDROID_VERSION"),
        _required_env("YI_LANGUAGE"),
    )
    account_password = _required_env("YI_PASSWORD")
    login, login_diag = cloud.get_json(
        host,
        "/v4/users/login",
        cloud.login_params(
            region,
            _required_env("YI_ACCOUNT"),
            account_password,
            _required_env("YI_DEVICE_BRAND"),
            _required_env("YI_DEVICE_MODEL"),
            _required_env("YI_ANDROID_VERSION"),
        ),
        headers,
        timeout,
        auth_request=True,
    )
    login_data = login.get("data")
    if not isinstance(login_data, dict):
        raise RuntimeError("Unexpected login schema")
    user_id = cloud._user_id(login_data.get("userid"))
    token = login_data.get("token")
    token_secret = login_data.get("token_secret")
    if user_id is None or not isinstance(token, str) or not token or not isinstance(token_secret, str) or not token_secret:
        raise RuntimeError("Required live authentication state is unavailable")

    devices, devices_diag = cloud.get_json(
        host,
        "/v4/devices/list",
        cloud.device_list_params(user_id, token, token_secret),
        headers,
        timeout,
    )
    cameras = devices.get("data")
    if not isinstance(cameras, list) or not all(isinstance(item, dict) for item in cameras):
        raise RuntimeError("Unexpected devices/list schema")

    selected = next((item for item in cameras if str(item.get("name", "")) == TARGET), None)
    if selected is None:
        raise RuntimeError("Exact Phase 3E target was not found; fallback refused")

    raw_model = str(selected.get("model", ""))
    if raw_model != RAW_MODEL or selected.get("type") != 2:
        raise RuntimeError(
            f"Phase 3E target identity mismatch: raw_model={raw_model!r}, p2p_type={selected.get('type')!r}"
        )

    uid = selected.get("uid")
    encrypted_password = selected.get("password")
    if not isinstance(uid, str) or not uid:
        raise RuntimeError("Exact Phase 3E target has no usable cloud UID")
    if selected.get("hasPincode") is True:
        raise RuntimeError("Phase 3E target unexpectedly requires PIN-gated password retrieval")
    if not isinstance(encrypted_password, str) or not encrypted_password:
        raise RuntimeError("Exact Phase 3E target has no encrypted camera password")

    camera_password = cloud._decrypt_camera_password(uid, encrypted_password)
    ipc = cloud._ipc_params(selected)
    p2p_encrypt = cloud._p2p_encrypt(ipc.get("p2p_encrypt"))

    tnp, tnp_diag = cloud.get_json(
        host,
        "/v4/tnp/device_info",
        cloud.tnp_device_info_params(user_id, uid, token, token_secret),
        headers,
        timeout,
    )
    tnp_data = tnp.get("data")
    if not isinstance(tnp_data, dict):
        raise RuntimeError("Unexpected tnp/device_info schema")
    pppp_did = tnp_data.get("DID")
    server = tnp_data.get("InitString")
    license_value = tnp_data.get("License")
    if not all(isinstance(value, str) and value for value in (pppp_did, server, license_value, camera_password)):
        raise RuntimeError("Exact Phase 3E target is missing required TNP connection material")
    device_key = license_value.split(":", 1)[0]
    if not device_key:
        raise RuntimeError("TNP license has no usable device-key component")

    material = oracle.CameraMaterial(
        name=TARGET,
        raw_model=raw_model,
        normalized_model=NORMALIZED_MODEL,
        cloud_uid=uid,
        pppp_did=pppp_did,
        server=server,
        device_key=device_key,
        password=camera_password,
        encrypted=p2p_encrypt,
        wakeup=ipc.get("wakeup") is True,
    )
    report = {
        "selected_camera": TARGET,
        "raw_model": raw_model,
        "normalized_model": NORMALIZED_MODEL,
        "p2p_type": 2,
        "cloud_online_reported": selected.get("online") is True,
        "p2p_encrypt": p2p_encrypt,
        "wakeup": material.wakeup,
        "gateway": host,
        "requests": [login_diag, devices_diag, tnp_diag],
        "evidence": "LIVE_CLOUD_EXACT_TARGET",
    }

    del account_password, token, token_secret, encrypted_password, license_value, tnp_data, login, devices, tnp
    return material, report


def _field(value: str) -> bytes:
    raw = value.encode("utf-8")
    if not raw or len(raw) >= 4096 or b"\0" in raw:
        raise RuntimeError("Phase 3E connection material has an invalid shape")
    return raw


def _nonce(length: int) -> str:
    return "".join(secrets.choice(NONCE_ALPHABET) for _ in range(length))


def _auth_info(password: str, encrypted: bool, prefix: str) -> bytes:
    if encrypted:
        nonce = prefix + _nonce(8)
        canonical = f"user=xiaoyiuser&nonce={nonce}".encode("utf-8")
        digest = hmac.new(password.encode("utf-8"), canonical, hashlib.sha1).digest()
        derived = base64.b64encode(digest).decode("ascii")[:15]
        auth = f"{nonce},{derived}".encode("ascii")
    else:
        auth = f"admin,{password}".encode("ascii")
    if len(auth) > 32:
        raise RuntimeError("TNP authentication header exceeds 32 bytes")
    return auth.ljust(32, b"\0")


def _unit(command: int, number: int, payload: bytes, password: str, encrypted: bool, prefix: str) -> bytes:
    auth = _auth_info(password, encrypted, prefix)
    ioctrl = struct.pack(">HHHH", command, number, 0, len(payload)) + auth + payload
    return bytes((TNP_VERSION, 3, 0, 0)) + struct.pack(">I", len(ioctrl)) + ioctrl


def _build_units(material: oracle.CameraMaterial) -> tuple[bytes, bytes, bytes, bytes]:
    prefix = _nonce(7)
    resolution_payload = struct.pack(">II", RESOLUTION, START_USE_COUNT - 1)
    start_payload = bytes((START_USE_COUNT, RESOLUTION, 1, 0))
    audio_payload = bytes(8)
    stop_payload = bytes(8)
    return (
        _unit(SET_RESOLUTION, 1, resolution_payload, material.password, material.encrypted, prefix),
        _unit(START_REALTIME, 2, start_payload, material.password, material.encrypted, prefix),
        _unit(START_AUDIO, 3, audio_payload, material.password, material.encrypted, prefix),
        _unit(STOP_LIVE, 4, stop_payload, material.password, material.encrypted, prefix),
    )


def validate_first_4882(header: bytes, body: bytes) -> bool:
    """Apply the same bounded Phase 3E checks as the native live probe."""
    if len(header) != 8 or header[0] != TNP_VERSION or header[1] != 3:
        return False
    declared_size = struct.unpack(">I", header[4:8])[0]
    if declared_size != len(body) or not 40 <= declared_size <= 4096 - len(header):
        return False
    command, number = struct.unpack(">HH", body[:4])
    auth_result = struct.unpack(">I", body[8:12])[0]
    return command == 4882 and number == 1 and auth_result == 0


def _payload(material: oracle.CameraMaterial, units: tuple[bytes, bytes, bytes, bytes]) -> bytes:
    did = _field(material.pppp_did)
    server = _field(material.server)
    key = _field(material.device_key)
    lengths = (len(did), len(server), len(key), *(len(unit) for unit in units))
    header = b"Y3E1" + bytes((1 if material.wakeup else 0, CONNECTION_FLAG)) + struct.pack(">H", 0)
    header += struct.pack(">IIIIIII", *lengths)
    return header + did + server + key + b"".join(units)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--qemu", default="qemu-aarch64")
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args()

    if not args.env_file.is_file():
        raise RuntimeError(f"env file not found: {args.env_file}")

    oracle.load_env_file(args.env_file)
    material: oracle.CameraMaterial | None = None
    try:
        material, preflight = _fresh_exact_target(timeout=10.0)
        units = _build_units(material)
        payload = _payload(material, units)
        exe = args.target_dir / "android_pppp_tnp_probe"
        if not exe.is_file():
            raise RuntimeError(f"Phase 3E probe executable missing: {exe}")

        secret_values = [
            value.encode("utf-8")
            for value in (material.cloud_uid, material.pppp_did, material.server, material.device_key, material.password)
            if value
        ]
        for unit in units:
            auth = unit[16:48].rstrip(b"\0")
            if auth:
                secret_values.append(auth)

        print("phase3e_host=START")
        print(f"target={material.name}")
        print(f"raw_cloud_model={material.raw_model}")
        print(f"model={material.normalized_model}")
        print(f"cloud_online_reported={str(preflight['cloud_online_reported']).lower()}")
        print(f"p2p_encrypt={str(material.encrypted).lower()}")
        print(f"wakeup={str(material.wakeup).lower()}")
        print("runtime_reachability_source=PPPP_transport_not_cloud_online_flag")
        print("connection_flag=0x4B")
        print("tnp_version=2")
        print("startup_burst=4881,9029,768")
        print("stop_command=767")
        print("secret_transport=stdin_only")
        print("phone_required=false")

        command = [
            args.qemu,
            "-L",
            str(args.runtime),
            "-E",
            "LD_LIBRARY_PATH=/data/local/tmp/yi-phase3e:/system/lib64",
            str(exe),
            "/data/local/tmp/yi-phase3e/libPPPP_API.so",
        ]
        try:
            proc = subprocess.run(
                command,
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=args.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print("PHASE3E_TNP=TIMEOUT", file=sys.stderr)
            return 124

        combined = proc.stdout + b"\n" + proc.stderr
        if any(secret and secret in combined for secret in secret_values):
            raise RuntimeError("Phase 3E child attempted to expose secret connection/auth material")

        if proc.stdout:
            sys.stdout.buffer.write(proc.stdout)
            if not proc.stdout.endswith(b"\n"):
                sys.stdout.buffer.write(b"\n")
        if proc.stderr:
            sys.stderr.buffer.write(proc.stderr)
            if not proc.stderr.endswith(b"\n"):
                sys.stderr.buffer.write(b"\n")

        print(f"probe_exit_code={proc.returncode}")
        if proc.returncode == 0 and b"phase3e_tnp_probe=PASS" in proc.stdout:
            print("PHASE3E_TNP=PASS")
            return 0
        print("PHASE3E_TNP=FAIL")
        return proc.returncode if proc.returncode else 1
    finally:
        if material is not None:
            material.clear()


if __name__ == "__main__":
    raise SystemExit(main())
