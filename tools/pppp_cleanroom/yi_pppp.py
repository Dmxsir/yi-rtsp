#!/usr/bin/env python3
"""Independent, payload-safe codecs for observed YI F1 PPPP transport.

This module contains no cloud, TNP, media, or proprietary-library code.  Wire
shapes are limited to independently observed CR-1 metadata.  Packet payloads
and endpoint addresses are deliberately hidden from object representations.
"""
from __future__ import annotations

import ipaddress
import struct
from dataclasses import dataclass, field
from enum import Enum, IntEnum, auto
from typing import Iterable

F1_MAGIC = 0xF1
DRW_INNER_MAGIC = 0xD1
SEQUENCE_MODULUS = 1 << 16
_LAN_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class Opcode(IntEnum):
    HELLO = 0x00
    HELLO_ACK = 0x01
    DEV_ONLINE_REQ = 0x18
    DEV_ONLINE_REQ_ACK = 0x19
    P2P_REQ = 0x20
    P2P_REQ_ACK = 0x21
    PUNCH_TO = 0x40
    PUNCH_PKT = 0x41
    P2P_RDY = 0x42
    P2P_RDY_ACK = 0x43
    CONNECT_REPORT = 0xA0
    DRW = 0xD0
    DRW_ACK = 0xD1
    YI_DRW_ACK = 0xD2
    ALIVE = 0xE0
    ALIVE_ACK = 0xE1
    CLOSE = 0xF0


@dataclass(frozen=True, slots=True)
class F1Packet:
    opcode: int
    payload: bytes = field(default=b"", repr=False)

    def __post_init__(self) -> None:
        if not 0 <= int(self.opcode) <= 0xFF:
            raise ValueError("opcode must fit in one byte")
        if not isinstance(self.payload, bytes):
            object.__setattr__(self, "payload", bytes(self.payload))
        if len(self.payload) > 0xFFFF:
            raise ValueError("payload too large for F1 framing")

    def encode(self) -> bytes:
        return struct.pack(">BBH", F1_MAGIC, int(self.opcode), len(self.payload)) + self.payload

    @classmethod
    def parse(cls, data: bytes) -> "F1Packet":
        if len(data) < 4:
            raise ValueError("F1 packet is shorter than its header")
        magic, opcode, length = struct.unpack(">BBH", data[:4])
        if magic != F1_MAGIC:
            raise ValueError("not an F1 packet")
        if length != len(data) - 4:
            raise ValueError("F1 declared length does not match datagram length")
        return cls(opcode, data[4:])


def try_parse_f1(data: bytes) -> F1Packet | None:
    try:
        return F1Packet.parse(data)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True, repr=False)
class DeviceId:
    raw: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.raw, bytes):
            object.__setattr__(self, "raw", bytes(self.raw))
        if len(self.raw) != 20:
            raise ValueError("device ID transport form must be exactly 20 bytes")

    def __repr__(self) -> str:
        return "DeviceId(<redacted>, length=20)"

    @classmethod
    def from_text(cls, value: str) -> "DeviceId":
        parts = value.split("-")
        if len(parts) != 3:
            raise ValueError("unexpected PPPP device ID shape")
        prefix, serial_text, suffix = parts
        if not serial_text.isdecimal():
            raise ValueError("PPPP device ID serial is not decimal")
        try:
            prefix_raw = prefix.encode("ascii")
            suffix_raw = suffix.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("PPPP device ID fields must be ASCII") from exc
        serial = int(serial_text)
        if not prefix_raw or not suffix_raw or len(prefix_raw) > 8 or len(suffix_raw) > 8:
            raise ValueError("PPPP device ID text fields exceed the observed transport shape")
        if not 0 <= serial <= 0xFFFFFFFF:
            raise ValueError("PPPP device ID serial exceeds 32 bits")
        return cls(prefix_raw.ljust(8, b"\0") + struct.pack(">I", serial) + suffix_raw.ljust(8, b"\0"))


