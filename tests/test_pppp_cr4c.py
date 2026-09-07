from __future__ import annotations

import io
import json
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLEANROOM = ROOT / "tools" / "pppp_cleanroom"
APP_DIR = ROOT / "yi_home" / "rootfs" / "opt" / "yi-home" / "app"
sys.path.insert(0, str(CLEANROOM))
sys.path.insert(0, str(APP_DIR))

import probe_rtsp_publish as runner  # noqa: E402
import rtsp_publish as publish  # noqa: E402
from yi_pppp import DeviceId  # noqa: E402
from yi_pppp_session import TransportError  # noqa: E402


def _metadata(**changes: object) -> bytes:
    video = {
        "codec_type": "video",
        "codec_name": "h264",
        "width": 1920,
        "height": 1080,
        "nb_read_packets": "20",
    }
    audio = {
        "codec_type": "audio",
        "codec_name": "aac",
        "sample_rate": "16000",
        "channels": 1,
        "nb_read_packets": "30",
    }
    for key, value in changes.items():
        target, field = key.split("_", 1)
        (video if target == "video" else audio)[field] = value
    return json.dumps({"streams": [video, audio]}).encode()


class _Clock:
    def __init__(self, values: list[float] | None = None) -> None:
        self.values = iter(values or [])
        self.now = 0.0

    def __call__(self) -> float:
        try:
            self.now = next(self.values)
        except StopIteration:
            self.now += 0.1
        return self.now


class _Process:
    def __init__(
        self,
        *,
        returncode: int | None = None,
        final_returncode: int = 0,
        output: bytes = b"",
        communicate_timeout: bool = False,
        terminate_timeout: bool = False,
    ) -> None:
        self.returncode = returncode
        self.final_returncode = final_returncode
        self.output = output
        self.communicate_timeout = communicate_timeout
        self.terminate_timeout = terminate_timeout
        self.terminated = False
        self.killed = False
        self.stdout = io.BytesIO()

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.terminate_timeout and self.terminated and not self.killed:
            raise subprocess.TimeoutExpired("synthetic", timeout)
        self.returncode = -9 if self.killed else self.final_returncode
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        if self.communicate_timeout and not self.killed:
            raise subprocess.TimeoutExpired("synthetic", timeout)
        self.returncode = -9 if self.killed else self.final_returncode
        return self.output, b""


