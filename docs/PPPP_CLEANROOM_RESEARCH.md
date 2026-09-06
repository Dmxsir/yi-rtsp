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

### CR-3 — reliable channel 0 — OFFLINE RUNNER IMPLEMENTED, LIVE UNPROVEN

`tools/pppp_cleanroom/yi_pppp.py` implements tested D0 framing, variable D1 ACKs, D2 parsing, sequencing, selective acknowledgement, retransmission, receive ordering, duplicate suppression, wraparound and channel byte-stream reads.

`tools/pppp_cleanroom/yi_pppp_session.py` now connects that reliability layer to the unchanged CR-2 rendezvous/punch/ready/keepalive state machine. `tools/pppp_cleanroom/probe_channel0_tnp.py` is a separate manual runner that lazy-loads the Phase 3E TNP builders and validator, selects exactly one camera by stable ID through `yi_camera_manager`, ACKs and discards non-zero-channel DRW without inspecting its payload, and stops after the first valid expected response. Its self-test imports no cloud/runtime support and performs no network, TNP, or media I/O.

Offline tests prove the runner/session mechanics only. D2 remains observational and does not change reliable send state. The retransmission, window, chunk, keepalive, and timeout defaults are explicit experimental values pending live measurement. This remains a research gate and does not enable production media or change the default transport.

Success criterion:

```text
4881 -> 9029 -> 768 -> first valid 4882 response -> 767 -> close
```

using the existing Phase 3E TNP construction and validation rules.

### CR-4 — media channels

Feed channel data from the clean transport into the existing media relay.

Success criterion:

- valid H264 frames
- valid AAC frames where supported
- sustained stream equivalent to the vendor path

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

CR-2 is live-proven for one owned `y291ga` direct F1 path. The CR-3 implementation and sanitized offline tests are complete, but CR-3 remains live-unknown until the user manually runs the explicit probe: establish the same clean session, enable only reliable DRW channel 0, send `4881 -> 9029 -> 768`, verify the first valid `4882` response, attempt stop-live `767`, and close. Other models, F2, relay, wakeup, IPv6, long-session D2 behavior, media parity, and production timing limits also remain unknown. No RTSP/media path or production default is enabled by this work.
