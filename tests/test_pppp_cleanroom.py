from __future__ import annotations

import io
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
CLEANROOM = ROOT / "tools" / "pppp_cleanroom"
sys.path.insert(0, str(CLEANROOM))

import parse_pcap  # noqa: E402
import probe_channel0_tnp as cr3_probe  # noqa: E402
import probe_legacy_punch as probe  # noqa: E402
import yi_pppp_session as session_module  # noqa: E402
from yi_pppp import (  # noqa: E402
    DeviceId,
    DrwAck,
    DrwPacket,
    Endpoint,
    F1Packet,
    Handshake,
    Opcode,
    ReliableChannel,
    TransportPhase,
    YiDrwAck,
    close,
    p2p_request,
    selective_ack,
    try_parse_f1,
)
from yi_pppp_session import CleanPpppSession, ExperimentalPolicy, TransportError  # noqa: E402


class PacketCodecTest(unittest.TestCase):
    def test_f1_round_trip_and_strict_length(self) -> None:
        packet = F1Packet(Opcode.ALIVE, bytes((1, 2, 3, 4)))
        self.assertEqual(F1Packet.parse(packet.encode()), packet)
        self.assertIsNone(try_parse_f1(packet.encode() + b"\0"))
        self.assertIsNone(try_parse_f1(b"\xF2\xE0\0\0"))
        self.assertNotIn("01020304", repr(packet))

    def test_device_and_endpoint_builders_are_exact_and_redacted(self) -> None:
        device_id = DeviceId(bytes(20))
        endpoint = Endpoint("10.0.0.2", 32100)
        request = p2p_request(device_id, endpoint)
        self.assertEqual(request.opcode, Opcode.P2P_REQ)
        self.assertEqual(len(request.payload), 36)
        self.assertEqual(Endpoint.parse_punch_to(endpoint.encode_tuple()), endpoint)

        extended = Endpoint.parse_punch_to(endpoint.encode_tuple() + bytes(24))
        self.assertEqual(extended.key, endpoint.key)
        self.assertEqual(len(extended.extension), 24)
        self.assertTrue(extended.is_lan)
        self.assertFalse(Endpoint("203.0.113.4", 32100).is_lan)
        with self.assertRaises(ValueError):
            Endpoint("0.0.0.0", 32100)
        self.assertNotIn(endpoint.address, repr(endpoint))
        self.assertIn("redacted", repr(device_id))

    def test_drw_and_variable_ack_codecs(self) -> None:
        data = DrwPacket(3, 0xFFFE, b"sanitized")
        self.assertEqual(DrwPacket.from_f1(F1Packet.parse(data.to_f1().encode())), data)

        ack = DrwAck(3, (0xFFFE, 1, 1, 7))
        self.assertEqual(DrwAck.from_f1(F1Packet.parse(ack.to_f1().encode())), ack)
        broken = bytearray(ack.to_f1().encode())
        broken[7] = 5
        with self.assertRaises(ValueError):
            DrwAck.from_f1(F1Packet.parse(bytes(broken)))

        d2 = YiDrwAck(2, 0x1234)
        self.assertEqual(YiDrwAck.from_f1(F1Packet.parse(d2.to_f1().encode())), d2)

    def test_malformed_drw_ack_and_yi_ack_are_rejected(self) -> None:
        malformed = (
            F1Packet(Opcode.DRW, b""),
            F1Packet(Opcode.DRW, b"\xD2\0\0\0"),
            F1Packet(Opcode.DRW_ACK, b"\xD1\0\0\1"),
            F1Packet(Opcode.YI_DRW_ACK, b"\xD2\0\0"),
        )
        parsers = (DrwPacket.from_f1, DrwPacket.from_f1, DrwAck.from_f1, YiDrwAck.from_f1)
        for parser, packet in zip(parsers, malformed):
            with self.subTest(opcode=packet.opcode), self.assertRaises(ValueError):
                parser(packet)


