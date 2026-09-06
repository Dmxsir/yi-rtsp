# CR-1 capture 02 — YI PPPP startup handshake

Status: sanitized passive-analysis report. The source pcap is intentionally **not** committed.

This report contains transport metadata only. Device identifiers, account data, InitStrings, license values, camera passwords, media bytes and raw private payloads are omitted.

## Capture summary

The second CR-1 capture covered 44.700 seconds and intentionally started before a YI RTSP App restart.

- 25,027 UDP packets in the pcap
- all 25,027 involved the App host because the capture filter was scoped to that host
- 24,619 packets validated as classic F1 PPPP messages by declared length
- no malformed F1/F2 candidate was observed
- five YI sessions were re-established
- every successful session selected a direct LAN camera endpoint rather than the WAN candidate
- no F2 message was observed in this startup

Observed PPPP totals:

| Type | Name | Out | In | Total |
|---:|---|---:|---:|---:|
| `0x00` | `HELLO` | 15 | 0 | 15 |
| `0x01` | `HELLO_ACK` | 0 | 14 | 14 |
| `0x18` | `DEV_ONLINE_REQ` | 45 | 0 | 45 |
| `0x19` | `DEV_ONLINE_REQ_ACK` | 0 | 45 | 45 |
| `0x20` | `P2P_REQ` | 18 | 0 | 18 |
| `0x21` | `P2P_REQ_ACK` | 0 | 17 | 17 |
| `0x40` | `PUNCH_TO` / `PUNCH_TO_EX` | 0 | 34 | 34 |
| `0x41` | `PUNCH_PKT` / `PUNCH_PKT_EX` | 60 | 0 | 60 |
| `0x42` | `P2P_RDY` / `P2P_RDY_EX` | 0 | 15 | 15 |
| `0xA0` | `CONNECT_REPORT` | 15 | 0 | 15 |
| `0xD0` | `DRW` | 17 | 17,354 | 17,371 |
| `0xD1` | `DRW_ACK` | 5,352 | 7 | 5,359 |
| `0xD2` | YI DRW ACK extension | 7 | 3 | 10 |
| `0xE0` | `ALIVE` | 329 | 478 | 807 |
| `0xE1` | `ALIVE_ACK` | 460 | 330 | 790 |
| `0xF0` | `CLOSE` | 0 | 4 | 4 |

The four `CLOSE` packets occurred near the beginning while pre-restart sessions were being torn down. Fresh connection establishment began later in the capture.

## Startup timeline

The five reconnects followed the same high-level sequence:

```text
DEV_ONLINE_REQ -> DEV_ONLINE_REQ_ACK
HELLO          -> HELLO_ACK
P2P_REQ        -> P2P_REQ_ACK
               -> PUNCH_TO (WAN candidate)
               -> PUNCH_TO (LAN candidate)
PUNCH_PKT      -> both candidate endpoints
P2P_RDY        <- LAN camera endpoint
ALIVE / ACK
DRW channel 0
```

Each connection used one UDP socket and contacted three redundant YI PPPP servers on UDP 32100, matching the generic PPPP three-server design.

The five measured `P2P_REQ -> first P2P_RDY` times were approximately:

```text
264 ms
176 ms
253 ms
172 ms
520 ms
```

All five `P2P_RDY` packets arrived from the LAN candidate.

## IPv4 endpoint structure is now independently confirmed

The first 16 bytes of each observed `PUNCH_TO` payload have this structure:

```text
00 02
<port:u16-le>
<IPv4 address:4 bytes, little-endian/reversed network order>
00 00 00 00 00 00 00 00
```

For every camera the server supplied two candidates:

1. the camera's current public/NAT endpoint;
2. the camera's current private/LAN endpoint.

The App sent `PUNCH_PKT` traffic to both. The LAN candidate answered first and became the actual direct transport endpoint.

This also explains why a clean client does not need the camera's LAN port in advance: the YI server supplies the current randomized UDP endpoint as part of `PUNCH_TO`.

## `P2P_REQ` structure

The captured IPv4 `P2P_REQ` payload is 36 bytes:

```text
<device-id raw:20>
00 02
<client UDP port:u16-le>
<client local IPv4:4 bytes, little-endian/reversed network order>
00 00 00 00 00 00 00 00
```

The 20-byte raw device ID is consistent with the publicly documented PPPP representation:

```text
prefix: 8-byte NUL-padded ASCII
serial: 4-byte big-endian integer
suffix/check: 8-byte NUL-padded ASCII
```

The App's self-reported local address can be a container-private address. This does not prevent the direct LAN path: once the App sends a punch datagram to the camera's LAN candidate, the camera learns the NATed source endpoint from the packet itself.

## HELLO_ACK

The 16-byte `HELLO_ACK` payload uses the same endpoint tuple shape and reports the client's server-observed public UDP endpoint.

