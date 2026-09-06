#!/usr/bin/env python3
"""Passive, dependency-free PPPP pcap classifier for clean-room research.

Reads classic Ethernet pcap files and reports only metadata needed to reason
about PPPP transport behavior. Payload bytes, DIDs, credentials and media are
never printed.
"""
from __future__ import annotations

import argparse
import collections
import ipaddress
import socket
import statistics
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

PPPP_TYPES = {
    0x00: "HELLO",
    0x01: "HELLO_ACK",
    0x04: "P2P_SERVER_REQ",
    0x06: "SESSION_RESPONSE",
    0x18: "DEV_ONLINE_REQ",
    0x19: "DEV_ONLINE_REQ_ACK",
    0x1A: "DEV_WAKEUP_REQ",
    0x30: "LAN_SEARCH",
    0x31: "LAN_NOTIFY",
    0x32: "LAN_NOTIFY_ACK",
    0x40: "PUNCH_TO",
    0x41: "PUNCH_PKT",
    0x42: "P2P_RDY",
    0x43: "P2P_RDY_ACK",
    0xD0: "DRW",
    0xD1: "DRW_ACK",
    0xD2: "YI_DRW_ACK",
    0xE0: "ALIVE",
    0xE1: "ALIVE_ACK",
    0xF0: "CLOSE",
}


@dataclass(frozen=True)
class Packet:
    ts: float
    src: str
    dst: str
    sport: int
    dport: int
    payload: bytes


def _pcap_records(path: Path) -> Iterator[tuple[float, bytes]]:
    with path.open("rb") as fh:
        header = fh.read(24)
        if len(header) != 24:
            raise ValueError("not a classic pcap file")
        magic = header[:4]
        if magic == b"\xd4\xc3\xb2\xa1":
            endian, scale = "<", 1_000_000
        elif magic == b"\xa1\xb2\xc3\xd4":
            endian, scale = ">", 1_000_000
        elif magic == b"M<\xb2\xa1":
            endian, scale = "<", 1_000_000_000
        elif magic == b"\xa1\xb2<M":
            endian, scale = ">", 1_000_000_000
        else:
            raise ValueError("unsupported pcap magic")
        _magic, _maj, _min, _zone, _sig, _snaplen, linktype = struct.unpack(
            endian + "IHHIIII", header
        )
        if linktype != 1:
            raise ValueError(f"unsupported link type {linktype}; Ethernet required")
        while True:
            record = fh.read(16)
            if not record:
                return
            if len(record) != 16:
                raise ValueError("truncated pcap record header")
            sec, subsec, incl_len, _orig_len = struct.unpack(endian + "IIII", record)
            frame = fh.read(incl_len)
            if len(frame) != incl_len:
                raise ValueError("truncated pcap frame")
            yield sec + subsec / scale, frame


def _udp_from_ethernet(ts: float, frame: bytes) -> Packet | None:
    if len(frame) < 14:
        return None
    ethertype = struct.unpack("!H", frame[12:14])[0]
    offset = 14
    if ethertype in (0x8100, 0x88A8):
        if len(frame) < 18:
            return None
        ethertype = struct.unpack("!H", frame[16:18])[0]
        offset = 18

    if ethertype == 0x0800:
        if len(frame) < offset + 20:
            return None
        ihl = (frame[offset] & 0x0F) * 4
        if ihl < 20 or len(frame) < offset + ihl + 8:
            return None
        if frame[offset + 9] != 17:
            return None
        src = socket.inet_ntoa(frame[offset + 12 : offset + 16])
        dst = socket.inet_ntoa(frame[offset + 16 : offset + 20])
        udp_offset = offset + ihl
    elif ethertype == 0x86DD:
        if len(frame) < offset + 48:
            return None
        next_header = frame[offset + 6]
        src = socket.inet_ntop(socket.AF_INET6, frame[offset + 8 : offset + 24])
        dst = socket.inet_ntop(socket.AF_INET6, frame[offset + 24 : offset + 40])
        udp_offset = offset + 40
        while next_header in (0, 43, 60):
            if len(frame) < udp_offset + 8:
                return None
            next_header, ext_len = frame[udp_offset], (frame[udp_offset + 1] + 1) * 8
            udp_offset += ext_len
        if next_header == 44:
            if len(frame) < udp_offset + 8:
                return None
            next_header = frame[udp_offset]
            udp_offset += 8
        if next_header != 17 or len(frame) < udp_offset + 8:
            return None
    else:
        return None

    sport, dport, udp_len, _checksum = struct.unpack("!HHHH", frame[udp_offset : udp_offset + 8])
    if udp_len < 8:
        return None
    payload = frame[udp_offset + 8 : min(len(frame), udp_offset + udp_len)]
    return Packet(ts, src, dst, sport, dport, payload)


def read_udp(path: Path) -> list[Packet]:
    packets: list[Packet] = []
    for ts, frame in _pcap_records(path):
        pkt = _udp_from_ethernet(ts, frame)
        if pkt is not None:
            packets.append(pkt)
    return packets


