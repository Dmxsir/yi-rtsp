# PPPP clean-room replacement research

Status: research only. This document does **not** change the production `YI RTSP` runtime.

The production App remains on the proven vendor-library path until an independent transport implementation reaches protocol parity on real YI hardware.

## Goal

Remove the runtime dependency on the user-supplied YI Home APK / `libPPPP_API.so` without changing the already-working Home Assistant, YI cloud, TNP, media, or RTSP layers.

Target architecture:

```text
YI cloud account/session
        |
        | DID + InitString + License/device key + camera password
        v
clean-room YI PPPP transport
        |
        | ordered/reliable PPPP channels
        v
existing YI TNP implementation
        |
        v
existing H264/AAC relay -> go2rtc/RTSP -> Home Assistant / Frigate
```

## Existing proprietary boundary

The current project already implements the layers above PPPP itself.

`tools/phase3_pppp_probe/run_phase3e_tnp.py` obtains the YI connection material from the cloud and builds the TNP v2 control units. The proven startup sequence is:

```text
4881 -> 9029 -> 768
```

with the first `4882` authentication response verified, and `767` used for stop-live.

The cloud path already provides:

- PPPP DID
- `InitString`
- YI `License`
- device-key component derived from the license
- camera password
- encryption capability flag
- wakeup capability flag

The proprietary library is therefore acting primarily as the PPPP transport abstraction: initialize, connect, check, read/write channel data, and close.

This is important: replacing PPPP does **not** require rewriting the account login, TNP authentication, media parsing, AAC/H264 handling, go2rtc publishing, Home Assistant API, or Frigate integration.

## Observed vendor API surface from our own tests

Previous oracle/runtime captures already reduce CR-0 substantially. One proven stream session used this sequence:

```text
PPPP_Initialize        -> 0
PPPP_Config_Debug      -> 1              # diagnostics only
PPPP_GetAPIVersion     -> 0xA2030401      # observed oracle build
PPPP_Connect           -> session 1       # flag 75 in that test
PPPP_Check             -> 0, mode 0
PPPP_Write             -> channel 0
PPPP_Read              -> channels 0, 2, 3
...
PPPP_Write             -> channel 0 / TNP stop-live 767
PPPP_Connect_Break     -> 0
PPPP_ForceClose        -> 0
```

The same capture proved:

- channel 0 carries TNP control/authentication;
- channel 2 and channel 3 carry media records;
- `PPPP_Read` is used as a bounded blocking read for an exact requested length;
- connection shutdown wakes blocked readers, which returned `-3014` in the captured run;
- the clean transport therefore needs explicit cancellation/close semantics, not only UDP packet parsing.

The current `0.2.0` private vendor library observed after Web-UI import is 243264 bytes with SHA-256:

```text
8c53f2ecc7ce6c362960af29a5d8de8396347a120dc10e88b24928437c4286eb
```

An older analysis APK contained a 243576-byte ARM64 `libPPPP_API.so`; do not assume its exact binary/API-version fingerprint is identical to the currently imported build. The API call pattern above is still directly relevant to the adapter contract.

## Evidence that YI PPPP is a CS2 fork

Wladimir Palant's PPPP protocol overview identifies a distinct **Yi Technology** PPPP variant and concludes that YI appears to have licensed/forked the original CS2 Network implementation.

Relevant observations from that research:

- CS2 and YI expose very similar public API concepts (`PPPP_Initialize`, `PPPP_ConnectByServer`, etc.).
- YI retains the CS2 init-string concept and decoder design but uses a different lookup table.
- YI made substantial wire-level changes; generic CS2/iLnk clients are not drop-in compatible.
- YI added/changed server, punch, relay and wakeup messages.
- YI introduced an `F2` extended message header, while legacy message forms are still accepted in some paths.
- YI's application-level protocol is TNP; PPPP is the transport below it.

Reference:

- https://palant.info/2025/11/05/an-overview-of-the-pppp-protocol-for-iot-cameras/

## Device-side YI TNP source reference

`frankzhangshcn/p2p_tnp` contains a large device-side YI TNP implementation which calls the PPPP API directly.

It confirms the same boundary observed in our project:

- `PPPP_Initialize(...)`
- `PPPP_Listen(...)`
- `PPPP_Check(...)`
- `PPPP_Read(...)`
- `PPPP_Write(...)`
- PPPP DRW reliability modes
- channel 0 for IO control and separate audio/video channels

Repository:

- https://github.com/frankzhangshcn/p2p_tnp

**License/provenance warning:** GitHub currently reports no repository license and there is no LICENSE file in the tree. Treat this code as a protocol/reference artifact only. Do not copy source from it into this project.

## Open / reusable projects

### 1. `elastic/camera-hacks` — MIT

- https://github.com/elastic/camera-hacks
- https://github.com/elastic/camera-hacks/blob/main/p2p/p2p_client.py

Value:

- standalone Python implementation of a PPPP-like server rendezvous / UDP hole-punch flow
- shows `HELLO -> P2P request -> PUNCH_TO -> PUNCH_PKT -> ready/alive -> DRW`
- useful for remote-server state-machine structure

Limitation:

- targets AJCloud, not YI
- server port, DID encoding, message sizes and opcodes differ from the YI fork

Use: architecture and state-machine reference, not packet constants.

### 2. `devbis/aiopppp` — Apache-2.0

- https://github.com/devbis/aiopppp

Value:

- pure Python asyncio PPPP transport
- packet framing, UDP session lifecycle, DRW sequencing/ACK handling, keepalive handling
- useful implementation pattern for a native-library-free transport

Limitation:

- targets iLnk/SHIX-family cameras rather than YI
- application protocol and handshake are not YI-compatible

Use: reusable design patterns and selected generic helpers where license-compatible; YI wire behavior must be implemented independently.

### 3. `nosoop-onlyslop/p2pcam` — Unlicense

- https://github.com/nosoop-onlyslop/p2pcam

Value:

- pure Go implementation replacing `libPPPP_API.so` for a real Uniden/CS2-family device
- owner reports live-device verification
- implements LAN discovery, punch/ready session flow, DRW transport and RTSP without the vendor library

Limitation:

- older CS2/Uniden variant rather than YI
- primarily direct LAN, not the YI server/wakeup path
- repository author explicitly notes much of the implementation was AI-generated and only functionally spot-checked

Use: feasibility evidence only. Although the output repository is Unlicense, its own source comments describe proprietary decompilation as an implementation source. Do not copy or derive this project's code into the YI clean-room implementation.

### 4. `magicus/pppp-dissector` — MIT

- https://github.com/magicus/pppp-dissector

Value:

- documented base PPPP framing and Wireshark dissector
- useful for validating packet types, lengths, discovery and DRW behavior from captures

Limitation:

- generic/older PPPP documentation; YI diverges substantially

Use: capture tooling and baseline protocol vocabulary.

## Reference-only reverse engineering material

### `xen0bit/libPPCS_API`

- https://github.com/xen0bit/libPPCS_API

Description: decompilation of a CS2 `libPPCS_API` implementation. It exposes a very broad API surface including connect, connect-by-server, check, read/write, force-close, network detect and license helpers.

**No license is declared.** Do not copy implementation code. It may be used only to help identify public API concepts and protocol behavior that must then be independently implemented and validated.

### `frankzhangshcn/p2p_tnp`

As above: valuable YI/TNP reference but no declared license. Do not copy.

## Clean-room rules for this branch

1. No YI APK, YI binary library, decompiled YI source, credentials, DID, password, init string or license value may be committed.
2. Do not copy code from repositories without a compatible explicit license.
3. Protocol constants/packet shapes must be backed by public documentation or independently observed traffic.
4. Real-device tests must use only cameras/accounts owned and authorized by the tester.
5. Experimental transport stays opt-in and out of production until it matches the vendor transport on the required flows.
6. The stable `main` branch remains on the proven `0.2.0` vendor-runtime path during research.

## Proposed internal transport contract

The clean implementation should initially present only the semantics the existing YI code actually needs:

```text
initialize(init_string)
connect(did, device_key/license metadata, wakeup)
check(session)
read(session, channel, max_bytes, timeout)
write(session, channel, bytes)
connect_break()
force_close(session)
deinitialize()
```

