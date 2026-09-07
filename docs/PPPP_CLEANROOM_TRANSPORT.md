# YI PPPP clean-room transport specification

Status: offline implementation is complete for the observed F1 direct-session
subset. CR-2 establishment, CR-3 reliable channel-0/TNP through valid `4882`,
and CR-4 real H.264 I/P plus AAC parsing are live-proven on one owned `y291ga`
with raw model `83` on a direct path. CR-4B sustained media/mux is also LIVE
PROVEN on that one path. CR-4C temporary loopback publication is OFFLINE
IMPLEMENTED / LIVE UNPROVEN.

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
- On the same owned direct path, a controlled CR-3 run carried the existing
  164-byte TNP startup over reliable channel 0, received a selective D1 ACK,
  reconstructed ordered response bytes, validated the first expected `4882`,
  sent `767`, and closed without the proprietary library performing transport
  or channel I/O.
- A controlled CR-4 run on that path reconstructed independent channel-1/2/3
  TNP records and passed real H.264 I/P plus AAC through the existing parsers
  and video reorder logic before stop and close.
- A controlled CR-4B run on that path sustained clean media for 33.124 seconds,
  observed 10 I-frames, 531 P-frames, 541 reordered video frames, and 540 AAC
  frames, then validated 1,243,808 bytes of framed MPEG-TS as H.264 1920x1080
  plus AAC 16 kHz mono. It recorded zero DRW retries and no D2 during that run,
  then sent `767` and closed.

Sanitized live evidence: [`PPPP_CR2_LIVE_01.md`](PPPP_CR2_LIVE_01.md),
[`PPPP_CR3_LIVE_01.md`](PPPP_CR3_LIVE_01.md), and
[`PPPP_CR4_LIVE_01.md`](PPPP_CR4_LIVE_01.md), and
[`PPPP_CR4B_LIVE_01.md`](PPPP_CR4B_LIVE_01.md).

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
- Sustained clean media behavior under loss or over long sessions.
- RTSP/go2rtc/Frigate parity through the clean transport.
- Production timing, retry, window, chunk, buffer, and timeout limits.

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

`tools/pppp_cleanroom/yi_pppp_session.py` provides the research-only session
boundary used by CR-3. It enters DRW mode only after the existing CR-2 state
has selected a peer and received keepalive confirmation. During channel-0 ACK
and read waits it continues explicit keepalives, applies only selective D1 ACKs
to pending send state, retransmits with a bounded configurable policy, and
turns remote close/retry/read failures into stage-specific categories. D2 is
counted but never applied. Non-zero DRW is ACKed and discarded without logging,
decoding, storing, or forwarding its payload.

`tools/pppp_cleanroom/probe_channel0_tnp.py` queues the Phase 3E `4881`, `9029`,
and `768` units as adjacent channel-0 writes, validates the first expected
`4882` with the Phase 3E validation helper, attempts `767`, and closes. The
controlled run recorded in [`PPPP_CR3_LIVE_01.md`](PPPP_CR3_LIVE_01.md) proved
that boundary on one owned direct `y291ga` path. RTSP/media publication and
production transport selection remain out of scope.

## CR-4 media-channel model — LIVE PROVEN

The CR-3 session keeps its original payload-blind behavior unless a research
caller explicitly enables media reads. `probe_media_tnp.py` does so only after
validating the CR-3 `4882` boundary. Enabled channels 1, 2, and 3 each use an
independent `ReliableChannel`, first-packet sequence origin, selective D1 ACK
state, duplicate suppression, wraparound ordering, and configurable byte bound.
D2 is still parsed and counted without changing reliable state. Keepalive,
remote close, retry bounds, and stage-specific failures remain active during
media reads.

`tnp_stream.py` retains partial TNP headers/bodies across read timeouts and
returns only complete, version/type-checked, size-bounded channel records. The
manual runner then calls the existing `yi_live_relay` video decoder/reorder
buffer and `yi_native_av_relay` AAC decrypt/ADTS parser. It requires a valid
channel-2 I-frame, channel-3 P-frame, channel-1 AAC record, and positive ordered
video output before PASS. It holds only bounded transient bytes, prints no
payload, writes no media, invokes no FFmpeg/RTSP path, and always attempts `767`
and transport close after startup.

