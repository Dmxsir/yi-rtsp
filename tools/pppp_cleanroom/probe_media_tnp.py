#!/usr/bin/env python3
"""CR-4 manual probe for bounded H.264/AAC validation over clean PPPP."""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

HERE = Path(__file__).resolve()
try:
    from .probe_channel0_tnp import _read_exact
    from .probe_legacy_punch import load_material, parse_camera_id, parse_server
    from .tnp_stream import TnpStreamError, TnpUnitReader
    from .yi_pppp import DeviceId
    from .yi_pppp_session import CleanPpppSession, ExperimentalPolicy, TransportError
except ImportError:  # Direct execution from a relocated clean-room directory.
    from probe_channel0_tnp import _read_exact  # type: ignore[no-redef]
    from probe_legacy_punch import load_material, parse_camera_id, parse_server  # type: ignore[no-redef]
    from tnp_stream import TnpStreamError, TnpUnitReader  # type: ignore[no-redef]
    from yi_pppp import DeviceId  # type: ignore[no-redef]
    from yi_pppp_session import (  # type: ignore[no-redef]
        CleanPpppSession,
        ExperimentalPolicy,
        TransportError,
    )

DEFAULT_MAX_RECORD = 2 * 1024 * 1024 + 32


class ProbeError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def _app_source_candidates(explicit: Path | None = None) -> tuple[Path, ...]:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.resolve())
    for parent in HERE.parents:
        candidate = parent / "yi_home" / "rootfs" / "opt" / "yi-home" / "app"
        if candidate.is_dir():
            candidates.append(candidate)
    runtime = Path("/opt/yi-home/app")
    if runtime.is_dir():
        candidates.append(runtime)
    return tuple(dict.fromkeys(candidates))


def _phase3e_candidates(app_dirs: tuple[Path, ...]) -> tuple[Path, ...]:
    relative = Path("tools/phase3_pppp_probe/run_phase3e_tnp.py")
    candidates = [parent / relative for parent in HERE.parents]
    candidates.extend(app_dir / relative for app_dir in app_dirs)
    return tuple(dict.fromkeys(candidates))


def _media_support(app_source: Path | None = None) -> SimpleNamespace:
    """Load branch Phase 3E plus installed App media parsers without I/O."""
    app_dirs = _app_source_candidates(app_source)
    app_dir = next(
        (
            candidate
            for candidate in app_dirs
            if (candidate / "yi_live_relay.py").is_file()
            and (candidate / "yi_native_av_relay.py").is_file()
        ),
        None,
    )
    if app_dir is None:
        raise RuntimeError("YI App media parser sources not found")
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))

    phase3e: ModuleType | None = None
    for module_path in _phase3e_candidates(app_dirs):
        if not module_path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("_yi_cr4_phase3e", module_path)
        if spec is None or spec.loader is None:
            continue
        candidate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(candidate)
        if hasattr(candidate, "_build_units") and hasattr(candidate, "validate_first_4882"):
            phase3e = candidate
            break
    if phase3e is None:
        raise RuntimeError("relocation-safe Phase 3E helpers not found")

    video = importlib.import_module("yi_live_relay")
    audio = importlib.import_module("yi_native_av_relay")
    if not hasattr(video, "_decode_video_unit") or not hasattr(video, "SequenceReorderBuffer"):
        raise RuntimeError("YI video parser helpers not found")
    if not hasattr(audio, "decrypt_audio_unit"):
        raise RuntimeError("YI audio parser helper not found")
    return SimpleNamespace(phase3e=phase3e, video=video, audio=audio)


def self_test() -> int:
    forbidden = (
        "yi_camera_manager",
        "yi_tnp_oracle",
        "yi_cloud_probe",
        "yi_live_relay",
        "yi_native_av_relay",
        "_yi_cr4_phase3e",
    )
    assert not any(name in sys.modules for name in forbidden)
    assert parse_camera_id("0123456789ABCDEFabcd") == "0123456789abcdefabcd"
    assert TnpUnitReader(1, DEFAULT_MAX_RECORD).partial_bytes == 0
    print(
        "CR4_SELF_TEST=PASS; cloud_used=false; network_used=false; tnp_sent=false; "
        "media_requested=false; runtime_support_imported=false"
    )
    return 0


