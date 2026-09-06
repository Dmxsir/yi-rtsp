from __future__ import annotations

import io
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLEANROOM = ROOT / "tools" / "pppp_cleanroom"
APP_DIR = ROOT / "yi_home" / "rootfs" / "opt" / "yi-home" / "app"
sys.path.insert(0, str(CLEANROOM))
sys.path.insert(0, str(APP_DIR))

import probe_media_tnp as cr4  # noqa: E402
import yi_pppp_session as session_module  # noqa: E402
from tnp_stream import TnpStreamError, TnpUnitReader  # noqa: E402
from yi_pppp import (  # noqa: E402
    DeviceId,
    DrwAck,
    DrwPacket,
    F1Packet,
    Handshake,
    Opcode,
    ReliableChannel,
    TransportPhase,
    YiDrwAck,
    close,
    selective_ack,
)
from yi_pppp_session import CleanPpppSession, ExperimentalPolicy, TransportError  # noqa: E402


class _SinkSocket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def sendto(self, data: bytes, peer: tuple[str, int]) -> int:
        self.sent.append((data, peer))
        return len(data)

    def close(self) -> None:
        self.closed = True


def _ready_session(media_buffer_bytes: int = 1024) -> tuple[CleanPpppSession, _SinkSocket]:
    session = CleanPpppSession(
        (("192.0.2.1", 32100),),
        ExperimentalPolicy(media_buffer_bytes=media_buffer_bytes),
    )
    state = Handshake(DeviceId(bytes(20)), frozenset())
    state.phase = TransportPhase.ESTABLISHED
    state.alive_ack_seen = True
    session.state = state
    session._peer = ("10.0.0.2", 40000)
    sock = _SinkSocket()
    session._socket = sock
    session._next_keepalive = 999.0
    return session, sock