The client does not need to know that endpoint before sending `P2P_REQ`; in the capture `P2P_REQ` was already sent while HELLO responses were still arriving.

## Two YI handshake generations are present

Four sessions used the YI extended forms:

```text
PUNCH_TO_EX payload = 40 bytes
PUNCH_PKT_EX payload = 44 bytes
P2P_RDY_EX payload   = 40 bytes
```

One session used the legacy/base forms:

```text
PUNCH_TO payload = 16 bytes
PUNCH_PKT payload = 20 bytes
P2P_RDY payload   = 20 bytes
```

This is particularly valuable because the legacy/base format is present on real YI hardware in the same deployment.

Public protocol research notes that YI retained compatibility with some smaller legacy messages even after adding signed extended forms. That gives CR-2 a high-value experiment: send a legacy 20-byte `PUNCH_PKT` to a modern YI camera and test whether it still responds with `P2P_RDY`.

If that succeeds, the clean transport can avoid reproducing YI's extended punch signature entirely for direct sessions.

## PUNCH repetition behavior

The vendor transport repeats punch datagrams. Depending on how many redundant servers returned candidates, the capture showed either three, six or nine packets sent to a given WAN/LAN endpoint.

A minimal implementation should deduplicate endpoint candidates but still retransmit a small number of `PUNCH_PKT` messages because UDP delivery is not guaranteed.

## Session-ready behavior

The camera sent `P2P_RDY` three times on each successful direct session.

Immediately afterward the App sent YI `ALIVE` packets with a four-byte payload. The observed client-side payload was stable across the captured sessions. `ALIVE_ACK` itself used an empty payload.

The first channel-0 DRW packet followed roughly 4–6 ms after the initial ALIVE burst on four sessions and about 1 ms after it on the fifth.

No `P2P_RDY_ACK (0x43)` was emitted by the vendor client in these five sessions. This does not prove that other YI models or transport paths never require it.

## DRW stream semantics — important new finding

The existing native worker performs three application writes for the proven TNP startup burst:

```text
4881 -> 9029 -> 768
```

Their successful write lengths are 56, 52 and 56 bytes respectively, totaling 164 bytes.

In this pcap those three writes appeared on the wire as one channel-0 DRW data packet containing exactly 164 bytes. The same DRW sequence ID was retransmitted until acknowledged.

This confirms that PPPP behaves as a reliable byte-stream abstraction above DRW packets: application `write()` boundaries do not necessarily equal DRW packet boundaries.

For the clean transport this means the adapter should not promise datagram/message boundaries to the TNP layer. It should expose ordered channel byte streams and may coalesce adjacent writes.

## `D2` behavior

The YI-specific four-byte `D2` payload has the shape:

```text
D2 <channel> <u16-be value>
```

Capture 02 gives stronger evidence that the 16-bit value is a cumulative/next-expected sequence indicator rather than a second ordinary selective ACK.

Examples occurred after contiguous sequence runs followed by a duplicate or older packet; the reported value matched the next expected sequence in several independent cases. The receiver still also emitted regular variable-list `D1` ACK packets.

Treat this as a strong working model, not a finalized specification. CR-3 should first implement selective `D1` ACKs and retransmission, then validate whether `D2` is required for long-running parity.

## CONNECT_REPORT (`0xA0`)

The capture contains one `CONNECT_REPORT` to each of the three servers after each successful direct connection: 15 total packets for five sessions.

This happens after `P2P_RDY` and is therefore not required to obtain the initial direct session. It can be deferred in the first CR-2 probe.

## What capture 02 proves

1. We now have the complete successful direct-session handshake on real YI hardware.
2. The client can build `P2P_REQ` from only the device ID plus its own UDP socket address.
3. YI's servers provide both WAN and LAN camera endpoint candidates through `PUNCH_TO`.
4. Sending punch traffic to the supplied LAN candidate is sufficient for all five observed sessions to become direct LAN sessions.
5. `P2P_RDY` is the clean handoff point from rendezvous to DRW transport.
6. The TNP startup writes are carried as a channel byte stream and may be coalesced into one DRW packet.
7. The major unknown blocking a first native-free session is now the modern `PUNCH_PKT_EX` compatibility/signature behavior, not server rendezvous or DRW framing.

## Next experiment — CR-2A

Build a transport-only probe that:

```text
uses a fresh cloud DID
-> sends HELLO + P2P_REQ to known YI servers
-> parses WAN/LAN PUNCH_TO candidates
-> deliberately sends legacy 20-byte PUNCH_PKT
-> waits only for P2P_RDY
-> performs ALIVE/ACK
-> closes without sending TNP or media commands
```

Success on a modern camera would eliminate the extended punch signature from the minimum clean-room implementation.

If the modern camera rejects the legacy punch, CR-2B will focus specifically on the 44-byte YI `PUNCH_PKT_EX` extension using independently observed packets and public protocol research.
