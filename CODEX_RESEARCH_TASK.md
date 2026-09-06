# Codex Research Task — CR-4 clean media-channel validation

## Scope

Work only in repository `Dmxsir/yi-rtsp` on branch `pppp-cleanroom`.

Do **not** modify, merge, rebase, tag, release, or otherwise change `main` or the installed production `0.2.0` vendor-runtime path. Keep PR #2 open and draft.

The goal of this task is the next clean-room gate after the successful CR-3 live proof: extend the clean PPPP session from reliable channel 0 into bounded media channels 1/2/3, reuse the project's existing YI TNP media parsers, and prepare a short manual live probe that proves valid H.264 and AAC media records without publishing RTSP and without using `libPPPP_API.so` for PPPP transport/channel I/O.

Do **not** perform an unattended live-device experiment. Implement and test CR-4 offline, push only the research branch, then stop with secret-safe manual commands for the user to run inside the Home Assistant App container.

## Read first

Read these files before changing code:

- `docs/PPPP_CLEANROOM_RESEARCH.md`
- `docs/PPPP_CLEANROOM_TRANSPORT.md`
- `docs/PPPP_CR1_CAPTURE_01.md`
- `docs/PPPP_CR1_CAPTURE_02_STARTUP.md`
- `docs/PPPP_CR2_LIVE_01.md`
- `docs/PPPP_CR3_LIVE_01.md`
- `tools/pppp_cleanroom/yi_pppp.py`
- `tools/pppp_cleanroom/yi_pppp_session.py`
- `tools/pppp_cleanroom/probe_legacy_punch.py`
- `tools/pppp_cleanroom/probe_channel0_tnp.py`
- `tests/test_pppp_cleanroom.py`
- `yi_home/rootfs/opt/yi-home/app/tools/phase3_pppp_probe/run_phase3e_tnp.py`
- `yi_home/rootfs/opt/yi-home/app/yi_live_relay.py`
- `yi_home/rootfs/opt/yi-home/app/yi_native_av_relay.py`
- `yi_home/rootfs/opt/yi-home/app/yi_tnp_oracle.py`
- `yi_home/rootfs/opt/yi-home/app/yi_camera_manager.py`

Inspect the existing media parsing path before implementing anything new. In particular, reuse the existing semantics around:

- `yi_live_relay._decode_video_unit(...)`;
- `yi_live_relay.SequenceReorderBuffer` where useful;
- `yi_native_av_relay.decrypt_audio_unit(...)`;
- the existing TNP v2 unit framing/size checks;
- the existing H.264 codec/framing/NAL validation;
- the existing AAC decrypt + ADTS validation.

Do not independently redesign media decryption or TNP media parsing if the repository already has the required logic.

## Current proven state

Treat the following as established evidence.

### CR-1 — direct F1 and DRW capture

Observed direct-session flow:

```text
DEV_ONLINE
-> HELLO / HELLO_ACK
-> P2P_REQ / P2P_REQ_ACK
-> PUNCH_TO
-> PUNCH_PKT
-> P2P_RDY
-> ALIVE / ALIVE_ACK
-> DRW
```

Observed media/reliability facts:

- outer `F1 D0` carries inner `D1 <channel> <seq:u16-be> <stream bytes>`;
- outer `F1 D1` carries selective ACK lists;
- outer `F1 D2` has the observed four-byte form `D2 <channel> <value:u16-be>`;
- D2 meaning remains inferred and must not be silently treated as cumulative ACK state;
- channel 0 is TNP control;
- channel 1 is audio;
- channel 2 is realtime I-frame traffic;
- channel 3 is realtime P-frame traffic;
- each channel is an ordered byte stream; application/TNP record boundaries are not guaranteed to match DRW datagram boundaries;
- duplicate DRW occurs and must be ACKed without duplicate byte-stream delivery;
- the three startup writes `56 + 52 + 56` were observed as one 164-byte channel-0 DRW payload.

### CR-2 — LIVE PROVEN

One owned YI `y291ga` / raw model `83` direct-LAN path successfully completed clean-room rendezvous, legacy 20-byte punch, matching `P2P_RDY`, selected-peer `ALIVE` / `ALIVE_ACK`, and close without the proprietary PPPP library performing the transport handshake.

### CR-3 — LIVE PROVEN

