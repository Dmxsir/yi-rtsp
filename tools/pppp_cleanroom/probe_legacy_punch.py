#!/usr/bin/env python3
"""CR-2A: secret-safe YI PPPP legacy-punch compatibility probe.

This is a transport-only research tool. It fetches fresh connection material
through the existing YI cloud code, performs only PPPP rendezvous/punch/ready
traffic, then closes. It sends no TNP commands and requests no media.

The experiment deliberately sends the 20-byte legacy MSG_PUNCH_PKT payload
(raw DID only), even when the server offers a YI extended PUNCH_TO candidate.
Success proves that a modern camera accepts the legacy punch form and lets the
clean-room transport avoid reproducing the proprietary extended signature.

No DID, InitString, license/device key, camera password, endpoint address, or
raw packet payload is printed.
"""
from __future__ import annotations

import argparse
import importlib.util
import select
import socket
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
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
        alive,
        alive_ack,
        close,
        hello,
        p2p_request,
        punch,
        selective_ack,
        try_parse_f1,
    )
except ImportError:  # Direct execution from checkout or a copied /tmp directory.
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
        alive,
        alive_ack,
        close,
        hello,
        p2p_request,
        punch,
        selective_ack,
        try_parse_f1,
    )

DEFAULT_PORT = 32100
ALIVE_PAYLOAD_CAPTURE02 = bytes.fromhex("a2050400")


def route_local_ip(server: str, port: int) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((server, port))
        return str(probe.getsockname()[0])


def parse_server(value: str) -> tuple[str, int]:
    host, sep, port_text = value.partition(":")
    if not host:
        raise argparse.ArgumentTypeError("empty server")
    port = DEFAULT_PORT if not sep else int(port_text)
    try:
        host = socket.inet_ntoa(socket.inet_aton(host))
    except OSError as exc:
        raise argparse.ArgumentTypeError("servers must be IPv4 literals") from exc
    if not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError("invalid UDP port")
    return host, port


def _app_source_candidates() -> tuple[Path, ...]:
    """Find App sources without assuming a fixed number of path parents."""
    candidates: list[Path] = []
    for parent in HERE.parents:
        candidate = parent / "yi_home" / "rootfs" / "opt" / "yi-home" / "app"
        if candidate.is_dir():
            candidates.append(candidate)
    runtime = Path("/opt/yi-home/app")
    if runtime.is_dir():
        candidates.append(runtime)
    return tuple(dict.fromkeys(candidates))


def _runtime_support() -> tuple[Any, Any]:
    for app_dir in _app_source_candidates():
        if str(app_dir) not in sys.path:
            sys.path.insert(0, str(app_dir))
        phase3_path = app_dir / "tools" / "phase3_pppp_probe" / "run_phase3e_tnp.py"
        if not phase3_path.is_file():
            continue
        import yi_tnp_oracle as oracle

        spec = importlib.util.spec_from_file_location("_yi_phase3e_tnp", phase3_path)
        if spec is None or spec.loader is None:
            continue
        phase3 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(phase3)
        return oracle, phase3
    raise RuntimeError(
        "YI App sources not found; run from the repository checkout or inside the App container"
    )


def load_material(env_file: Path, timeout: float) -> tuple[Any, dict[str, Any]]:
    oracle, phase3 = _runtime_support()
    if env_file.is_file():
        oracle.load_env_file(env_file)
    # Phase 3E's exact-target preflight records cloud online only as a hint.
    # The UDP handshake below is the connectivity decision.
    return phase3._fresh_exact_target(timeout=timeout)


