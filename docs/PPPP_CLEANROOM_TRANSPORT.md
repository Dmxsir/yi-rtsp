# YI PPPP clean-room transport specification

Status: offline implementation is complete for the observed F1 direct-session
subset, and CR-2 real-device establishment is now proven on one owned `y291ga`
/ raw model `83` direct path. CR-3 reliable channel-0/TNP remains unproven live.

This specification contains only sanitized transport observations. It does not
contain a real device identifier, endpoint, InitString, license, key, password,
TNP payload, media payload, pcap, APK, or proprietary library.

## Evidence labels

- **PROVEN** — repeated in sanitized owned-device captures, enforced by offline
  round-trip tests against documented shapes, or isolated by a controlled live
  owned-device experiment.
- **INFERRED** — consistent with captures and licensed generic PPPP projects,
  but not isolated by a targeted YI experiment.
- **UNKNOWN** — insufficient evidence; the implementation must not guess.

## Evidence audit and corrected assumptions

### PROVEN

- The five captured direct-LAN sessions used complete, unencrypted outer F1
  datagrams with a one-byte opcode and a two-byte big-endian payload length.
- Startup order was online query, HELLO, P2P request, server candidate delivery,
  punch, ready, keepalive, then DRW.
- `P2P_REQ` was 20 bytes of raw device ID plus a 16-byte IPv4 endpoint tuple.
- Legacy and YI-extended forms coexisted: PUNCH_TO was 16 or 40 bytes,
  PUNCH_PKT was 20 or 44 bytes, and P2P_RDY was 20 or 40 bytes.
- The first 16 bytes of both observed PUNCH_TO forms encoded the endpoint as
  family `00 02`, little-endian port, reversed IPv4 bytes, and eight zero bytes.
- The successful ready peer was one of the server-supplied candidates in every
  captured session. All five selected the LAN candidate.
- A controlled live CR-2 run on one owned `y291ga` / raw model `83` camera
  established a direct F1 session using the clean-room client and the legacy
  20-byte `PUNCH_PKT`, without the proprietary PPPP library performing the
  transport handshake.
- That CR-2 run received valid rendezvous acknowledgements, one LAN and one WAN
  server-supplied candidate, a matching `P2P_RDY`, then both `ALIVE` and
  `ALIVE_ACK` from the selected LAN peer before best-effort `CLOSE`.
- DRW uses inner marker `D1`, channel byte, and a 16-bit big-endian sequence.
- D1 carries a channel plus a variable-length list of 16-bit ACK sequences.
- Duplicate DRW datagrams occur and are acknowledged without being delivered
  twice at the byte-stream boundary.
- Channels observed by YI TNP are 0 control, 1 audio, 2 realtime I-frame, and
  3 realtime P-frame.
- Three application writes of 56, 52, and 56 bytes appeared as one 164-byte
  channel-0 DRW payload. Application write boundaries are not transport record
  boundaries.
- ALIVE and ALIVE_ACK are bidirectional. The captured client ALIVE payload was
  four opaque bytes; ALIVE_ACK was empty.
- Cloud `online` is not a transport result. The CR-2 probe records it only as a
  hint and decides connectivity from ready plus keepalive traffic.

Sanitized live evidence: [`PPPP_CR2_LIVE_01.md`](PPPP_CR2_LIVE_01.md).

### INFERRED

- The four-byte D2 form `D2, channel, u16-be value` is probably cumulative or
  next-expected ACK state. The codec preserves it, but reliable-send state does
  not act on it.
- A small punch retry burst is needed for UDP loss tolerance. Three repeats
  matched the successful CR-2 experiment but are not established as a protocol
  constant.
- A 1024-byte default DRW data chunk is a conservative generic-PPPP choice, not
  a YI limit. It is configurable and must be measured before production use.
- DEV_ONLINE is useful status telemetry but has not been proven to be a
  prerequisite for P2P rendezvous. CR-2 does not gate on it.

### UNKNOWN

- Meaning and construction of the 24-byte YI extended punch/ready suffix.
- Whether camera models/firmware beyond the tested `y291ga` direct path accept
  the legacy 20-byte PUNCH_PKT.
- Whether P2P_RDY_ACK is needed by models not represented in the captured/live
  tests.
- Exact D2 send/receive semantics and whether it is required for long sessions.
- Maximum safe YI DRW payload, retransmission timing, receive window, and ACK
  batching policy under loss.