A controlled live run on 2026-09-06 then proved reliable channel-0 TNP over the clean transport on the same owned direct-LAN `y291ga` path.

The live output established:

```text
cr2_transport_established=true
selected_path=lan
channel0_drw_started=true
startup_tnp_bytes_sent=164
startup_drw_packets=1
startup_drw_acked=true
first_4882_valid=true
stop_live_767_sent=true
transport_closed=true
cr3_result=PASS
```

Therefore the following are now live-proven on that path:

- clean PPPP session establishment;
- reliable DRW channel 0;
- the existing TNP startup `4881 -> 9029 -> 768` over the clean transport;
- selective D1 ACK of the startup DRW;
- ordered channel-0 reconstruction of the first response;
- valid expected `4882`, request number `1`, authentication result `0`;
- `STOP_LIVE 767` and clean close.

Do not regress or weaken these proven gates.

## CR-4 goal

Implement a **research-only**, short, bounded clean media probe with this intended sequence:

```text
clean CR-2 session
-> reliable channel 0
-> 4881 -> 9029 -> 768
-> valid first 4882
-> enable bounded reliable receive streams for channels 1/2/3
-> reconstruct complete TNP media units from each channel byte stream
-> validate/decode H.264 using the existing parser
-> validate/decrypt AAC using the existing parser where present
-> collect only sanitized counters/metadata
-> STOP_LIVE 767
-> CLOSE
```

The live proof must **not** start RTSP, go2rtc, Frigate, FFmpeg publication, or a production backend selector.

The probe may hold media bytes transiently in memory only as required for parsing. It must not write media to disk, stdout, logs, GitHub, fixtures, or artifacts.

## Required implementation work

### 1. Preserve CR-2 / CR-3 behavior

Do not rewrite the working handshake or channel-0 path unnecessarily.

The CR-3 runner and tests must continue to pass unchanged or with strictly compatible behavior.

### 2. Extend the clean transport to channels 1/2/3

Extend the research session abstraction so each media channel has independent reliability state.

Requirements:

- one `ReliableChannel` state per active PPPP channel;
- independent uint16 sequence spaces;
- D1 selective ACK handling per channel;
- retransmission only for channels on which the client actually sends reliable data;
- inbound out-of-order buffering and duplicate suppression per channel;
- duplicate frames are ACKed but never delivered twice;
- channel isolation: bytes from 1/2/3 can never contaminate channel 0 or each other;
- D2 remains parsed/observational only unless new isolated live evidence proves required semantics;
- keepalive and remote-close behavior remain active during the bounded media probe;
- memory/buffer limits must be explicit and bounded.

The existing CR-3 behavior that payload-blindly ACKs/discards non-zero DRW should evolve into buffering for CR-4 only when the CR-4 runner explicitly enables those channels. CR-3 must still be able to discard non-zero traffic safely without changing its success boundary.

Prefer an explicit opt-in such as enabling readable media channels on the session rather than making every CR-3 session retain media by default.

### 3. Expose byte-stream reads for media channels

Provide a bounded equivalent of:

```text
read_channel(1, ...)
read_channel(2, ...)
read_channel(3, ...)
```

Media reads must preserve byte-stream semantics and must not assume one DRW equals one TNP media unit.

### 4. Reconstruct complete TNP media units

Add a small research-only unit reader that reconstructs one TNP v2 unit from a selected channel byte stream.

Ground its framing in the repository's existing media/parser code. Do not invent a new record format.

At minimum:

- read the fixed TNP header first;
- use the existing big-endian declared body size;
- enforce a strict bounded maximum consistent with the existing relay (`MAX_RECORD` or a smaller justified research limit);
- reject impossible/truncated/malformed sizes with a stage-specific failure;
- read exactly the declared body length from that channel's byte stream;
- return one complete raw TNP unit to the existing parser;
- never print the raw unit.

The reader must work when:

- one TNP unit spans multiple DRW packets;
- multiple TNP units arrive in one contiguous channel stream;
- a `read_channel()` call ends in the middle of a TNP header or body.

### 5. Reuse existing video parser

For channel 2/3 media units, reuse the existing YI video parser/decrypt path rather than duplicating it.

The live probe should validate only sanitized facts such as:

- parser accepted the record;
- codec is H.264 / codec id expected by the existing implementation;
- framing is recognized by the existing NAL analyzer;
- frame type (`I` / `P`);
- NAL types as small numeric metadata if already considered safe;
- sequence/timestamp counters or deltas if useful;
- output payload length only, not payload bytes.

For the tested `y291ga` path, a strong CR-4 video success boundary should include:

- at least one valid channel-2 I-frame;
- at least one valid channel-3 P-frame, unless live evidence shows startup legitimately does not provide one within the bounded window;
- at least one frame accepted through the existing sequence reorder logic if that logic is used by the production relay.

If a P-frame is not required for the first narrow proof after inspecting existing semantics, document the reason clearly and use a stricter alternative that still proves real H.264, not merely a header.

### 6. Reuse existing audio parser

For channel 1, reuse `decrypt_audio_unit(...)` or factor the same already-proven helper into a neutral reusable module without changing production behavior.

For the tested `y291ga` path, validate sanitized audio facts only:

- TNP audio unit accepted;
- existing AES handling succeeds when encryption is enabled;
- ADTS header is valid;
- sample rate / channel count / AAC object type are reported as sanitized metadata;
- payload length may be counted but payload bytes must never be logged.

Because the current proven production path is H.264 + AAC, make at least one valid AAC unit part of CR-4 PASS for this `y291ga` live test unless repository evidence establishes a legitimate camera state in which audio is optional. If optionality is necessary, distinguish `video_result=PASS` from `audio_result=NOT_OBSERVED` and do not claim full H264+AAC parity.

### 7. No FFmpeg / RTSP publication in CR-4

Do not invoke FFmpeg, ffprobe, go2rtc, RTSP, Frigate, or the normal production stream publication path in the CR-4 live probe.

CR-4 is parser/media-validation only.

Do not save a `.h264`, `.aac`, `.ts`, `.mp4`, `.pcap`, or any other media/capture file.

### 8. Short bounded live runner

Create a separate explicit research tool, for example:

```text
tools/pppp_cleanroom/probe_media_tnp.py
```

The exact name may differ, but do not overload the CR-3 runner.

The runner should:

- select exactly one camera by secret-safe stable ID via `yi_camera_manager`;
- take one or more explicit YI rendezvous server IPv4 endpoints;
- use `/data/yi.env` by default or explicit `--env-file`;
- establish the proven CR-3 control path first;
- only after valid `4882`, enable/buffer channels 1/2/3;
- collect a bounded number of records or run for a short explicit timeout;
- stop as soon as the CR-4 proof boundary is met;
- send `767` in `finally` whenever startup was sent and the session is still usable;
- close cleanly;
- never fall back to the vendor PPPP library.

Use explicit limits such as maximum duration, maximum video records, maximum audio records, maximum bytes buffered per channel, and maximum TNP record size. Defaults are experimental and must be documented as such.

### 9. Relocated App-container execution

The CR-4 runner must work when its research files are copied under a nested temporary directory such as:

```text
/tmp/yi-cr4/tools/pppp_cleanroom/
```

We already observed that flat `/tmp/run_phase3e_tnp.py` is fragile because `run_phase3e_tnp.py` currently derives `ROOT` from fixed `parents[2]` when imported from a relocated path.

Address this deliberately:

- either make the Phase 3E helper's source-root discovery relocation-safe while preserving existing production behavior; or
- design the CR-4 support loader so the branch helper can be imported from a safe nested path without relying on an invalid parent depth.

Add an offline test for the **actual live-support import path**, not only `--self-test`.

Do not overwrite installed `/opt/yi-home/app` production files during manual tests.

### 10. Self-test must stay offline

The CR-4 tool must have `--self-test` or equivalent.

Self-test must perform:

```text
cloud_used=false
network_used=false
tnp_sent=false
media_requested=false
runtime_support_imported=false
```

It may use synthetic/fake channel bytes and fake parser callbacks, but no real captured media, credentials, endpoint values, or proprietary runtime.

## Media/parser architecture preference

Prefer small reusable boundaries instead of copying the production relay wholesale.

A reasonable design is:

```text
CleanPpppSession
  channel0 -> existing CR-3 TNP control
  channel1 -> bounded byte stream -> TNP unit reader -> existing AAC parser
  channel2 -> bounded byte stream -> TNP unit reader -> existing H264 parser
  channel3 -> bounded byte stream -> TNP unit reader -> existing H264 parser
```