class MediaChannelTest(unittest.TestCase):
    def test_media_requires_opt_in_and_channels_are_isolated(self) -> None:
        session, sock = _ready_session()
        with self.assertRaises(ValueError):
            session.read_channel(1, 1, 0.01)
        session.enable_read_channels()

        peer = session._peer
        assert peer is not None
        session._handle_datagram(DrwPacket(1, 0xFFFF, b"a1").to_f1().encode(), peer)
        session._handle_datagram(DrwPacket(2, 10, b"v2a").to_f1().encode(), peer)
        session._handle_datagram(DrwPacket(2, 12, b"v2c").to_f1().encode(), peer)
        session._handle_datagram(DrwPacket(3, 90, b"v3").to_f1().encode(), peer)
        session._handle_datagram(DrwPacket(2, 11, b"v2b").to_f1().encode(), peer)
        session._handle_datagram(DrwPacket(1, 0, b"a2").to_f1().encode(), peer)
        session._handle_datagram(DrwPacket(3, 90, b"duplicate").to_f1().encode(), peer)

        self.assertEqual(session.read_channel(1, 99, 0.01), b"a1a2")
        self.assertEqual(session.read_channel(2, 99, 0.01), b"v2av2bv2c")
        self.assertEqual(session.read_channel(3, 99, 0.01), b"v3")
        self.assertEqual(session.channel0.read(99), b"")
        channel3_acks = [
            DrwAck.from_f1(F1Packet.parse(data)).sequences
            for data, _peer in sock.sent
            if F1Packet.parse(data).opcode == Opcode.DRW_ACK
            and DrwAck.from_f1(F1Packet.parse(data)).channel == 3
        ]
        self.assertEqual(channel3_acks, [(90,), (90,)])

    def test_selective_ack_and_d2_state_are_independent_per_channel(self) -> None:
        session, _sock = _ready_session()
        session.enable_read_channels()
        media = session._channels[1]
        media.queue(b"client-test")
        packet = media.emit(0.0)[0]
        control_pending = session.channel0.pending_sequences
        peer = session._peer
        assert peer is not None
        session._handle_datagram(YiDrwAck(1, packet.sequence).to_f1().encode(), peer)
        self.assertEqual(media.pending_sequences, (packet.sequence,))
        self.assertEqual(session.channel0.pending_sequences, control_pending)
        session._handle_datagram(
            selective_ack(1, (packet.sequence,)).to_f1().encode(), peer
        )
        self.assertEqual(media.pending_sequences, ())

    def test_media_buffer_is_bounded(self) -> None:
        session, _sock = _ready_session(media_buffer_bytes=4)
        session.enable_read_channels((1,))
        peer = session._peer
        assert peer is not None
        with self.assertRaises(TransportError) as raised:
            session._handle_datagram(DrwPacket(1, 7, b"12345").to_f1().encode(), peer)
        self.assertEqual(raised.exception.category, "MEDIA_BUFFER_LIMIT")
        self.assertEqual(session._channels[1].buffered_bytes, 0)

    def test_remote_close_and_keepalive_remain_active_for_media(self) -> None:
        session, sock = _ready_session()
        session.enable_read_channels((1,))
        peer = session._peer
        assert peer is not None
        with self.assertRaises(TransportError) as raised:
            session._handle_datagram(close().encode(), peer)
        self.assertEqual(raised.exception.category, "REMOTE_CLOSE")

        session, sock = _ready_session()
        session.enable_read_channels((1,))
        session._next_keepalive = 0.0

        class Clock:
            now = 0.0

            def __call__(self) -> float:
                self.now += 0.02
                return self.now

        with (
            mock.patch.object(session_module.time, "monotonic", side_effect=Clock()),
            mock.patch.object(
                session_module.select, "select", return_value=([], [], [])
            ),
        ):
            with self.assertRaises(TransportError) as timeout:
                session.read_channel(1, 1, 0.12)
        self.assertEqual(timeout.exception.category, "MEDIA_CHANNEL_TIMEOUT")
        self.assertTrue(
            any(F1Packet.parse(data).opcode == Opcode.ALIVE for data, _peer in sock.sent)
        )

    def test_reliable_media_start_sequence_wrap_and_overflow_accounting(self) -> None:
        channel = ReliableChannel(
            2,
            max_buffered_bytes=6,
            start_at_first_packet=True,
        )
        self.assertEqual(channel.receive(DrwPacket(2, 0xFFFF, b"aa")), 0xFFFF)
        self.assertEqual(channel.receive(DrwPacket(2, 0, b"bb")), 0)
        self.assertEqual(channel.buffered_bytes, 4)
        self.assertEqual(channel.read(2), b"aa")
        self.assertEqual(channel.buffered_bytes, 2)
        self.assertEqual(channel.receive(DrwPacket(2, 0, b"duplicate")), 0)
        self.assertEqual(channel.read(99), b"bb")


class _ByteSession:
    def __init__(self, streams: dict[int, bytes], chunk_sizes: list[int] | None = None) -> None:
        self.streams = {channel: bytearray(data) for channel, data in streams.items()}
        self.chunk_sizes = list(chunk_sizes or [])

    def read_channel(self, channel: int, max_bytes: int, timeout: float) -> bytes:
        stream = self.streams.setdefault(channel, bytearray())
        if not stream:
            raise TransportError("MEDIA_CHANNEL_TIMEOUT")
        limit = self.chunk_sizes.pop(0) if self.chunk_sizes else max_bytes
        size = min(max_bytes, limit, len(stream))
        data = bytes(stream[:size])
        del stream[:size]
        return data


def _media_unit(channel: int, payload: bytes = b"") -> bytes:
    io_type = 2 if channel == 1 else 1
    body = bytes(24) + payload
    return bytes((2, io_type, 0, 0)) + struct.pack(">I", len(body)) + body