`config_debug()` and `get_api_version()` are diagnostic conveniences rather than requirements for media operation.

Implementation details do not need to mimic the vendor library ABI. The adapter only needs to provide equivalent behavior to our Python/native relay boundary.

## Milestones

### CR-0 — fingerprint the exact vendor surface — PARTIALLY COMPLETE

Known from existing captures:

- current library SHA-256 and size recorded above;
- observed API version for the earlier oracle build: `0xA2030401`;
- proven runtime call surface: initialize, connect, check, read, write, connect-break, force-close;
- diagnostic calls: config-debug and get-API-version.

Remaining:

- ELF build ID of the current 243264-byte build, if present;
- complete exported `PPPP_*` symbol list from that current build;
- confirm whether the production native worker calls any additional symbol not visible in oracle logs.

Deliverable: smallest possible compatibility contract.

### CR-1 — passive YI packet capture / classification — COMPLETE FOR DIRECT F1

Capture one normal connection made by the current vendor runtime and classify only transport packets:

- server addresses from decoded/observed init-string behavior
- local UDP port
- `HELLO`
- YI server request / response
- punch messages
- ready/alive messages
- DRW + DRW ACK
- wakeup messages when applicable

Do not log TNP credentials or media payloads.

Deliverable: packet timeline with type/size/direction and endpoint only.

### CR-2 — direct-LAN session probe — LIVE PROVEN FOR ONE `y291ga` PATH

`tools/pppp_cleanroom/probe_legacy_punch.py` performs only:

```text
YI server rendezvous
-> legacy 20-byte punch to server-supplied candidates
-> matching ready
-> selected-peer keepalive confirmation
-> close
```

A controlled live run on 2026-09-06 succeeded on one owned `y291ga` / raw model `83` camera without `libPPPP_API.so` performing the transport handshake. The probe received three HELLO acknowledgements and three P2P request acknowledgements, received one LAN and one WAN candidate, established `P2P_RDY` through the legacy 20-byte `PUNCH_PKT`, selected the LAN candidate, observed both `ALIVE` and `ALIVE_ACK`, reported `transport_result=PASS`, and sent `CLOSE`.

No DRW, TNP command, audio, video, RTSP, or production transport change was part of that experiment.

Sanitized report: [`PPPP_CR2_LIVE_01.md`](PPPP_CR2_LIVE_01.md).

What is proven is intentionally narrow: a direct F1 session on this owned `y291ga` path accepts the legacy punch form. Other models, relay/F2/wakeup paths, and broader compatibility remain unproven.

### CR-3 — reliable channel 0 — LIVE PROVEN FOR ONE `y291ga` PATH

`tools/pppp_cleanroom/yi_pppp.py` implements tested D0 framing, variable D1 ACKs, D2 parsing, sequencing, selective acknowledgement, retransmission, receive ordering, duplicate suppression, wraparound and channel byte-stream reads.

`tools/pppp_cleanroom/yi_pppp_session.py` now connects that reliability layer to the unchanged CR-2 rendezvous/punch/ready/keepalive state machine. `tools/pppp_cleanroom/probe_channel0_tnp.py` is a separate manual runner that lazy-loads the Phase 3E TNP builders and validator, selects exactly one camera by stable ID through `yi_camera_manager`, ACKs and discards non-zero-channel DRW without inspecting its payload, and stops after the first valid expected response. Its self-test imports no cloud/runtime support and performs no network, TNP, or media I/O.

A controlled live run on 2026-09-06 proved this boundary through the first valid `4882` response on the same owned `y291ga` / raw model `83` direct-LAN path used for CR-2. The camera selectively acknowledged the single 164-byte startup DRW packet; the clean transport reconstructed ordered channel-0 bytes, the existing validator accepted `4882`, and the probe sent `767` before closing. Sanitized report: [`PPPP_CR3_LIVE_01.md`](PPPP_CR3_LIVE_01.md).

D2 remains observational and does not change reliable send state. Sustained operation under loss and the retransmission, window, chunk, keepalive, buffer, and timeout defaults remain unproven production values. This research gate does not enable production media or change the default transport.

Success criterion:

```text
4881 -> 9029 -> 768 -> first valid 4882 response -> 767 -> close
```

using the existing Phase 3E TNP construction and validation rules.

### CR-4 — media channels — LIVE PROVEN FOR ONE `y291ga` PATH

The clean research session can now opt in to independent reliable byte streams for channels 1, 2, and 3 after the proven channel-0 startup boundary. Each channel has independent sequencing, selective ACK handling, duplicate suppression, wraparound ordering, and a configurable byte bound. CR-3 behavior is unchanged by default: non-zero DRW is still acknowledged and discarded until the CR-4 runner explicitly enables media reads. D2 remains observational.

`tools/pppp_cleanroom/tnp_stream.py` reconstructs complete, size-bounded TNP units across partial/coalesced channel reads while retaining incomplete header/body state across timeouts. `tools/pppp_cleanroom/probe_media_tnp.py` reuses the existing video decode/reorder and audio decrypt/ADTS parsers for a short, bounded, manual-only validation. It does not invoke FFmpeg, publish RTSP, write media, or change production startup/transport selection. Its isolated self-test imports no App/cloud/media runtime support; the separate support-loader smoke test imports those helpers without cloud, device, TNP, or media traffic.

Offline tests establish channel isolation/order/ACK/duplicate/wrap/bounds, TNP stream reconstruction, synthetic H264/AAC parser integration, STOP/CLOSE cleanup, and nested relocation/support loading.

A controlled live run on 2026-09-06 then proved real channel-2 H.264 I-frame, channel-3 H.264 P-frame, sequence-reordered output, and channel-1 AAC through the clean transport and existing parsers on the same owned direct-LAN path. It sent `767`, closed cleanly, logged no media payload, and invoked no FFmpeg or publication path. Sanitized report: [`PPPP_CR4_LIVE_01.md`](PPPP_CR4_LIVE_01.md).

Success criterion:

- valid channel-2 H264 I-frame and channel-3 H264 P-frame
- valid channel-1 AAC
- existing sequence reorder accepts the video frames

### CR-4B — sustained media and relay/mux boundary — LIVE PROVEN FOR ONE `y291ga` PATH

`tools/pppp_cleanroom/probe_sustained_mux.py` extends the proven parser path without changing the CR-4 runner. It requires independently sustained I-frame, P-frame, and AAC activity spans, conservative configurable counts, continued bounded channel/TNP/pre-mux state, and the existing sequence reorder behavior. It starts the mux incrementally once the first accepted reordered video and validated AAC are available rather than retaining the full run in memory.

`tools/pppp_cleanroom/mux_pipe.py` applies the existing relay's H.264/AAC copy-mux and 90 kHz SETTS expressions, continuously drains FFmpeg MPEG-TS stdout into ffprobe stdin, validates TS packet framing plus H.264 1920x1080 and AAC 16 kHz mono metadata, and bounds process waits and pump chunks. It persists and prints no media bytes. Offline tests cover duration/count gating, parser/reorder feed order, A/V offset calculation, queue bounds, starvation and transport failure propagation, TS pumping, validator EPIPE, invalid streams, early exits, timeouts, cleanup, and nested isolated loading.

On 2026-09-07 a bounded manual run on the same owned `y291ga` / raw model `83` direct-LAN path proved this gate. The safe metrics were:

```text
media_active_seconds=33.124
video_i_frames=10
video_p_frames=531
video_reordered_frames=541
audio_frames=540
channel1_tnp_units=540
channel2_tnp_units=10
channel3_tnp_units=531
aac_sample_rate=16000
aac_channels=1
initial_av_delta_ms=35
drw_retries=0
d2_observed=0
mpegts_bytes_observed=1243808
ffprobe_video_codec=h264
ffprobe_video_size=1920x1080
ffprobe_audio_codec=aac
ffprobe_audio_sample_rate=16000
ffprobe_audio_channels=1
```

The source duration gate, FFmpeg copy-mux, MPEG-TS framing, ffprobe metadata validation, `767`, and clean close all passed. This is evidence for that one run only; the duration/count/buffer/process defaults, loss behavior, and longer-session behavior remain experimental. Sanitized report: [`PPPP_CR4B_LIVE_01.md`](PPPP_CR4B_LIVE_01.md).

