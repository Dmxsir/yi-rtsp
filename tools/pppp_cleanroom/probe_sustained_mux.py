#!/usr/bin/env python3
"""CR-4B sustained clean-media and pipe-only MPEG-TS validation probe."""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    from .mux_pipe import MuxError, PipeMuxValidator, ffmpeg_command, ffprobe_command
    from .probe_channel0_tnp import _read_exact
    from .probe_legacy_punch import load_material, parse_camera_id, parse_server
    from .probe_media_tnp import DEFAULT_MAX_RECORD, _media_support
    from .tnp_stream import TnpStreamError, TnpUnitReader
    from .yi_pppp import DeviceId
    from .yi_pppp_session import CleanPpppSession, ExperimentalPolicy, TransportError
except ImportError:  # Direct execution from a relocated clean-room directory.
    from mux_pipe import MuxError, PipeMuxValidator, ffmpeg_command, ffprobe_command
    from probe_channel0_tnp import _read_exact
    from probe_legacy_punch import load_material, parse_camera_id, parse_server
    from probe_media_tnp import DEFAULT_MAX_RECORD, _media_support
    from tnp_stream import TnpStreamError, TnpUnitReader
    from yi_pppp import DeviceId
    from yi_pppp_session import CleanPpppSession, ExperimentalPolicy, TransportError


class ProbeError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


@dataclass(slots=True)
class SustainedProgress:
    min_active_seconds: float
    min_i_frames: int
    min_p_frames: int
    min_audio_frames: int
    counts: dict[str, int] = field(
        default_factory=lambda: {"I": 0, "P": 0, "audio": 0}
    )
    first_at: dict[str, float] = field(default_factory=dict)
    last_at: dict[str, float] = field(default_factory=dict)
    reordered_frames: int = 0
    last_progress_at: float | None = None

    def observe(self, kind: str, now: float) -> None:
        self.counts[kind] += 1
        self.first_at.setdefault(kind, now)
        self.last_at[kind] = now
        self.last_progress_at = now

    @property
    def active_seconds(self) -> float:
        if any(kind not in self.first_at for kind in ("I", "P", "audio")):
            return 0.0
        return min(
            self.last_at[kind] - self.first_at[kind]
            for kind in ("I", "P", "audio")
        )

    @property
    def passed(self) -> bool:
        return (
            self.counts["I"] >= self.min_i_frames
            and self.counts["P"] >= self.min_p_frames
            and self.counts["audio"] >= self.min_audio_frames
            and self.reordered_frames > 0
            and self.active_seconds >= self.min_active_seconds
        )