- ALIVE four-byte field semantics and whether other models require other values.
- Relay, wakeup, F2, IPv6, and non-direct paths.
- Whether CLOSE has an acknowledgement; none was observed.
- Live CR-3 channel-0/TNP behavior over the clean transport.

The earlier statement that the transport has “no protocol-level encryption”
is therefore narrowed to the observed direct-LAN F1 sessions. It is not a
claim about F2, relay, wakeup, other devices, or every YI PPPP generation.

## Licensed implementation audit

Reviewed on 2026-09-06. No source was copied into this implementation.

| Project | Reviewed commit | Declared license | Reuse decision |
|---|---|---|---|
| `devbis/aiopppp` | `376b40379f78b28e3f8f69e5afcf8bda7eeab8a9` | Apache-2.0 | Generic F1/DRW, keepalive, cancellation, and sequence patterns corroborate our captures. Its device-ID and channel conventions are not YI-compatible and were not reused. |
| `elastic/camera-hacks` | `d3d9b5de00c59991101a9a3774a0f0be45ebe620` | MIT | Rendezvous/punch retry structure is useful corroboration. AJCloud identifiers, message lengths, endpoints, payloads, and application commands were not reused. |
| `magicus/pppp-dissector` | `3ea9a7261f028fe2c75d141cb430bc29a162e2b0` | MIT | F1 header and variable D1 ACK layout corroborate the independently captured YI layout. Generic encryption and application-layer material are outside scope. |
| `nosoop-onlyslop/p2pcam` | `60722746d8646a2177d9ed4fd13e4e957caca9f0` | Unlicense | Not copied. Its own comments describe proprietary decompilation as an implementation source, so it is treated only as evidence that replacement transports are feasible. |

`frankzhangshcn/p2p_tnp` and `xen0bit/libPPCS_API` still have no compatible
declared license. They remain reference-only and provide no implementation
source for this work.

## Packet subset

All integers are unsigned.

### F1 envelope — PROVEN

```text
offset  size  field
0       1     0xF1
1       1     opcode
2       2     payload length, big-endian
4       n     payload
```

The declared length must equal the remaining datagram length exactly. Truncated
or trailing-byte datagrams are rejected.

### Raw device ID — PROVEN

```text
prefix[8], NUL padded
serial u32-be
suffix[8], NUL padded
```

Code stores and compares this value but redacts its object representation.

### IPv4 endpoint tuple — PROVEN

```text
00 02
port u16-le
IPv4 bytes in reverse network order
8 zero bytes
```

PUNCH_TO may append 24 opaque bytes. The extension is preserved but never
interpreted. “LAN” means RFC1918 only; Python's broader `is_private` category
is not used.

### Direct-session control packets

| Opcode | Payload | Evidence |
|---:|---|---|
| `00` HELLO | empty | PROVEN |
| `01` HELLO_ACK | 16-byte endpoint tuple | PROVEN |
| `20` P2P_REQ | raw device ID + local endpoint tuple | PROVEN |
| `21` P2P_REQ_ACK | status payload remains opaque | PROVEN framing, UNKNOWN fields |
| `40` PUNCH_TO | 16-byte tuple or tuple + 24 opaque bytes | PROVEN |
| `41` PUNCH_PKT | raw 20-byte device ID in the CR-2 legacy path | PROVEN on one live `y291ga` direct path; broader compatibility UNKNOWN |
| `42` P2P_RDY | raw device ID, optionally + 20 opaque bytes | PROVEN |
| `E0` ALIVE | captured opaque four bytes for the client probe | PROVEN value and live exchange on tested path; field meaning UNKNOWN |
| `E1` ALIVE_ACK | empty | PROVEN |
| `F0` CLOSE | empty | PROVEN send behavior; acknowledgement UNKNOWN |

### DRW and ACKs — PROVEN framing

```text
F1 D0 <len:u16-be>
D1 <channel:u8> <sequence:u16-be> <stream bytes...>

F1 D1 <len:u16-be>
D1 <channel:u8> <count:u16-be> <sequence:u16-be>...

F1 D2 0004
D2 <channel:u8> <value:u16-be>
```

The D2 wire codec is implemented; its inferred cumulative effect is not.

## CR-2 direct-session state machine

Only packets from the configured server set or a server-supplied punched
candidate may advance state. Endpoint values remain internal and are not
printed.