Offline tests prove the implemented channel, stream-reader, synthetic parser,
cleanup, self-test, support-loader, and nested-relocation mechanics. The live
run recorded in [`PPPP_CR4_LIVE_01.md`](PPPP_CR4_LIVE_01.md) additionally
proved real H.264 I/P and AAC parsing on one owned direct path. CR-4B later
proved sustained media on that path; loss behavior and production parity remain
unproven.

## CR-4B sustained media/mux model — LIVE PROVEN

`probe_sustained_mux.py` preserves the proven startup and media parser path, but
requires valid I, P, and AAC observations to span the configured minimum active
window. Counts are only a secondary guard and cannot satisfy the duration gate.
It bounds reliable/TNP buffers, pre-mux frames and bytes, pump chunks, record
counts, starvation intervals, and child waits. D2 remains observational and
session reads continue the existing keepalive/remote-close behavior.

Once the existing reorder logic emits video and the existing AAC parser accepts
audio, the runner starts an incremental pipe-only mux. `mux_pipe.py` reuses the
production relay's `_setts` expressions and equivalent H.264/AAC copy-mux
options, continuously pumps MPEG-TS stdout into ffprobe stdin, validates 188-byte
sync framing and the expected H.264 1920x1080 plus AAC 16 kHz mono metadata, and
normalizes validator EPIPE only when final metadata is valid. It writes no media
or capture file and prints no payload or absolute camera timestamp.

Offline tests establish the duration gate, parser/reorder feed boundary, A/V
offset, bounded pre-mux state, starvation/error propagation, output draining,
metadata rejection, EPIPE, early-exit/timeout cleanup, and nested isolation.
The controlled run in [`PPPP_CR4B_LIVE_01.md`](PPPP_CR4B_LIVE_01.md) proved
the full sustained source and pipe-only mux/ffprobe boundary on one owned direct
`y291ga` path. It does not establish loss tolerance, long-session behavior, or
compatibility with other hardware/firmware.

## CR-4C temporary RTSP publication model — OFFLINE IMPLEMENTED / LIVE UNPROVEN

`probe_rtsp_publish.py` keeps the source path identical through the CR-4B
collector and shared FFmpeg command, then replaces only the pipe validator sink
with `rtsp_publish.py`'s bounded publication sink. The sink waits for complete
MPEG-TS packets before opening its POST, streams them through the existing
go2rtc incoming MPEG-TS endpoint semantics, retains at most one partial packet,
and persists no media.

The runner owns one temporary go2rtc child and generated config/log beneath
`/tmp/yi-cr4c`. Its only stream is the fixed synthetic `yi_cr4c_probe`; API and
RTSP listen on `127.0.0.1` using non-production defaults `11984` and `18554`,
and WebRTC is disabled. Ports `1984` and `8554` are rejected, occupied ports
hard-fail, and the runner neither imports nor invokes the normal App publisher
or lifecycle.

After ingest begins, the coordinator requires an exact one-stream go2rtc
registry and a registered MPEG-TS producer with non-empty media information.
Only then does a bounded local ffprobe consumer read
`rtsp://127.0.0.1:18554/yi_cr4c_probe`. PASS requires H.264 1920x1080, AAC
16 kHz mono, positive packet counts for both streams, the configured consumer
span, the unchanged CR-4B sustained source gate, successful `767`, and cleanup
of all processes, sockets, threads, and temporary files. These mechanics are
offline-tested; real-device go2rtc/RTSP and Home Assistant/Frigate parity remain
UNKNOWN.

The first bounded manual CR-4C attempt on 2026-09-07 reached temporary go2rtc
readiness, clean control, media enablement, and real MPEG-TS producer
registration/media readiness, then failed with `AUDIO_PARSE_INVALID`. Its
`767`, consumer, temporary go2rtc, and transport cleanup completed. This partial
evidence does not satisfy the RTSP consumer gate; CR-4C remains **LIVE
UNPROVEN**.

Production already treats only `AudioUnitValidationError` as an isolated
malformed/corrupt audio record that may be dropped. The shared research
collector therefore remains strict by default for CR-4B, while CR-4C explicitly
permits an absolute, configurable `--max-audio-validation-drops` count. Its
experimental default is `3`; the next classified error fails as
`AUDIO_VALIDATION_DROP_LIMIT`. Dropped records do not advance valid audio
counts or timing, initialize format/timestamp state, enter the mux, or satisfy
the source PASS gate. Other parser/runtime errors and AAC format changes remain
fatal. The value `3` is a conservative first-retry bound, not a protocol
invariant.