class SustainedCollector:
    """Reuse existing parsers/reorder and stream accepted frames into one mux."""

    def __init__(
        self,
        support: Any,
        material: Any,
        args: argparse.Namespace,
        mux: Any,
        *,
        max_audio_validation_drops: int = 0,
    ) -> None:
        self.support = support
        self.material = material
        self.args = args
        self.mux = mux
        self.reorder = support.video.SequenceReorderBuffer(
            max_pending=args.reorder_pending,
            max_wait_seconds=args.reorder_wait,
        )
        self.progress = SustainedProgress(
            args.min_active_seconds,
            args.min_i_frames,
            args.min_p_frames,
            args.min_audio_frames,
        )
        self.channel_counts = {1: 0, 2: 0, 3: 0}
        self.pre_video: list[tuple[int, bytes]] = []
        self.pre_audio: list[tuple[int, bytes]] = []
        self.pre_mux_bytes = 0
        self.first_video_ts: int | None = None
        self.first_audio_ts: int | None = None
        self.audio_format: dict[str, int] | None = None
        self.initial_av_delta_ms: int | None = None
        self.max_audio_validation_drops = max_audio_validation_drops
        self.audio_validation_drops = 0

    def _reserve_pre_mux(self, payload: bytes, frame_count: int, limit: int) -> None:
        if frame_count >= limit or self.pre_mux_bytes + len(payload) > self.args.pre_mux_bytes:
            raise ProbeError("MEDIA_BUFFER_LIMIT")
        self.pre_mux_bytes += len(payload)

    def _start_mux_if_ready(self) -> None:
        if self.mux.started or self.first_video_ts is None or self.first_audio_ts is None:
            return
        delta = self.support.audio.signed_delta32(self.first_audio_ts, self.first_video_ts)
        self.initial_av_delta_ms = delta
        self.mux.start(max(0, -delta), max(0, delta))
        for _timestamp, payload in self.pre_video:
            self.mux.feed_video(payload)
        for _timestamp, payload in self.pre_audio:
            self.mux.feed_audio(payload)
        self.pre_video.clear()
        self.pre_audio.clear()
        self.pre_mux_bytes = 0

    def _video(self, channel: int, unit: bytes, now: float) -> None:
        try:
            frame = self.support.video._decode_video_unit(
                channel, unit, self.material.password, self.material.encrypted
            )
        except Exception as exc:
            raise ProbeError("VIDEO_PARSE_INVALID") from exc
        nal_types = {int(value) for value in frame["nal_unit_types"]}
        if (
            int(frame.get("width", 0)) != 1920
            or int(frame.get("height", 0)) != 1080
            or (channel == 2 and (frame["frame_type"] != "I" or not nal_types & {5, 7, 8}))
            or (channel == 3 and (frame["frame_type"] != "P" or 1 not in nal_types))
        ):
            raise ProbeError("VIDEO_PARSE_INVALID")
        kind = "I" if channel == 2 else "P"
        self.channel_counts[channel] += 1
        self.progress.observe(kind, now)
        if self.channel_counts[2] + self.channel_counts[3] > self.args.max_video_records:
            raise ProbeError("MEDIA_SUSTAIN_TIMEOUT")

        for ready in self.reorder.push(frame, now):
            timestamp_ms = int(ready["timestamp_ms"])
            payload = ready["output_payload"]
            self.progress.reordered_frames += 1
            if self.first_video_ts is None:
                self.first_video_ts = timestamp_ms
            if self.mux.started:
                self.mux.feed_video(payload)
            else:
                self._reserve_pre_mux(
                    payload, len(self.pre_video), self.args.pre_mux_video_frames
                )
                self.pre_video.append((timestamp_ms, payload))
                self._start_mux_if_ready()

    def _audio(self, unit: bytes, now: float) -> None:
        try:
            timestamp_ms, payload, audio_format = self.support.audio.decrypt_audio_unit(
                unit, self.material.password
            )
        except Exception as exc:
            validation_error = getattr(
                self.support.audio, "AudioUnitValidationError", None
            )
            if (
                self.max_audio_validation_drops > 0
                and isinstance(validation_error, type)
                and issubclass(validation_error, Exception)
                and isinstance(exc, validation_error)
            ):
                self.audio_validation_drops += 1
                if self.audio_validation_drops > self.max_audio_validation_drops:
                    raise ProbeError("AUDIO_VALIDATION_DROP_LIMIT") from exc
                return
            raise ProbeError("AUDIO_PARSE_INVALID") from exc
        if audio_format != {"sample_rate": 16000, "channels": 1, "object_type": 2}:
            raise ProbeError("AUDIO_PARSE_INVALID")
        if self.audio_format is None:
            self.audio_format = audio_format
        elif audio_format != self.audio_format:
            raise ProbeError("AUDIO_PARSE_INVALID")
        self.channel_counts[1] += 1
        self.progress.observe("audio", now)
        if self.channel_counts[1] > self.args.max_audio_records:
            raise ProbeError("MEDIA_SUSTAIN_TIMEOUT")
        if self.first_audio_ts is None:
            self.first_audio_ts = timestamp_ms
        if self.mux.started:
            self.mux.feed_audio(payload)
        else:
            self._reserve_pre_mux(
                payload, len(self.pre_audio), self.args.pre_mux_audio_frames
            )
            self.pre_audio.append((timestamp_ms, payload))
            self._start_mux_if_ready()

    def accept(self, channel: int, unit: bytes, now: float) -> None:
        if channel == 1:
            self._audio(unit, now)
        else:
            self._video(channel, unit, now)


def _collect_sustained(
    session: CleanPpppSession,
    support: Any,
    material: Any,
    args: argparse.Namespace,
    mux: Any,
    clock: Callable[[], float] = time.monotonic,
    *,
    progress_hook: Callable[[SustainedCollector], None] | None = None,
    completion: Callable[[], bool] | None = None,
    incomplete_category: str = "MEDIA_SUSTAIN_TIMEOUT",
    max_audio_validation_drops: int = 0,
) -> SustainedCollector:
    readers = {
        channel: TnpUnitReader(channel, args.max_record_bytes) for channel in (1, 2, 3)
    }
    collector = SustainedCollector(
        support,
        material,
        args,
        mux,
        max_audio_validation_drops=max_audio_validation_drops,
    )
    started = clock()
    deadline = started + args.duration
    while clock() < deadline:
        for channel in (2, 3, 1):
            now = clock()
            if now >= deadline:
                break
            try:
                unit = readers[channel].read_one(
                    session, min(args.read_slice, deadline - now)
                )
            except TnpStreamError as exc:
                if exc.category != "MEDIA_CHANNEL_TIMEOUT":
                    raise ProbeError(exc.category) from exc
            else:
                collector.accept(channel, unit, clock())
                if progress_hook is not None:
                    progress_hook(collector)
                if (
                    collector.progress.passed
                    and mux.started
                    and (completion is None or completion())
                ):
                    return collector

            now = clock()
            last = collector.progress.last_progress_at
            if last is None and now - started >= args.media_start_timeout:
                raise ProbeError("MEDIA_START_TIMEOUT")
            if last is not None and now - last >= args.media_stall_timeout:
                raise ProbeError("MEDIA_SUSTAIN_TIMEOUT")
    if progress_hook is not None:
        progress_hook(collector)
    if collector.progress.passed and completion is not None and not completion():
        raise ProbeError(incomplete_category)
    raise ProbeError("MEDIA_SUSTAIN_TIMEOUT")


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
    progress = SustainedProgress(30.0, 2, 2, 2)
    for kind in ("I", "P", "audio"):
        progress.observe(kind, 1.0)
        progress.observe(kind, 1.0)
    assert not progress.passed
    print(
        "CR4B_SELF_TEST=PASS; cloud_used=false; network_used=false; tnp_sent=false; "
        "media_requested=false; ffmpeg_started=false; ffprobe_started=false; "
        "runtime_support_imported=false"
    )
    return 0