class HandshakeTest(unittest.TestCase):
    def test_state_machine_requires_trusted_server_candidate_and_keepalive(self) -> None:
        device_id = DeviceId(bytes(20))
        other_id = DeviceId(bytes(19) + b"\x01")
        server = Endpoint("192.0.2.1", 32100)
        untrusted = Endpoint("192.0.2.2", 32100)
        candidate = Endpoint("10.0.0.2", 40000)
        state = Handshake(device_id, frozenset((server.key,)))
        self.assertIsNone(state.observe(F1Packet(Opcode.HELLO_ACK), server.key))
        self.assertEqual(state.hello_acks, 0)
        state.start()

        offer = F1Packet(Opcode.PUNCH_TO, candidate.encode_tuple() + bytes(24))
        self.assertIsNone(state.observe(offer, untrusted.key))
        self.assertEqual(state.candidates, {})
        self.assertEqual(state.observe(offer, server.key), Endpoint(candidate.address, candidate.port, bytes(24)))
        self.assertEqual(state.phase, TransportPhase.PUNCHING)

        state.observe(F1Packet(Opcode.P2P_RDY, device_id.raw), untrusted.key)
        self.assertEqual(state.phase, TransportPhase.PUNCHING)
        state.observe(F1Packet(Opcode.P2P_RDY, other_id.raw), candidate.key)
        self.assertEqual(state.phase, TransportPhase.PUNCHING)
        state.observe(F1Packet(Opcode.P2P_RDY, device_id.raw + bytes(20)), candidate.key)
        self.assertEqual(state.phase, TransportPhase.READY)
        self.assertFalse(state.keepalive_confirmed)

        state.observe(F1Packet(Opcode.ALIVE_ACK), untrusted.key)
        self.assertEqual(state.phase, TransportPhase.READY)
        state.observe(F1Packet(Opcode.ALIVE_ACK), candidate.key)
        self.assertEqual(state.phase, TransportPhase.ESTABLISHED)
        self.assertTrue(state.keepalive_confirmed)
        state.observe(close(), candidate.key)
        self.assertEqual(state.phase, TransportPhase.CLOSED)


class ReliableChannelTest(unittest.TestCase):
    def test_coalescing_selective_ack_and_retransmission(self) -> None:
        channel = ReliableChannel(0, max_payload=256)
        channel.queue(bytes(56), bytes(52), bytes(56))
        packets = channel.emit(now=10.0)
        self.assertEqual(len(packets), 1)
        self.assertEqual(len(packets[0].data), 164)
        self.assertEqual(channel.retransmit_due(10.4, retry_after=0.5, max_attempts=3), ())
        self.assertEqual(channel.retransmit_due(10.5, retry_after=0.5, max_attempts=3), packets)
        self.assertEqual(channel.acknowledge(selective_ack(0, (packets[0].sequence,))), 1)
        self.assertEqual(channel.pending_sequences, ())

        channel.queue(b"first")
        channel.emit(now=20.0)
        channel.next_send = 1
        channel.queue(b"collision")
        with self.assertRaises(BufferError):
            channel.emit(now=21.0)

    def test_receive_orders_stream_across_sequence_wrap_and_deduplicates(self) -> None:
        channel = ReliableChannel(0, next_receive=0xFFFF)
        self.assertEqual(channel.receive(DrwPacket(0, 0, b"second")), 0)
        self.assertEqual(channel.read(99), b"")
        self.assertEqual(channel.receive(DrwPacket(0, 0xFFFF, b"first")), 0xFFFF)
        self.assertEqual(channel.read(99), b"firstsecond")
        self.assertEqual(channel.receive(DrwPacket(0, 0xFFFF, b"duplicate")), 0xFFFF)
        self.assertEqual(channel.read(99), b"")

    def test_retry_limit_and_channel_mismatch(self) -> None:
        channel = ReliableChannel(0)
        channel.queue(b"one")
        packet = channel.emit(now=0.0)
        self.assertEqual(channel.retransmit_due(0.5, retry_after=0.5, max_attempts=2), packet)
        with self.assertRaises(TimeoutError):
            channel.retransmit_due(1.0, retry_after=0.5, max_attempts=2)
        with self.assertRaises(ValueError):
            channel.acknowledge(selective_ack(1, (0,)))
        with self.assertRaises(ValueError):
            channel.receive(DrwPacket(1, 0, b"wrong channel"))


