from __future__ import annotations

import io
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLEANROOM = ROOT / "tools" / "pppp_cleanroom"
APP_DIR = ROOT / "yi_home" / "rootfs" / "opt" / "yi-home" / "app"
sys.path.insert(0, str(CLEANROOM))
sys.path.insert(0, str(APP_DIR))

import mux_pipe  # noqa: E402
import probe_sustained_mux as cr4b  # noqa: E402
import yi_live_relay  # noqa: E402
import yi_native_av_relay  # noqa: E402
from mux_pipe import MuxError, PipeMuxValidator, parse_ffprobe_metadata  # noqa: E402
from yi_pppp import DeviceId  # noqa: E402
from yi_pppp_session import TransportError  # noqa: E402


def _args(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "env_file": Path("unused"),
        "cloud_timeout": 1.0,
        "camera_id": "placeholder",
        "app_source": None,
        "servers": [("192.0.2.1", 32100)],
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
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
        "ack_timeout": 1.0,
        "control_read_timeout": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeMux:
    def __init__(self, events: list[str] | None = None) -> None:
        self.started = False
        self.finished = False
        self.events = events if events is not None else []
        self.video: list[bytes] = []
        self.audio: list[bytes] = []
        self.offsets: tuple[int, int] | None = None
        self.aborted = False

    def start(self, video_offset_ms: int, audio_offset_ms: int) -> None:
        self.started = True
        self.offsets = (video_offset_ms, audio_offset_ms)
        self.events.append("mux_start")

    def feed_video(self, payload: bytes) -> None:
        self.video.append(payload)

    def feed_audio(self, payload: bytes) -> None:
        self.audio.append(payload)

    def finish(self) -> dict[str, object]:
        self.events.append("mux_finish")
        self.finished = True
        return {
            "video_codec": "h264",
            "video_size": "1920x1080",
            "audio_codec": "aac",
            "audio_sample_rate": 16000,
            "audio_channels": 1,
            "mpegts_bytes": 188,
        }

    def abort(self) -> None:
        self.events.append("mux_abort")
        self.aborted = True


class _Video:
    SequenceReorderBuffer = yi_live_relay.SequenceReorderBuffer

    @staticmethod
    def _decode_video_unit(
        channel: int, unit: bytes, _password: str, _encrypted: bool
    ) -> dict[str, object]:
        if unit == b"bad":
            raise ValueError("synthetic invalid video")
        sequence, timestamp = struct.unpack(">HI", unit)
        return {
            "sequence": sequence,
            "timestamp_ms": timestamp,
            "frame_type": "I" if channel == 2 else "P",
            "nal_unit_types": [5] if channel == 2 else [1],
            "width": 1920,
            "height": 1080,
            "output_payload": b"I" if channel == 2 else b"P",
        }


class _Audio:
    @staticmethod
    def decrypt_audio_unit(unit: bytes, _password: str):
        if unit == b"bad":
            raise ValueError("synthetic invalid audio")
        return int.from_bytes(unit, "big"), b"A", {
            "sample_rate": 16000,
            "channels": 1,
            "object_type": 2,
        }

    @staticmethod
    def signed_delta32(current: int, base: int) -> int:
        value = (current - base) & 0xFFFFFFFF
        return value - 0x100000000 if value & 0x80000000 else value

    @staticmethod
    def _setts(kind: str, start: int) -> str:
        return f"{kind}:{start}"


def _support() -> SimpleNamespace:
    return SimpleNamespace(video=_Video, audio=_Audio)


def _video(sequence: int, timestamp: int) -> bytes:
    return struct.pack(">HI", sequence, timestamp)


class SustainedStateTest(unittest.TestCase):
    def test_counts_cannot_fake_minimum_activity_span(self) -> None:
        progress = cr4b.SustainedProgress(30.0, 2, 2, 2)
        for kind in ("I", "P", "audio"):
            progress.observe(kind, 1.0)
            progress.observe(kind, 1.0)
        progress.reordered_frames = 6
        self.assertFalse(progress.passed)
        for kind in ("I", "P", "audio"):
            progress.observe(kind, 31.0)
        self.assertTrue(progress.passed)
        self.assertEqual(progress.active_seconds, 30.0)

    def test_parser_reorder_prerequisites_and_existing_av_offset(self) -> None:
        mux = _FakeMux()
        collector = cr4b.SustainedCollector(
            _support(),
            SimpleNamespace(password="x" * 15, encrypted=False),
            _args(),
            mux,
        )
        collector.accept(3, _video(11, 1010), 0.0)
        self.assertFalse(mux.started)
        self.assertEqual(mux.video, [])
        collector.accept(1, (1020).to_bytes(4, "big"), 0.0)
        self.assertFalse(mux.started)
        collector.accept(2, _video(10, 1000), 0.0)
        self.assertTrue(mux.started)
        self.assertEqual(mux.video, [b"I", b"P"])
        self.assertEqual(mux.audio, [b"A"])
        self.assertEqual(collector.initial_av_delta_ms, 20)
        self.assertEqual(mux.offsets, (0, 20))
        self.assertEqual(
            _Audio.signed_delta32(collector.first_audio_ts, collector.first_video_ts),
            yi_native_av_relay.signed_delta32(
                collector.first_audio_ts, collector.first_video_ts
            ),
        )

        collector.accept(3, _video(12, 1060), 31.0)
        collector.accept(2, _video(13, 1070), 31.0)
        collector.accept(1, (1080).to_bytes(4, "big"), 31.0)
        self.assertTrue(collector.progress.passed)

    def test_invalid_media_and_pre_mux_bounds_fail_stage_specifically(self) -> None:
        collector = cr4b.SustainedCollector(
            _support(),
            SimpleNamespace(password="x" * 15, encrypted=False),
            _args(pre_mux_audio_frames=1),
            _FakeMux(),
        )
        with self.assertRaises(cr4b.ProbeError) as video:
            collector.accept(2, b"bad", 0.0)
        self.assertEqual(video.exception.category, "VIDEO_PARSE_INVALID")
        with self.assertRaises(cr4b.ProbeError) as audio:
            collector.accept(1, b"bad", 0.0)
        self.assertEqual(audio.exception.category, "AUDIO_PARSE_INVALID")

        collector.accept(1, (1).to_bytes(4, "big"), 0.0)
        with self.assertRaises(cr4b.ProbeError) as bounded:
            collector.accept(1, (2).to_bytes(4, "big"), 1.0)
        self.assertEqual(bounded.exception.category, "MEDIA_BUFFER_LIMIT")

    def test_starvation_and_transport_failures_keep_categories(self) -> None:
        class Clock:
            now = 0.0

            def __call__(self) -> float:
                self.now += 0.5
                return self.now

        class Starved:
            def read_channel(self, *_args: object) -> bytes:
                raise TransportError("MEDIA_CHANNEL_TIMEOUT")

        with self.assertRaises(cr4b.ProbeError) as starved:
            cr4b._collect_sustained(
                Starved(),
                _support(),
                SimpleNamespace(password="x" * 15, encrypted=False),
                _args(duration=10.0, media_start_timeout=2.0),
                _FakeMux(),
                Clock(),
            )
        self.assertEqual(starved.exception.category, "MEDIA_START_TIMEOUT")

        for category in ("REMOTE_CLOSE", "MEDIA_RETRY_LIMIT", "MEDIA_BUFFER_LIMIT"):
            class Failed:
                def read_channel(self, *_args: object) -> bytes:
                    raise TransportError(category)

            with self.subTest(category=category):
                with self.assertRaises(TransportError) as failed:
                    cr4b._collect_sustained(
                        Failed(),
                        _support(),
                        SimpleNamespace(password="x" * 15, encrypted=False),
                        _args(),
                        _FakeMux(),
                    )
                self.assertEqual(failed.exception.category, category)


def _metadata() -> bytes:
    return json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "16000",
                    "channels": 1,
                },
            ]
        }
    ).encode()