@dataclass(frozen=True, slots=True, repr=False)
class Endpoint:
    address: str
    port: int
    extension: bytes = field(default=b"", repr=False)

    def __post_init__(self) -> None:
        try:
            parsed_address = ipaddress.IPv4Address(self.address)
        except ipaddress.AddressValueError as exc:
            raise ValueError("endpoint must be an IPv4 address") from exc
        if parsed_address.is_unspecified or parsed_address.is_multicast or parsed_address == ipaddress.IPv4Address("255.255.255.255"):
            raise ValueError("endpoint must be a usable unicast IPv4 address")
        if not 1 <= self.port <= 0xFFFF:
            raise ValueError("endpoint port is out of range")
        object.__setattr__(self, "address", str(parsed_address))
        if not isinstance(self.extension, bytes):
            object.__setattr__(self, "extension", bytes(self.extension))

    def __repr__(self) -> str:
        return f"Endpoint(<redacted>, lan={self.is_lan}, extension_length={len(self.extension)})"

    @property
    def key(self) -> tuple[str, int]:
        return self.address, self.port

    @property
    def is_lan(self) -> bool:
        address = ipaddress.IPv4Address(self.address)
        return any(address in network for network in _LAN_NETWORKS)

    def encode_tuple(self) -> bytes:
        return b"\x00\x02" + struct.pack("<H", self.port) + ipaddress.IPv4Address(self.address).packed[::-1] + b"\0" * 8

    @classmethod
    def parse_punch_to(cls, payload: bytes) -> "Endpoint":
        if len(payload) not in (16, 40):
            raise ValueError("PUNCH_TO payload must use an observed legacy or extended length")
        if payload[:2] != b"\x00\x02" or payload[8:16] != b"\0" * 8:
            raise ValueError("PUNCH_TO IPv4 tuple is malformed")
        port = struct.unpack("<H", payload[2:4])[0]
        address = str(ipaddress.IPv4Address(payload[4:8][::-1]))
        return cls(address, port, payload[16:])


@dataclass(frozen=True, slots=True, repr=False)
class Ready:
    device_id: DeviceId = field(repr=False)
    extension: bytes = field(default=b"", repr=False)

    @classmethod
    def from_packet(cls, packet: F1Packet) -> "Ready":
        if packet.opcode != Opcode.P2P_RDY or len(packet.payload) not in (20, 40):
            raise ValueError("P2P_RDY must use an observed legacy or extended payload length")
        return cls(DeviceId(packet.payload[:20]), packet.payload[20:])


def hello() -> F1Packet:
    return F1Packet(Opcode.HELLO)


def p2p_request(device_id: DeviceId, endpoint: Endpoint) -> F1Packet:
    return F1Packet(Opcode.P2P_REQ, device_id.raw + endpoint.encode_tuple())


def punch(device_id: DeviceId) -> F1Packet:
    return F1Packet(Opcode.PUNCH_PKT, device_id.raw)


def alive(payload: bytes = b"") -> F1Packet:
    return F1Packet(Opcode.ALIVE, payload)


def alive_ack() -> F1Packet:
    return F1Packet(Opcode.ALIVE_ACK)


def close() -> F1Packet:
    return F1Packet(Opcode.CLOSE)


@dataclass(frozen=True, slots=True)
class DrwPacket:
    channel: int
    sequence: int
    data: bytes = field(default=b"", repr=False)

    def __post_init__(self) -> None:
        if not 0 <= self.channel <= 0xFF:
            raise ValueError("DRW channel must fit in one byte")
        if not 0 <= self.sequence < SEQUENCE_MODULUS:
            raise ValueError("DRW sequence must fit in 16 bits")
        if not isinstance(self.data, bytes):
            object.__setattr__(self, "data", bytes(self.data))

    def to_f1(self) -> F1Packet:
        return F1Packet(
            Opcode.DRW,
            bytes((DRW_INNER_MAGIC, self.channel)) + struct.pack(">H", self.sequence) + self.data,
        )

    @classmethod
    def from_f1(cls, packet: F1Packet) -> "DrwPacket":
        if packet.opcode != Opcode.DRW or len(packet.payload) < 4:
            raise ValueError("not a complete DRW packet")
        if packet.payload[0] != DRW_INNER_MAGIC:
            raise ValueError("unexpected DRW inner marker")
        return cls(packet.payload[1], struct.unpack(">H", packet.payload[2:4])[0], packet.payload[4:])


