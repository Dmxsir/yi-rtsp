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
import ipaddress
import os
import select
import socket
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve()
SOURCE_APP = HERE.parents[2] / "yi_home" / "rootfs" / "opt" / "yi-home" / "app"
RUNTIME_APP = Path("/opt/yi-home/app")
for candidate in (SOURCE_APP, RUNTIME_APP):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import yi_tnp_oracle as oracle  # noqa: E402

DEFAULT_PORT = 32100
ALIVE_PAYLOAD_CAPTURE02 = bytes.fromhex("a2050400")


@dataclass(frozen=True)
class Candidate:
    address: str
    port: int
    private: bool


def f1(opcode: int, payload: bytes = b"") -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("payload too large for F1 framing")
    return b"\xF1" + bytes((opcode,)) + len(payload).to_bytes(2, "big") + payload


def parse_f1(data: bytes) -> tuple[int, bytes] | None:
    if len(data) < 4 or data[0] != 0xF1:
        return None
    length = int.from_bytes(data[2:4], "big")
    if length + 4 != len(data):
        return None
    return data[1], data[4:]


def raw_did(value: str) -> bytes:
    """Convert canonical PPPP DID text into its 20-byte transport form."""
    parts = value.split("-")
    if len(parts) != 3:
        raise ValueError("unexpected PPPP DID shape")
    prefix, serial_text, suffix = parts
    if not serial_text.isdecimal():
        raise ValueError("PPPP DID serial is not decimal")
    prefix_b = prefix.encode("ascii")
    suffix_b = suffix.encode("ascii")
    serial = int(serial_text)
    if len(prefix_b) > 8 or len(suffix_b) > 8 or not (0 <= serial <= 0xFFFFFFFF):
        raise ValueError("PPPP DID fields exceed the observed transport format")
    return prefix_b.ljust(8, b"\x00") + struct.pack(">I", serial) + suffix_b.ljust(8, b"\x00")


def route_local_ip(server: str, port: int) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((server, port))
        return str(probe.getsockname()[0])


def endpoint_payload(local_ip: str, local_port: int) -> bytes:
    return (
        b"\x00\x02"
        + struct.pack("<H", local_port)
        + socket.inet_aton(local_ip)[::-1]
        + b"\x00" * 8
    )


def parse_candidate(payload: bytes) -> Candidate | None:
    """Parse the independently observed 16-byte IPv4 prefix of PUNCH_TO."""
    if len(payload) not in (16, 40) or payload[:2] != b"\x00\x02":
        return None
    port = struct.unpack("<H", payload[2:4])[0]
    address = socket.inet_ntoa(payload[4:8][::-1])
    if not port:
        return None
    try:
        private = ipaddress.ip_address(address).is_private
    except ValueError:
        return None
    return Candidate(address, port, private)


def parse_server(value: str) -> tuple[str, int]:
    host, sep, port_text = value.partition(":")
    if not host:
        raise argparse.ArgumentTypeError("empty server")
    port = DEFAULT_PORT if not sep else int(port_text)
    try:
        socket.inet_aton(host)
    except OSError as exc:
        raise argparse.ArgumentTypeError("servers must be IPv4 literals") from exc
    if not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError("invalid UDP port")
    return host, port