def support_smoke_test(app_source: Path | None) -> int:
    support = _media_support(app_source)
    assert callable(support.video._decode_video_unit)
    assert callable(support.video.SequenceReorderBuffer)
    assert callable(support.audio.decrypt_audio_unit)
    assert callable(support.audio.signed_delta32)
    assert callable(support.audio._setts)
    assert callable(support.audio.start_ffmpeg)
    print(
        "CR4B_SUPPORT_SMOKE=PASS; cloud_used=false; network_used=false; "
        "device_traffic=false; processes_started=false"
    )
    return 0


def mux_support_smoke_test(
    ffmpeg: str, ffprobe: str, app_source: Path | None
) -> int:
    support = _media_support(app_source)
    ffmpeg_path = shutil.which(ffmpeg)
    ffprobe_path = shutil.which(ffprobe)
    ffmpeg_command(ffmpeg, 10, 11, 0, 5, support.audio._setts)
    ffprobe_command(ffprobe)
    passed = bool(ffmpeg_path and ffprobe_path)
    print(
        f"CR4B_MUX_SUPPORT_SMOKE={'PASS' if passed else 'FAIL'}; "
        f"ffmpeg_available={str(bool(ffmpeg_path)).lower()}; "
        f"ffprobe_available={str(bool(ffprobe_path)).lower()}; "
        "processes_started=false; cloud_used=false; device_traffic=false"
    )
    return 0 if passed else 2


def _validate_args(args: argparse.Namespace) -> None:
    positive = (
        args.duration,
        args.min_active_seconds,
        args.media_start_timeout,
        args.media_stall_timeout,
        args.read_slice,
        args.child_timeout,
    )
    if (
        any(value <= 0 for value in positive)
        or args.duration <= args.min_active_seconds
        or min(args.min_i_frames, args.min_p_frames, args.min_audio_frames) < 1
        or min(args.max_video_records, args.max_audio_records) < 1
        or min(args.pre_mux_video_frames, args.pre_mux_audio_frames, args.pre_mux_bytes) < 1
    ):
        raise ProbeError("CR4B_SETUP")