class _FakeSocket:
    def __init__(self, incoming: list[tuple[bytes, tuple[str, int]]] | None = None) -> None:
        self.incoming = incoming or []
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.bound = ("192.0.2.10", 41000)
        self.closed = False

    def __enter__(self) -> "_FakeSocket":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def connect(self, _peer: tuple[str, int]) -> None:
        return None

    def bind(self, peer: tuple[str, int]) -> None:
        self.bound = (peer[0], 41000)

    def setblocking(self, _value: bool) -> None:
        return None

    def getsockname(self) -> tuple[str, int]:
        return self.bound

    def sendto(self, data: bytes, peer: tuple[str, int]) -> int:
        self.sent.append((data, peer))
        return len(data)

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        if not self.incoming:
            raise BlockingIOError
        return self.incoming.pop(0)

    def close(self) -> None:
        self.closed = True


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.01
        return self.now


class TransportSessionTest(unittest.TestCase):
    server = ("192.0.2.1", 32100)
    candidate = ("10.0.0.2", 40000)

    def _connect(self, include_keepalive: bool = True) -> tuple[CleanPpppSession, _FakeSocket]:
        device_id = DeviceId(bytes(20))
        endpoint = Endpoint(*self.candidate)
        incoming = [
            (F1Packet(Opcode.HELLO_ACK).encode(), self.server),
            (F1Packet(Opcode.P2P_REQ_ACK, bytes(4)).encode(), self.server),
            (F1Packet(Opcode.PUNCH_TO, endpoint.encode_tuple() + bytes(24)).encode(), self.server),
            (F1Packet(Opcode.P2P_RDY, device_id.raw).encode(), self.candidate),
        ]
        if include_keepalive:
            incoming.append((F1Packet(Opcode.ALIVE_ACK).encode(), self.candidate))
        route_socket = _FakeSocket()
        transport_socket = _FakeSocket(incoming)
        patches = (
            mock.patch.object(
                session_module.socket, "socket", side_effect=(route_socket, transport_socket)
            ),
            mock.patch.object(
                session_module.select,
                "select",
                side_effect=lambda sockets, _w, _x, _timeout: (
                    (sockets if transport_socket.incoming else []),
                    [],
                    [],
                ),
            ),
            mock.patch.object(session_module.time, "monotonic", side_effect=_Clock()),
            mock.patch.object(session_module.time, "sleep", return_value=None),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        session = CleanPpppSession(
            (self.server,),
            ExperimentalPolicy(
                handshake_timeout=0.5,
                keepalive_timeout=0.2,
                keepalive_interval=0.5,
                request_retry_after=0.1,
                retry_after=0.5,
                max_attempts=2,
            ),
        )
        session.connect(device_id)
        return session, transport_socket

    def test_channel0_starts_only_after_keepalive_confirmed_cr2(self) -> None:
        session, transport_socket = self._connect()
        self.assertTrue(session.established)
        self.assertFalse(
            any(F1Packet.parse(data).opcode == Opcode.DRW for data, _ in transport_socket.sent)
        )
        session.write_channel(0, bytes(56))
        self.assertEqual(session.flush_channel(0), 1)
        self.assertTrue(
            any(F1Packet.parse(data).opcode == Opcode.DRW for data, _ in transport_socket.sent)
        )

    def test_ready_without_keepalive_cannot_enter_drw_mode(self) -> None:
        with self.assertRaises(TransportError) as raised:
            self._connect(include_keepalive=False)
        self.assertEqual(raised.exception.category, "NO_KEEPALIVE")

    def test_d1_ack_d2_observation_and_malformed_safe_ignore(self) -> None:
        session, transport_socket = self._connect()
        session.write_channel(0, b"queued")
        session.flush_channel(0)
        pending = session.channel0.pending_sequences
        session._handle_datagram(YiDrwAck(0, pending[0]).to_f1().encode(), self.candidate)
        self.assertEqual(session.d2_observed, 1)
        self.assertEqual(session.channel0.pending_sequences, pending)

        malformed = (
            b"not-f1",
            F1Packet(Opcode.DRW, b"bad").encode(),
            F1Packet(Opcode.DRW_ACK, b"\xD1\0\0\1").encode(),
            F1Packet(Opcode.YI_DRW_ACK, b"\xD2\0\0").encode(),
        )
        for datagram in malformed:
            session._handle_datagram(datagram, self.candidate)
        self.assertEqual(session.malformed_ignored, len(malformed))

        transport_socket.incoming.append(
            (selective_ack(0, pending).to_f1().encode(), self.candidate)
        )
        session.wait_channel_acked(0, 0.2)
        self.assertEqual(session.channel0.pending_sequences, ())
        self.assertEqual(session.drw_acked, 1)

    def test_nonzero_discard_and_partial_channel0_reads(self) -> None:
        session, transport_socket = self._connect()
        transport_socket.incoming.extend(
            (
                (DrwPacket(2, 9, b"discarded").to_f1().encode(), self.candidate),
                (DrwPacket(0, 0, b"abcdef").to_f1().encode(), self.candidate),
            )
        )
        self.assertEqual(session.read_channel(0, 3, 0.3), b"abc")
        self.assertEqual(session.read_channel(0, 8, 0.3), b"def")
        self.assertEqual(session.nonzero_drw_discarded, 1)
        with self.assertRaises(ValueError):
            session.read_channel(1, 1, 0.1)

    def test_adjacent_startup_writes_coalesce_to_observed_164_bytes(self) -> None:
        session, transport_socket = self._connect()
        session.write_channel(0, bytes(56))
        session.write_channel(0, bytes(52))
        session.write_channel(0, bytes(56))
        self.assertEqual(session.flush_channel(0), 1)
        packets = [
            DrwPacket.from_f1(F1Packet.parse(data))
            for data, _ in transport_socket.sent
            if F1Packet.parse(data).opcode == Opcode.DRW
        ]
        self.assertEqual(len(packets), 1)
        self.assertEqual(len(packets[0].data), 164)

    def test_close_is_idempotent_and_sends_best_effort_close(self) -> None:
        session, transport_socket = self._connect()
        session.close()
        session.close()
        self.assertTrue(session.closed)
        self.assertTrue(session.close_sent)
        self.assertTrue(transport_socket.closed)

    def test_session_reports_bounded_retry_failure(self) -> None:
        session, _transport_socket = self._connect()
        session.policy = ExperimentalPolicy(
            handshake_timeout=0.5,
            keepalive_timeout=0.2,
            keepalive_interval=0.5,
            request_retry_after=0.1,
            retry_after=0.03,
            max_attempts=1,
        )
        session.write_channel(0, b"queued")
        session.flush_channel(0)
        with self.assertRaises(TransportError) as raised:
            session.wait_channel_acked(0, 0.2)
        self.assertEqual(raised.exception.category, "RETRY_LIMIT")


class ToolExecutionTest(unittest.TestCase):
    def _run_self_test(self, script: Path, cwd: Path) -> None:
        result = subprocess.run(
            [sys.executable, str(script), "--self-test"],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CR2_SELF_TEST=PASS", result.stdout)
        self.assertIn("tnp_sent=false", result.stdout)
        self.assertIn("media_requested=false", result.stdout)

    def test_probe_from_checkout_and_relocated_tmp_directory(self) -> None:
        script = CLEANROOM / "probe_legacy_punch.py"
        self._run_self_test(script, ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            shutil.copy2(script, target / script.name)
            shutil.copy2(CLEANROOM / "yi_pppp.py", target / "yi_pppp.py")
            self._run_self_test(target / script.name, target)

    def _run_cr3_self_test(self, script: Path, cwd: Path) -> None:
        result = subprocess.run(
            [sys.executable, str(script), "--self-test"],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CR3_SELF_TEST=PASS", result.stdout)
        for marker in (
            "cloud_used=false",
            "network_used=false",
            "tnp_sent=false",
            "media_requested=false",
            "runtime_support_imported=false",
        ):
            self.assertIn(marker, result.stdout)

    def test_cr3_self_test_from_checkout_and_relocated_directory(self) -> None:
        script = CLEANROOM / "probe_channel0_tnp.py"
        self._run_cr3_self_test(script, ROOT)
        required = (
            script,
            CLEANROOM / "probe_legacy_punch.py",
            CLEANROOM / "yi_pppp.py",
            CLEANROOM / "yi_pppp_session.py",
        )
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            for source in required:
                shutil.copy2(source, target / source.name)
            self._run_cr3_self_test(target / script.name, target)

    def test_phase3e_units_and_first_4882_validator_are_reused(self) -> None:
        phase3e = cr3_probe._phase3e_support()
        units = phase3e._build_units(SimpleNamespace(password="", encrypted=False))
        self.assertEqual(tuple(map(len, units)), (56, 52, 56, 56))
        body = bytearray(40)
        struct.pack_into(">HH", body, 0, 4882, 1)
        struct.pack_into(">I", body, 8, 0)
        header = bytes((2, 3, 0, 0)) + struct.pack(">I", len(body))
        self.assertTrue(phase3e.validate_first_4882(header, bytes(body)))
        body[11] = 1
        self.assertFalse(phase3e.validate_first_4882(header, bytes(body)))
        self.assertFalse(phase3e.validate_first_4882(header[:-1], bytes(body)))

    def test_cr3_runner_offline_transcript_reaches_full_success_boundary(self) -> None:
        body = bytearray(40)
        struct.pack_into(">HH", body, 0, 4882, 1)
        header = bytes((2, 3, 0, 0)) + struct.pack(">I", len(body))

        class FakeMaterial:
            pppp_did = ""

            def clear(self) -> None:
                return None

        class FakePhase3E:
            @staticmethod
            def _build_units(_material: object) -> tuple[bytes, bytes, bytes, bytes]:
                return bytes(56), bytes(52), bytes(56), bytes(56)

            @staticmethod
            def validate_first_4882(candidate_header: bytes, candidate_body: bytes) -> bool:
                return candidate_header == header and candidate_body == bytes(body)

        class FakeSession:
            last: "FakeSession | None" = None

            def __init__(self, *_args: object) -> None:
                self.selected_path = "lan"
                self.closed = False
                self._queued: list[bytes] = []
                self.writes: list[bytes] = []
                self._readable = bytearray(header + body)
                FakeSession.last = self

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

            def read_channel(self, channel: int, max_bytes: int, timeout: float) -> bytes:
                data = bytes(self._readable[:max_bytes])
                del self._readable[:max_bytes]
                return data

            def close(self) -> None:
                self.closed = True

        args = SimpleNamespace(
            env_file=Path("unused"),
            cloud_timeout=1.0,
            camera_id="placeholder",
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
            ack_timeout=1.0,
            read_timeout=1.0,
        )
        output = io.StringIO()
        with (
            mock.patch.object(cr3_probe, "load_material", return_value=(FakeMaterial(), {})),
            mock.patch.object(cr3_probe, "_phase3e_support", return_value=FakePhase3E()),
            mock.patch.object(cr3_probe, "CleanPpppSession", FakeSession),
            mock.patch.object(cr3_probe.DeviceId, "from_text", return_value=DeviceId(bytes(20))),
            redirect_stdout(output),
        ):
            self.assertEqual(cr3_probe._run_live(args), 0)
        self.assertIsNotNone(FakeSession.last)
        self.assertEqual(tuple(map(len, FakeSession.last.writes)), (164, 56))
        for marker in (
            "cr2_transport_established=true",
            "channel0_drw_started=true",
            "startup_tnp_bytes_sent=164",
            "startup_drw_acked=true",
            "first_4882_valid=true",
            "stop_live_767_sent=true",
            "transport_closed=true",
            "cr3_result=PASS",
        ):
            self.assertIn(marker, output.getvalue())

    def test_shallow_tmp_path_does_not_assume_three_parents(self) -> None:
        original = probe.HERE
        try:
            probe.HERE = Path("/tmp/probe_legacy_punch.py")
            self.assertIsInstance(probe._app_source_candidates(), tuple)
        finally:
            probe.HERE = original

    def test_capture_parser_knows_startup_types(self) -> None:
        self.assertEqual(parse_pcap.PPPP_TYPES[Opcode.P2P_REQ], "P2P_REQ")
        self.assertEqual(parse_pcap.PPPP_TYPES[Opcode.P2P_REQ_ACK], "P2P_REQ_ACK")
        self.assertEqual(parse_pcap.PPPP_TYPES[Opcode.CONNECT_REPORT], "CONNECT_REPORT")


if __name__ == "__main__":
    unittest.main()