def load_material(env_file: Path, timeout: float) -> oracle.CameraMaterial:
    if env_file.is_file():
        oracle.load_env_file(env_file)
    material, _report = oracle.fresh_cloud_preflight(timeout=timeout)
    return material


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CR-2A transport-only YI legacy PUNCH_PKT compatibility probe"
    )
    parser.add_argument(
        "--server",
        dest="servers",
        action="append",
        type=parse_server,
        required=True,
        help="YI PPPP server IPv4[:port]; repeat for redundancy",
    )
    parser.add_argument("--env-file", type=Path, default=Path("/data/yi.env"))
    parser.add_argument("--cloud-timeout", type=float, default=10.0)
    parser.add_argument("--handshake-timeout", type=float, default=4.0)
    parser.add_argument("--punch-repeat", type=int, default=3)
    args = parser.parse_args()

    if args.punch_repeat < 1 or args.punch_repeat > 6:
        raise SystemExit("--punch-repeat must be in range 1..6")

    material: oracle.CameraMaterial | None = None
    sock: socket.socket | None = None
    ready_peer: tuple[str, int] | None = None
    started = time.monotonic()

    try:
        material = load_material(args.env_file, args.cloud_timeout)
        did = raw_did(material.pppp_did)
        print("cloud_material=ok; did_transport_bytes=20; secrets_exposed=false", flush=True)

        first_server = args.servers[0]
        local_ip = route_local_ip(first_server[0], first_server[1])
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((local_ip, 0))
        sock.setblocking(False)
        local_port = int(sock.getsockname()[1])

        request = f1(0x20, did + endpoint_payload(local_ip, local_port))
        hello = f1(0x00)
        for server in args.servers:
            sock.sendto(hello, server)
            sock.sendto(request, server)
        print(
            f"rendezvous_sent=true; server_count={len(args.servers)}; local_port_allocated=true; secrets_exposed=false",
            flush=True,
        )

        candidates: dict[tuple[str, int], Candidate] = {}
        punched: set[tuple[str, int]] = set()
        deadline = time.monotonic() + args.handshake_timeout
        retry_at = time.monotonic() + 0.7
        request_retried = False
        p2p_req_ack = 0
        hello_ack = 0

        while time.monotonic() < deadline and ready_peer is None:
            remaining = max(0.0, min(0.10, deadline - time.monotonic()))
            readable, _, _ = select.select([sock], [], [], remaining)
            if readable:
                try:
                    data, peer = sock.recvfrom(2048)
                except BlockingIOError:
                    continue
                parsed = parse_f1(data)
                if parsed is None:
                    continue
                opcode, payload = parsed

                if opcode == 0x01:
                    hello_ack += 1
                elif opcode == 0x21:
                    p2p_req_ack += 1
                elif opcode == 0x40:
                    candidate = parse_candidate(payload)
                    if candidate is None:
                        continue
                    key = (candidate.address, candidate.port)
                    candidates[key] = candidate
                    if key not in punched:
                        # Intentionally use the legacy payload: raw DID only.
                        packet = f1(0x41, did)
                        for _ in range(args.punch_repeat):
                            sock.sendto(packet, key)
                            time.sleep(0.01)
                        punched.add(key)
                elif opcode == 0x42:
                    # Both legacy and extended RDY forms begin with the raw DID.
                    if len(payload) >= 20 and payload[:20] == did:
                        ready_peer = (peer[0], int(peer[1]))
                        break
                elif opcode == 0xE0:
                    sock.sendto(f1(0xE1), peer)

            if not request_retried and time.monotonic() >= retry_at and ready_peer is None:
                for server in args.servers:
                    sock.sendto(request, server)
                request_retried = True

        lan_candidates = sum(candidate.private for candidate in candidates.values())
        wan_candidates = len(candidates) - lan_candidates
        print(
            "rendezvous_result="
            f"hello_ack:{hello_ack},p2p_req_ack:{p2p_req_ack},"
            f"candidates:{len(candidates)},lan:{lan_candidates},wan:{wan_candidates}; "
            "endpoint_values_exposed=false",
            flush=True,
        )

        if ready_peer is None:
            print(
                "legacy_punch_result=NO_P2P_RDY; interpretation=legacy_punch_not_proven; secrets_exposed=false",
                flush=True,
            )
            return 2

        selected_private = ipaddress.ip_address(ready_peer[0]).is_private
        elapsed_ms = int((time.monotonic() - started) * 1000)
        print(
            f"legacy_punch_result=PASS; p2p_rdy=true; selected_path={'lan' if selected_private else 'wan'}; "
            f"elapsed_ms={elapsed_ms}; secrets_exposed=false",
            flush=True,
        )

        # Exercise only transport keepalive semantics; do not send TNP or media.
        alive = f1(0xE0, ALIVE_PAYLOAD_CAPTURE02)
        for _ in range(3):
            sock.sendto(alive, ready_peer)
        alive_seen = False
        alive_ack_seen = False
        alive_deadline = time.monotonic() + 0.8
        while time.monotonic() < alive_deadline:
            readable, _, _ = select.select([sock], [], [], 0.08)
            if not readable:
                continue
            try:
                data, peer = sock.recvfrom(2048)
            except BlockingIOError:
                continue
            parsed = parse_f1(data)
            if parsed is None:
                continue
            opcode, _payload = parsed
            if opcode == 0xE0:
                alive_seen = True
                sock.sendto(f1(0xE1), peer)
            elif opcode == 0xE1:
                alive_ack_seen = True

        print(
            f"keepalive_probe=done; alive_seen={str(alive_seen).lower()}; "
            f"alive_ack_seen={str(alive_ack_seen).lower()}; tnp_sent=false; media_requested=false",
            flush=True,
        )
        return 0
    finally:
        if sock is not None:
            if ready_peer is not None:
                try:
                    sock.sendto(f1(0xF0), ready_peer)
                except OSError:
                    pass
            sock.close()
        if material is not None:
            material.clear()


if __name__ == "__main__":
    raise SystemExit(main())