The second bounded manual attempt on 2026-09-07 passed the sustained source
gate for 32.759 seconds with 10 I-frames, 531 P-frames, 541 reordered video
frames, 536 valid AAC frames, and 3 bounded audio-validation drops. The
temporary MPEG-TS producer registered and became media-ready. The loopback
RTSP consumer then passed for 10.372 seconds with H.264 1920x1080, AAC 16 kHz
mono, 144 video packets, and 125 audio packets. `767`, consumer, temporary
go2rtc, and transport cleanup completed, but terminal ingest finalization was
reported as `GO2RTC_INGEST_FAILED`. CR-4C therefore remains **LIVE UNPROVEN**.

Finalization now reports `http_response` for a normal HTTP response and
`peer_closed_after_terminal` only for exact `RemoteDisconnected` after the
terminal chunk was sent successfully. The latter is accepted only by the
top-level CR-4C gate after valid TS with positive published bytes, source PASS,
producer registration/media readiness, RTSP consumer PASS, `767`, and all
cleanup checks. Terminal send failure, timeout, reset/protocol failure, HTTP
rejection, and opening or mid-stream ingest failure remain fatal. No such
terminal outcome is accepted by CR-4B or production code.

The latest bounded attempt on 2026-09-07 used the explicit experimental
`--max-audio-validation-drops 12` override without changing the CR-4C default
of `3`. The source gate passed for 32.168 seconds with 10 I-frames, 509
P-frames, 519 reordered frames, 516 valid AAC frames, and 2 bounded drops.
The producer registered and became media-ready, and RTSP passed for 10.540
seconds with H.264 1920x1080, AAC 16 kHz mono, 160 video packets, and 125
audio packets. `767` and cleanup completed, but terminal HTTP finalization
reported `GO2RTC_INGEST_REJECTED`; CR-4C remains **LIVE UNPROVEN**.

The App-pinned go2rtc 1.9.14 MPEG-TS producer returns its read-loop error when
a finite request body reaches clean EOF, which the HTTP handler can expose as
status 500 with body `EOF`. After a successfully sent terminal chunk, the
research sink now classifies only exact HTTP 500 plus a small bounded body
that trims to exactly `EOF` as `go2rtc_eof_after_terminal`. The top-level gate
accepts that mode only with the unchanged source, valid/positive TS, producer,
RTSP, `767`, and cleanup proofs. Other 500 bodies, all other HTTP rejections,
and every socket, protocol, streaming, mux, or cleanup failure remain fatal.

## Running the safe checks

From a repository checkout:

```bash
python3 tools/pppp_cleanroom/probe_legacy_punch.py --self-test
python3 tools/pppp_cleanroom/probe_channel0_tnp.py --self-test
python3 tools/pppp_cleanroom/probe_media_tnp.py --self-test
python3 tools/pppp_cleanroom/probe_media_tnp.py --support-smoke-test
python3 tools/pppp_cleanroom/probe_sustained_mux.py --self-test
python3 tools/pppp_cleanroom/probe_sustained_mux.py --support-smoke-test
python3 tools/pppp_cleanroom/probe_sustained_mux.py --mux-support-smoke-test
python3 tools/pppp_cleanroom/probe_rtsp_publish.py --self-test
python3 tools/pppp_cleanroom/probe_rtsp_publish.py --support-smoke-test
python3 tools/pppp_cleanroom/probe_rtsp_publish.py --rtsp-support-smoke-test
python3 -m unittest tests.test_pppp_cleanroom tests.test_pppp_cr4 tests.test_pppp_cr4b tests.test_pppp_cr4c -v
```

For the offline CR-3 relocation check, keep its four clean-room files together:

```bash
cp tools/pppp_cleanroom/{probe_channel0_tnp.py,probe_legacy_punch.py,yi_pppp.py,yi_pppp_session.py} /tmp/
python3 /tmp/probe_channel0_tnp.py --self-test
```

The live CR-3 command additionally requires the matching Phase 3E helper from
this branch copied beside them as `/tmp/run_phase3e_tnp.py`, a selected camera
stable ID, one or more explicitly supplied YI server IPv4 addresses, and the
existing protected App environment file. The helper continues to obtain its
normal Python dependencies from `/opt/yi-home/app`; no installed production
file is overwritten. The probe never prints endpoint values or secret
connection material. A live run remains manual and is not performed by the
offline test suite.

