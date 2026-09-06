from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLEANROOM = ROOT / "tools" / "pppp_cleanroom"
sys.path.insert(0, str(CLEANROOM))

import parse_pcap  # noqa: E402
import probe_legacy_punch as probe  # noqa: E402
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