def support_smoke_test(app_source: Path | None = None) -> int:
    support = _media_support(app_source)
    assert callable(support.phase3e._build_units)
    assert callable(support.phase3e.validate_first_4882)
    assert callable(support.video._decode_video_unit)
    assert callable(support.audio.decrypt_audio_unit)
    print(
        "CR4_SUPPORT_SMOKE=PASS; cloud_used=false; network_used=false; "
        "device_traffic=false; parsers_loaded=true"
    )
    return 0


def _collect_media(
    session: CleanPpppSession,
    support: SimpleNamespace,
    material: Any,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if args.duration <= 0 or args.max_video_records < 1 or args.max_audio_records < 1:
        raise ProbeError("CR4_SETUP")
    readers = {
        channel: TnpUnitReader(channel, args.max_record_bytes) for channel in (1, 2, 3)
    }
    reorder = support.video.SequenceReorderBuffer(
        max_pending=args.reorder_pending,
        max_wait_seconds=args.reorder_wait,
    )
    counts = {1: 0, 2: 0, 3: 0}
    i_valid = False
    p_valid = False
    audio_format: dict[str, int] | None = None
    reordered = 0
    nal_types: set[int] = set()
    deadline = time.monotonic() + args.duration

    while time.monotonic() < deadline:
        for channel in (2, 3, 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if channel == 1 and counts[1] >= args.max_audio_records:
                continue
            if channel in (2, 3) and counts[2] + counts[3] >= args.max_video_records:
                continue
            try:
                unit = readers[channel].read_one(session, min(0.05, remaining))
            except TnpStreamError as exc:
                if exc.category == "MEDIA_CHANNEL_TIMEOUT":
                    continue
                raise ProbeError(exc.category) from exc

            if channel == 1:
                try:
                    _timestamp_ms, _aac, fmt = support.audio.decrypt_audio_unit(
                        unit, material.password
                    )
                except Exception as exc:
                    raise ProbeError("AAC_PARSE_INVALID") from exc
                counts[1] += 1
                audio_format = fmt
            else:
                try:
                    frame = support.video._decode_video_unit(
                        channel, unit, material.password, material.encrypted
                    )
                except Exception as exc:
                    raise ProbeError("H264_PARSE_INVALID") from exc
                types = {int(value) for value in frame["nal_unit_types"]}
                if channel == 2 and (
                    frame["frame_type"] != "I" or not types.intersection((5, 7, 8))
                ):
                    raise ProbeError("H264_PARSE_INVALID")
                if channel == 3 and (frame["frame_type"] != "P" or 1 not in types):
                    raise ProbeError("H264_PARSE_INVALID")
                counts[channel] += 1
                nal_types.update(types)
                i_valid = i_valid or channel == 2
                p_valid = p_valid or channel == 3
                reordered += len(reorder.push(frame))

            if i_valid and p_valid and audio_format is not None and reordered > 0:
                return {
                    "counts": counts,
                    "i_valid": True,
                    "p_valid": True,
                    "reordered": reordered,
                    "nal_types": tuple(sorted(nal_types)),
                    "audio_format": audio_format,
                }
    raise ProbeError("MEDIA_CHANNEL_TIMEOUT")


def _run_live(args: argparse.Namespace) -> int:
    material: Any | None = None
    session: CleanPpppSession | None = None
    stop_unit: bytes | None = None
    startup_sent = False
    stop_attempted = False
    stop_sent = False
    media: dict[str, Any] | None = None
    failure: str | None = None
    try:
        material, _report = load_material(args.env_file, args.cloud_timeout, args.camera_id)
        support = _media_support(args.app_source)
        units = support.phase3e._build_units(material)
        if tuple(map(len, units)) != (56, 52, 56, 56):
            raise ProbeError("CR4_SETUP")
        stop_unit = units[3]
        session = CleanPpppSession(
            tuple(args.servers),
            ExperimentalPolicy(
                handshake_timeout=args.handshake_timeout,
                keepalive_timeout=args.keepalive_timeout,
                keepalive_interval=args.keepalive_interval,
                request_retry_after=args.request_retry_after,
                retry_after=args.retry_after,
                max_attempts=args.max_attempts,
                punch_repeat=args.punch_repeat,
                max_payload=args.max_drw_payload,
                receive_window=args.receive_window,
                control_buffer_bytes=args.control_buffer_bytes,
                media_buffer_bytes=args.media_buffer_bytes,
            ),
        )
        session.connect(DeviceId.from_text(material.pppp_did))
        for unit in units[:3]:
            session.write_channel(0, unit)
        startup_sent = True
        session.flush_channel(0)
        session.wait_channel_acked(0, args.ack_timeout)
        header = _read_exact(session, 8, args.read_timeout)
        body_size = int.from_bytes(header[4:8], "big")
        if not 40 <= body_size <= 4096 - 8:
            raise ProbeError("TNP_RESPONSE_INVALID")
        body = _read_exact(session, body_size, args.read_timeout)
        if not support.phase3e.validate_first_4882(header, body):
            raise ProbeError("TNP_RESPONSE_INVALID")
        print("cr3_control_result=PASS", flush=True)

        session.enable_read_channels((1, 2, 3))
        print("media_channels_enabled=true", flush=True)
        media = _collect_media(session, support, material, args)
        counts = media["counts"]
        audio_format = media["audio_format"]
        print(f"channel2_tnp_units_valid={counts[2]}", flush=True)
        print(f"channel3_tnp_units_valid={counts[3]}", flush=True)
        print("h264_i_frame_valid=true", flush=True)
        print("h264_p_frame_valid=true", flush=True)
        print("h264_framing_valid=true", flush=True)
        print(f"h264_frames_reordered={media['reordered']}", flush=True)
        print("h264_nal_types=" + ",".join(map(str, media["nal_types"])), flush=True)
        print(f"channel1_tnp_units_valid={counts[1]}", flush=True)
        print("aac_valid=true", flush=True)
        print(f"aac_sample_rate={audio_format['sample_rate']}", flush=True)
        print(f"aac_channels={audio_format['channels']}", flush=True)
        print(f"aac_object_type={audio_format['object_type']}", flush=True)
    except (ProbeError, TransportError) as exc:
        failure = exc.category
    except Exception:
        failure = "CR4_SETUP"
    finally:
        if startup_sent and not stop_attempted and session is not None and stop_unit is not None:
            stop_attempted = True
            try:
                session.write_channel(0, stop_unit)
                stop_sent = session.flush_channel(0) > 0
            except Exception:
                stop_sent = False
            print(f"stop_live_767_sent={str(stop_sent).lower()}", flush=True)
        if session is not None:
            session.close()
            print("transport_closed=true", flush=True)
        if material is not None:
            material.clear()

    passed = bool(media and stop_sent and session and session.closed)
    if passed:
        print("cr4_result=PASS", flush=True)
        return 0
    print(f"cr4_result=FAIL; failure_category={failure or 'STOP_LIVE_FAILED'}", flush=True)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="CR-4 bounded clean PPPP H.264/AAC probe")
    parser.add_argument("--server", dest="servers", action="append", type=parse_server)
    parser.add_argument("--camera-id", type=parse_camera_id)
    parser.add_argument("--env-file", type=Path, default=Path("/data/yi.env"))
    parser.add_argument("--app-source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--cloud-timeout", type=float, default=10.0)
    parser.add_argument("--handshake-timeout", type=float, default=4.0)
    parser.add_argument("--keepalive-timeout", type=float, default=0.8)
    parser.add_argument("--keepalive-interval", type=float, default=0.5)
    parser.add_argument("--request-retry-after", type=float, default=0.7)
    parser.add_argument("--retry-after", type=float, default=0.25)
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--punch-repeat", type=int, default=3)
    parser.add_argument("--max-drw-payload", type=int, default=1024)
    parser.add_argument("--receive-window", type=int, default=4096)
    parser.add_argument("--control-buffer-bytes", type=int, default=64 * 1024)
    parser.add_argument("--media-buffer-bytes", type=int, default=DEFAULT_MAX_RECORD)
    parser.add_argument("--max-record-bytes", type=int, default=DEFAULT_MAX_RECORD)
    parser.add_argument("--max-video-records", type=int, default=128)
    parser.add_argument("--max-audio-records", type=int, default=128)
    parser.add_argument("--reorder-pending", type=int, default=24)
    parser.add_argument("--reorder-wait", type=float, default=0.35)
    parser.add_argument("--ack-timeout", type=float, default=2.0)
    parser.add_argument("--read-timeout", type=float, default=4.0)
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--support-smoke-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.support_smoke_test:
        return support_smoke_test(args.app_source)
    if not args.servers:
        parser.error("at least one --server is required")
    if not args.camera_id:
        parser.error("--camera-id is required for a live probe")
    return _run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
