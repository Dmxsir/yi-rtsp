"""Phase 2E live H.264 relay for the proven YI y291ga TNP path.

The Android oracle remains the PPPP transport engine. This host receives the
live channel-2/channel-3 TNP units over adb reverse, decrypts encrypted
I-frames, reorders the two video channels by the 16-bit TNP sequence number,
and emits a standard H.264 Annex-B elementary stream without transcoding.

All diagnostics go to stderr. When --stdout is selected, stdout contains H.264
bytes only.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import struct
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, BinaryIO

import yi_tnp_oracle as oracle

ALLOWED_TARGETS = ("POOL", "מחסן")
DEFAULT_TARGET = "מחסן"
H264_CODEC_ID = 78


class SequenceReorderBuffer:
    """Bounded uint16 sequence reorder buffer for interleaved I/P channels."""

    def __init__(self, max_pending: int = 24, max_wait_seconds: float = 0.35) -> None:
        if max_pending < 2:
            raise ValueError("max_pending must be >= 2")
        if max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be > 0")
        self.max_pending = max_pending
        self.max_wait_seconds = max_wait_seconds
        self.expected: int | None = None
        self.pending: dict[int, tuple[float, dict[str, Any]]] = {}
        self._seen_order: deque[int] = deque()
        self._seen: set[int] = set()

    @staticmethod
    def _distance(sequence: int, base: int) -> int:
        return (sequence - base) & 0xFFFF

    def _remember(self, sequence: int) -> None:
        if sequence in self._seen:
            return
        self._seen.add(sequence)
        self._seen_order.append(sequence)
        while len(self._seen_order) > 512:
            old = self._seen_order.popleft()
            self._seen.discard(old)

    def _drain(self) -> list[dict[str, Any]]:
        ready: list[dict[str, Any]] = []
        while self.expected is not None and self.expected in self.pending:
            sequence = self.expected
            _, frame = self.pending.pop(sequence)
            ready.append(frame)
            self._remember(sequence)
            self.expected = (sequence + 1) & 0xFFFF
        return ready

    def _trim_prestart(self) -> None:
        if len(self.pending) <= self.max_pending:
            return
        oldest = min(self.pending, key=lambda seq: self.pending[seq][0])
        self.pending.pop(oldest, None)
        self._remember(oldest)

    def push(self, frame: dict[str, Any], now: float | None = None) -> list[dict[str, Any]]:
        now = time.monotonic() if now is None else now
        sequence = int(frame["sequence"]) & 0xFFFF

        if sequence in self._seen or sequence in self.pending:
            return []

        self.pending[sequence] = (now, frame)

        if self.expected is None:
            if frame.get("frame_type") != "I":
                self._trim_prestart()
                return []
            self.expected = sequence
            # Keep the I-frame and frames that are forward from it. Frames behind
            # the first I-frame cannot contribute to a decodable stream start.
            for candidate in list(self.pending):
                if self._distance(candidate, sequence) >= 0x8000:
                    self.pending.pop(candidate, None)
                    self._remember(candidate)
        else:
            # A late frame behind the already-emitted position is stale.
            if self._distance(sequence, self.expected) >= 0x8000:
                self.pending.pop(sequence, None)
                self._remember(sequence)
                return []

        ready = self._drain()
        if ready or not self.pending or self.expected is None:
            return ready

        oldest_wait = now - min(arrived for arrived, _ in self.pending.values())
        if len(self.pending) >= self.max_pending or oldest_wait >= self.max_wait_seconds:
            # A frame was lost or excessively delayed. Advance to the nearest
            # forward sequence so a live stream cannot stall indefinitely.
            nearest = min(self.pending, key=lambda seq: self._distance(seq, self.expected or 0))
            self.expected = nearest
            ready.extend(self._drain())
        return ready


def _decode_video_unit(
    channel: int,
    raw: bytes,
    password: str,
    encrypted: bool,
) -> dict[str, Any]:
    frame = oracle._parse_unit(channel, raw, password, encrypted)
    if frame["codec_id"] != H264_CODEC_ID:
        raise RuntimeError(f"Phase 2E expected H.264 codec id 78, got {frame['codec_id']}")
    framing, nal_types, output_payload = oracle.analyze_nals(frame["payload"], frame["codec_id"])
    if framing == "UNKNOWN":
        raise RuntimeError("Phase 2E received an unrecognized H.264 payload framing")
    frame["framing"] = framing
    frame["nal_unit_types"] = nal_types
    frame["output_payload"] = output_payload
    return frame


def _safe_identity(material: oracle.CameraMaterial, preflight: dict[str, Any], target: str) -> None:
    if (
        material.name != target
        or material.raw_model != "83"
        or material.normalized_model != "y291ga"
        or preflight.get("p2p_type") != 2
        or preflight.get("cloud_online") is not True
    ):
        raise RuntimeError("Phase 2E target identity mismatch")


def _log(message: str) -> None:
    print(f"[yi-live-relay] {message}", file=sys.stderr, flush=True)


def _send_config(
    connection: socket.socket,
    material: oracle.CameraMaterial,
    duration: int,
    resolution: int,
) -> None:
    config = {
        "did": material.pppp_did,
        "server": material.server,
        "deviceKey": material.device_key,
        "password": material.password,
        "wakeup": material.wakeup,
        "encrypted": material.encrypted,
        "connectionFlag": 0x4B,
        "resolution": resolution,
        "startUseCount": oracle.START_USE_COUNT,
        "captureSeconds": duration,
    }
    encoded = json.dumps(config, separators=(",", ":")).encode("utf-8")
    connection.sendall(struct.pack(">I", len(encoded)) + encoded)
    del encoded, config


def run_relay(
    material: oracle.CameraMaterial,
    preflight: dict[str, Any],
    adb: Path,
    apk: Path,
    duration: int,
    sink: BinaryIO,
    resolution: int = 1,
    reorder_pending: int = 24,
    reorder_wait: float = 0.35,
    install_apk: bool = True,
) -> dict[str, Any]:
    if not adb.is_file() or not apk.is_file():
        raise RuntimeError("ADB or the built oracle APK is missing")

    secrets = material.secret_values()
    reorder = SequenceReorderBuffer(reorder_pending, reorder_wait)
    counts = {2: 0, 3: 0}
    emitted = 0
    emitted_bytes = 0
    first_frame_at: float | None = None
    last_frame_at: float | None = None
    connection_mode = "UNKNOWN"
    authenticated = False
    oracle_finished = False

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", oracle.ORACLE_PORT))
            server.listen(1)
            server.settimeout(30)

            oracle._adb(adb, "reverse", f"tcp:{oracle.ORACLE_PORT}", f"tcp:{oracle.ORACLE_PORT}")
            if install_apk:
                oracle._adb(adb, "install", "-r", str(apk), timeout=120)
            oracle._adb(adb, "shell", "am", "force-stop", oracle.APP_PACKAGE)
            oracle._adb(adb, "shell", "am", "start", "-n", oracle.ORACLE_COMPONENT)

            connection, _ = server.accept()
            with connection:
                connection.settimeout(duration + 90)
                first_length = struct.unpack(">I", oracle._recv_exact(connection, 4))[0]
                first = oracle._recv_exact(connection, first_length)
                if first[:1] != b"\x01" or oracle._safe_event(first[1:], secrets).get("event") != "oracle_ready":
                    raise RuntimeError("Oracle did not complete its safe handshake")

                _send_config(connection, material, duration, resolution)
                _log(f"target={material.name}; finite live validation={duration}s")

                while not oracle_finished:
                    message_length = struct.unpack(">I", oracle._recv_exact(connection, 4))[0]
                    if message_length <= 1 or message_length > 2 * 1024 * 1024 + 32:
                        raise RuntimeError("Invalid oracle message length")
                    message = oracle._recv_exact(connection, message_length)
                    kind = message[0]

                    if kind == 1:
                        event = oracle._safe_event(message[1:], secrets)
                        name = event.get("event")
                        if name == "PPPP_Check" and event.get("returnValue") == 0:
                            connection_mode = {0: "DIRECT_P2P", 1: "RELAY", 2: "TCP", 3: "SDEV"}.get(
                                event.get("mode"), "UNKNOWN"
                            )
                            _log(f"PPPP connected: {connection_mode}")
                        elif name == "TNP_authentication":
                            authenticated = event.get("responseCommand") == 4882 and event.get("authResult") == 0
                            if not authenticated:
                                raise RuntimeError("TNP authentication did not return successful 4882")
                            _log("TNP v2 authentication: SUCCESS")
                        elif name == "oracle_error":
                            raise RuntimeError(
                                f"Android oracle error: {event.get('exceptionType', 'UNKNOWN')}"
                            )
                        elif name == "oracle_finished":
                            oracle_finished = True
                        continue

                    if kind != 2 or len(message) < 14:
                        raise RuntimeError("Unknown or malformed oracle message kind")

                    channel = message[1]
                    _, raw_length = struct.unpack(">QI", message[2:14])
                    raw = message[14:]
                    if channel not in (2, 3) or raw_length != len(raw) or any(secret in raw for secret in secrets):
                        raise RuntimeError("Unsafe or malformed oracle frame record")

                    frame = _decode_video_unit(channel, raw, material.password, material.encrypted)
                    counts[channel] += 1
                    now = time.monotonic()
                    if first_frame_at is None:
                        first_frame_at = now
                    last_frame_at = now

                    for ready in reorder.push(frame, now):
                        payload = ready["output_payload"]
                        try:
                            sink.write(payload)
                            sink.flush()
                        except BrokenPipeError:
                            raise RuntimeError("H.264 consumer closed the output pipe") from None
                        emitted += 1
                        emitted_bytes += len(payload)

    finally:
        try:
            oracle._adb(adb, "shell", "am", "force-stop", oracle.APP_PACKAGE)
        except Exception:
            pass
        try:
            oracle._adb(adb, "reverse", "--remove", f"tcp:{oracle.ORACLE_PORT}")
        except Exception:
            pass

    live_seconds = 0.0
    if first_frame_at is not None and last_frame_at is not None:
        live_seconds = max(0.0, last_frame_at - first_frame_at)

    return {
        "target": material.name,
        "PPPP_mode": connection_mode,
        "TNP_authentication": "SUCCESS" if authenticated else "FAIL",
        "channel_2_units": counts[2],
        "channel_3_units": counts[3],
        "emitted_frames": emitted,
        "emitted_bytes": emitted_bytes,
        "live_video_seconds": round(live_seconds, 3),
        "oracle_finished": oracle_finished,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Phase 2E YI TNP live H.264 Annex-B relay")
    result.add_argument("--target", choices=ALLOWED_TARGETS, default=DEFAULT_TARGET)
    result.add_argument("--env-file", type=Path, default=Path(".env.local"))
    result.add_argument("--timeout", type=float, default=10.0)
    default_adb = shutil.which("adb")
    result.add_argument("--adb", type=Path, default=Path(default_adb) if default_adb else None)
    result.add_argument("--apk", type=Path, default=Path("oracle/android/build/yi-tnp-oracle.apk"))
    result.add_argument("--duration", type=int, default=60, choices=range(5, 3601))
    result.add_argument(
        "--resolution",
        type=int,
        default=1,
        choices=range(0, 5),
        help="YI TNP quality value; y291ga: 1 = proven 1920x1080, 2 = proven 640x360",
    )
    result.add_argument("--reorder-pending", type=int, default=24)
    result.add_argument("--reorder-wait", type=float, default=0.35)
    output = result.add_mutually_exclusive_group(required=True)
    output.add_argument("--stdout", action="store_true", help="write H.264 bytes only to stdout")
    output.add_argument("--output", type=Path, help="write H.264 Annex-B to a file")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    material: oracle.CameraMaterial | None = None
    sink: BinaryIO | None = None
    close_sink = False

    try:
        if not args.env_file.is_file():
            raise RuntimeError(".env.local is missing")
        if args.adb is None:
            raise RuntimeError("adb was not found; pass --adb explicitly")

        oracle.load_env_file(args.env_file)
        oracle.APPROVED_TARGETS = (args.target, args.target)
        material, preflight = oracle.fresh_cloud_preflight(args.timeout)
        _safe_identity(material, preflight, args.target)

        if args.stdout:
            sink = sys.stdout.buffer
        else:
            assert args.output is not None
            sink = args.output.open("xb")
            close_sink = True

        report = run_relay(
            material,
            preflight,
            args.adb,
            args.apk,
            args.duration,
            sink,
            args.resolution,
            args.reorder_pending,
            args.reorder_wait,
        )
        _log(json.dumps(report, sort_keys=True, ensure_ascii=False))
        return 0 if report["TNP_authentication"] == "SUCCESS" and report["emitted_frames"] > 0 else 1

    except Exception as failure:
        _log(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "category": "phase_2e_failed",
                        "exception_type": failure.__class__.__name__,
                    },
                },
                sort_keys=True,
            )
        )
        return 1
    finally:
        if close_sink and sink is not None:
            sink.close()
        if material is not None:
            material.clear()


if __name__ == "__main__":
    raise SystemExit(main())