class _RecordingInput(io.BytesIO):
    def __init__(self, broken: bool = False) -> None:
        super().__init__()
        self.broken = broken
        self.snapshot = b""

    def write(self, data: bytes) -> int:
        if self.broken:
            raise BrokenPipeError
        return super().write(data)

    def close(self) -> None:
        if not self.closed:
            self.snapshot = self.getvalue()
        super().close()


class _FakeProcess:
    def __init__(
        self,
        *,
        stdin: _RecordingInput | None = None,
        stdout: io.BytesIO | None = None,
        output: bytes = b"",
        returncode: int = 0,
        timeout: bool = False,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.output = output
        self.final_rc = returncode
        self.returncode: int | None = None
        self.timeout = timeout
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("fake", timeout)
        self.returncode = -9 if self.killed else self.final_rc
        return self.returncode

    def kill(self) -> None:
        self.killed = True

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("fake", timeout)
        if self.stdout is not None and self.stdout.closed:
            raise AssertionError("stdout closed before communicate")
        self.returncode = -9 if self.killed else self.final_rc
        return self.output, b""


class _PopenFactory:
    def __init__(
        self,
        ts: bytes,
        *,
        broken_probe: bool = False,
        mux_rc: int = 0,
        mux_timeout: bool = False,
        probe_timeout: bool = False,
    ) -> None:
        self.probe_input = _RecordingInput(broken_probe)
        self.probe = _FakeProcess(
            stdin=self.probe_input,
            stdout=io.BytesIO(),
            output=_metadata(),
            timeout=probe_timeout,
        )
        self.mux = _FakeProcess(
            stdout=io.BytesIO(ts),
            returncode=mux_rc,
            timeout=mux_timeout,
        )

    def __call__(self, command: list[str], **_kwargs: object) -> _FakeProcess:
        return self.probe if "ffprobe" in command[0] else self.mux


class MuxPipeTest(unittest.TestCase):
    def test_metadata_and_command_match_existing_formats(self) -> None:
        self.assertEqual(parse_ffprobe_metadata(_metadata())["video_size"], "1920x1080")
        with self.assertRaises(MuxError):
            parse_ffprobe_metadata(b'{"streams": []}')
        wrong = json.loads(_metadata())
        wrong["streams"][1]["sample_rate"] = "8000"
        with self.assertRaises(MuxError):
            parse_ffprobe_metadata(json.dumps(wrong).encode())
        command = mux_pipe.ffmpeg_command(
            "ffmpeg", 10, 11, 4, 5, lambda kind, value: f"{kind}:{value}"
        )
        self.assertIn("video:4", command)
        self.assertIn("audio:5", command)
        self.assertEqual(command[-2:], ["mpegts", "pipe:1"])

    def _validator(self, factory: _PopenFactory) -> PipeMuxValidator:
        validator = PipeMuxValidator(
            "ffmpeg", "ffprobe", lambda kind, value: f"{kind}:{value}", 0.2, 188
        )
        patcher = mock.patch.object(mux_pipe.subprocess, "Popen", side_effect=factory)
        self.addCleanup(patcher.stop)
        patcher.start()
        validator.start(0, 0)
        return validator

    def test_mux_output_is_drained_to_ffprobe_without_persistence(self) -> None:
        ts = (b"\x47" + bytes(187)) * 2
        factory = _PopenFactory(ts)
        validator = self._validator(factory)
        result = validator.finish()
        self.assertEqual(result["mpegts_bytes"], len(ts))
        self.assertEqual(factory.probe_input.snapshot, ts)
        self.assertFalse(validator.pump_alive)
        self.assertTrue(validator._video.closed)
        self.assertTrue(validator._audio.closed)

    def test_successful_probe_epipe_is_normalized_while_output_is_drained(self) -> None:
        factory = _PopenFactory((b"\x47" + bytes(187)) * 2, broken_probe=True)
        validator = self._validator(factory)
        result = validator.finish()
        self.assertTrue(result["ffprobe_epipe_normalized"])
        self.assertFalse(validator.pump_alive)

    def test_feed_backpressure_is_bounded(self) -> None:
        factory = _PopenFactory((b"\x47" + bytes(187)) * 2)
        validator = self._validator(factory)
        with mock.patch.object(mux_pipe.os, "write", side_effect=BlockingIOError):
            with self.assertRaises(MuxError) as raised:
                validator.feed_video(b"synthetic")
        self.assertEqual(raised.exception.category, "MUX_PIPE_BACKPRESSURE")
        validator.abort()
        self.assertFalse(validator.pump_alive)

    def test_invalid_ts_early_exit_and_timeout_are_failures_with_cleanup(self) -> None:
        cases = (
            (_PopenFactory(bytes(188)), "MPEGTS_STREAM_INVALID"),
            (_PopenFactory((b"\x47" + bytes(187)), mux_rc=1), "MUX_EARLY_EXIT"),
            (_PopenFactory((b"\x47" + bytes(187)), mux_timeout=True), "MUX_PIPE_BACKPRESSURE"),
            (_PopenFactory((b"\x47" + bytes(187)), probe_timeout=True), "FFPROBE_TIMEOUT"),
        )
        for factory, category in cases:
            with self.subTest(category=category):
                validator = self._validator(factory)
                with self.assertRaises(MuxError) as raised:
                    validator.finish()
                self.assertEqual(raised.exception.category, category)
                validator.abort()
                self.assertFalse(validator.pump_alive)
                self.assertIsNotNone(factory.mux.poll())
                self.assertIsNotNone(factory.probe.poll())


class RunnerTest(unittest.TestCase):
    def _support(self) -> SimpleNamespace:
        class Phase:
            @staticmethod
            def _build_units(_material: object) -> tuple[bytes, bytes, bytes, bytes]:
                return bytes(56), bytes(52), bytes(56), bytes(56)

            @staticmethod
            def validate_first_4882(_header: bytes, _body: bytes) -> bool:
                return True

        support = _support()
        support.phase3e = Phase()
        return support

    def _session(self, events: list[str]) -> type:
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
                return None

            def write_channel(self, _channel: int, data: bytes) -> None:
                self.queued.append(data)

            def flush_channel(self, _channel: int) -> int:
                events.append("stop" if len(b"".join(self.queued)) == 56 else "startup")
                self.queued.clear()
                return 1

            def wait_channel_acked(self, *_args: object) -> None:
                return None

            def read_channel(self, channel: int, length: int, _timeout: float) -> bytes:
                if channel != 0:
                    raise AssertionError("collector is mocked")
                data = bytes(self.stream[:length])
                del self.stream[:length]
                return data

            def enable_read_channels(self, _channels: tuple[int, ...]) -> None:
                return None

            def close(self) -> None:
                events.append("close")
                self.closed = True

        return Session

    def _run(self, fail: bool = False) -> tuple[int, str, list[str], _FakeMux]:
        events: list[str] = []
        session_type = self._session(events)
        mux = _FakeMux(events)
        material = SimpleNamespace(
            pppp_did="",
            password="x" * 15,
            encrypted=False,
            clear=lambda: None,
        )
        progress = cr4b.SustainedProgress(30.0, 2, 2, 2)
        for kind in ("I", "P", "audio"):
            progress.observe(kind, 0.0)
            progress.observe(kind, 30.0)
        progress.reordered_frames = 4
        collector = SimpleNamespace(
            progress=progress,
            channel_counts={1: 2, 2: 2, 3: 2},
            initial_av_delta_ms=5,
        )

        def collect(*_args: object) -> object:
            mux.start(0, 5)
            if fail:
                raise cr4b.ProbeError("MEDIA_SUSTAIN_TIMEOUT")
            return collector

        output = io.StringIO()
        with (
            mock.patch.object(cr4b, "load_material", return_value=(material, {})),
            mock.patch.object(cr4b, "_media_support", return_value=self._support()),
            mock.patch.object(cr4b, "CleanPpppSession", session_type),
            mock.patch.object(cr4b, "PipeMuxValidator", return_value=mux),
            mock.patch.object(cr4b, "_collect_sustained", side_effect=collect),
            mock.patch.object(cr4b.DeviceId, "from_text", return_value=DeviceId(bytes(20))),
            mock.patch("sys.stdout", output),
        ):
            result = cr4b._run_live(_args())
        return result, output.getvalue(), events, mux

    def test_success_transcript_stops_then_finishes_mux_then_closes(self) -> None:
        result, output, events, mux = self._run()
        self.assertEqual(result, 0)
        for marker in (
            "cr3_control_result=PASS",
            "media_channels_enabled=true",
            "media_active_seconds=30.000",
            "sustained_media_result=PASS",
            "mux_validation_result=PASS",
            "stop_live_767_sent=true",
            "transport_closed=true",
            "cr4b_result=PASS",
        ):
            self.assertIn(marker, output)
        self.assertLess(events.index("stop"), events.index("mux_finish"))
        self.assertLess(events.index("mux_finish"), events.index("close"))
        self.assertTrue(mux.aborted)

    def test_failure_after_start_still_stops_aborts_and_closes(self) -> None:
        result, output, events, mux = self._run(fail=True)
        self.assertEqual(result, 2)
        self.assertIn("failure_category=MEDIA_SUSTAIN_TIMEOUT", output)
        self.assertIn("stop_live_767_sent=true", output)
        self.assertIn("close", events)
        self.assertTrue(mux.aborted)

    def test_runner_has_no_vendor_fallback_or_media_persistence(self) -> None:
        source = (CLEANROOM / "probe_sustained_mux.py").read_text(encoding="utf-8")
        self.assertNotIn("libPPPP", source)
        for suffix in (".h264", ".aac", ".ts", ".mp4", ".pcap"):
            self.assertNotIn(suffix, source.casefold())
        self.assertNotIn("go2rtc", source.casefold())
        self.assertNotIn("frigate", source.casefold())


class RelocationTest(unittest.TestCase):
    cleanroom_files = (
        "mux_pipe.py",
        "probe_sustained_mux.py",
        "probe_media_tnp.py",
        "tnp_stream.py",
        "probe_channel0_tnp.py",
        "probe_legacy_punch.py",
        "yi_pppp_session.py",
        "yi_pppp.py",
    )

    def test_nested_self_test_and_support_loader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "yi-cr4b"
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
            script = cleanroom / "probe_sustained_mux.py"
            isolated = subprocess.run(
                [sys.executable, str(script), "--self-test"],
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(isolated.returncode, 0, isolated.stderr)
            for marker in (
                "CR4B_SELF_TEST=PASS",
                "network_used=false",
                "ffmpeg_started=false",
                "ffprobe_started=false",
                "runtime_support_imported=false",
            ):
                self.assertIn(marker, isolated.stdout)

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
            self.assertIn("CR4B_SUPPORT_SMOKE=PASS", support.stdout)
            self.assertIn("device_traffic=false", support.stdout)

    def test_checkout_self_test_is_strictly_isolated(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(CLEANROOM / "probe_sustained_mux.py"),
                "--self-test",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for marker in (
            "cloud_used=false",
            "network_used=false",
            "tnp_sent=false",
            "media_requested=false",
            "ffmpeg_started=false",
            "ffprobe_started=false",
            "runtime_support_imported=false",
        ):
            self.assertIn(marker, result.stdout)

    def test_mux_smoke_checks_binaries_without_starting_processes(self) -> None:
        with (
            mock.patch.object(cr4b, "_media_support", return_value=_support()),
            mock.patch.object(cr4b.shutil, "which", return_value="/synthetic/tool"),
            mock.patch.object(mux_pipe.subprocess, "Popen") as popen,
        ):
            output = io.StringIO()
            with mock.patch("sys.stdout", output):
                result = cr4b.mux_support_smoke_test("ffmpeg", "ffprobe", None)
        self.assertEqual(result, 0)
        self.assertIn("CR4B_MUX_SUPPORT_SMOKE=PASS", output.getvalue())
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
