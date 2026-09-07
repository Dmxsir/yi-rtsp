#!/usr/bin/env python3
"""CR-4C temporary loopback-only go2rtc/RTSP publication probe."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from .mux_pipe import TS_PACKET_BYTES, MuxError, ffmpeg_command
    from .probe_channel0_tnp import _read_exact
    from .probe_legacy_punch import load_material, parse_camera_id, parse_server
    from .probe_media_tnp import DEFAULT_MAX_RECORD, _media_support
    from .probe_sustained_mux import ProbeError, SustainedCollector, _collect_sustained
    from .rtsp_publish import (
        CR4CError,
        DEFAULT_API_PORT,
        DEFAULT_RTSP_PORT,
        INGEST_FINALIZE_GO2RTC_EOF,
        INGEST_FINALIZE_HTTP_RESPONSE,
        INGEST_FINALIZE_PEER_CLOSED,
        STREAM_NAME,
        ChunkedIngestSink,
        MpegTsIngestMux,
        PublicationCoordinator,
        RtspConsumer,
        TemporaryGo2RTC,
        render_go2rtc_config,
        rtsp_consumer_command,
    )
    from .yi_pppp import DeviceId
    from .yi_pppp_session import CleanPpppSession, ExperimentalPolicy, TransportError
except ImportError:  # Direct execution from a relocated clean-room directory.
    from mux_pipe import TS_PACKET_BYTES, MuxError, ffmpeg_command
    from probe_channel0_tnp import _read_exact
    from probe_legacy_punch import load_material, parse_camera_id, parse_server
    from probe_media_tnp import DEFAULT_MAX_RECORD, _media_support
    from probe_sustained_mux import ProbeError, SustainedCollector, _collect_sustained
    from rtsp_publish import (
        CR4CError,
        DEFAULT_API_PORT,
        DEFAULT_RTSP_PORT,
        INGEST_FINALIZE_GO2RTC_EOF,
        INGEST_FINALIZE_HTTP_RESPONSE,
        INGEST_FINALIZE_PEER_CLOSED,
        STREAM_NAME,
        ChunkedIngestSink,
        MpegTsIngestMux,
        PublicationCoordinator,
        RtspConsumer,
        TemporaryGo2RTC,
        render_go2rtc_config,
        rtsp_consumer_command,
    )
    from yi_pppp import DeviceId
    from yi_pppp_session import CleanPpppSession, ExperimentalPolicy, TransportError


def self_test() -> int:
    forbidden = (
        "yi_camera_manager",
        "yi_tnp_oracle",
        "yi_cloud_probe",
        "yi_live_relay",
        "yi_native_av_relay",
        "yi_media_publisher",
        "yi_runtime_lifecycle",
        "_yi_cr4_phase3e",
    )
    assert not any(name in sys.modules for name in forbidden)
    config = render_go2rtc_config(DEFAULT_API_PORT, DEFAULT_RTSP_PORT)
    command = rtsp_consumer_command("ffprobe", DEFAULT_RTSP_PORT, 8.0, 5.0)
    assert STREAM_NAME in config and STREAM_NAME in command[-1]
    print(
        "CR4C_SELF_TEST=PASS; cloud_used=false; network_used=false; "
        "tnp_sent=false; media_requested=false; ffmpeg_started=false; "
        "ffprobe_started=false; go2rtc_started=false; "
        "rtsp_consumer_started=false; runtime_support_imported=false"
    )
    return 0


def support_smoke_test(app_source: Path | None) -> int:
    support = _media_support(app_source)
    assert callable(support.video._decode_video_unit)
    assert callable(support.video.SequenceReorderBuffer)
    assert callable(support.audio.decrypt_audio_unit)
    assert callable(support.audio.signed_delta32)
    assert callable(support.audio._setts)
    print(
        "CR4C_SUPPORT_SMOKE=PASS; cloud_used=false; network_used=false; "
        "device_traffic=false; processes_started=false"
    )
    return 0


def rtsp_support_smoke_test(
    ffmpeg: str, ffprobe: str, go2rtc: str, app_source: Path | None
) -> int:
    support = _media_support(app_source)
    availability = {
        "ffmpeg": bool(shutil.which(ffmpeg)),
        "ffprobe": bool(shutil.which(ffprobe)),
        "go2rtc": bool(shutil.which(go2rtc)),
    }
    ffmpeg_command(ffmpeg, 10, 11, 0, 5, support.audio._setts)
    rtsp_consumer_command(ffprobe, DEFAULT_RTSP_PORT, 8.0, 5.0)
    render_go2rtc_config(DEFAULT_API_PORT, DEFAULT_RTSP_PORT)
    passed = all(availability.values())
    print(
        f"CR4C_RTSP_SUPPORT_SMOKE={'PASS' if passed else 'FAIL'}; "
        f"ffmpeg_available={str(availability['ffmpeg']).lower()}; "
        f"ffprobe_available={str(availability['ffprobe']).lower()}; "
        f"go2rtc_available={str(availability['go2rtc']).lower()}; "
        "processes_started=false; network_used=false"
    )
    return 0 if passed else 2


def _validate_args(args: argparse.Namespace) -> None:
    positive = (
        args.duration,
        args.min_active_seconds,
        args.min_rtsp_consumer_seconds,
        args.media_start_timeout,
        args.media_stall_timeout,
        args.read_slice,
        args.child_timeout,
        args.go2rtc_startup_timeout,
        args.producer_timeout,
        args.ingest_timeout,
        args.ingest_io_timeout,
        args.rtsp_io_timeout,
        args.terminate_grace,
    )
    if (
        any(value <= 0 for value in positive)
        or args.duration <= args.min_active_seconds
        or args.duration <= args.min_rtsp_consumer_seconds
        or min(args.min_i_frames, args.min_p_frames, args.min_audio_frames) < 1
        or min(args.max_video_records, args.max_audio_records) < 1
        or args.max_record_bytes < 8
        or args.max_audio_validation_drops < 0
        or not TS_PACKET_BYTES <= args.pump_chunk_bytes <= 1024 * 1024
        or min(args.pre_mux_video_frames, args.pre_mux_audio_frames, args.pre_mux_bytes) < 1
    ):
        raise CR4CError("CR4C_SETUP")
    render_go2rtc_config(args.research_api_port, args.research_rtsp_port)


def _print_source(collector: SustainedCollector | None, session: Any | None) -> None:
    if collector is None:
        return
    progress = collector.progress
    print("source_media_started=true", flush=True)
    print(f"source_media_active_seconds={progress.active_seconds:.3f}", flush=True)
    print(f"source_video_i_frames={progress.counts['I']}", flush=True)
    print(f"source_video_p_frames={progress.counts['P']}", flush=True)
    print(f"source_video_reordered_frames={progress.reordered_frames}", flush=True)
    print(f"source_audio_frames={progress.counts['audio']}", flush=True)
    print(
        f"audio_validation_drops={collector.audio_validation_drops}", flush=True
    )
    for channel in (1, 2, 3):
        print(f"channel{channel}_tnp_units={collector.channel_counts[channel]}", flush=True)
    print("source_aac_sample_rate=16000", flush=True)
    print("source_aac_channels=1", flush=True)
    print(f"initial_av_delta_ms={collector.initial_av_delta_ms}", flush=True)
    print(f"drw_retries={getattr(session, 'drw_retried', 0)}", flush=True)
    print(f"d2_observed={getattr(session, 'd2_observed', 0)}", flush=True)
    print(
        f"source_media_result={'PASS' if progress.passed else 'FAIL'}", flush=True
    )


def _print_publication(
    mux_result: dict[str, Any] | None,
    coordinator: PublicationCoordinator | None,
) -> None:
    if mux_result is not None:
        print("mpegts_mux_started=true", flush=True)
        print("ingest_connected=true", flush=True)
        print(
            f"mpegts_published_bytes={mux_result['mpegts_published_bytes']}",
            flush=True,
        )
        print(
            f"ingest_finalize_mode={mux_result['ingest_finalize_mode']}",
            flush=True,
        )
    if coordinator is None:
        return
    if coordinator.producer is not None:
        print("producer_registered=true", flush=True)
        print("producer_media_ready=true", flush=True)
    result = coordinator.result
    if result is None:
        return
    print("rtsp_consumer_connected=true", flush=True)
    print(f"rtsp_video_codec={result['video_codec']}", flush=True)
    print(f"rtsp_video_size={result['video_size']}", flush=True)
    print(f"rtsp_audio_codec={result['audio_codec']}", flush=True)
    print(f"rtsp_audio_sample_rate={result['audio_sample_rate']}", flush=True)
    print(f"rtsp_audio_channels={result['audio_channels']}", flush=True)
    print(f"rtsp_video_packets={result['video_packets']}", flush=True)
    print(f"rtsp_audio_packets={result['audio_packets']}", flush=True)
    print(f"rtsp_consumer_active_seconds={result['active_seconds']:.3f}", flush=True)
    print("rtsp_consumer_result=PASS", flush=True)


def _run_live(args: argparse.Namespace) -> int:
    material: Any | None = None
    session: CleanPpppSession | None = None
    controller: TemporaryGo2RTC | None = None
    mux: MpegTsIngestMux | None = None
    coordinator: PublicationCoordinator | None = None
    collector: SustainedCollector | None = None
    stop_unit: bytes | None = None
    startup_sent = False
    stop_sent = False
    mux_result: dict[str, Any] | None = None
    failure: str | None = None
    try:
        _validate_args(args)
        controller = TemporaryGo2RTC(
            args.go2rtc,
            args.research_api_port,
            args.research_rtsp_port,
            args.go2rtc_startup_timeout,
            args.producer_timeout,
            args.terminate_grace,
            Path("/tmp/yi-cr4c"),
        )
        controller.start()
        print("temporary_go2rtc_ready=true", flush=True)

        material, _report = load_material(args.env_file, args.cloud_timeout, args.camera_id)
        support = _media_support(args.app_source)
        units = support.phase3e._build_units(material)
        if tuple(map(len, units)) != (56, 52, 56, 56):
            raise CR4CError("CR4C_SETUP")
        stop_unit = units[3]
        sink = ChunkedIngestSink(args.research_api_port, args.ingest_io_timeout)
        mux = MpegTsIngestMux(
            args.ffmpeg,
            support.audio._setts,
            sink,
            args.child_timeout,
            args.pump_chunk_bytes,
        )
        consumer = RtspConsumer(
            args.ffprobe,
            args.research_rtsp_port,
            args.min_rtsp_consumer_seconds,
            args.rtsp_io_timeout,
        )
        coordinator = PublicationCoordinator(
            controller, mux, consumer, args.ingest_timeout
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
            raise CR4CError("CONTROL_AUTH_FAILED")
        body = _read_exact(session, body_size, args.control_read_timeout)
        if not support.phase3e.validate_first_4882(header, body):
            raise CR4CError("CONTROL_AUTH_FAILED")
        print("cr3_control_result=PASS", flush=True)

        session.enable_read_channels((1, 2, 3))
        print("media_channels_enabled=true", flush=True)
        coordinator.start()

        def check_progress(current: SustainedCollector) -> None:
            nonlocal collector
            collector = current
            coordinator.raise_if_failed()

        collector = _collect_sustained(
            session,
            support,
            material,
            args,
            mux,
            progress_hook=check_progress,
            completion=lambda: coordinator.done,
            incomplete_category="RTSP_CONSUMER_TIMEOUT",
            max_audio_validation_drops=args.max_audio_validation_drops,
        )
        coordinator.raise_if_failed()
    except (CR4CError, ProbeError, MuxError, TransportError) as exc:
        failure = exc.category
    except Exception:
        failure = "CR4C_SETUP"
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
            if (
                collector is not None
                and collector.progress.passed
                and coordinator is not None
                and coordinator.done
            ):
                try:
                    mux_result = mux.finish()
                except CR4CError as exc:
                    if failure is None:
                        failure = exc.category
            try:
                mux.abort()
            except CR4CError as exc:
                if failure is None:
                    failure = exc.category

        if coordinator is not None:
            try:
                coordinator.stop()
            except CR4CError as exc:
                if failure is None:
                    failure = exc.category
            print(
                f"rtsp_consumer_stopped={str(coordinator.consumer.stopped).lower()}",
                flush=True,
            )
        if controller is not None:
            try:
                controller.stop()
            except CR4CError as exc:
                if failure is None:
                    failure = exc.category
            print(
                f"temporary_go2rtc_stopped={str(controller.stopped).lower()}",
                flush=True,
            )
        if session is not None:
            session.close()
            print("transport_closed=true", flush=True)
        if material is not None:
            material.clear()

    _print_source(collector, session)
    _print_publication(mux_result, coordinator)
    producer = coordinator.producer if coordinator is not None else None
    rtsp_result = coordinator.result if coordinator is not None else None
    passed = bool(
        collector
        and collector.progress.passed
        and mux_result
        and mux_result.get("mpegts_published_bytes", 0) > 0
        and mux_result.get("ingest_finalize_mode")
        in (
            INGEST_FINALIZE_HTTP_RESPONSE,
            INGEST_FINALIZE_PEER_CLOSED,
            INGEST_FINALIZE_GO2RTC_EOF,
        )
        and coordinator
        and coordinator.done
        and producer
        and producer.get("producer_registered")
        and producer.get("producer_media_ready")
        and rtsp_result
        and rtsp_result.get("video_codec") == "h264"
        and rtsp_result.get("video_size") == "1920x1080"
        and rtsp_result.get("audio_codec") == "aac"
        and rtsp_result.get("audio_sample_rate") == 16000
        and rtsp_result.get("audio_channels") == 1
        and rtsp_result.get("video_packets", 0) > 0
        and rtsp_result.get("audio_packets", 0) > 0
        and coordinator.consumer.stopped
        and stop_sent
        and controller
        and controller.stopped
        and session
        and session.closed
        and failure is None
    )
    if passed:
        print("cr4c_result=PASS", flush=True)
        return 0
    print(f"cr4c_result=FAIL; failure_category={failure or 'CR4C_SETUP'}", flush=True)
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CR-4C isolated RTSP publication probe")
    parser.add_argument("--server", dest="servers", action="append", type=parse_server)
    parser.add_argument("--camera-id", type=parse_camera_id)
    parser.add_argument("--env-file", type=Path, default=Path("/data/yi.env"))
    parser.add_argument("--app-source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--go2rtc", default="/usr/local/bin/go2rtc")
    parser.add_argument("--research-api-port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument("--research-rtsp-port", type=int, default=DEFAULT_RTSP_PORT)
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
    parser.add_argument("--min-rtsp-consumer-seconds", type=float, default=8.0)
    parser.add_argument("--min-i-frames", type=int, default=2)
    parser.add_argument("--min-p-frames", type=int, default=2)
    parser.add_argument("--min-audio-frames", type=int, default=2)
    parser.add_argument("--max-video-records", type=int, default=4096)
    parser.add_argument("--max-audio-records", type=int, default=4096)
    parser.add_argument("--max-audio-validation-drops", type=int, default=3)
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
    parser.add_argument("--go2rtc-startup-timeout", type=float, default=10.0)
    parser.add_argument("--producer-timeout", type=float, default=10.0)
    parser.add_argument("--ingest-timeout", type=float, default=10.0)
    parser.add_argument("--ingest-io-timeout", type=float, default=5.0)
    parser.add_argument("--rtsp-io-timeout", type=float, default=5.0)
    parser.add_argument("--terminate-grace", type=float, default=3.0)
    parser.add_argument("--ack-timeout", type=float, default=2.0)
    parser.add_argument("--control-read-timeout", type=float, default=4.0)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--support-smoke-test", action="store_true")
    parser.add_argument("--rtsp-support-smoke-test", action="store_true")
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.support_smoke_test:
        return support_smoke_test(args.app_source)
    if args.rtsp_support_smoke_test:
        return rtsp_support_smoke_test(
            args.ffmpeg, args.ffprobe, args.go2rtc, args.app_source
        )
    if not args.servers:
        parser.error("at least one --server is required")
    if not args.camera_id:
        parser.error("--camera-id is required for a live probe")
    return _run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