def _private(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_private
    except ValueError:
        return False


def _peer(pkt: Packet, host: str) -> tuple[str, str] | None:
    if pkt.src == host:
        return "out", pkt.dst
    if pkt.dst == host:
        return "in", pkt.src
    return None


def _pppp_type(payload: bytes) -> int | None:
    if len(payload) >= 4 and payload[0] in (0xF1, 0xF2):
        declared = int.from_bytes(payload[2:4], "big")
        header_len = 28 if payload[0] == 0xF2 else 4
        if declared + header_len == len(payload):
            return payload[1]
    return None


def _drw(payload: bytes) -> tuple[int, int] | None:
    if len(payload) >= 8 and payload[:2] == b"\xf1\xd0" and payload[4] == 0xD1:
        return payload[5], int.from_bytes(payload[6:8], "big")
    return None


def _drw_ack(payload: bytes) -> tuple[int, list[int]] | None:
    if len(payload) < 8 or payload[:2] != b"\xf1\xd1" or payload[4] != 0xD1:
        return None
    channel = payload[5]
    count = int.from_bytes(payload[6:8], "big")
    expected = 8 + count * 2
    if expected != len(payload):
        return None
    return channel, [int.from_bytes(payload[i : i + 2], "big") for i in range(8, expected, 2)]


def _safe_aliases(peers: list[str]) -> dict[str, str]:
    private = sorted(p for p in peers if _private(p))
    public = sorted(p for p in peers if not _private(p))
    aliases: dict[str, str] = {}
    for i, peer in enumerate(private, 1):
        aliases[peer] = f"lan-peer-{i}"
    for i, peer in enumerate(public, 1):
        aliases[peer] = f"public-peer-{i}"
    return aliases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--host-ip", required=True, help="controller/App IP to analyze")
    parser.add_argument("--show-public-ips", action="store_true")
    args = parser.parse_args()

    packets = read_udp(args.pcap)
    if not packets:
        raise SystemExit("no UDP packets found")
    t0, t1 = packets[0].ts, packets[-1].ts
    selected = [p for p in packets if args.host_ip in (p.src, p.dst)]

    pppp = []
    types = collections.Counter()
    type_dirs: dict[int, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    peer_counts = collections.Counter()
    online_devices: dict[bytes, set[str]] = collections.defaultdict(set)

    for pkt in selected:
        relation = _peer(pkt, args.host_ip)
        if relation is None:
            continue
        direction, peer = relation
        typ = _pppp_type(pkt.payload)
        if typ is None:
            continue
        pppp.append(pkt)
        types[typ] += 1
        type_dirs[typ][direction] += 1
        peer_counts[peer] += 1
        if typ == 0x18 and direction == "out":
            # Keep the raw device descriptor only in process memory so repeated
            # requests can be grouped. It is never rendered or written out.
            online_devices[pkt.payload[4:]].add(peer)

    pppp_peers = sorted(peer_counts)
    aliases = _safe_aliases(pppp_peers)

    print(f"pcap_seconds={t1 - t0:.3f}")
    print(f"udp_packets={len(packets)}")
    print(f"host_udp_packets={len(selected)}")
    print(f"validated_pppp_packets={len(pppp)}")
    print("message_types:")
    for typ, count in sorted(types.items()):
        name = PPPP_TYPES.get(typ, "UNKNOWN")
        dirs = type_dirs[typ]
        print(f"  0x{typ:02X} {name:<20} total={count} out={dirs['out']} in={dirs['in']}")

    if online_devices:
        print(f"online_request_devices={len(online_devices)}")
        for idx, (_descriptor, servers) in enumerate(
            sorted(online_devices.items(), key=lambda item: sorted(item[1])), 1
        ):
            labels = []
            for server in sorted(servers):
                if args.show_public_ips and not _private(server):
                    labels.append(server)
                else:
                    labels.append(aliases[server])
            print(f"  device-{idx}: server_peers={','.join(labels)}")

    print("pppp_peers:")
    for peer, count in peer_counts.most_common():
        shown = peer if args.show_public_ips and not _private(peer) else aliases[peer]
        print(f"  {shown}: packets={count}")

    print("drw_receive_summary:")
    for peer in sorted(peer_counts):
        rx: dict[int, list[int]] = collections.defaultdict(list)
        acked: dict[int, list[int]] = collections.defaultdict(list)
        ack_batch_sizes: dict[int, list[int]] = collections.defaultdict(list)
        yi_d2 = 0
        alive = collections.Counter()
        for pkt in selected:
            relation = _peer(pkt, args.host_ip)
            if relation is None or relation[1] != peer:
                continue
            direction, _ = relation
            if direction == "in":
                item = _drw(pkt.payload)
                if item:
                    channel, seq = item
                    rx[channel].append(seq)
            else:
                item = _drw_ack(pkt.payload)
                if item:
                    channel, seqs = item
                    acked[channel].extend(seqs)
                    ack_batch_sizes[channel].append(len(seqs))
                if len(pkt.payload) == 8 and pkt.payload[:2] == b"\xf1\xd2":
                    yi_d2 += 1
            typ = _pppp_type(pkt.payload)
            if typ in (0xE0, 0xE1):
                alive[(direction, typ, len(pkt.payload) - 4)] += 1
        if not rx:
            continue
        print(f"  {aliases[peer]}:")
        for channel in sorted(rx):
            seqs = rx[channel]
            uniq = set(seqs)
            ack_uniq = set(acked[channel])
            batch = ack_batch_sizes[channel]
            median_batch = statistics.median(batch) if batch else 0
            print(
                f"    ch{channel}: rx={len(seqs)} unique={len(uniq)} duplicates={len(seqs)-len(uniq)} "
                f"acked_unique={len(ack_uniq)} pending_at_capture_end={len(uniq-ack_uniq)} "
                f"ack_batch_median={median_batch:g} ack_batch_max={max(batch) if batch else 0}"
            )
        if yi_d2:
            print(f"    yi_d2_packets_out={yi_d2}")
        if alive:
            parts = [
                f"{direction}/0x{typ:02X}/payload{payload_len}={count}"
                for (direction, typ, payload_len), count in sorted(alive.items())
            ]
            print("    alive=" + ",".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
