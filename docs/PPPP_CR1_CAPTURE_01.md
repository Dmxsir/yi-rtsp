# CR-1 capture 01 — steady-state YI PPPP transport

Status: sanitized passive-analysis report. The source pcap is intentionally **not** committed.

This report contains transport metadata only. Device identifiers, TNP payloads, credentials, InitStrings, license values, media bytes and private payload contents are omitted.

## Capture summary

The first CR-1 capture covered 29.473 seconds around a live YI RTSP deployment.

- 20,381 UDP packets in the pcap
- 20,107 UDP packets involved the YI RTSP controller/App host
- 19,802 packets validated as F1/F2 PPPP messages by header length
- five simultaneous direct-LAN PPPP peers were active
- nine public PPPP server peers were observed

Observed PPPP message totals:

| Type | Name | Out | In | Total |
|---:|---|---:|---:|---:|
| `0x18` | `DEV_ONLINE_REQ` | 45 | 0 | 45 |
| `0x19` | `DEV_ONLINE_REQ_ACK` | 0 | 45 | 45 |
| `0xD0` | `DRW` | 0 | 13,166 | 13,166 |
| `0xD1` | `DRW_ACK` | 4,869 | 0 | 4,869 |
| `0xD2` | YI `DRW_ACK` extension | 13 | 0 | 13 |
| `0xE0` | `ALIVE` | 324 | 508 | 832 |
| `0xE1` | `ALIVE_ACK` | 508 | 324 | 832 |

No `HELLO`, `P2P_SERVER_REQ`, `PUNCH_TO`, `PUNCH_PKT`, `P2P_RDY`, `P2P_RDY_ACK`, `CLOSE` or F2 message was present in this capture.

That is the most important limitation of capture 01: it gives us excellent steady-state DRW evidence, but it does **not** contain the initial connection-establishment handshake.

## Server online checks

The capture contains five distinct `DEV_ONLINE_REQ` payloads, corresponding to the five active devices. The raw descriptors are not recorded here.

For each device:

- the request is F1 type `0x18`;
- payload length is exactly 20 bytes;
- three public server peers are contacted;
- three identical request datagrams were observed per server;
- matching F1 type `0x19` acknowledgements have an 8-byte payload.

Across all five devices this produced 45 requests and 45 acknowledgements.

The first four bytes of each observed `0x19` payload decode as a plausible big-endian UNIX timestamp within seconds of the capture window. The remaining four bytes had the observed value `01 00 00 00`.

**Working inference only:** the response likely carries a last-online/server timestamp plus an online/state field. This is not yet treated as an implementation requirement.

Public protocol research independently identifies YI message type `0x18` as `MSG_DEV_ONLINE_REQ` with a 20-byte payload and `0x19` as `MSG_DEV_ONLINE_REQ_ACK` with an 8-byte payload.

## DRW framing confirmed on real YI traffic

Every captured media/data packet from the direct LAN peers used outer message type `F1 D0`.

The observed packet begins:

```text
F1 D0 <payload_len:u16-be>
D1 <channel:u8> <sequence:u16-be>
...
```

The controller acknowledgement uses outer message type `F1 D1` and the payload shape:

```text
D1 <channel:u8> <count:u16-be> <sequence:u16-be>...
```

The full datagram length always matched the declared outer PPPP payload length in the packets accepted by the analyzer.

This is directly compatible with the generic DRW framing documented by open PPPP implementations such as `devbis/aiopppp`, while the YI fork adds behavior beyond that baseline.

## Channel behavior

The five live sessions all received DRW data on channels 1, 2 and 3.

A public device-side YI TNP source reference defines the channel enum as:

```text
0 = IOCTRL
1 = AUDIO
2 = VIDEO_REALTIME_IFRAME
3 = VIDEO_REALTIME_PFRAME
```

That mapping is consistent with this capture:

- channel 1 consisted of small regular records and was acknowledged one sequence at a time;
- channel 2 arrived in large bursts and its acknowledgements batched many sequence IDs;
- channel 3 was sustained video traffic with smaller acknowledgement batches.

The source repository carrying that enum has no declared license and is therefore **reference-only**. No source code is copied from it.

## ACK batching and reliability behavior

Per-peer metadata showed:

- channel 1: ACK batch size was always 1;
- channel 2: median ACK batch size varied by peer from 12 to 65, with a captured maximum of 79 sequence IDs;
- channel 3: median ACK batch size was 2–4, with a captured maximum of 5;
- duplicate DRW sequence IDs were observed on two of the five peers;
- the ordinary `D1` ACK lists included duplicates when duplicate data was received, while the set of acknowledged sequence IDs still matched the received set;
- one peer emitted 13 outbound F1 `D2` packets with a 4-byte payload shaped like `D2 <channel> <sequence:u16-be>`.

The `D2` semantics are not yet proven. Public YI protocol research lists type `0xD2` as a YI-specific `MSG_DRW_ACK` with a four-byte payload. It may represent a second ACK/recovery mechanism. We should not implement guessed behavior until a targeted loss/retransmission capture clarifies it.

One peer had 21 channel-2 sequence IDs not yet acknowledged when the pcap ended. All 21 packets arrived in the final ~3 ms of the capture, so this is a capture-boundary artifact rather than evidence of missing ACK logic.

## ALIVE behavior

Both directions actively exchange `0xE0` / `0xE1` traffic.

Four peers primarily used four-byte `ALIVE` payloads in the established YI session. One peer additionally emitted frequent zero-payload `ALIVE` messages. Every observed ALIVE direction had corresponding ALIVE_ACK traffic.

This confirms that a clean transport needs an independent keepalive/ack loop and cannot depend on application-level TNP traffic to keep the PPPP session alive.

## What capture 01 proves

1. The steady-state YI transport is plain, parseable F1 PPPP framing; there is no protocol-level encryption in the captured DRW/ALIVE traffic.
2. Generic PPPP DRW concepts are reusable: channel, 16-bit sequence ID, variable ACK lists and ALIVE/ALIVE_ACK.
3. YI-specific extensions matter: `0x18/0x19`, four-byte ALIVE variants and `0xD2` are present on real hardware.
4. Channels 1/2/3 have distinct reliability/batching behavior consistent with audio, realtime I-frame and realtime P-frame traffic.
5. The existing TNP/media layer can remain above a replacement transport; the hard missing piece is session establishment plus a reliable DRW engine.

## What capture 01 does not prove

The capture did not include the connection handshake. Specifically it contains none of:

```text
HELLO
P2P_SERVER_REQ / SESSION_RESPONSE
PUNCH_TO / PUNCH_PKT
P2P_RDY / P2P_RDY_ACK
DEV_WAKEUP_REQ (F2)
CLOSE
```

The next CR-1 capture must therefore begin while the YI RTSP App is fully stopped and continue through App startup and the first RTSP consumer reconnect.

## Analyzer

`tools/pppp_cleanroom/parse_pcap.py` is a dependency-free passive parser for classic Ethernet pcap files. By default it:

- validates F1/F2 declared message lengths;
- aliases all PPPP peers rather than printing addresses;
- groups online requests without printing or hashing device descriptors;
- reports message counts and direction;
- parses DRW channel/sequence metadata;
- parses variable D1 ACK lists;
- reports duplicate/ACK coverage and ALIVE metadata;
- never prints payload bytes.

This makes subsequent captures reproducible without committing sensitive capture material.
