"""Secret-safe host coordinator and analyzer for the Phase 2C Android oracle."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import yi_cloud_probe as cloud


ORACLE_PORT = 27183
ORACLE_COMPONENT = "com.local.yitnporacle/.OracleActivity"
APP_PACKAGE = "com.local.yitnporacle"
OFFICIAL_DEFAULT_RESOLUTION = 2
START_USE_COUNT = 2
APPROVED_TARGETS = ("מחסן", "POOL")
FORBIDDEN_EVENT_KEYS = {"did", "server", "devicekey", "password", "license", "token", "tokensecret"}


@dataclass(repr=False)
class CameraMaterial:
    name: str
    raw_model: str
    normalized_model: str
    cloud_uid: str
    pppp_did: str
    server: str
    device_key: str
    password: str
    encrypted: bool
    wakeup: bool

    def clear(self) -> None:
        self.cloud_uid = self.pppp_did = self.server = self.device_key = self.password = ""

    def secret_values(self) -> tuple[bytes, ...]:
        return tuple(value.encode("utf-8") for value in (self.cloud_uid, self.pppp_did, self.server, self.device_key, self.password) if value)


def load_env_file(path: Path) -> None:
    """Load simple dotenv assignments into memory without echoing values."""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


def fresh_cloud_preflight(timeout: float = 10.0) -> tuple[CameraMaterial, dict[str, Any]]:
    """Login, discover, select an approved online y291ga, and fetch fresh TNP data."""
    # EU is explicit in the Phase 2C authorization; an environment override may
    # only repeat it, never silently select a different production region.
    region = os.getenv("YI_REGION", "eu").casefold()
    country = _env("YI_COUNTRY").upper()
    if region != "eu" or country != "IL":
        raise RuntimeError("Phase 2C is restricted to the explicitly configured EU/IL account")
    host = cloud.GATEWAY_HOSTS[region]
    headers = cloud.request_headers(country, _env("YI_DEVICE_MODEL"), _env("YI_ANDROID_VERSION"), _env("YI_LANGUAGE"))
    account = _env("YI_ACCOUNT")
    account_password = _env("YI_PASSWORD")

    login, login_diag = cloud.get_json(
        host,
        "/v4/users/login",
        cloud.login_params(region, account, account_password, _env("YI_DEVICE_BRAND"), _env("YI_DEVICE_MODEL"), _env("YI_ANDROID_VERSION")),
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

    by_name = {str(item.get("name", "")): item for item in cameras}
    primary = by_name.get(APPROVED_TARGETS[0])
    fallback = by_name.get(APPROVED_TARGETS[1])
    selected = primary if primary and primary.get("online") is True else fallback if fallback and fallback.get("online") is True else None
    if selected is None:
        raise RuntimeError("No approved Phase 2C y291ga target is currently online")
    raw_model = str(selected.get("model", ""))
    normalized = cloud.normalize_model(raw_model, selected.get("did"))
    if raw_model != "83" or normalized != "y291ga" or selected.get("type") != 2 or selected.get("online") is not True:
        raise RuntimeError("The approved online target no longer matches model 83 / y291ga / TNP")

    uid = selected.get("uid")
    encrypted_password = selected.get("password")
    if not isinstance(uid, str) or not uid or selected.get("hasPincode") is True:
        raise RuntimeError("Selected target does not expose a usable cloud UID/password path")
    if not isinstance(encrypted_password, str) or not encrypted_password:
        raise RuntimeError("Selected target has no encrypted camera password; fallback credentials are not used in Phase 2C")
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
        raise RuntimeError("Selected target is missing required TNP connection material")
    device_key = license_value.split(":", 1)[0]
    if not device_key:
        raise RuntimeError("TNP license has no usable device-key component")

    material = CameraMaterial(
        name=str(selected["name"]),
        raw_model=raw_model,
        normalized_model=normalized,
        cloud_uid=uid,
        pppp_did=pppp_did,
        server=server,
        device_key=device_key,
        password=camera_password,
        encrypted=p2p_encrypt,
        wakeup=ipc.get("wakeup") is True,
    )
    report = {
        "selected_camera": material.name,
        "raw_model": raw_model,
        "normalized_model": normalized,
        "p2p_type": 2,
        "cloud_online": True,
        "DID": "AVAILABLE",
        "InitString": "AVAILABLE",
        "license_device_key": "AVAILABLE",
        "camera_password": "AVAILABLE",
        "p2p_encrypt": p2p_encrypt,
        "wakeup": material.wakeup,
        "appParam_type": cloud._json_type(selected.get("appParam")),
        "gateway": host,
        "requests": [login_diag, devices_diag, tnp_diag],
        "evidence": "LIVE_CLOUD_PROVEN",
    }
    del account_password, token, token_secret, camera_password, license_value, tnp_data, login, devices, tnp
    return material, report


def realtime_payload() -> bytes:
    """Official fresh-default path: set-resolution increments once, then live-start."""
    return bytes((START_USE_COUNT, OFFICIAL_DEFAULT_RESOLUTION, 1, 0))


def _safe_event(raw: bytes, secrets: tuple[bytes, ...]) -> dict[str, Any]:
    if any(secret in raw for secret in secrets):
        raise RuntimeError("Oracle attempted to emit secret-bearing diagnostics")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Oracle event is not an object")
    for key in value:
        folded = key.casefold().replace("_", "")
        if folded in FORBIDDEN_EVENT_KEYS or (any(name in folded for name in FORBIDDEN_EVENT_KEYS) and not folded.endswith("length")):
            raise RuntimeError("Oracle event contains a forbidden field")
    return value


def _recv_exact(stream: Any, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        chunk = stream.recv(length - len(result))
        if not chunk:
            raise EOFError("Oracle socket closed")
        result.extend(chunk)
    return bytes(result)


def _adb(adb: Path, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(adb), *args], check=True, capture_output=True, text=True, timeout=timeout)


def run_android_oracle(material: CameraMaterial, preflight: dict[str, Any], adb: Path, apk: Path, capture_seconds: int) -> tuple[dict[str, Any], Path]:
    if not adb.is_file() or not apk.is_file():
        raise RuntimeError("ADB or the built oracle APK is missing")
    captures = Path("captures")
    run_dir = captures / ("oracle-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    raw_dirs = {2: run_dir / "channel2", 3: run_dir / "channel3"}
    for directory in raw_dirs.values():
        directory.mkdir(parents=True, exist_ok=False)

    events: list[dict[str, Any]] = []
    frame_records: list[dict[str, Any]] = []
    secrets = material.secret_values()

    def snapshot(status: str, failure: BaseException | None = None) -> dict[str, Any]:
        report = dict(preflight)
        report.update({
            "oracle_platform": "ANDROID",
            "native_yi_library": "APK arm64-v8a libPPPP_API.so",
            "official_resolution_source": "APK fresh-default CAMERA_PLAYER_HD=-1 => resolution 2",
            "realtime_payload": list(realtime_payload()),
            "oracle_status": status,
            "events": events,
            "raw_frame_counts": {
                "channel2": sum(item["channel"] == 2 for item in frame_records),
                "channel3": sum(item["channel"] == 3 for item in frame_records),
            },
        })
        if failure is not None:
            report["failure"] = {"exception_type": failure.__class__.__name__}
        (run_dir / "oracle-events.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return report

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", ORACLE_PORT))
            server.listen(1)
            server.settimeout(30)
            _adb(adb, "reverse", f"tcp:{ORACLE_PORT}", f"tcp:{ORACLE_PORT}")
            _adb(adb, "install", "-r", str(apk), timeout=120)
            _adb(adb, "shell", "am", "force-stop", APP_PACKAGE)
            _adb(adb, "shell", "am", "start", "-n", ORACLE_COMPONENT)
            connection, _ = server.accept()
            with connection:
                connection.settimeout(capture_seconds + 90)
                first_length = struct.unpack(">I", _recv_exact(connection, 4))[0]
                first = _recv_exact(connection, first_length)
                if first[:1] != b"\x01" or _safe_event(first[1:], secrets).get("event") != "oracle_ready":
                    raise RuntimeError("Oracle did not complete its safe handshake")
                config = {
                    "did": material.pppp_did,
                    "server": material.server,
                    "deviceKey": material.device_key,
                    "password": material.password,
                    "wakeup": material.wakeup,
                    "encrypted": material.encrypted,
                    "connectionFlag": 0x4B,
                    "resolution": OFFICIAL_DEFAULT_RESOLUTION,
                    "startUseCount": START_USE_COUNT,
                    "captureSeconds": capture_seconds,
                }
                encoded = json.dumps(config, separators=(",", ":")).encode("utf-8")
                connection.sendall(struct.pack(">I", len(encoded)) + encoded)
                del encoded, config

                frame_counts = {2: 0, 3: 0}
                finished = False
                while not finished:
                    message_length = struct.unpack(">I", _recv_exact(connection, 4))[0]
                    if message_length <= 1 or message_length > 2 * 1024 * 1024 + 32:
                        raise RuntimeError("Invalid oracle message length")
                    message = _recv_exact(connection, message_length)
                    kind = message[0]
                    if kind == 1:
                        event = _safe_event(message[1:], secrets)
                        events.append(event)
                        finished = event.get("event") == "oracle_finished"
                    elif kind == 2:
                        channel = message[1]
                        timestamp_ns, raw_length = struct.unpack(">QI", message[2:14])
                        raw = message[14:]
                        if channel not in raw_dirs or raw_length != len(raw) or any(secret in raw for secret in secrets):
                            raise RuntimeError("Unsafe or malformed oracle frame record")
                        frame_counts[channel] += 1
                        path = raw_dirs[channel] / f"{frame_counts[channel]:06d}.bin"
                        path.write_bytes(raw)
                        frame_records.append({"channel": channel, "timestampNanos": timestamp_ns, "path": path})
                    else:
                        raise RuntimeError("Unknown oracle message kind")
    except Exception as failure:
        snapshot("FAILED", failure)
        raise
    finally:
        try:
            _adb(adb, "shell", "am", "force-stop", APP_PACKAGE)
        except Exception:
            pass
        try:
            _adb(adb, "reverse", "--remove", f"tcp:{ORACLE_PORT}")
        except Exception:
            pass

    report = snapshot("FINISHED")
    return report, run_dir


def _parse_unit(channel: int, raw: bytes, password: str, encrypted: bool) -> dict[str, Any]:
    if len(raw) < 32:
        raise ValueError("TNP video unit is shorter than 8+24 bytes")
    version, io_type, data_size = raw[0], raw[1], int.from_bytes(raw[4:8], "big")
    if io_type != 1 or data_size != len(raw) - 8:
        raise ValueError("TNP outer header failed validation")
    frame = raw[8:32]
    payload = bytearray(raw[32:])
    codec = int.from_bytes(frame[0:2], "big")
    flags = frame[2]
    is_i_frame = bool(flags & 1)
    if encrypted and is_i_frame and len(payload) >= 36:
        key = (password + "0").encode("ascii")
        if len(key) != 16:
            raise ValueError("APK-derived video key is not 16 bytes")
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        payload[4:36] = decryptor.update(bytes(payload[4:36])) + decryptor.finalize()
    return {
        "channel": channel,
        "tnp_application_version": version,
        "outer_header_size": 8,
        "frame_header_size": 24,
        "codec_id": codec,
        "flags": flags,
        "frame_type": "I" if is_i_frame else "P",
        "live_flag": frame[3],
        "online_count": frame[4],
        "use_count": frame[5],
        "sequence": int.from_bytes(frame[6:8], "big"),
        "width": int.from_bytes(frame[8:10], "big"),
        "height": int.from_bytes(frame[10:12], "big"),
        "timestamp": int.from_bytes(frame[12:16], "big"),
        "is_day": frame[16],
        "reference": frame[17],
        "out_loss": frame[18],
        "in_loss": frame[19],
        "timestamp_ms": int.from_bytes(frame[20:24], "big"),
        "compressed_payload_length": len(payload),
        "payload": bytes(payload),
    }


def _annex_b_units(payload: bytes) -> list[bytes]:
    starts: list[tuple[int, int]] = []
    index = 0
    while index + 3 <= len(payload):
        if payload[index:index + 4] == b"\x00\x00\x00\x01":
            starts.append((index, 4))
            index += 4
        elif payload[index:index + 3] == b"\x00\x00\x01":
            starts.append((index, 3))
            index += 3
        else:
            index += 1
    if not starts or starts[0][0] != 0:
        return []
    return [payload[start + prefix:(starts[pos + 1][0] if pos + 1 < len(starts) else len(payload))] for pos, (start, prefix) in enumerate(starts)]


def _length_prefixed_units(payload: bytes) -> list[bytes]:
    result = []
    offset = 0
    while offset + 4 <= len(payload):
        length = int.from_bytes(payload[offset:offset + 4], "big")
        offset += 4
        if length <= 0 or offset + length > len(payload):
            return []
        result.append(payload[offset:offset + length])
        offset += length
    return result if offset == len(payload) and result else []


def analyze_nals(payload: bytes, codec_id: int) -> tuple[str, list[int], bytes]:
    units = _annex_b_units(payload)
    framing = "ANNEX_B" if units else "UNKNOWN"
    output = payload
    if not units:
        units = _length_prefixed_units(payload)
        if units:
            framing = "LENGTH_PREFIXED"
            output = b"".join(b"\x00\x00\x00\x01" + unit for unit in units)
    if codec_id == 78:
        types = [unit[0] & 0x1F for unit in units if unit]
    elif codec_id == 81:
        types = [(unit[0] >> 1) & 0x3F for unit in units if unit]
    else:
        types = []
    return framing, types, output


def analyze_capture(run_dir: Path, material: CameraMaterial, report: dict[str, Any]) -> dict[str, Any]:
    parsed = []
    for channel in (2, 3):
        for path in sorted((run_dir / f"channel{channel}").glob("*.bin")):
            raw = path.read_bytes()
            frame = _parse_unit(channel, raw, material.password, material.encrypted)
            frame["source_file"] = str(path.relative_to(run_dir))
            _, raw_nal_types, _ = analyze_nals(raw[32:], frame["codec_id"])
            frame["pre_decrypt_nal_unit_types"] = raw_nal_types
            framing, nal_types, output_payload = analyze_nals(frame["payload"], frame["codec_id"])
            frame["framing"] = framing
            frame["nal_unit_types"] = nal_types
            frame["output_payload"] = output_payload
            parsed.append(frame)
    if not parsed:
        raise RuntimeError("No video units were captured")
    first_i = next((item for item in parsed if item["frame_type"] == "I"), None)
    if first_i is None:
        raise RuntimeError("No I-frame was captured")
    base = first_i["sequence"]
    ordered = sorted((item for item in parsed if ((item["sequence"] - base) & 0xFFFF) < 0x8000), key=lambda item: ((item["sequence"] - base) & 0xFFFF))
    unique = []
    seen = set()
    for frame in ordered:
        if frame["sequence"] not in seen:
            seen.add(frame["sequence"])
            unique.append(frame)

    codec_ids = {item["codec_id"] for item in unique}
    codec = "H264" if codec_ids == {78} else "H265" if codec_ids == {81} else "UNKNOWN"
    suffix = ".h264" if codec == "H264" else ".h265" if codec == "H265" else ".bin"
    output_path = Path("captures") / ("warehouse-test" + suffix)
    if output_path.exists():
        raise RuntimeError(f"Refusing to overwrite existing {output_path}")
    with output_path.open("xb") as destination:
        for frame in unique:
            destination.write(frame["output_payload"])

    metadata = []
    for frame in parsed:
        metadata.append({key: value for key, value in frame.items() if key not in {"payload", "output_payload"}})
    (run_dir / "frame-metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    i_frames = [item for item in parsed if item["frame_type"] == "I"]
    channel2 = [item for item in parsed if item["channel"] == 2]
    channel3 = [item for item in parsed if item["channel"] == 3]
    all_types = [nal for item in parsed for nal in item["nal_unit_types"]]
    framing_values = {item["framing"] for item in parsed if item["framing"] != "UNKNOWN"}
    if codec == "H264":
        sps_count, pps_count, vps_count = all_types.count(7), all_types.count(8), 0
        valid_i_types = {5, 7, 8}
    elif codec == "H265":
        sps_count, pps_count, vps_count = all_types.count(33), all_types.count(34), all_types.count(32)
        valid_i_types = {19, 20, 21, 32, 33, 34}
    else:
        sps_count = pps_count = vps_count = 0
        valid_i_types = set()
    sps_i_frames = sum((7 if codec == "H264" else 33) in item["nal_unit_types"] for item in i_frames)
    pps_i_frames = sum((8 if codec == "H264" else 34) in item["nal_unit_types"] for item in i_frames)
    decrypt_proven = bool(i_frames and any(item["framing"] != "UNKNOWN" and valid_i_types.intersection(item["nal_unit_types"]) for item in i_frames))

    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe_ok = False
    ffmpeg_ok = False
    if ffprobe:
        ffprobe_ok = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-of", "json", str(output_path)], capture_output=True, timeout=30).returncode == 0
    if ffmpeg:
        ffmpeg_ok = subprocess.run([ffmpeg, "-v", "error", "-i", str(output_path), "-f", "null", "-"], capture_output=True, timeout=60).returncode == 0

    check_event = next((item for item in report["events"] if item.get("event") == "PPPP_Check"), {})
    auth_event = next((item for item in report["events"] if item.get("event") == "TNP_authentication"), {})
    event_names = {item.get("event") for item in report["events"]}
    mode = {0: "P2P", 1: "RELAY", 2: "TCP", 3: "SDEV"}.get(check_event.get("mode"), "UNKNOWN")
    report.update({
        "PPPP_initialization": "SUCCESS" if "PPPP_Initialize" in event_names else "FAIL",
        "PPPP_connection": "SUCCESS" if check_event.get("returnValue") == 0 else "FAIL",
        "connection_mode": mode,
        "TNP_authentication": "SUCCESS" if auth_event.get("authResult") == 0 else "FAIL",
        "PPPP_transport_header_variant": "UNKNOWN",
        "TNP_application_version": auth_event.get("tnpApplicationVersion", "UNKNOWN"),
        "realtime_command_9029": "LIVE CONFIRMED" if "realtime_start" in event_names and i_frames else "FAIL",
        "realtime_4_byte_payload": "LIVE CONFIRMED" if "realtime_start" in event_names else "FAIL",
        "channel_2": f"{len(channel2)} units; flags classify all as I={bool(channel2 and all(item['frame_type'] == 'I' for item in channel2))}",
        "channel_3": f"{len(channel3)} units; flags classify all as P={bool(channel3 and all(item['frame_type'] == 'P' for item in channel3))}",
        "codec": codec,
        "resolution": sorted({f"{item['width']}x{item['height']}" for item in parsed}),
        "frame_format": next(iter(framing_values)) if len(framing_values) == 1 else "UNKNOWN",
        "SPS": {"count": sps_count, "i_frame_count": len(i_frames), "i_frames_containing_sps": sps_i_frames},
        "PPS": {"count": pps_count, "i_frame_count": len(i_frames), "i_frames_containing_pps": pps_i_frames},
        "VPS": "NOT APPLICABLE" if codec == "H264" else {"count": vps_count},
        "encryption_algorithm": "AES-128-ECB/NoPadding, two independent blocks at payload offsets 4 and 20" if material.encrypted else "NOT ENABLED",
        "I_frame_decryption": "SUCCESS" if decrypt_proven else "FAIL",
        "reconstructed_elementary_stream": "SUCCESS" if output_path.stat().st_size else "FAIL",
        "elementary_stream_path": str(output_path),
        "ffprobe": "SUCCESS" if ffprobe_ok else "FAIL",
        "ffmpeg_decode": "SUCCESS" if ffmpeg_ok else "FAIL",
        "clean_shutdown": "SUCCESS" if {"realtime_stop", "PPPP_ForceClose", "PPPP_DeInitialize", "reader_shutdown"} <= event_names else "FAIL",
    })
    (run_dir / "oracle-report.json").write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Controlled YI TNP behavioral oracle")
    result.add_argument("command", choices=("preflight", "run"))
    result.add_argument("--env-file", type=Path, default=Path(".env.local"))
    result.add_argument("--timeout", type=float, default=10.0)
    result.add_argument("--adb", type=Path)
    result.add_argument("--apk", type=Path, default=Path("oracle/android/build/yi-tnp-oracle.apk"))
    result.add_argument("--capture-seconds", type=int, default=15, choices=range(5, 31))
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    material: CameraMaterial | None = None
    try:
        if not args.env_file.is_file():
            raise RuntimeError(".env.local is missing")
        load_env_file(args.env_file)
        material, preflight = fresh_cloud_preflight(args.timeout)
        if args.command == "preflight":
            print(json.dumps(preflight, indent=2, sort_keys=True, ensure_ascii=False))
            return 0
        if args.adb is None:
            raise RuntimeError("--adb is required for the Android oracle")
        report, run_dir = run_android_oracle(material, preflight, args.adb, args.apk, args.capture_seconds)
        report = analyze_capture(run_dir, material, report)
        print(json.dumps({key: value for key, value in report.items() if key != "events"}, indent=2, sort_keys=True, ensure_ascii=False))
        required = (
            report["PPPP_connection"] == "SUCCESS",
            report["TNP_authentication"] == "SUCCESS",
            report["realtime_command_9029"] == "LIVE CONFIRMED",
            report["I_frame_decryption"] == "SUCCESS",
            report["reconstructed_elementary_stream"] == "SUCCESS",
            report["ffprobe"] == "SUCCESS",
            report["ffmpeg_decode"] == "SUCCESS",
            report["clean_shutdown"] == "SUCCESS",
        )
        return 0 if all(required) else 1
    except Exception as failure:
        print(json.dumps({"ok": False, "error": {"category": "phase_2c_failed", "exception_type": failure.__class__.__name__}}, indent=2), file=sys.stderr)
        return 1
    finally:
        if material is not None:
            material.clear()


if __name__ == "__main__":
    raise SystemExit(main())