If existing parser functions are too tightly coupled to the production relay, extract only the smallest neutral parsing helpers into an App module and have **both** production relay and CR-4 import that helper. Any such refactor must have tests proving production parser behavior is unchanged.

Do not copy/decompile proprietary media implementation code.

## Security / privacy rules

Never commit, print, save, attach, or include in fixtures:

- YI APKs;
- `libPPPP_API.so` or proprietary binary bytes;
- pcap files;
- account credentials;
- UID / DID;
- InitString;
- License or device key;
- camera password;
- login token / token secret;
- raw TNP authentication material;
- raw DRW payloads;
- raw H.264 NAL payloads;
- raw AAC payloads;
- media files;
- real endpoint addresses;
- real stable IDs.

Do not print hashes of secret/auth material or full media payloads as a workaround.

Safe diagnostics may include:

- stage booleans;
- model family / raw model number;
- `lan` / `wan` selected path class;
- channel numbers;
- DRW packet/ACK/retry counts;
- TNP record counts;
- byte counts;
- frame types;
- H.264 NAL type numbers;
- AAC sample rate/channel count/object type;
- sequence/timestamp deltas;
- bounded elapsed timings;
- parser pass/fail categories.

## Clean-room / licensing rules

- Protocol constants/shapes must come from independently observed project evidence or compatible public documentation.
- Do not copy implementation code from repositories without a compatible explicit license.
- `frankzhangshcn/p2p_tnp` and `xen0bit/libPPCS_API` remain reference-only.
- `nosoop-onlyslop/p2pcam` remains feasibility/reference evidence only because its provenance includes proprietary decompilation; do not copy or derive from it.
- Existing repository TNP/media parser code may of course be reused/refactored inside this repository.

## Offline tests required

Extend focused tests to cover at least the following.

### Transport/channel tests

- CR-3 channel-0 behavior still passes;
- media channels are not readable before explicit CR-4 enablement;
- independent channel 1/2/3 sequence spaces;
- D1 ACK per channel;
- out-of-order receive ordering per channel;
- duplicate suppression with duplicate ACK;
- uint16 sequence wraparound per channel;
- channel isolation;
- malformed D0/D1/D2 safe handling;
- D2 observation does not mutate reliable state;
- bounded media buffering / overflow behavior;
- remote close during media collection produces a stage-specific failure;
- keepalive continues during bounded media reads.

### TNP unit-stream tests

Using synthetic bytes only:

- complete unit in one stream chunk;
- header split across reads;
- body split across reads;
- two units coalesced in one channel stream;
- declared length too small / too large;
- truncated unit timeout;
- channel 1/2/3 streams cannot cross-contaminate.

### Parser integration tests

Do not use real captured media.

Use synthetic minimal valid inputs where practical, or mock only at the external parser boundary while separately retaining the existing production parser tests.

Cover:

- valid video parser result increments sanitized video counters;
- invalid video unit produces a stage-specific parser failure/drop policy;
- valid audio parser result increments sanitized audio counters;
- invalid audio unit cannot be mistaken for PASS;
- sequence reorder logic handles interleaved channel 2/3 frames as intended;
- raw payload bytes never appear in diagnostic output.

### Runner tests

- CR-4 success transcript reaches the full offline success boundary with fake session/parser objects;
- STOP 767 is attempted on success;
- STOP 767 is attempted in failure paths after startup when possible;
- clean close occurs in all paths;
- no vendor PPPP fallback exists;
- self-test from repository checkout;
- self-test from relocated nested temporary directory;
- actual Phase 3E/media support loader import works from that relocated nested directory;
- self-test imports no cloud/runtime/media support and performs no network I/O.

Do not require a live camera in CI.

## Live CR-4 success criteria

A live run on an owned camera is `CR4 PASS` only after the existing CR-3 boundary has succeeded **and** real media records have passed the existing parsers.

For the first tested `y291ga` path, target output should be equivalent to:

```text
cr3_control_result=PASS
media_channels_enabled=true

channel2_tnp_units_valid=<positive count>
channel3_tnp_units_valid=<positive count>
h264_i_frame_valid=true
h264_p_frame_valid=true
h264_framing_valid=true
h264_frames_reordered=<positive count>

channel1_tnp_units_valid=<positive count>
aac_valid=true
aac_sample_rate=<sanitized value>
aac_channels=<sanitized value>

stop_live_767_sent=true
transport_closed=true
cr4_result=PASS
```