class TnpUnitReaderTest(unittest.TestCase):
    def test_complete_split_and_coalesced_units(self) -> None:
        first = _media_unit(2, b"first")
        second = _media_unit(2, b"second")
        session = _ByteSession({2: first + second}, [2, 3, 3, 4, 5, 99, 99])
        reader = TnpUnitReader(2, 256)
        self.assertEqual(reader.read_one(session, 1.0), first)
        self.assertEqual(reader.read_one(session, 1.0), second)
        self.assertEqual(reader.partial_bytes, 0)

    def test_invalid_lengths_type_and_truncated_unit(self) -> None:
        invalid_lengths = (
            bytes((2, 1, 0, 0)) + struct.pack(">I", 23),
            bytes((2, 1, 0, 0)) + struct.pack(">I", 249),
        )
        for header in invalid_lengths:
            with self.subTest(header=header):
                with self.assertRaises(TnpStreamError) as raised:
                    TnpUnitReader(2, 256).read_one(_ByteSession({2: header}), 1.0)
                self.assertEqual(raised.exception.category, "TNP_MEDIA_LENGTH_INVALID")

        wrong_type = bytes((2, 2, 0, 0)) + struct.pack(">I", 24)
        with self.assertRaises(TnpStreamError) as raised:
            TnpUnitReader(2, 256).read_one(_ByteSession({2: wrong_type}), 1.0)
        self.assertEqual(raised.exception.category, "VIDEO_TNP_INVALID")

        unit = _media_unit(1, b"body")
        reader = TnpUnitReader(1, 256)
        with self.assertRaises(TnpStreamError) as timeout:
            reader.read_one(_ByteSession({1: unit[:-2]}, [3, 5, 99]), 1.0)
        self.assertEqual(timeout.exception.category, "MEDIA_CHANNEL_TIMEOUT")
        self.assertEqual(reader.partial_bytes, len(unit) - 2)

    def test_channel_streams_do_not_cross_contaminate(self) -> None:
        audio = _media_unit(1, b"audio")
        video = _media_unit(3, b"video")
        session = _ByteSession({1: audio, 3: video})
        self.assertEqual(TnpUnitReader(3, 256).read_one(session, 1.0), video)
        self.assertEqual(TnpUnitReader(1, 256).read_one(session, 1.0), audio)


def _video_unit(channel: int, sequence: int) -> bytes:
    frame = bytearray(24)
    frame[0:2] = (78).to_bytes(2, "big")
    frame[2] = 1 if channel == 2 else 0
    frame[6:8] = sequence.to_bytes(2, "big")
    frame[8:10] = (1920).to_bytes(2, "big")
    frame[10:12] = (1080).to_bytes(2, "big")
    nal = b"\x00\x00\x00\x01" + (b"\x65" if channel == 2 else b"\x41")
    body = bytes(frame) + nal
    return bytes((2, 1, 0, 0)) + struct.pack(">I", len(body)) + body


def _audio_unit() -> bytes:
    media = bytearray(24)
    media[0:2] = (138).to_bytes(2, "big")
    adts = bytes((0xFF, 0xF1, 0x60, 0x40, 0x00, 0x1F, 0xFC))
    body = bytes(media) + adts
    return bytes((2, 2, 0, 0)) + struct.pack(">I", len(body)) + body


class ExistingParserIntegrationTest(unittest.TestCase):
    def test_synthetic_h264_aac_and_interleaved_reorder(self) -> None:
        import yi_live_relay
        import yi_native_av_relay

        prefix = chr(120) * 15
        i_frame = yi_live_relay._decode_video_unit(2, _video_unit(2, 10), prefix, False)
        p_frame = yi_live_relay._decode_video_unit(3, _video_unit(3, 11), prefix, False)
        self.assertEqual(i_frame["nal_unit_types"], [5])
        self.assertEqual(p_frame["nal_unit_types"], [1])

        reorder = yi_live_relay.SequenceReorderBuffer()
        self.assertEqual(reorder.push(p_frame, now=0.0), [])
        self.assertEqual(
            [frame["sequence"] for frame in reorder.push(i_frame, now=0.1)],
            [10, 11],
        )

        _timestamp, aac, fmt = yi_native_av_relay.decrypt_audio_unit(
            _audio_unit(), prefix
        )
        self.assertEqual(aac[:2], b"\xff\xf1")
        self.assertEqual(fmt, {"sample_rate": 16000, "channels": 1, "object_type": 2})
        with self.assertRaises(Exception):
            yi_live_relay._decode_video_unit(2, _media_unit(2), prefix, False)
        with self.assertRaises(yi_native_av_relay.AudioUnitValidationError):
            yi_native_av_relay.decrypt_audio_unit(_media_unit(1), prefix)