def _run_live(args: argparse.Namespace) -> int:
    material: Any | None = None
    session: CleanPpppSession | None = None
    mux: PipeMuxValidator | None = None
    collector: SustainedCollector | None = None
    stop_unit: bytes | None = None
    startup_sent = False
    stop_sent = False
    mux_result: dict[str, Any] | None = None
    failure: str | None = None
    try:
        _validate_args(args)
        material, _report = load_material(args.env_file, args.cloud_timeout, args.camera_id)
        support = _media_support(args.app_source)
        units = support.phase3e._build_units(material)
        if tuple(map(len, units)) != (56, 52, 56, 56):
            raise ProbeError("CR4B_SETUP")
        stop_unit = units[3]
        mux = PipeMuxValidator(
            args.ffmpeg,
            args.ffprobe,
            support.audio._setts,
            args.child_timeout,
            args.pump_chunk_bytes,
        )
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
        header = _read_exact(session, 8, args.control_read_timeout)
        body_size = int.from_bytes(header[4:8], "big")
        if not 40 <= body_size <= 4096 - 8:
            raise ProbeError("CONTROL_AUTH_FAILED")
        body = _read_exact(session, body_size, args.control_read_timeout)
        if not support.phase3e.validate_first_4882(header, body):
            raise ProbeError("CONTROL_AUTH_FAILED")
        print("cr3_control_result=PASS", flush=True)

        session.enable_read_channels((1, 2, 3))
        print("media_channels_enabled=true", flush=True)
        collector = _collect_sustained(session, support, material, args, mux)
    except (ProbeError, MuxError, TransportError) as exc:
        failure = exc.category
    except Exception:
        failure = "CR4B_SETUP"
    finally:
        if startup_sent and session is not None and stop_unit is not None:
            try:
                session.write_channel(0, stop_unit)
                stop_sent = session.flush_channel(0) > 0
            except Exception:
                stop_sent = False
            print(f"stop_live_767_sent={str(stop_sent).lower()}", flush=True)
            if not stop_sent and failure is None:
                failure = "STOP_LIVE_FAILED"

        if mux is not None:
            if collector is not None and collector.progress.passed and mux.started:
                try:
                    mux_result = mux.finish()
                except MuxError as exc:
                    failure = exc.category
            mux.abort()

        if session is not None:
            session.close()
            print("transport_closed=true", flush=True)
        if material is not None:
            material.clear()

    if collector is not None:
        progress = collector.progress
        print("sustained_media_started=true", flush=True)
        print(f"media_active_seconds={progress.active_seconds:.3f}", flush=True)
        print(f"video_i_frames={progress.counts['I']}", flush=True)
        print(f"video_p_frames={progress.counts['P']}", flush=True)
        print(f"video_reordered_frames={progress.reordered_frames}", flush=True)
        print(f"audio_frames={progress.counts['audio']}", flush=True)
        for channel in (1, 2, 3):
            print(
                f"channel{channel}_tnp_units={collector.channel_counts[channel]}",
                flush=True,
            )
        print("aac_sample_rate=16000", flush=True)
        print("aac_channels=1", flush=True)
        print(f"initial_av_delta_ms={collector.initial_av_delta_ms}", flush=True)
        print(f"drw_retries={getattr(session, 'drw_retried', 0)}", flush=True)
        print(f"d2_observed={getattr(session, 'd2_observed', 0)}", flush=True)
        print(
            f"sustained_media_result={'PASS' if progress.passed else 'FAIL'}",
            flush=True,
        )
    if mux_result is not None:
        print("mux_started=true", flush=True)
        print(f"mpegts_bytes_observed={mux_result['mpegts_bytes']}", flush=True)
        print(f"ffprobe_video_codec={mux_result['video_codec']}", flush=True)
        print(f"ffprobe_audio_codec={mux_result['audio_codec']}", flush=True)
        print(f"ffprobe_video_size={mux_result['video_size']}", flush=True)
        print(
            f"ffprobe_audio_sample_rate={mux_result['audio_sample_rate']}", flush=True
        )
        print(f"ffprobe_audio_channels={mux_result['audio_channels']}", flush=True)
        print("mux_validation_result=PASS", flush=True)

    passed = bool(
        collector
        and collector.progress.passed
        and mux_result
        and stop_sent
        and session
        and session.closed
        and failure is None
    )
    if passed:
        print("cr4b_result=PASS", flush=True)
        return 0
    print(f"cr4b_result=FAIL; failure_category={failure or 'CR4B_SETUP'}", flush=True)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="CR-4B sustained clean media/mux probe")
    parser.add_argument("--server", dest="servers", action="append", type=parse_server)
    parser.add_argument("--camera-id", type=parse_camera_id)
    parser.add_argument("--env-file", type=Path, default=Path("/data/yi.env"))
    parser.add_argument("--app-source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
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
    parser.add_argument("--duration", type=float, default=45.0)
    parser.add_argument("--min-active-seconds", type=float, default=30.0)
    parser.add_argument("--min-i-frames", type=int, default=2)
    parser.add_argument("--min-p-frames", type=int, default=2)
    parser.add_argument("--min-audio-frames", type=int, default=2)
    parser.add_argument("--max-video-records", type=int, default=4096)
    parser.add_argument("--max-audio-records", type=int, default=4096)
    parser.add_argument("--media-start-timeout", type=float, default=5.0)
    parser.add_argument("--media-stall-timeout", type=float, default=5.0)
    parser.add_argument("--read-slice", type=float, default=0.05)
    parser.add_argument("--reorder-pending", type=int, default=24)
    parser.add_argument("--reorder-wait", type=float, default=0.35)
    parser.add_argument("--pre-mux-video-frames", type=int, default=24)
    parser.add_argument("--pre-mux-audio-frames", type=int, default=32)
    parser.add_argument("--pre-mux-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--pump-chunk-bytes", type=int, default=65536)
    parser.add_argument("--child-timeout", type=float, default=20.0)
    parser.add_argument("--ack-timeout", type=float, default=2.0)
    parser.add_argument("--control-read-timeout", type=float, default=4.0)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--support-smoke-test", action="store_true")
    parser.add_argument("--mux-support-smoke-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.support_smoke_test:
        return support_smoke_test(args.app_source)
    if args.mux_support_smoke_test:
        return mux_support_smoke_test(args.ffmpeg, args.ffprobe, args.app_source)
    if not args.servers:
        parser.error("at least one --server is required")
    if not args.camera_id:
        parser.error("--camera-id is required for a live probe")
    return _run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