### CR-4C — temporary clean RTSP publication — OFFLINE IMPLEMENTED / LIVE UNPROVEN

`tools/pppp_cleanroom/probe_rtsp_publish.py` reuses the CR-4B collector, parsers, reorder logic, FFmpeg copy-mux command, and timestamp expressions. It owns a separate temporary go2rtc child under `/tmp/yi-cr4c`, with fixed synthetic stream identity `yi_cr4c_probe`, loopback-only API/RTSP listeners, WebRTC disabled, and non-production default ports `11984` and `18554`. It rejects production ports `1984` and `8554` and hard-fails if either selected loopback port is already occupied.

The research-only MPEG-TS pump obtains complete TS packets before opening the HTTP chunked POST, uses the same `/api/stream.ts?dst=...` headers as the production publisher, retains only a bounded partial TS packet, and writes no media to disk or stdout. The coordinator requires an exact one-stream registry and a media-ready MPEG-TS producer before starting a bounded loopback RTSP ffprobe consumer. PASS additionally requires H.264 1920x1080, AAC 16 kHz mono, positive audio/video packet counts, and the configured minimum consumer interval while the CR-4B source duration gate remains active.

Offline tests cover port/config isolation, startup/early-exit/timeout and terminate/kill paths, temporary cleanup, ingest prebuffer/chunk framing/errors, producer readiness, RTSP format/packet/span failures, orchestration cleanup, and nested relocation. Self and smoke modes start no process or socket. No normal App publisher, lifecycle, registry, discovery, Home Assistant, Frigate, or external port mapping is called or changed.

The first bounded manual CR-4C attempt on 2026-09-07 reached temporary go2rtc readiness, clean CR-3 control, media enablement, and real MPEG-TS producer registration/media readiness. It then failed with `AUDIO_PARSE_INVALID`; `767`, the RTSP consumer, temporary go2rtc, and the clean transport all stopped successfully. This does not prove the full RTSP consumer gate, so CR-4C remains **LIVE UNPROVEN**.

The production relay already drops only isolated `AudioUnitValidationError` records while leaving session/config and other parser failures fatal. The shared research collector now keeps CR-4B strict by default with a zero drop allowance. CR-4C explicitly uses the configurable absolute `--max-audio-validation-drops` limit, with an experimental default of `3` for the next manual retry. Dropped records do not change valid audio counts/timing/format/timestamp state or enter the mux; exceeding the limit fails as `AUDIO_VALIDATION_DROP_LIMIT`. This bound is a conservative research setting, not a protocol invariant.

### CR-5 — YI server rendezvous

Implement YI-specific server connection using cloud-supplied DID + InitString + license/device-key information.

Success criterion: connect when direct LAN discovery is unavailable while preserving the existing cloud/TNP pipeline.

### CR-6 — wakeup / relay fallbacks

Add only the paths actually required by validated camera models.

### CR-7 — optional App backend

Add a development-only selector:

```text
vendor_pppp | clean_pppp
```

Run parity tests before any production default changes.

### CR-8 — remove APK dependency

Only after clean PPPP passes repeated real-world parity tests should the App stop requiring the YI APK. Keep the vendor path available for at least one transition release if legally/technically appropriate.

## Current specification and immediate next experiment

The evidence audit, packet contract, state machine, implementation limits and PROVEN / INFERRED / UNKNOWN ledger are in [`PPPP_CLEANROOM_TRANSPORT.md`](PPPP_CLEANROOM_TRANSPORT.md).

CR-2 direct F1, CR-3 reliable channel-0/TNP through the first valid `4882`, CR-4 real H.264 I/P plus AAC parsing, and CR-4B sustained clean media plus pipe-only MPEG-TS/ffprobe validation are live-proven on one owned `y291ga` direct-LAN path. CR-4C temporary loopback go2rtc publication and RTSP validation are offline implemented but remain live unproven. Sustained loss behavior, other models, F2, relay, wakeup, IPv6, long-session D2 behavior, RTSP/go2rtc real-device parity, Home Assistant/Frigate parity, and production timing limits remain unknown. No production publication path or transport default is enabled by this work.
