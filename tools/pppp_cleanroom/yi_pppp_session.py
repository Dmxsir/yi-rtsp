#!/usr/bin/env python3
"""Research-only clean PPPP session for reliable channel experiments."""
from __future__ import annotations

import select
import socket
import time
from dataclasses import dataclass

try:
    from .yi_pppp import (
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
        alive,
        alive_ack,
        close,
        hello,
        p2p_request,
        punch,
        selective_ack,
        try_parse_f1,
    )
except ImportError:  # Direct execution from a relocated clean-room directory.
    from yi_pppp import (  # type: ignore[no-redef]
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
        alive,
        alive_ack,
        close,
        hello,
        p2p_request,
        punch,
        selective_ack,
        try_parse_f1,
    )

ALIVE_PAYLOAD_CAPTURE02 = bytes.fromhex("a2050400")


class TransportError(RuntimeError):
    """A secret-safe, stage-specific clean transport failure."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True)
class ExperimentalPolicy:
    """Unproven transport timing/window defaults; every value is configurable."""

    handshake_timeout: float = 4.0
    keepalive_timeout: float = 0.8
    keepalive_interval: float = 0.5
    request_retry_after: float = 0.7
    retry_after: float = 0.25
    max_attempts: int = 4
    punch_repeat: int = 3
    max_payload: int = 1024
    receive_window: int = 4096
    control_buffer_bytes: int | None = None
    media_buffer_bytes: int = 2 * 1024 * 1024 + 32

    def __post_init__(self) -> None:
        positive = (
            self.handshake_timeout,
            self.keepalive_timeout,
            self.keepalive_interval,
            self.request_retry_after,
            self.retry_after,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("experimental timeouts must be positive")
        if (
            not 1 <= self.punch_repeat <= 6
            or self.max_attempts < 1
            or (self.control_buffer_bytes is not None and self.control_buffer_bytes < 1)
            or self.media_buffer_bytes < 1
        ):
            raise ValueError("invalid experimental retry policy")


class CleanPpppSession:
    """Minimal UDP PPPP session with opt-in reliable channel byte streams."""

    def __init__(
        self,
        servers: tuple[tuple[str, int], ...],
        policy: ExperimentalPolicy | None = None,
    ) -> None:
        if not servers:
            raise ValueError("at least one rendezvous server is required")
        self.servers = tuple((host, int(port)) for host, port in servers)
        self.policy = policy or ExperimentalPolicy()
        self.channel0 = ReliableChannel(
            0,
            max_payload=self.policy.max_payload,
            receive_window=self.policy.receive_window,
            max_buffered_bytes=self.policy.control_buffer_bytes,
        )
        self._channels = {0: self.channel0}
        self.state: Handshake | None = None
        self._socket: socket.socket | None = None
        self._peer: tuple[str, int] | None = None
        self._next_keepalive = 0.0
        self.closed = False
        self.close_sent = False
        self.d2_observed = 0
        self.malformed_ignored = 0
        self.nonzero_drw_discarded = 0
        self.drw_sent = 0
        self.drw_retried = 0
        self.drw_acked = 0

    def enable_read_channels(self, channels: tuple[int, ...] = (1, 2, 3)) -> None:
        """Opt in to bounded media buffering after CR-3 authentication."""
        if not self.established:
            raise TransportError("SESSION_NOT_ESTABLISHED")
        for channel in channels:
            if channel not in (1, 2, 3):
                raise ValueError("research media channels must be 1, 2, or 3")
            self._channels.setdefault(
                channel,
                ReliableChannel(
                    channel,
                    max_payload=self.policy.max_payload,
                    receive_window=self.policy.receive_window,
                    max_buffered_bytes=self.policy.media_buffer_bytes,
                    start_at_first_packet=True,
                ),
            )

    @staticmethod
    def _route_local_ip(server: tuple[str, int]) -> str:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(server)
            return str(probe.getsockname()[0])

    @property
    def established(self) -> bool:
        return bool(
            self.state
            and self.state.phase is TransportPhase.ESTABLISHED
            and self.state.keepalive_confirmed
        )

    @property
    def selected_path(self) -> str | None:
        if self.state is None or self._peer is None:
            return None
        candidate = self.state.candidates.get(self._peer)
        return None if candidate is None else ("lan" if candidate.is_lan else "wan")

    def _send(self, packet: F1Packet, peer: tuple[str, int]) -> None:
        if self._socket is None:
            raise TransportError("SESSION_NOT_CONNECTED")
        try:
            self._socket.sendto(packet.encode(), peer)
        except OSError as exc:
            raise TransportError("UDP_IO") from exc

    def _receive(self, timeout: float) -> tuple[bytes, tuple[str, int]] | None:
        if self._socket is None:
            raise TransportError("SESSION_NOT_CONNECTED")
        try:
            readable, _, _ = select.select([self._socket], [], [], max(0.0, timeout))
            if not readable:
                return None
            data, peer = self._socket.recvfrom(65535)
        except BlockingIOError:
            return None
        except OSError as exc:
            raise TransportError("UDP_IO") from exc
        return data, (peer[0], int(peer[1]))

    def connect(self, device_id: DeviceId) -> None:
        """Run the proven CR-2 path and require peer keepalive confirmation."""
        if self.state is not None or self.closed:
            raise RuntimeError("session connect may only be attempted once")
        local_ip = self._route_local_ip(self.servers[0])
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((local_ip, 0))
            sock.setblocking(False)
            self._socket = sock
            local_port = int(sock.getsockname()[1])
            self.state = Handshake(device_id, frozenset(self.servers))
            self.state.start()

            request = p2p_request(device_id, Endpoint(local_ip, local_port))
            for server in self.servers:
                self._send(hello(), server)
                self._send(request, server)

            punched: set[tuple[str, int]] = set()
            deadline = time.monotonic() + self.policy.handshake_timeout
            retry_at = time.monotonic() + self.policy.request_retry_after
            request_retried = False
            while time.monotonic() < deadline and self._peer is None:
                received = self._receive(min(0.1, max(0.0, deadline - time.monotonic())))
                if received is not None:
                    data, peer = received
                    packet = try_parse_f1(data)
                    if packet is not None:
                        candidate = self.state.observe(packet, peer)
                        if candidate is not None and candidate.key not in punched:
                            for _ in range(self.policy.punch_repeat):
                                self._send(punch(device_id), candidate.key)
                                time.sleep(0.01)
                            punched.add(candidate.key)
                        if self.state.phase is TransportPhase.READY:
                            self._peer = self.state.ready_peer
                if not request_retried and time.monotonic() >= retry_at and self._peer is None:
                    for server in self.servers:
                        self._send(request, server)
                    request_retried = True

            if self._peer is None:
                raise TransportError("NO_P2P_RDY")

            for _ in range(3):
                self._send(alive(ALIVE_PAYLOAD_CAPTURE02), self._peer)
            keepalive_deadline = time.monotonic() + self.policy.keepalive_timeout
            while time.monotonic() < keepalive_deadline and not self.established:
                received = self._receive(
                    min(0.08, max(0.0, keepalive_deadline - time.monotonic()))
                )
                if received is not None:
                    self._handle_datagram(*received)
            if not self.established:
                raise TransportError("NO_KEEPALIVE")
            self._next_keepalive = time.monotonic() + self.policy.keepalive_interval
        except Exception:
            if self._socket is sock:
                sock.close()
                self._socket = None
            if self.state is not None:
                self.state.mark_closed()
            raise

    def _require_channel0(self, channel: int) -> None:
        if channel != 0:
            raise ValueError("the CR-3 session exposes only channel 0")
        if not self.established:
            raise TransportError("SESSION_NOT_ESTABLISHED")

    def _read_channel(self, channel: int) -> ReliableChannel:
        if not self.established:
            raise TransportError("SESSION_NOT_ESTABLISHED")
        try:
            return self._channels[channel]
        except KeyError as exc:
            raise ValueError("channel is not enabled for reading") from exc

    def write_channel(self, channel: int, data: bytes) -> None:
        self._require_channel0(channel)
        self.channel0.queue(data)

    def flush_channel(self, channel: int) -> int:
        self._require_channel0(channel)
        now = time.monotonic()
        packets = self.channel0.emit(now)
        for packet in packets:
            self._send(packet.to_f1(), self._peer)  # type: ignore[arg-type]
        self.drw_sent += len(packets)
        return len(packets)

    def _handle_datagram(self, data: bytes, peer: tuple[str, int]) -> None:
        if self.state is None or peer != self._peer:
            return
        packet = try_parse_f1(data)
        if packet is None:
            self.malformed_ignored += 1
            return
        if packet.opcode in (Opcode.ALIVE, Opcode.ALIVE_ACK, Opcode.CLOSE):
            self.state.observe(packet, peer)
            if packet.opcode == Opcode.ALIVE:
                self._send(alive_ack(), peer)
            if packet.opcode == Opcode.CLOSE:
                raise TransportError("REMOTE_CLOSE")
            return
        if packet.opcode == Opcode.DRW:
            try:
                drw = DrwPacket.from_f1(packet)
            except ValueError:
                self.malformed_ignored += 1
                return
            target = self._channels.get(drw.channel)
            if target is not None:
                try:
                    sequence = target.receive(drw)
                except BufferError as exc:
                    category = "CONTROL_BUFFER_LIMIT" if drw.channel == 0 else "MEDIA_BUFFER_LIMIT"
                    raise TransportError(category) from exc
                if sequence is not None:
                    self._send(selective_ack(drw.channel, (sequence,)).to_f1(), peer)
            else:
                self.nonzero_drw_discarded += 1
                self._send(selective_ack(drw.channel, (drw.sequence,)).to_f1(), peer)
            return
        if packet.opcode == Opcode.DRW_ACK:
            try:
                ack = DrwAck.from_f1(packet)
            except ValueError:
                self.malformed_ignored += 1
                return
            target = self._channels.get(ack.channel)
            if target is not None:
                self.drw_acked += target.acknowledge(ack)
            return
        if packet.opcode == Opcode.YI_DRW_ACK:
            try:
                YiDrwAck.from_f1(packet)
            except ValueError:
                self.malformed_ignored += 1
                return
            self.d2_observed += 1

    def _service(self, timeout: float) -> None:
        if not self.established:
            raise TransportError("SESSION_NOT_ESTABLISHED")
        now = time.monotonic()
        for channel, target in self._channels.items():
            if not target.pending_sequences:
                continue
            try:
                retries = target.retransmit_due(
                    now, self.policy.retry_after, self.policy.max_attempts
                )
            except TimeoutError as exc:
                category = "RETRY_LIMIT" if channel == 0 else "MEDIA_RETRY_LIMIT"
                raise TransportError(category) from exc
            for packet in retries:
                self._send(packet.to_f1(), self._peer)  # type: ignore[arg-type]
            self.drw_retried += len(retries)
        if now >= self._next_keepalive:
            self._send(alive(ALIVE_PAYLOAD_CAPTURE02), self._peer)  # type: ignore[arg-type]
            self._next_keepalive = now + self.policy.keepalive_interval
        received = self._receive(timeout)
        if received is not None:
            self._handle_datagram(*received)

    def wait_channel_acked(self, channel: int, timeout: float) -> None:
        self._require_channel0(channel)
        deadline = time.monotonic() + timeout
        while self.channel0.pending_sequences and time.monotonic() < deadline:
            self._service(min(0.05, max(0.0, deadline - time.monotonic())))
        if self.channel0.pending_sequences:
            raise TransportError("NO_DRW_ACK")

    def read_channel(self, channel: int, max_bytes: int, timeout: float) -> bytes:
        target = self._read_channel(channel)
        data = target.read(max_bytes)
        if data:
            return data
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._service(min(0.05, max(0.0, deadline - time.monotonic())))
            data = target.read(max_bytes)
            if data:
                return data
        category = "CHANNEL0_READ_TIMEOUT" if channel == 0 else "MEDIA_CHANNEL_TIMEOUT"
        raise TransportError(category)

    def close(self) -> None:
        if self.closed:
            return
        if self._socket is not None:
            if self._peer is not None:
                try:
                    self._send(close(), self._peer)
                    self.close_sent = True
                except TransportError:
                    pass
            self._socket.close()
            self._socket = None
        if self.state is not None:
            self.state.mark_closed()
        self.closed = True