@dataclass(frozen=True, slots=True)
class DrwAck:
    channel: int
    sequences: tuple[int, ...]

    def __post_init__(self) -> None:
        if not 0 <= self.channel <= 0xFF:
            raise ValueError("DRW ACK channel must fit in one byte")
        values = tuple(self.sequences)
        if len(values) > 0x7FFD or any(not 0 <= value < SEQUENCE_MODULUS for value in values):
            raise ValueError("invalid DRW ACK sequence list")
        object.__setattr__(self, "sequences", values)

    def to_f1(self) -> F1Packet:
        payload = bytes((DRW_INNER_MAGIC, self.channel)) + struct.pack(">H", len(self.sequences))
        payload += b"".join(struct.pack(">H", value) for value in self.sequences)
        return F1Packet(Opcode.DRW_ACK, payload)

    @classmethod
    def from_f1(cls, packet: F1Packet) -> "DrwAck":
        if packet.opcode != Opcode.DRW_ACK or len(packet.payload) < 4:
            raise ValueError("not a complete DRW ACK packet")
        if packet.payload[0] != DRW_INNER_MAGIC:
            raise ValueError("unexpected DRW ACK inner marker")
        count = struct.unpack(">H", packet.payload[2:4])[0]
        if len(packet.payload) != 4 + count * 2:
            raise ValueError("DRW ACK count does not match payload length")
        sequences = tuple(
            struct.unpack(">H", packet.payload[offset : offset + 2])[0]
            for offset in range(4, len(packet.payload), 2)
        )
        return cls(packet.payload[1], sequences)


@dataclass(frozen=True, slots=True)
class YiDrwAck:
    channel: int
    value: int

    def __post_init__(self) -> None:
        if not 0 <= self.channel <= 0xFF or not 0 <= self.value < SEQUENCE_MODULUS:
            raise ValueError("invalid YI D2 ACK fields")

    def to_f1(self) -> F1Packet:
        return F1Packet(Opcode.YI_DRW_ACK, bytes((Opcode.YI_DRW_ACK, self.channel)) + struct.pack(">H", self.value))

    @classmethod
    def from_f1(cls, packet: F1Packet) -> "YiDrwAck":
        if packet.opcode != Opcode.YI_DRW_ACK or len(packet.payload) != 4:
            raise ValueError("YI D2 ACK payload must be four bytes")
        if packet.payload[0] != Opcode.YI_DRW_ACK:
            raise ValueError("unexpected YI D2 ACK inner marker")
        return cls(packet.payload[1], struct.unpack(">H", packet.payload[2:])[0])


class TransportPhase(Enum):
    NEW = auto()
    RENDEZVOUS = auto()
    PUNCHING = auto()
    READY = auto()
    ESTABLISHED = auto()
    CLOSED = auto()


@dataclass(slots=True, repr=False)
class Handshake:
    """Pure CR-2 state tracker; it performs no network or application I/O."""

    device_id: DeviceId
    servers: frozenset[tuple[str, int]]
    phase: TransportPhase = TransportPhase.NEW
    hello_acks: int = 0
    request_acks: int = 0
    candidates: dict[tuple[str, int], Endpoint] = field(default_factory=dict)
    ready_peer: tuple[str, int] | None = None
    alive_seen: bool = False
    alive_ack_seen: bool = False

    def start(self) -> None:
        if self.phase is not TransportPhase.NEW:
            raise RuntimeError("handshake already started")
        self.phase = TransportPhase.RENDEZVOUS

    def observe(self, packet: F1Packet, peer: tuple[str, int]) -> Endpoint | None:
        peer = (peer[0], int(peer[1]))
        if self.phase is TransportPhase.CLOSED:
            return None
        if peer in self.servers and self.phase in (
            TransportPhase.RENDEZVOUS,
            TransportPhase.PUNCHING,
        ):
            if packet.opcode == Opcode.HELLO_ACK:
                self.hello_acks += 1
            elif packet.opcode == Opcode.P2P_REQ_ACK:
                self.request_acks += 1
            elif packet.opcode == Opcode.PUNCH_TO:
                try:
                    candidate = Endpoint.parse_punch_to(packet.payload)
                except ValueError:
                    return None
                self.candidates[candidate.key] = candidate
                self.phase = TransportPhase.PUNCHING
                return candidate
            return None

        if packet.opcode == Opcode.P2P_RDY and peer in self.candidates:
            try:
                ready = Ready.from_packet(packet)
            except ValueError:
                return None
            if ready.device_id == self.device_id:
                self.ready_peer = peer
                self.phase = TransportPhase.READY
            return None

        if peer != self.ready_peer:
            return None
        if packet.opcode == Opcode.ALIVE:
            self.alive_seen = True
            self.phase = TransportPhase.ESTABLISHED
        elif packet.opcode == Opcode.ALIVE_ACK:
            self.alive_ack_seen = True
            self.phase = TransportPhase.ESTABLISHED
        elif packet.opcode == Opcode.CLOSE:
            self.phase = TransportPhase.CLOSED
        return None

    @property
    def keepalive_confirmed(self) -> bool:
        return self.alive_seen or self.alive_ack_seen

    def mark_closed(self) -> None:
        self.phase = TransportPhase.CLOSED