| State | Accepted input | Action / next state |
|---|---|---|
| `NEW` | local start | Bind one UDP socket; construct the local endpoint; enter `RENDEZVOUS`. |
| `RENDEZVOUS` | start | Send HELLO and P2P_REQ to each configured YI server. |
| `RENDEZVOUS` | HELLO_ACK or P2P_REQ_ACK from a configured server | Count as diagnostics; do not decide reachability. |
| `RENDEZVOUS` / `PUNCHING` | valid PUNCH_TO from a configured server | Deduplicate candidate; send a bounded legacy PUNCH_PKT retry burst; enter `PUNCHING`. |
| `PUNCHING` | matching P2P_RDY from a punched candidate | Select that peer; enter `READY`. Other sources and device IDs are ignored. |
| `READY` | local action | Send the captured transport-only ALIVE burst. No DRW, TNP, or media is allowed in CR-2. |
| `READY` | ALIVE or ALIVE_ACK from selected peer | Reply to ALIVE; enter `ESTABLISHED`. This is the CR-2 success boundary. |
| `ESTABLISHED` | transport-only probe complete | Send CLOSE best-effort and enter `CLOSED`. |
| any peer state | CLOSE from selected peer | Enter `CLOSED`. |
| any nonterminal state | deadline | Close socket and report a nonzero, stage-specific result. |

P2P_RDY alone proves legacy punch compatibility for that session, but it does
not produce a full CR-2 PASS. The probe requires selected-peer keepalive
evidence as well. The 2026-09-06 `y291ga` live run satisfied both conditions.

## CR-3 reliable byte-stream model

`tools/pppp_cleanroom/yi_pppp.py` provides an offline-tested `ReliableChannel`:

1. Adjacent writes enter one byte queue, so 56 + 52 + 56 can emit as one
   164-byte DRW payload.
2. Outbound DRW sequences increment modulo 65536 and remain pending until a
   selective D1 ACK removes them.
3. A configurable timer returns the same pending DRW packet for retransmission
   and stops at a bounded attempt count.
4. Inbound packets are buffered inside a bounded forward sequence window.
5. Contiguous packets become one readable byte stream; out-of-order and wrap
   cases preserve order, and duplicates are ACKed without duplicate delivery.
6. Variable D1 lists, including duplicate sequence entries, round-trip exactly.
7. D2 packets round-trip but do not alter reliability state.

This remains offline CR-3 codec/state logic, not a live CR-3 session. CR-2 has
now passed on owned hardware, so a separate controlled research probe may wire
only channel 0 to the existing TNP builders for the next gate. RTSP/media and
production transport selection remain out of scope.

## Running the safe checks

From a repository checkout:

```bash
python3 tools/pppp_cleanroom/probe_legacy_punch.py --self-test
python3 -m unittest tests.test_pppp_cleanroom -v
```

For relocation inside the Home Assistant App container, copy both clean-room
files so the codec remains beside the probe:

```bash
cp tools/pppp_cleanroom/probe_legacy_punch.py tools/pppp_cleanroom/yi_pppp.py /tmp/
python3 /tmp/probe_legacy_punch.py --self-test
```

The live transport-only command additionally requires a selected camera stable
ID, one or more explicitly supplied YI server IPv4 addresses, and the existing
protected App environment file. It never prints endpoint values or secret
connection material. A live run remains manual and is not performed by the
offline test suite.

## Current progress

### PROVEN

- Packet codecs reject malformed lengths and round-trip all implemented shapes.
- State transitions reject unconfigured server sources, unoffered ready peers,
  and mismatched device IDs.
- The probe runs from checkout and from a relocated temporary directory.
- Selective ACK, retransmission, coalescing, ordering, duplicate suppression,
  and 16-bit wraparound pass sanitized offline tests.
- No code path in the self-test imports cloud support or sends network, TNP, or
  media traffic.
- One owned `y291ga` / raw model `83` camera completed the full CR-2 live
  transport boundary: server rendezvous, legacy 20-byte punch, matching ready,
  LAN path selection, ALIVE/ALIVE_ACK confirmation, and CLOSE, with no TNP or
  media sent.

### INFERRED

- The initial retry/window/chunk defaults are suitable for the first controlled
  CR-3 device experiment.
- D1 alone may be sufficient for the first short CR-3 channel-0 experiment.

### UNKNOWN

- Legacy-punch acceptance on other YI models/firmware and non-direct paths.
- Loss behavior and D2 requirements on YI hardware.
- CR-3 TNP exchange, which remains intentionally unattempted.