Exact names may differ, but the result must clearly distinguish:

- transport/control success;
- DRW/media stream success;
- H.264 parser success;
- AAC parser success;
- stop/close success.

Do **not** claim CR-4 PASS merely because bytes arrived on channels 1/2/3.

Do **not** claim H.264 PASS merely because a codec/header field resembles H.264. The existing video parser/NAL analyzer must accept real media payload.

Do **not** claim AAC PASS without the existing audio decrypt/ADTS validator accepting a real unit.

Useful stage-specific failures include, for example:

```text
MEDIA_CHANNEL_TIMEOUT
MEDIA_BUFFER_LIMIT
TNP_MEDIA_LENGTH_INVALID
VIDEO_TNP_INVALID
H264_PARSE_INVALID
AUDIO_TNP_INVALID
AAC_PARSE_INVALID
MEDIA_RETRY_LIMIT
REMOTE_CLOSE
STOP_LIVE_FAILED
```

Do not expose raw payload to diagnose failure.

## Live execution policy

Do **not** run the live CR-4 experiment automatically from Codex.

At the end of this task:

1. ensure all offline tests pass;
2. run normal repository validation/CI as applicable;
3. push only to `pppp-cleanroom`;
4. keep PR #2 open and draft;
5. provide the exact branch commit SHA;
6. confirm `main` SHA remains unchanged;
7. provide the precise files changed;
8. state whether any production file/runtime behavior changed;
9. provide a secret-safe manual copy command using a nested temp path such as `/tmp/yi-cr4/tools/pppp_cleanroom`;
10. provide the manual CR-4 self-test command first;
11. provide a separate support-loader smoke test that performs no cloud/device traffic;
12. provide the manual live command template using `<CAMERA_STABLE_ID>` and placeholder YI server addresses;
13. stop and wait for the user's live output.

The user will run the live probe manually. Codex must perform **no live device traffic**.

## Documentation updates

Update research/specification documentation only for facts established by implementation/offline tests.

CR-3 must now be recorded as live-proven based on `docs/PPPP_CR3_LIVE_01.md`.

Before the user's CR-4 live run, label CR-4 as:

```text
OFFLINE IMPLEMENTED / LIVE UNPROVEN
```

Do not create a `CR4_LIVE` PASS document or mark media live-proven from offline tests alone.

Keep the evidence ledger explicit:

### PROVEN

- CR-2 direct F1 live on the tested owned `y291ga` path;
- CR-3 reliable channel-0/TNP through valid `4882` live on that path;
- exact offline CR-4 mechanics that tests truly establish.

### INFERRED

- D2 cumulative/next-expected meaning;
- retry/window/chunk/buffer/timeout defaults until measured live;
- any media timing assumption not already enforced by existing parser evidence.

### UNKNOWN until manual CR-4 run

- clean media channel parsing on real hardware;
- sustained media under loss;
- long-session D2 requirements;
- other camera models;
- F2;
- relay;
- wakeup;
- IPv6;
- RTSP/go2rtc/Frigate parity;
- production timing limits.

## Production constraints

- Do not modify production startup behavior.
- Do not add a production `clean_pppp` selector yet.
- Do not remove or disable the APK/vendor-library path.
- Do not change Home Assistant discovery.
- Do not change RTSP ports.
- Do not change go2rtc or Frigate configuration.
- Do not enable clean transport in production.
- Do not create a release or tag.
- Do not merge PR #2.

A parser-helper refactor is allowed only if behavior is demonstrably unchanged and needed to reuse existing parsing code; it must not change production transport selection or startup.

## Final Codex report

Return a concise report containing:

```text
branch SHA
main SHA (confirm unchanged)
PR #2 open/draft status
CI status
focused test result
files changed
production behavior changed: yes/no

PROVEN
INFERRED
UNKNOWN

manual nested-temp copy command
manual CR-4 self-test command
manual support-loader smoke test
manual live CR-4 command template
```

Explicitly state whether Codex performed live device traffic. The expected answer is:

```text
live device traffic performed by Codex: no
```