For the CR-4 App-container relocation checks, preserve the nested layout so the
Phase 3E helper can resolve both a checkout and `/opt/yi-home/app`:

```bash
mkdir -p /tmp/yi-cr4/tools/pppp_cleanroom /tmp/yi-cr4/tools/phase3_pppp_probe
cp tools/pppp_cleanroom/{probe_media_tnp.py,probe_channel0_tnp.py,probe_legacy_punch.py,tnp_stream.py,yi_pppp.py,yi_pppp_session.py} /tmp/yi-cr4/tools/pppp_cleanroom/
cp yi_home/rootfs/opt/yi-home/app/tools/phase3_pppp_probe/run_phase3e_tnp.py /tmp/yi-cr4/tools/phase3_pppp_probe/
python3 /tmp/yi-cr4/tools/pppp_cleanroom/probe_media_tnp.py --self-test
python3 /tmp/yi-cr4/tools/pppp_cleanroom/probe_media_tnp.py --support-smoke-test
```

For CR-4B use the same structure under `/tmp/yi-cr4b`, adding
`probe_sustained_mux.py` and `mux_pipe.py`. Self-test and support/mux smoke modes
perform no cloud or device traffic; the mux smoke only checks executable
availability and command construction and starts no process.

For CR-4C preserve the same nested layout under `/tmp/yi-cr4c` and add
`probe_rtsp_publish.py` plus `rtsp_publish.py`. Its self-test and support smoke
start no process or socket; the separate RTSP-support smoke only checks local
binary availability plus safe config/command construction.

## Current progress

### PROVEN

- Packet codecs reject malformed lengths and round-trip all implemented shapes.
- State transitions reject unconfigured server sources, unoffered ready peers,
  and mismatched device IDs.
- The probe runs from checkout and from a relocated temporary directory.
- Selective ACK, retransmission, coalescing, ordering, duplicate suppression,
  and 16-bit wraparound pass sanitized offline tests.
- Session gating after keepalive-confirmed CR-2, partial stream reads, bounded
  retry failure, channel isolation, D2 non-mutation, payload-blind non-zero
  discard, and relocated CR-3 self-test behavior pass sanitized offline tests.
- Opt-in channels 1/2/3 have independent receive sequencing and ACK state,
  bounded buffering, duplicate suppression, wraparound ordering, and isolated
  TNP unit reconstruction across partial/coalesced reads in offline tests.
- Synthetic records pass the existing H264 decode/reorder and AAC decrypt/ADTS
  parsers; runner cleanup and relocated self-test/support loading also pass.
- No code path in the self-test imports cloud support or sends network, TNP, or
  media traffic.
- One owned `y291ga` / raw model `83` camera completed the full CR-2 live
  transport boundary: server rendezvous, legacy 20-byte punch, matching ready,
  LAN path selection, ALIVE/ALIVE_ACK confirmation, and CLOSE, with no TNP or
  media sent.
- The same owned path completed CR-3 reliable channel-0/TNP through the first
  valid `4882`, then `767` and close.
- The same owned path completed CR-4 with real H.264 I/P reorder and AAC parser
  acceptance through clean media channels, then `767` and close.
- The same owned path completed CR-4B with 33.124 seconds of sustained media,
  zero DRW retries, no observed D2, and 1,243,808 bytes of framed MPEG-TS
  validated as the expected H.264/AAC formats, then `767` and close.
- CR-4C loopback-only temporary config, complete-TS prebuffer/chunked ingest,
  producer readiness, bounded RTSP metadata/packet/span validation, cleanup,
  smoke isolation, and relocation pass offline tests.

### INFERRED

- D2 likely represents cumulative/next-expected state, but D1 alone may be
  sufficient for a short media validation.
- Retry/window/chunk/buffer/keepalive/timeout defaults and any new media timing
  assumptions are experimental until measured live.
- Temporary publication timing and process limits remain experimental until a
  manual CR-4C run.

### UNKNOWN

- Legacy-punch acceptance on other YI models/firmware and non-direct paths.
- Loss behavior and long-session D2 requirements on YI hardware.
- F2, relay, wakeup, and IPv6 paths.
- RTSP/go2rtc real-device parity, Home Assistant/Frigate parity, and production
  timing limits.