class ConfigAndControllerTest(unittest.TestCase):
    def test_config_is_loopback_only_and_uses_only_synthetic_identity(self) -> None:
        config = publish.render_go2rtc_config(11984, 18554)
        self.assertIn('listen: "127.0.0.1:11984"', config)
        self.assertIn('listen: "127.0.0.1:18554"', config)
        self.assertEqual(config.count(publish.STREAM_NAME), 1)
        for forbidden in ("0.0.0.0", "[::]", "stable_id", "camera", "did", "uid"):
            self.assertNotIn(forbidden, config.casefold())

    def test_production_invalid_and_duplicate_ports_are_rejected(self) -> None:
        for api, rtsp in ((1984, 19000), (19000, 8554), (19000, 19000), (0, 19000)):
            with self.subTest(api=api, rtsp=rtsp):
                with self.assertRaises(publish.CR4CError) as raised:
                    publish.render_go2rtc_config(api, rtsp)
                self.assertEqual(raised.exception.category, "CR4C_PORT_INVALID")

        self.assertIn("18554", publish.render_go2rtc_config(18554, 19000))
        command = publish.rtsp_consumer_command("ffprobe", 11984, 8.0, 5.0)
        self.assertIn(":11984/", command[-1])

    def test_missing_binary_fails_before_port_or_process_activity(self) -> None:
        controller = publish.TemporaryGo2RTC("missing-go2rtc")
        with (
            mock.patch.object(publish.shutil, "which", return_value=None),
            mock.patch.object(controller, "_preflight_ports") as preflight,
            mock.patch.object(publish.subprocess, "Popen") as popen,
        ):
            with self.assertRaises(publish.CR4CError) as raised:
                controller.start()
        self.assertEqual(raised.exception.category, "GO2RTC_START_FAILED")
        preflight.assert_not_called()
        popen.assert_not_called()

    def test_port_conflict_is_a_hard_failure_without_spawning(self) -> None:
        controller = publish.TemporaryGo2RTC("go2rtc")
        conflicting = mock.Mock()
        conflicting.bind.side_effect = OSError("synthetic conflict")
        with (
            mock.patch.object(publish.shutil, "which", return_value="/synthetic/go2rtc"),
            mock.patch.object(publish.socket, "socket", return_value=conflicting),
            mock.patch.object(publish.subprocess, "Popen") as popen,
        ):
            with self.assertRaises(publish.CR4CError) as raised:
                controller.start()
        self.assertEqual(raised.exception.category, "CR4C_PORT_IN_USE")
        popen.assert_not_called()
        conflicting.close.assert_called()

    def _start_controller(
        self, temporary: str, process: _Process, api_values: list[dict[str, object] | None]
    ) -> tuple[publish.TemporaryGo2RTC, Path]:
        controller = publish.TemporaryGo2RTC(
            "go2rtc",
            startup_timeout=0.5,
            temp_root=Path(temporary) / "yi-cr4c",
            clock=_Clock(),
            sleeper=lambda _seconds: None,
        )
        with (
            mock.patch.object(publish.shutil, "which", return_value="/synthetic/go2rtc"),
            mock.patch.object(controller, "_preflight_ports"),
            mock.patch.object(controller, "_api_json", side_effect=api_values),
            mock.patch.object(publish.subprocess, "Popen", return_value=process),
        ):
            controller.start()
        assert controller.run_dir is not None
        return controller, controller.run_dir

    def test_startup_success_and_normal_cleanup_remove_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            process = _Process()
            controller, run_dir = self._start_controller(
                temporary, process, [{publish.STREAM_NAME: {}}]
            )
            try:
                self.assertTrue(controller.ready)
                self.assertTrue((run_dir / "go2rtc.yaml").is_file())
                if sys.platform != "win32":
                    self.assertEqual(
                        (run_dir / "go2rtc.yaml").stat().st_mode & 0o777, 0o600
                    )
            finally:
                controller.stop()
            self.assertTrue(process.terminated)
            self.assertFalse(run_dir.exists())
            self.assertTrue(controller.stopped)

    def test_startup_timeout_and_early_exit_cleanup(self) -> None:
        for process, expected in (
            (_Process(), "GO2RTC_START_TIMEOUT"),
            (_Process(returncode=1), "GO2RTC_EARLY_EXIT"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                controller = publish.TemporaryGo2RTC(
                    "go2rtc",
                    startup_timeout=0.2,
                    temp_root=Path(temporary) / "yi-cr4c",
                    clock=_Clock(),
                    sleeper=lambda _seconds: None,
                )
                with (
                    mock.patch.object(publish.shutil, "which", return_value="/x/go2rtc"),
                    mock.patch.object(controller, "_preflight_ports"),
                    mock.patch.object(controller, "_api_json", return_value={}),
                    mock.patch.object(publish.subprocess, "Popen", return_value=process),
                ):
                    with self.assertRaises(publish.CR4CError) as raised:
                        controller.start()
                self.assertEqual(raised.exception.category, expected)
                self.assertIsNone(controller.run_dir)
                self.assertTrue(controller.stopped)

    def test_cleanup_escalates_from_terminate_to_kill(self) -> None:
        controller = publish.TemporaryGo2RTC("go2rtc")
        process = _Process(terminate_timeout=True)
        controller.process = process
        controller.stop()
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertIsNotNone(process.poll())

    def test_cleanup_timeout_cannot_report_stopped(self) -> None:
        class Unkillable(_Process):
            def wait(self, timeout: float | None = None) -> int:
                raise subprocess.TimeoutExpired("synthetic", timeout)

            def kill(self) -> None:
                self.killed = True

        controller = publish.TemporaryGo2RTC("go2rtc", terminate_grace=0.01)
        process = Unkillable()
        controller.process = process
        with self.assertRaises(publish.CR4CError) as raised:
            controller.stop()
        self.assertEqual(raised.exception.category, "CR4C_CLEANUP_TIMEOUT")
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertFalse(controller.stopped)


class ProducerReadinessTest(unittest.TestCase):
    def test_registered_and_ready_states(self) -> None:
        cases = (
            (None, (False, False)),
            ({"producers": []}, (False, False)),
            ({"producers": [{"format_name": "mpegts", "medias": []}]}, (True, False)),
            (
                {"producers": [{"format_name": "mpegts", "medias": ["video"]}]},
                (True, True),
            ),
        )
        for snapshot, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(publish._registered_and_ready(snapshot), expected)

    def test_absent_registered_empty_and_ready_are_distinguished(self) -> None:
        controller = publish.TemporaryGo2RTC(
            "go2rtc", producer_timeout=2.0, clock=_Clock(), sleeper=lambda _: None
        )
        controller.process = _Process()
        snapshots = [
            {publish.STREAM_NAME: {}},
            {"producers": []},
            {publish.STREAM_NAME: {}},
            {"producers": [{"format_name": "mpegts", "medias": []}]},
            {publish.STREAM_NAME: {}},
            {"producers": [{"format_name": "mpegts", "medias": ["video"]}]},
        ]
        with mock.patch.object(controller, "_api_json", side_effect=snapshots):
            result = controller.wait_producer_ready()
        self.assertEqual(
            result, {"producer_registered": True, "producer_media_ready": True}
        )

    def test_extra_or_mismatched_stream_never_satisfies_gate(self) -> None:
        controller = publish.TemporaryGo2RTC(
            "go2rtc", producer_timeout=0.15, clock=_Clock(), sleeper=lambda _: None
        )
        controller.process = _Process()
        with mock.patch.object(
            controller,
            "_api_json",
            return_value={publish.STREAM_NAME: {}, "unexpected": {}},
        ):
            with self.assertRaises(publish.CR4CError) as raised:
                controller.wait_producer_ready()
        self.assertEqual(raised.exception.category, "GO2RTC_PRODUCER_NOT_REGISTERED")

    def test_registered_but_never_media_ready_has_specific_failure(self) -> None:
        controller = publish.TemporaryGo2RTC(
            "go2rtc", producer_timeout=0.25, clock=_Clock(), sleeper=lambda _: None
        )
        controller.process = _Process()

        def api(path: str, _timeout: float = 1.0) -> dict[str, object]:
            if "?src=" in path:
                return {"producers": [{"format_name": "mpegts", "medias": []}]}
            return {publish.STREAM_NAME: {}}

        with mock.patch.object(controller, "_api_json", side_effect=api):
            with self.assertRaises(publish.CR4CError) as raised:
                controller.wait_producer_ready()
        self.assertEqual(raised.exception.category, "GO2RTC_PRODUCER_NOT_READY")


class _Response:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def read(self, _limit: int) -> bytes:
        return b""


class _Connection:
    def __init__(self, *_args: object, fail_at: int | None = None, status: int = 200, **_kwargs: object) -> None:
        self.fail_at = fail_at
        self.status = status
        self.sent: list[bytes] = []
        self.request: tuple[str, str] | None = None
        self.headers: list[tuple[str, str]] = []
        self.closed = False

    def putrequest(self, method: str, path: str) -> None:
        self.request = (method, path)

    def putheader(self, name: str, value: str) -> None:
        self.headers.append((name, value))

    def endheaders(self) -> None:
        return None

    def send(self, payload: bytes) -> None:
        if self.fail_at is not None and len(self.sent) >= self.fail_at:
            raise BrokenPipeError("synthetic downstream close")
        self.sent.append(payload)

    def getresponse(self) -> _Response:
        return _Response(self.status)

    def close(self) -> None:
        self.closed = True


class IngestTest(unittest.TestCase):
    def test_first_chunk_precedes_connection_and_framing_is_exact(self) -> None:
        calls: list[str] = []
        connection = _Connection()

        def factory(*args: object, **kwargs: object) -> _Connection:
            calls.append("connection")
            return connection

        sink = publish.ChunkedIngestSink(11984, 1.0, factory)
        self.assertEqual(calls, [])
        self.assertEqual(
            sink.path, "/api/stream.ts?dst=" + publish.STREAM_NAME
        )
        sink.open(b"first")
        sink.send(b"next")
        sink.finish()
        self.assertEqual(calls, ["connection"])
        self.assertEqual(connection.request, ("POST", sink.path))
        self.assertEqual(
            connection.headers,
            [
                ("Content-Type", "video/mp2t"),
                ("Transfer-Encoding", "chunked"),
                ("Cache-Control", "no-store"),
            ],
        )
        self.assertEqual(
            connection.sent,
            [b"5\r\n", b"first", b"\r\n", b"4\r\n", b"next", b"\r\n", b"0\r\n\r\n"],
        )
        self.assertEqual(sink.published_bytes, 9)
        self.assertTrue(connection.closed)

    def test_refusal_reset_http_error_and_timeout_are_sanitized(self) -> None:
        def refused(*_args: object, **_kwargs: object) -> _Connection:
            raise ConnectionRefusedError

        cases = (
            publish.ChunkedIngestSink(11984, 1.0, refused),
            publish.ChunkedIngestSink(
                11984,
                1.0,
                lambda *_a, **_k: (_ for _ in ()).throw(socket.timeout()),
            ),
            publish.ChunkedIngestSink(11984, 1.0, lambda *_a, **_k: _Connection(fail_at=0)),
            publish.ChunkedIngestSink(11984, 1.0, lambda *_a, **_k: _Connection(status=500)),
            publish.ChunkedIngestSink(
                11984,
                1.0,
                lambda *_a, **_k: _Connection(fail_at=3),
            ),
        )
        for index, sink in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(publish.CR4CError) as raised:
                    sink.open(b"first")
                    if index == 3:
                        sink.finish()
                    elif index == 4:
                        sink.send(b"timeout")
                self.assertEqual(raised.exception.category, "GO2RTC_INGEST_FAILED")

    def test_mux_pump_prebuffers_a_complete_ts_chunk_and_retains_no_payload(self) -> None:
        packet = b"\x47" + bytes(187)
        events: list[tuple[str, int]] = []

        class Sink:
            connected = False
            published_bytes = 0

            def open(self, chunk: bytes) -> None:
                events.append(("open", len(chunk)))
                self.connected = True
                self.published_bytes += len(chunk)

            def send(self, chunk: bytes) -> None:
                events.append(("send", len(chunk)))
                self.published_bytes += len(chunk)

            def finish(self) -> None:
                events.append(("finish", 0))

            def abort(self) -> None:
                events.append(("abort", 0))

        mux = publish.MpegTsIngestMux("ffmpeg", lambda *_: "setts", Sink(), 1.0, 188)
        mux._mux = _Process()
        mux._mux.stdout = io.BytesIO(packet * 2)
        mux._pump_output()
        self.assertEqual(events, [("open", 188), ("send", 188), ("finish", 0)])
        self.assertTrue(mux.ingest_connected.is_set())
        self.assertEqual(mux._result, {"mpegts_bytes": 376, "mpegts_packets": 2})
        self.assertFalse(hasattr(mux, "payload"))

    def test_mux_detects_partial_ts_and_downstream_epipe(self) -> None:
        class FailedSink:
            connected = False
            published_bytes = 0

            def open(self, _chunk: bytes) -> None:
                raise publish.CR4CError("GO2RTC_INGEST_FAILED")

            def abort(self) -> None:
                return None

        for raw, category in (
            (b"\x47", "MPEGTS_STREAM_INVALID"),
            (bytes(188), "MPEGTS_STREAM_INVALID"),
            ((b"\x47" + bytes(187)), "GO2RTC_INGEST_FAILED"),
        ):
            with self.subTest(category=category):
                mux = publish.MpegTsIngestMux(
                    "ffmpeg", lambda *_: "setts", FailedSink(), 1.0, 188
                )
                mux._mux = _Process()
                mux._mux.stdout = io.BytesIO(raw)
                mux._pump_output()
                self.assertEqual(mux.failure_category, category)

    def test_mux_feed_backpressure_is_bounded(self) -> None:
        sink = mock.Mock()
        mux = publish.MpegTsIngestMux("ffmpeg", lambda *_: "setts", sink, 0.01, 188)
        mux.started = True
        mux._mux = _Process()
        mux._video = mock.Mock()
        with mock.patch.object(publish, "write_mux_input", side_effect=publish.MuxError("MUX_PIPE_BACKPRESSURE")):
            with self.assertRaises(publish.MuxError) as raised:
                mux.feed_video(b"synthetic")
        self.assertEqual(raised.exception.category, "MUX_PIPE_BACKPRESSURE")

    def test_mux_early_exit_and_timeout_are_bounded(self) -> None:
        class Sink:
            published_bytes = 188

            def abort(self) -> None:
                return None

        class TimeoutOnce(_Process):
            def wait(self, timeout: float | None = None) -> int:
                if not self.killed:
                    raise subprocess.TimeoutExpired("synthetic", timeout)
                return super().wait(timeout)

        for process, category in (
            (_Process(final_returncode=1), "MUX_EARLY_EXIT"),
            (TimeoutOnce(), "MUX_PIPE_BACKPRESSURE"),
        ):
            with self.subTest(category=category):
                mux = publish.MpegTsIngestMux(
                    "ffmpeg", lambda *_: "setts", Sink(), 0.01, 188
                )
                mux.started = True
                mux._mux = process
                mux._video = io.BytesIO()
                mux._audio = io.BytesIO()
                mux._result = {"mpegts_bytes": 188, "mpegts_packets": 1}
                with self.assertRaises(publish.CR4CError) as raised:
                    mux.finish()
                self.assertEqual(raised.exception.category, category)
                self.assertIsNotNone(process.poll())


class RtspConsumerTest(unittest.TestCase):
    def test_command_is_bounded_loopback_and_nonproduction(self) -> None:
        command = publish.rtsp_consumer_command("ffprobe", 18554, 8.0, 5.0)
        self.assertIn("-count_packets", command)
        self.assertIn("%+8", command)
        self.assertEqual(command[-1], "rtsp://127.0.0.1:18554/yi_cr4c_probe")
        self.assertNotIn(":8554/", command[-1])

    def test_correct_metadata_packets_and_span_are_accepted(self) -> None:
        result = publish.parse_rtsp_result(_metadata(), 8.0, 8.0)
        self.assertEqual(result["video_packets"], 20)
        self.assertEqual(result["audio_packets"], 30)

    def test_wrong_format_is_rejected(self) -> None:
        variants = (
            {"video_codec_name": "hevc"},
            {"video_width": 1280},
            {"video_height": 720},
            {"audio_codec_name": "opus"},
            {"audio_sample_rate": "8000"},
            {"audio_channels": 2},
        )
        for changes in variants:
            with self.subTest(changes=changes):
                with self.assertRaises(publish.CR4CError) as raised:
                    publish.parse_rtsp_result(_metadata(**changes), 8.0, 8.0)
                self.assertEqual(raised.exception.category, "RTSP_FORMAT_INVALID")

    def test_zero_packets_and_short_span_are_starvation(self) -> None:
        cases = (
            (_metadata(video_nb_read_packets="0"), 8.0),
            (_metadata(audio_nb_read_packets="0"), 8.0),
            (_metadata(), 7.9),
        )
        for raw, active in cases:
            with self.subTest(active=active):
                with self.assertRaises(publish.CR4CError) as raised:
                    publish.parse_rtsp_result(raw, active, 8.0)
                self.assertEqual(raised.exception.category, "RTSP_CONSUMER_STARVED")

    def test_consumer_success_early_exit_timeout_and_cleanup(self) -> None:
        cases = (
            (_Process(output=_metadata()), _Clock([0.0, 8.0]), None),
            (_Process(final_returncode=1), _Clock([0.0, 1.0]), "RTSP_CONNECT_FAILED"),
            (
                _Process(output=_metadata(), communicate_timeout=True),
                _Clock([0.0]),
                "RTSP_CONSUMER_TIMEOUT",
            ),
        )
        for process, clock, expected in cases:
            with self.subTest(expected=expected):
                consumer = publish.RtspConsumer(
                    "ffprobe", 18554, 8.0, 1.0, clock=clock
                )
                with mock.patch.object(publish.subprocess, "Popen", return_value=process):
                    if expected is None:
                        self.assertEqual(consumer.run()["video_codec"], "h264")
                    else:
                        with self.assertRaises(publish.CR4CError) as raised:
                            consumer.run()
                        self.assertEqual(raised.exception.category, expected)
                self.assertTrue(consumer.stopped)
                self.assertIsNotNone(process.poll())


class CoordinatorTest(unittest.TestCase):
    def test_success_sequence_and_failure_propagation(self) -> None:
        events: list[str] = []
        mux = SimpleNamespace(ingest_connected=threading.Event(), failure_category=None)
        mux.ingest_connected.set()

        class Controller:
            def wait_producer_ready(self, _cancel: object) -> dict[str, bool]:
                events.append("producer")
                return {"producer_registered": True, "producer_media_ready": True}

        class Consumer:
            stopped = False

            def run(self) -> dict[str, object]:
                events.append("consumer")
                self.stopped = True
                return {"video_codec": "h264"}

            def abort(self) -> None:
                self.stopped = True

        coordinator = publish.PublicationCoordinator(Controller(), mux, Consumer(), 1.0)
        coordinator.start()
        coordinator.thread.join(1.0)
        self.assertTrue(coordinator.done)
        self.assertEqual(events, ["producer", "consumer"])
        coordinator.stop()

        class FailedController:
            def wait_producer_ready(self, _cancel: object) -> dict[str, bool]:
                raise publish.CR4CError("GO2RTC_PRODUCER_NOT_READY")

        failed = publish.PublicationCoordinator(FailedController(), mux, Consumer(), 1.0)
        failed.start()
        failed.thread.join(1.0)
        with self.assertRaises(publish.CR4CError) as raised:
            failed.raise_if_failed()
        self.assertEqual(raised.exception.category, "GO2RTC_PRODUCER_NOT_READY")
        failed.stop()

    def test_ingest_timeout_and_mux_failure_do_not_start_consumer(self) -> None:
        for failure, expected in (
            (None, "GO2RTC_INGEST_FAILED"),
            ("MUX_EARLY_EXIT", "MUX_EARLY_EXIT"),
        ):
            with self.subTest(expected=expected):
                mux = SimpleNamespace(
                    ingest_connected=threading.Event(), failure_category=failure
                )
                consumer = mock.Mock()
                coordinator = publish.PublicationCoordinator(
                    mock.Mock(), mux, consumer, 0.05
                )
                coordinator.start()
                coordinator.thread.join(1.0)
                self.assertEqual(coordinator.error, expected)
                consumer.run.assert_not_called()
                coordinator.stop()


def _args(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "env_file": Path("unused"),
        "cloud_timeout": 1.0,
        "camera_id": "placeholder",
        "app_source": None,
        "servers": [("192.0.2.1", 32100)],
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
        "go2rtc": "go2rtc",
        "research_api_port": 11984,
        "research_rtsp_port": 18554,
        "temp_root": Path("unused"),
        "handshake_timeout": 1.0,
        "keepalive_timeout": 1.0,
        "keepalive_interval": 1.0,
        "request_retry_after": 1.0,
        "retry_after": 1.0,
        "max_attempts": 1,
        "punch_repeat": 1,
        "max_drw_payload": 1024,
        "receive_window": 32,
        "control_buffer_bytes": 4096,
        "media_buffer_bytes": 4096,
        "max_record_bytes": 256,
        "duration": 45.0,
        "min_active_seconds": 30.0,
        "min_rtsp_consumer_seconds": 8.0,
        "min_i_frames": 2,
        "min_p_frames": 2,
        "min_audio_frames": 2,
        "max_video_records": 100,
        "max_audio_records": 100,
        "media_start_timeout": 5.0,
        "media_stall_timeout": 5.0,
        "read_slice": 0.05,
        "reorder_pending": 24,
        "reorder_wait": 0.35,
        "pre_mux_video_frames": 24,
        "pre_mux_audio_frames": 32,
        "pre_mux_bytes": 4096,
        "pump_chunk_bytes": 188,
        "child_timeout": 1.0,
        "go2rtc_startup_timeout": 1.0,
        "producer_timeout": 1.0,
        "ingest_timeout": 1.0,
        "ingest_io_timeout": 1.0,
        "rtsp_io_timeout": 1.0,
        "terminate_grace": 1.0,
        "ack_timeout": 1.0,
        "control_read_timeout": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RunnerOrchestrationTest(unittest.TestCase):
    def _run(self, fail_stage: str | None = None) -> tuple[int, str, list[str]]:
        events: list[str] = []
        control = bytes((2, 3, 0, 0)) + struct.pack(">I", 40) + bytes(40)

        class Session:
            last: "Session | None" = None

            def __init__(self, *_args: object) -> None:
                self.closed = False
                self.stream = bytearray(control)
                self.queued: list[bytes] = []
                self.drw_retried = 0
                self.d2_observed = 0
                Session.last = self

            def connect(self, _device: object) -> None:
                events.append("connect")
                if fail_stage == "control":
                    raise TransportError("HANDSHAKE_TIMEOUT")

            def write_channel(self, _channel: int, data: bytes) -> None:
                self.queued.append(data)

            def flush_channel(self, _channel: int) -> int:
                stopping = len(b"".join(self.queued)) == 56
                events.append("stop" if stopping else "startup")
                self.queued.clear()
                if stopping and fail_stage == "stop":
                    return 0
                return 1

            def wait_channel_acked(self, *_args: object) -> None:
                return None

            def read_channel(self, channel: int, length: int, _timeout: float) -> bytes:
                if channel != 0:
                    raise AssertionError("collector mocked")
                data = bytes(self.stream[:length])
                del self.stream[:length]
                return data

            def enable_read_channels(self, _channels: tuple[int, ...]) -> None:
                events.append("channels")

            def close(self) -> None:
                events.append("transport_close")
                self.closed = True

        class Controller:
            def __init__(self, *_args: object) -> None:
                self.stopped = False

            def start(self) -> None:
                events.append("go2rtc_start")
                if fail_stage == "go2rtc":
                    raise publish.CR4CError("GO2RTC_START_FAILED")

            def stop(self) -> None:
                events.append("go2rtc_stop")
                self.stopped = True

        class Mux:
            def __init__(self, *_args: object) -> None:
                self.started = False

            def start(self, *_args: object) -> None:
                events.append("mux_start")
                self.started = True

            def finish(self) -> dict[str, int]:
                events.append("mux_finish")
                if fail_stage == "mux":
                    raise publish.CR4CError("MUX_EARLY_EXIT")
                return {"mpegts_published_bytes": 376}

            def abort(self) -> None:
                events.append("mux_abort")

        class Consumer:
            def __init__(self, *_args: object) -> None:
                self.stopped = False

        class Coordinator:
            def __init__(self, _controller: object, _mux: object, consumer: Consumer, *_args: object) -> None:
                self.consumer = consumer
                self.producer = None
                self.result = None
                self.error = None

            def start(self) -> None:
                events.append("coordinator_start")
                if fail_stage in ("ingest", "producer", "rtsp"):
                    self.error = {
                        "ingest": "GO2RTC_INGEST_FAILED",
                        "producer": "GO2RTC_PRODUCER_NOT_READY",
                        "rtsp": "RTSP_FORMAT_INVALID",
                    }[fail_stage]
                else:
                    self.producer = {
                        "producer_registered": True,
                        "producer_media_ready": True,
                    }
                    self.result = {
                        "video_codec": "h264",
                        "video_size": "1920x1080",
                        "audio_codec": "aac",
                        "audio_sample_rate": 16000,
                        "audio_channels": 1,
                        "video_packets": 20,
                        "audio_packets": 30,
                        "active_seconds": 8.0,
                    }

            @property
            def done(self) -> bool:
                return self.result is not None and self.error is None

            def raise_if_failed(self) -> None:
                if self.error:
                    raise publish.CR4CError(self.error)

            def stop(self) -> None:
                events.append("consumer_stop")
                self.consumer.stopped = True

        class Phase:
            @staticmethod
            def _build_units(_material: object) -> tuple[bytes, bytes, bytes, bytes]:
                return bytes(56), bytes(52), bytes(56), bytes(56)

            @staticmethod
            def validate_first_4882(_header: bytes, _body: bytes) -> bool:
                return True

        support = SimpleNamespace(phase3e=Phase(), audio=SimpleNamespace(_setts=lambda *_: "setts"))
        material = SimpleNamespace(
            pppp_did="", password="x" * 15, encrypted=False, clear=lambda: events.append("clear")
        )
        progress = SimpleNamespace(
            passed=True,
            active_seconds=30.0,
            counts={"I": 2, "P": 3, "audio": 4},
            reordered_frames=5,
        )
        collector = SimpleNamespace(
            progress=progress,
            channel_counts={1: 4, 2: 2, 3: 3},
            initial_av_delta_ms=5,
        )

        def collect(*call_args: object, **call_kwargs: object) -> object:
            events.append("collect")
            call_args[4].start(0, 5)
            call_kwargs["progress_hook"](collector)
            if fail_stage == "media":
                raise runner.ProbeError("MEDIA_SUSTAIN_TIMEOUT")
            return collector

        output = io.StringIO()
        with (
            mock.patch.object(runner, "load_material", return_value=(material, {})),
            mock.patch.object(runner, "_media_support", return_value=support),
            mock.patch.object(runner, "CleanPpppSession", Session),
            mock.patch.object(runner, "TemporaryGo2RTC", Controller),
            mock.patch.object(runner, "ChunkedIngestSink", return_value=mock.Mock()),
            mock.patch.object(runner, "MpegTsIngestMux", Mux),
            mock.patch.object(runner, "RtspConsumer", Consumer),
            mock.patch.object(runner, "PublicationCoordinator", Coordinator),
            mock.patch.object(runner, "_collect_sustained", side_effect=collect),
            mock.patch.object(runner.DeviceId, "from_text", return_value=DeviceId(bytes(20))),
            mock.patch("sys.stdout", output),
        ):
            result = runner._run_live(_args())
        return result, output.getvalue(), events

    def test_full_success_sequence_and_safe_transcript(self) -> None:
        result, output, events = self._run()
        self.assertEqual(result, 0)
        for marker in (
            "temporary_go2rtc_ready=true",
            "cr3_control_result=PASS",
            "source_media_result=PASS",
            "ingest_connected=true",
            "mpegts_published_bytes=376",
            "producer_media_ready=true",
            "rtsp_consumer_result=PASS",
            "stop_live_767_sent=true",
            "temporary_go2rtc_stopped=true",
            "transport_closed=true",
            "cr4c_result=PASS",
        ):
            self.assertIn(marker, output)
        expected = (
            "go2rtc_start",
            "connect",
            "startup",
            "channels",
            "coordinator_start",
            "collect",
            "mux_start",
            "stop",
            "mux_finish",
            "mux_abort",
            "consumer_stop",
            "go2rtc_stop",
            "transport_close",
            "clear",
        )
        self.assertEqual(tuple(events), expected)

    def test_major_stage_failures_cleanup_without_pass(self) -> None:
        for stage, category in (
            ("go2rtc", "GO2RTC_START_FAILED"),
            ("control", "HANDSHAKE_TIMEOUT"),
            ("media", "MEDIA_SUSTAIN_TIMEOUT"),
            ("ingest", "GO2RTC_INGEST_FAILED"),
            ("producer", "GO2RTC_PRODUCER_NOT_READY"),
            ("rtsp", "RTSP_FORMAT_INVALID"),
            ("mux", "MUX_EARLY_EXIT"),
            ("stop", "STOP_LIVE_FAILED"),
        ):
            with self.subTest(stage=stage):
                result, output, events = self._run(stage)
                self.assertEqual(result, 2)
                self.assertIn(f"failure_category={category}", output)
                self.assertNotIn("cr4c_result=PASS", output)
                self.assertIn("go2rtc_stop", events)
                if stage != "go2rtc":
                    self.assertIn("transport_close", events)


class IsolationAndRelocationTest(unittest.TestCase):
    cleanroom_files = (
        "mux_pipe.py",
        "probe_rtsp_publish.py",
        "rtsp_publish.py",
        "probe_sustained_mux.py",
        "probe_media_tnp.py",
        "tnp_stream.py",
        "probe_channel0_tnp.py",
        "probe_legacy_punch.py",
        "yi_pppp_session.py",
        "yi_pppp.py",
    )

    def test_runner_has_no_production_publisher_or_media_persistence(self) -> None:
        sources = "\n".join(
            (CLEANROOM / name).read_text(encoding="utf-8")
            for name in ("probe_rtsp_publish.py", "rtsp_publish.py")
        )
        for forbidden in (
            "YiGo2RTCPublisher",
            "frigate",
            "libPPPP",
        ):
            self.assertNotIn(forbidden, sources)
        for forbidden_import in (
            "from yi_media_publisher",
            "from yi_runtime_lifecycle",
            "from yi_camera_manager",
            "import yi_media_publisher",
            "import yi_runtime_lifecycle",
            "import yi_camera_manager",
        ):
            self.assertNotIn(forbidden_import, sources)
        for persistence in ("write_bytes(", "open(\"wb\"", "open('wb'", ".mp4"):
            self.assertNotIn(persistence, sources.casefold())

    def test_both_support_smokes_do_not_open_network_or_start_processes(self) -> None:
        with (
            mock.patch.object(runner, "_media_support", return_value=SimpleNamespace(
                video=SimpleNamespace(_decode_video_unit=lambda: None, SequenceReorderBuffer=lambda: None),
                audio=SimpleNamespace(
                    decrypt_audio_unit=lambda: None,
                    signed_delta32=lambda: None,
                    _setts=lambda kind, value: f"{kind}:{value}",
                ),
            )),
            mock.patch.object(runner.shutil, "which", return_value="/synthetic/tool"),
            mock.patch.object(publish.subprocess, "Popen") as popen,
            mock.patch.object(publish.socket, "socket") as network,
        ):
            output = io.StringIO()
            with mock.patch("sys.stdout", output):
                self.assertEqual(runner.support_smoke_test(None), 0)
                self.assertEqual(
                    runner.rtsp_support_smoke_test("ffmpeg", "ffprobe", "go2rtc", None),
                    0,
                )
        popen.assert_not_called()
        network.assert_not_called()
        for marker in (
            "CR4C_SUPPORT_SMOKE=PASS",
            "CR4C_RTSP_SUPPORT_SMOKE=PASS",
            "processes_started=false",
            "network_used=false",
        ):
            self.assertIn(marker, output.getvalue())

    def test_nested_relocated_self_test_and_support_loader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "yi-cr4c"
            cleanroom = root / "tools" / "pppp_cleanroom"
            phase = root / "tools" / "phase3_pppp_probe"
            cleanroom.mkdir(parents=True)
            phase.mkdir(parents=True)
            for name in self.cleanroom_files:
                shutil.copy2(CLEANROOM / name, cleanroom / name)
            shutil.copy2(
                APP_DIR / "tools" / "phase3_pppp_probe" / "run_phase3e_tnp.py",
                phase / "run_phase3e_tnp.py",
            )
            script = cleanroom / "probe_rtsp_publish.py"
            isolated = subprocess.run(
                [sys.executable, str(script), "--self-test"],
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(isolated.returncode, 0, isolated.stderr)
            self.assertIn("CR4C_SELF_TEST=PASS", isolated.stdout)
            self.assertIn("runtime_support_imported=false", isolated.stdout)

            support = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--support-smoke-test",
                    "--app-source",
                    str(APP_DIR),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(support.returncode, 0, support.stderr)
            self.assertIn("CR4C_SUPPORT_SMOKE=PASS", support.stdout)
            self.assertIn("network_used=false", support.stdout)


if __name__ == "__main__":
    unittest.main()