@dataclass(slots=True)
class _Pending:
    packet: DrwPacket
    sent_at: float
    attempts: int = 1


@dataclass(slots=True, repr=False)
class ReliableChannel:
    """Offline-tested ordered DRW byte stream using selective D1 ACKs.

    D2 is intentionally parsed but not applied because its cumulative meaning
    remains inferred rather than proven.
    """

    channel: int
    max_payload: int = 1024
    receive_window: int = 4096
    next_send: int = 0
    next_receive: int = 0
    _queued: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _pending: dict[int, _Pending] = field(default_factory=dict, init=False, repr=False)
    _reorder: dict[int, bytes] = field(default_factory=dict, init=False, repr=False)
    _readable: bytearray = field(default_factory=bytearray, init=False, repr=False)

    def __post_init__(self) -> None:
        if not 0 <= self.channel <= 0xFF:
            raise ValueError("channel must fit in one byte")
        if self.max_payload < 1 or not 1 <= self.receive_window < 0x8000:
            raise ValueError("invalid reliable-channel limits")
        self.next_send %= SEQUENCE_MODULUS
        self.next_receive %= SEQUENCE_MODULUS

    def queue(self, *chunks: bytes) -> None:
        for chunk in chunks:
            self._queued.extend(chunk)

    def emit(self, now: float, max_packets: int | None = None) -> tuple[DrwPacket, ...]:
        packets: list[DrwPacket] = []
        while self._queued and (max_packets is None or len(packets) < max_packets):
            if self.next_send in self._pending:
                raise BufferError("DRW sequence space exhausted by unacknowledged packets")
            size = min(self.max_payload, len(self._queued))
            data = bytes(self._queued[:size])
            del self._queued[:size]
            sequence = self.next_send
            self.next_send = (self.next_send + 1) % SEQUENCE_MODULUS
            packet = DrwPacket(self.channel, sequence, data)
            self._pending[sequence] = _Pending(packet, now)
            packets.append(packet)
        return tuple(packets)

    def acknowledge(self, ack: DrwAck) -> int:
        if ack.channel != self.channel:
            raise ValueError("ACK channel mismatch")
        removed = 0
        for sequence in ack.sequences:
            if self._pending.pop(sequence, None) is not None:
                removed += 1
        return removed

    def retransmit_due(self, now: float, retry_after: float, max_attempts: int) -> tuple[DrwPacket, ...]:
        if retry_after <= 0 or max_attempts < 1:
            raise ValueError("invalid retransmission policy")
        due: list[DrwPacket] = []
        for sequence, pending in tuple(self._pending.items()):
            if now - pending.sent_at < retry_after:
                continue
            if pending.attempts >= max_attempts:
                raise TimeoutError(f"DRW sequence {sequence} exceeded retry limit")
            pending.attempts += 1
            pending.sent_at = now
            due.append(pending.packet)
        return tuple(due)

    def receive(self, packet: DrwPacket) -> int | None:
        if packet.channel != self.channel:
            raise ValueError("DRW channel mismatch")
        distance = (packet.sequence - self.next_receive) % SEQUENCE_MODULUS
        if distance == 0:
            self._readable.extend(packet.data)
            self.next_receive = (self.next_receive + 1) % SEQUENCE_MODULUS
            while self.next_receive in self._reorder:
                self._readable.extend(self._reorder.pop(self.next_receive))
                self.next_receive = (self.next_receive + 1) % SEQUENCE_MODULUS
            return packet.sequence
        if 0 < distance <= self.receive_window:
            self._reorder.setdefault(packet.sequence, packet.data)
            return packet.sequence
        if distance > 0x8000:
            return packet.sequence  # Old duplicate: acknowledge without re-delivery.
        return None  # Too far ahead or exactly ambiguous at half the sequence space.

    def read(self, max_bytes: int) -> bytes:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        size = min(max_bytes, len(self._readable))
        data = bytes(self._readable[:size])
        del self._readable[:size]
        return data

    @property
    def pending_sequences(self) -> tuple[int, ...]:
        return tuple(self._pending)


def selective_ack(channel: int, sequences: Iterable[int]) -> DrwAck:
    return DrwAck(channel, tuple(sequences))