class RunnerTest(unittest.TestCase):
    def _args(self) -> SimpleNamespace:
        return SimpleNamespace(
            env_file=Path("unused"),
            cloud_timeout=1.0,
            camera_id="placeholder",
            app_source=None,
            servers=[("192.0.2.1", 32100)],
            handshake_timeout=1.0,
            keepalive_timeout=1.0,
            keepalive_interval=1.0,
            request_retry_after=1.0,
            retry_after=1.0,
            max_attempts=1,
            punch_repeat=1,
            max_drw_payload=1024,
            receive_window=32,
            control_buffer_bytes=4096,
            media_buffer_bytes=4096,
            max_record_bytes=256,
            max_video_records=8,
            max_audio_records=8,
            reorder_pending=4,
            reorder_wait=0.1,
            ack_timeout=1.0,
            read_timeout=1.0,
            duration=1.0,
        )

    def _support(
        self, fail_video: bool = False, fail_audio: bool = False
    ) -> SimpleNamespace:
        class Phase:
            @staticmethod
            def _build_units(_material: object) -> tuple[bytes, bytes, bytes, bytes]:
                return bytes(56), bytes(52), bytes(56), bytes(56)

            @staticmethod
            def validate_first_4882(_header: bytes, _body: bytes) -> bool:
                return True

        class Reorder:
            def __init__(self, **_kwargs: object) -> None:
                return None

            def push(self, frame: dict[str, object]) -> list[dict[str, object]]:
                return [frame]

        class Video:
            SequenceReorderBuffer = Reorder

            @staticmethod
            def _decode_video_unit(
                channel: int, _unit: bytes, _password: str, _encrypted: bool
            ) -> dict[str, object]:
                if fail_video:
                    raise ValueError("synthetic parser rejection")
                return {
                    "sequence": 10 if channel == 2 else 11,
                    "frame_type": "I" if channel == 2 else "P",
                    "nal_unit_types": [5] if channel == 2 else [1],
                }

        class Audio:
            @staticmethod
            def decrypt_audio_unit(
                _unit: bytes, _password: str
            ) -> tuple[int, bytes, dict[str, int]]:
                if fail_audio:
                    raise ValueError("synthetic parser rejection")
                return 0, b"", {"sample_rate": 16000, "channels": 1, "object_type": 2}

        return SimpleNamespace(phase3e=Phase(), video=Video(), audio=Audio())

    def _session_type(self) -> type:
        control_body = bytes(40)
        control = bytes((2, 3, 0, 0)) + struct.pack(">I", 40) + control_body
        marker = b"private-synthetic-marker"

        class Session:
            last: "Session | None" = None

            def __init__(self, *_args: object) -> None:
                self.closed = False
                self.enabled = False
                self.writes: list[bytes] = []
                self._queued: list[bytes] = []
                self.streams = {
                    0: bytearray(control),
                    1: bytearray(_media_unit(1, marker)),
                    2: bytearray(_media_unit(2, marker)),
                    3: bytearray(_media_unit(3, marker)),
                }
                Session.last = self

            def connect(self, _device_id: object) -> None:
                return None

            def write_channel(self, channel: int, data: bytes) -> None:
                self._queued.append(data)

            def flush_channel(self, channel: int) -> int:
                self.writes.append(b"".join(self._queued))
                self._queued.clear()
                return 1

            def wait_channel_acked(self, channel: int, timeout: float) -> None:
                return None

            def enable_read_channels(self, channels: tuple[int, ...]) -> None:
                self.enabled = True

            def read_channel(self, channel: int, max_bytes: int, timeout: float) -> bytes:
                stream = self.streams[channel]
                if not stream:
                    raise TransportError("MEDIA_CHANNEL_TIMEOUT")
                data = bytes(stream[:max_bytes])
                del stream[:max_bytes]
                return data

            def close(self) -> None:
                self.closed = True

        return Session

    def _run(
        self, fail_video: bool = False, fail_audio: bool = False
    ) -> tuple[int, str, type]:
        session_type = self._session_type()
        material = SimpleNamespace(
            pppp_did="",
            password=chr(120) * 15,
            encrypted=False,
            clear=lambda: None,
        )
        output = io.StringIO()
        with (
            mock.patch.object(cr4, "load_material", return_value=(material, {})),
            mock.patch.object(
                cr4,
                "_media_support",
                return_value=self._support(fail_video, fail_audio),
            ),
            mock.patch.object(cr4, "CleanPpppSession", session_type),
            mock.patch.object(cr4.DeviceId, "from_text", return_value=DeviceId(bytes(20))),
            redirect_stdout(output),
        ):
            result = cr4._run_live(self._args())
        return result, output.getvalue(), session_type

    def test_success_reaches_parser_stop_and_close_boundary_without_payload_output(self) -> None:
        result, output, session_type = self._run()
        self.assertEqual(result, 0)
        self.assertNotIn("private-synthetic-marker", output)
        for marker in (
            "cr3_control_result=PASS",
            "media_channels_enabled=true",
            "channel2_tnp_units_valid=1",
            "channel3_tnp_units_valid=1",
            "h264_i_frame_valid=true",
            "h264_p_frame_valid=true",
            "h264_frames_reordered=2",
            "channel1_tnp_units_valid=1",
            "aac_valid=true",
            "stop_live_767_sent=true",
            "transport_closed=true",
            "cr4_result=PASS",
        ):
            self.assertIn(marker, output)
        self.assertEqual(tuple(map(len, session_type.last.writes)), (164, 56))
        self.assertTrue(session_type.last.enabled)
        self.assertTrue(session_type.last.closed)

    def test_parser_failure_still_attempts_stop_and_close(self) -> None:
        result, output, session_type = self._run(fail_video=True)
        self.assertEqual(result, 2)
        self.assertIn("failure_category=H264_PARSE_INVALID", output)
        self.assertIn("stop_live_767_sent=true", output)
        self.assertIn("transport_closed=true", output)
        self.assertEqual(tuple(map(len, session_type.last.writes)), (164, 56))
        self.assertTrue(session_type.last.closed)

    def test_audio_parser_failure_cannot_report_pass(self) -> None:
        result, output, session_type = self._run(fail_audio=True)
        self.assertEqual(result, 2)
        self.assertIn("failure_category=AAC_PARSE_INVALID", output)
        self.assertNotIn("cr4_result=PASS", output)
        self.assertIn("stop_live_767_sent=true", output)
        self.assertTrue(session_type.last.closed)

    def test_runner_has_no_vendor_fallback_or_publication_path(self) -> None:
        source = (CLEANROOM / "probe_media_tnp.py").read_text(encoding="utf-8")
        self.assertNotIn("libPPPP", source)
        for forbidden in ("ffmpeg", "ffprobe", "go2rtc", "frigate", "rtsp"):
            self.assertNotIn(forbidden, source.casefold())