def self_test() -> int:
    """Run a sanitized transcript without cloud, camera, TNP, or media I/O."""
    device_id = DeviceId(bytes(20))
    server = Endpoint("192.0.2.1", DEFAULT_PORT)
    candidate = Endpoint("10.0.0.2", 40000)
    state = Handshake(device_id, frozenset((server.key,)))
    state.start()
    state.observe(F1Packet(Opcode.HELLO_ACK), server.key)
    state.observe(F1Packet(Opcode.P2P_REQ_ACK, bytes(4)), server.key)
    offered = state.observe(
        F1Packet(Opcode.PUNCH_TO, candidate.encode_tuple() + bytes(24)), server.key
    )
    assert offered is not None and offered.key == candidate.key and len(offered.extension) == 24
    assert state.phase is TransportPhase.PUNCHING
    state.observe(F1Packet(Opcode.P2P_RDY, device_id.raw), candidate.key)
    assert state.phase is TransportPhase.READY
    state.observe(F1Packet(Opcode.ALIVE_ACK), candidate.key)
    assert state.phase is TransportPhase.ESTABLISHED

    channel = ReliableChannel(0, max_payload=256)
    channel.queue(bytes(56), bytes(52), bytes(56))
    frames = channel.emit(now=0.0)
    assert len(frames) == 1 and len(frames[0].data) == 164
    ack = selective_ack(0, (frames[0].sequence,))
    assert DrwAck.from_f1(F1Packet.parse(ack.to_f1().encode())) == ack
    assert channel.acknowledge(ack) == 1
    assert DrwPacket.from_f1(F1Packet.parse(frames[0].to_f1().encode())) == frames[0]
    print("CR2_SELF_TEST=PASS; cloud_used=false; tnp_sent=false; media_requested=false")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CR-2A transport-only YI legacy PUNCH_PKT compatibility probe"
    )
    parser.add_argument(
        "--server",
        dest="servers",
        action="append",
        type=parse_server,
        help="YI PPPP server IPv4[:port]; repeat for redundancy",
    )
    parser.add_argument("--env-file", type=Path, default=Path("/data/yi.env"))
    parser.add_argument("--cloud-timeout", type=float, default=10.0)
    parser.add_argument("--handshake-timeout", type=float, default=4.0)
    parser.add_argument("--punch-repeat", type=int, default=3)
    parser.add_argument("--self-test", action="store_true", help="run sanitized offline checks")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.servers:
        parser.error("at least one --server is required")
    if args.punch_repeat < 1 or args.punch_repeat > 6:
        raise SystemExit("--punch-repeat must be in range 1..6")

    material: Any | None = None
    sock: socket.socket | None = None
    state: Handshake | None = None
    ready_peer: tuple[str, int] | None = None
    started = time.monotonic()

    try:
        material, cloud_report = load_material(args.env_file, args.cloud_timeout)
        device_id = DeviceId.from_text(material.pppp_did)
        cloud_online = bool(cloud_report.get("cloud_online_reported", False))
        print(
            "cloud_material=ok; did_transport_bytes=20; "
            f"cloud_online_reported={str(cloud_online).lower()}; "
            "connectivity_decision=udp_transport; secrets_exposed=false",
            flush=True,
        )

        first_server = args.servers[0]
        local_ip = route_local_ip(first_server[0], first_server[1])
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((local_ip, 0))
        sock.setblocking(False)
        local_port = int(sock.getsockname()[1])

        state = Handshake(device_id, frozenset(args.servers))
        state.start()
        request = p2p_request(device_id, Endpoint(local_ip, local_port)).encode()
        hello_packet = hello().encode()
        for server in args.servers:
            sock.sendto(hello_packet, server)
            sock.sendto(request, server)
        print(
            f"rendezvous_sent=true; server_count={len(args.servers)}; local_port_allocated=true; secrets_exposed=false",
            flush=True,
        )

        punched: set[tuple[str, int]] = set()
        deadline = time.monotonic() + args.handshake_timeout
        retry_at = time.monotonic() + 0.7
        request_retried = False

        while time.monotonic() < deadline and ready_peer is None:
            remaining = max(0.0, min(0.10, deadline - time.monotonic()))
            readable, _, _ = select.select([sock], [], [], remaining)
            if readable:
                try:
                    data, peer = sock.recvfrom(2048)
                except BlockingIOError:
                    continue
                packet = try_parse_f1(data)
                if packet is None:
                    continue
                peer_key = (peer[0], int(peer[1]))
                candidate = state.observe(packet, peer_key)
                if candidate is not None and candidate.key not in punched:
                    # Intentionally use the independently observed legacy form.
                    punch_packet = punch(device_id).encode()
                    for _ in range(args.punch_repeat):
                        sock.sendto(punch_packet, candidate.key)
                        time.sleep(0.01)
                    punched.add(candidate.key)
                if state.phase is TransportPhase.READY:
                    ready_peer = state.ready_peer
                    break

            if not request_retried and time.monotonic() >= retry_at and ready_peer is None:
                for server in args.servers:
                    sock.sendto(request, server)
                request_retried = True

        lan_candidates = sum(candidate.is_lan for candidate in state.candidates.values())
        wan_candidates = len(state.candidates) - lan_candidates
        print(
            "rendezvous_result="
            f"hello_ack:{state.hello_acks},p2p_req_ack:{state.request_acks},"
            f"candidates:{len(state.candidates)},lan:{lan_candidates},wan:{wan_candidates}; "
            "endpoint_values_exposed=false",
            flush=True,
        )

        if ready_peer is None:
            print(
                "legacy_punch_result=NO_P2P_RDY; interpretation=legacy_punch_not_proven; secrets_exposed=false",
                flush=True,
            )
            return 2

        selected_lan = state.candidates[ready_peer].is_lan
        elapsed_ms = int((time.monotonic() - started) * 1000)
        print(
            f"legacy_punch_result=PASS; p2p_rdy=true; selected_path={'lan' if selected_lan else 'wan'}; "
            f"elapsed_ms={elapsed_ms}; secrets_exposed=false",
            flush=True,
        )

        # Exercise only transport keepalive semantics; do not send TNP or media.
        alive_packet = alive(ALIVE_PAYLOAD_CAPTURE02).encode()
        for _ in range(3):
            sock.sendto(alive_packet, ready_peer)
        alive_deadline = time.monotonic() + 0.8
        while time.monotonic() < alive_deadline:
            readable, _, _ = select.select([sock], [], [], 0.08)
            if not readable:
                continue
            try:
                data, peer = sock.recvfrom(2048)
            except BlockingIOError:
                continue
            packet = try_parse_f1(data)
            if packet is None:
                continue
            peer_key = (peer[0], int(peer[1]))
            state.observe(packet, peer_key)
            if peer_key == ready_peer and packet.opcode == Opcode.ALIVE:
                sock.sendto(alive_ack().encode(), ready_peer)

        print(
            f"keepalive_probe=done; alive_seen={str(state.alive_seen).lower()}; "
            f"alive_ack_seen={str(state.alive_ack_seen).lower()}; "
            "tnp_sent=false; media_requested=false",
            flush=True,
        )
        if not state.keepalive_confirmed:
            print("transport_result=READY_NO_KEEPALIVE; interpretation=session_not_proven")
            return 3
        print("transport_result=PASS; tnp_gate=closed; media_gate=closed")
        return 0
    finally:
        if sock is not None:
            if ready_peer is not None:
                try:
                    sock.sendto(close().encode(), ready_peer)
                    print("close_sent=true; close_ack_expected=false", flush=True)
                except OSError:
                    print("close_sent=false; close_ack_expected=false", flush=True)
            sock.close()
        if state is not None:
            state.mark_closed()
        if material is not None:
            material.clear()


if __name__ == "__main__":
    raise SystemExit(main())