class RelocationTest(unittest.TestCase):
    cleanroom_files = (
        "probe_media_tnp.py",
        "tnp_stream.py",
        "probe_channel0_tnp.py",
        "probe_legacy_punch.py",
        "yi_pppp_session.py",
        "yi_pppp.py",
    )

    def _nested_copy(self, root: Path) -> Path:
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
        return cleanroom / "probe_media_tnp.py"

    def test_nested_self_test_and_actual_support_loader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = self._nested_copy(Path(temporary) / "yi-cr4")
            self_test = subprocess.run(
                [sys.executable, str(script), "--self-test"],
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(self_test.returncode, 0, self_test.stderr)
            self.assertIn("CR4_SELF_TEST=PASS", self_test.stdout)
            self.assertIn("runtime_support_imported=false", self_test.stdout)

            smoke = subprocess.run(
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
            self.assertEqual(smoke.returncode, 0, smoke.stderr)
            self.assertIn("CR4_SUPPORT_SMOKE=PASS", smoke.stdout)
            self.assertIn("network_used=false", smoke.stdout)

    def test_checkout_self_test_is_runtime_isolated(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CLEANROOM / "probe_media_tnp.py"), "--self-test"],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CR4_SELF_TEST=PASS", result.stdout)
        for marker in (
            "cloud_used=false",
            "network_used=false",
            "tnp_sent=false",
            "media_requested=false",
            "runtime_support_imported=false",
        ):
            self.assertIn(marker, result.stdout)


if __name__ == "__main__":
    unittest.main()
