# Codex Research Task — CR-4B sustained clean media + relay/mux boundary

## Scope

Work only in repository `Dmxsir/yi-rtsp` on branch `pppp-cleanroom`.

Do **not** modify, merge, rebase, tag, release, or otherwise change `main` or the installed production `0.2.0` vendor-runtime path. Keep PR #2 open and draft.

This is the next gate after the successful CR-4 live proof. Call this gate **CR-4B** in code/docs so we do not collide with the roadmap's already-existing `CR-5 — YI server rendezvous` milestone.

The goal is to prove two things, still research-only and opt-in:

1. the clean PPPP/TNP path can carry **sustained ordered H.264 + AAC media for a materially longer bounded window**; and
2. those already-parsed clean media frames can be fed into the project's **existing relay/mux boundary** and produce a valid MPEG-TS stream in memory/pipes, without RTSP/go2rtc/Frigate publication and without the proprietary PPPP library performing transport/channel I/O.

Do **not** perform an unattended live-device experiment. Implement and test offline, push only `pppp-cleanroom`, then stop with safe manual commands for the user to run in the Home Assistant App container.

## Read first

Read these files before changing code:

- `docs/PPPP_CLEANROOM_RESEARCH.md`
- `docs/PPPP_CLEANROOM_TRANSPORT.md`
- `docs/PPPP_CR2_LIVE_01.md`
- `docs/PPPP_CR3_LIVE_01.md`
- `docs/PPPP_CR4_LIVE_01.md`
- `tools/pppp_cleanroom/yi_pppp.py`
- `tools/pppp_cleanroom/yi_pppp_session.py`
- `tools/pppp_cleanroom/tnp_stream.py`
- `tools/pppp_cleanroom/probe_channel0_tnp.py`
- `tools/pppp_cleanroom/probe_media_tnp.py`
- `tests/test_pppp_cleanroom.py`
- `tests/test_pppp_cr4.py`
- `yi_home/rootfs/opt/yi-home/app/yi_live_relay.py`
- `yi_home/rootfs/opt/yi-home/app/yi_native_av_relay.py`
- `yi_home/rootfs/opt/yi-home/app/yi_tnp_oracle.py`
- `yi_home/rootfs/opt/yi-home/app/tools/phase3_pppp_probe/run_phase3e_tnp.py`
- `yi_home/rootfs/opt/yi-home/app/yi_camera_manager.py`

Inspect the current production relay/mux code carefully. Reuse existing parser/reorder/mux semantics where possible. In particular inspect:

- `yi_live_relay._decode_video_unit(...)`
- `yi_live_relay.SequenceReorderBuffer`
- `yi_native_av_relay.decrypt_audio_unit(...)`
- `yi_native_av_relay.start_ffmpeg(...)` and its underlying pipe/mux helpers
- existing timestamp-offset / `setts` behavior
- existing H.264/AAC format expectations

Do **not** invoke `yi_native_av_relay.main()` from the research runner because that production path starts the vendor/QEMU worker. Reuse only neutral parser/mux helpers or factor out the smallest neutral helper while preserving production behavior.

## Current proven state

Treat these as established evidence and do not regress them.

### CR-2 — LIVE PROVEN

On one owned YI `y291ga` / raw model `83` direct-LAN path, the clean client completed rendezvous, legacy punch, matching ready, keepalive confirmation, and close without `libPPPP_API.so` performing the PPPP handshake.

### CR-3 — LIVE PROVEN

On the same owned direct path, the clean transport carried reliable channel-0 TNP startup:

```text
4881 -> 9029 -> 768 -> valid first 4882 -> 767 -> close
```

The 164-byte startup DRW was acknowledged through the implemented selective D1 ACK path.

### CR-4 — LIVE PROVEN

On 2026-09-06 the same owned direct-LAN path produced:

```text
cr3_control_result=PASS
media_channels_enabled=true
channel2_tnp_units_valid=1
channel3_tnp_units_valid=1
h264_i_frame_valid=true
h264_p_frame_valid=true
h264_framing_valid=true
h264_frames_reordered=2
h264_nal_types=1,5,7,8
channel1_tnp_units_valid=1
aac_valid=true
aac_sample_rate=16000
aac_channels=1
aac_object_type=2
stop_live_767_sent=true
transport_closed=true
cr4_result=PASS
```

Therefore real H.264 I/P video and AAC are already live-proven through the clean PPPP transport and the repository's existing parsers on this one path.

What is **not** yet proven:

- sustained media over a longer window;
- long-session D2 requirements;
- packet-loss/retransmission tuning;
- relay/mux acceptance of clean-media output;
- MPEG-TS structural/codec parity;
- RTSP/go2rtc/Frigate parity;
- other models / firmware;
- F2, relay, wakeup, IPv6, or non-direct operation.

## CR-4B goal

Implement a separate, explicit research runner, for example:

```text
tools/pppp_cleanroom/probe_sustained_mux.py
```

The exact filename may differ, but do not overload `probe_media_tnp.py` and do not change its CR-4 success boundary.

The intended live sequence is:

```text
clean CR-2 direct session
-> reliable channel 0
-> 4881 -> 9029 -> 768
-> valid first 4882
-> enable bounded reliable channels 1/2/3
-> reconstruct + parse H.264/AAC as CR-4 already does
-> sustain ordered media for >= configured minimum active window
-> feed reordered H.264 + decrypted AAC into existing FFmpeg mux boundary
-> validate MPEG-TS through an in-memory/pipe path
-> STOP_LIVE 767
-> terminate mux validator cleanly
-> CLOSE
```

No production transport selector, RTSP server, go2rtc, Frigate, Home Assistant discovery change, or APK/vendor dependency removal belongs in this task.

## Required implementation work

### 1. Preserve CR-2 / CR-3 / CR-4

All existing focused tests and runners must continue to work.

Do not weaken:

- handshake source gating;
- keepalive confirmation;
- channel-0 reliable ACK/retry semantics;
- D2 observational-only behavior;
- media-channel opt-in;
- bounded buffers;
- TNP media unit framing checks;
- existing H.264/AAC parser validation;
- STOP/CLOSE cleanup.

### 2. Sustained media collector

Build on the CR-4 parser path rather than creating a second media parser.

The sustained collector must:

- use independent reliable channels 1/2/3;
- reconstruct TNP units with `TnpUnitReader` or a small compatible extension;
- decode channels 2/3 with the existing video helper;
- reorder video with the existing `SequenceReorderBuffer` semantics;
- decode channel 1 with the existing AAC helper;
- retain only bounded transient bytes needed for parsing/muxing;
- continue PPPP keepalives while collecting;
- keep D2 observational only;
- fail stage-specifically on buffer overflow, remote close, retry limit, malformed TNP, parser failure, or prolonged media starvation.

Do not assume one DRW packet equals one TNP record.

### 3. Sustained proof boundary

Use explicit configurable limits. Recommended live defaults for the first manual run:

```text
--duration 45
--min-active-seconds 30
```

The runner must not call a 2-second burst “sustained” merely because many frames arrived quickly.

For PASS, require at minimum:

- valid clean CR-3 control/authentication first;
- valid H.264 I-frames and P-frames over the window;
- valid AAC over the window;
- ordered video output from the existing reorder buffer;
- media activity spanning at least `min_active_seconds` by monotonic arrival time or safe media timestamp span;
- no unhandled parser errors;
- no media/control buffer overflow;
- no remote close before the proof boundary;
- no retry-limit failure;
- STOP 767 attempted/sent and clean PPPP close.

Use sane minimum frame/record counts as a secondary guard, but do not make an undocumented exact camera FPS a protocol invariant. Counters must be configurable and conservative.

Suggested sanitized metrics:

```text
media_active_seconds=<bounded decimal>
video_i_frames=<count>
video_p_frames=<count>
video_reordered_frames=<count>
audio_frames=<count>
channel1_tnp_units=<count>
channel2_tnp_units=<count>
channel3_tnp_units=<count>
drw_retries=<count>
d2_observed=<count>
media_buffer_peak_bytes=<count or per-channel safe counters if available>
```

Do not print sequence payloads, timestamps that can identify user activity, or media bytes. Deltas/counters are acceptable.

### 4. Relay/mux boundary

After CR-4 parsing succeeds, feed the **already-decoded/reordered** media into the existing neutral mux semantics.

Preferred architecture:

```text
clean PPPP channels
 -> TNP readers
 -> existing video/AAC parsers
 -> existing video reorder
 -> H264 + AAC byte pipes
 -> existing FFmpeg MPEG-TS mux configuration
 -> in-memory/OS-pipe validator
```

Do not use the native vendor worker and do not call any PPPP symbol from `libPPPP_API.so`.

Reusing/factoring `yi_native_av_relay.start_ffmpeg(...)`, `_setts(...)`, or the smallest neutral equivalent is acceptable if production behavior stays byte-for-byte/semantically unchanged and tests cover the refactor.

### 5. No persisted media

The CR-4B live runner must not write H.264, AAC, TS, MP4, pcap, or any other media/capture file to disk.

Use pipes only.

Acceptable pattern:

```text
FFmpeg MPEG-TS stdout/pipe
 -> bounded reader / ffprobe stdin
 -> sanitized codec/stream metadata only
```

Do not print or hash raw media as a substitute for validation.

### 6. MPEG-TS validation

Validate that the mux boundary actually produces a usable MPEG-TS stream, not merely that FFmpeg stayed alive.

A strong PASS for the tested `y291ga` path should establish, via pipe-only validation:

- MPEG-TS bytes were produced;
- TS framing/sync is structurally valid enough for the validator;
- video stream recognized as H.264;
- expected tested resolution remains 1920x1080 if the current existing relay/parser evidence still makes that an invariant for this model/path;
- audio stream recognized as AAC;
- audio sample rate 16000 Hz;
- mono audio;
- mux process exits cleanly or is deliberately terminated after successful bounded validation;
- validator exits cleanly or has a documented normalized early-exit condition after it obtained required stream metadata.

Prefer using `ffprobe` on stdin/pipe for the live proof. If FFmpeg/ffprobe pipe lifecycle requires a small neutral helper, implement it with explicit timeout/cancellation and tests.

Do not save the MPEG-TS stream to a file for validation.

### 7. Backpressure and deadlock safety

The runner must actively drain mux output. Never allow FFmpeg stdout to block because nobody reads it.

Explicitly handle:

- FFmpeg output pipe backpressure;
- ffprobe consuming and exiting after enough data;
- EPIPE / broken pipe after the validator is satisfied;
- child process timeouts;
- shutdown ordering;
- blocked media reads while STOP/CLOSE must still happen;
- cancellation of any reader/pump threads.

No daemon thread should be required for correctness after process exit. Tests should prove cleanup terminates.

### 8. Timing / A-V semantics

Reuse the existing relay's timestamp-offset / 90 kHz SETTS semantics rather than inventing a new A/V timing model.

Track only safe timing diagnostics such as:

- initial A/V delta in milliseconds;
- first/last media active span;
- frame counts;
- monotonic progress intervals.

Do not log absolute camera timestamps if they are unnecessary.

If sustained clean media exposes a timing difference from the vendor path, return a stage-specific research result and document it rather than silently compensating with a new unproven rule.

### 9. Memory bounds

All queues/buffers must remain bounded.

Keep configurable limits for:

- control reliable buffer;
- each media reliable buffer;
- TNP record maximum;
- pre-mux video frames;
- pre-mux audio frames;
- mux output buffering/pump chunks;
- child process wait timeouts.

Do not collect 45 seconds of raw media in memory before starting FFmpeg. Start the mux once the existing production-equivalent prerequisites are met, then stream incrementally.

### 10. Stage-specific failures

Return sanitized categories. Useful examples:

```text
CR4B_SETUP
CONTROL_AUTH_FAILED
MEDIA_START_TIMEOUT
MEDIA_SUSTAIN_TIMEOUT
MEDIA_BUFFER_LIMIT
MEDIA_RETRY_LIMIT
VIDEO_PARSE_INVALID
AUDIO_PARSE_INVALID
MUX_START_FAILED
MUX_EARLY_EXIT
MUX_OUTPUT_TIMEOUT
MUX_PIPE_BACKPRESSURE
FFPROBE_FAILED
FFPROBE_TIMEOUT
MPEGTS_STREAM_INVALID
STOP_LIVE_FAILED
REMOTE_CLOSE
```

Do not fall back to the vendor PPPP library on any clean-path failure.

### 11. Relocated HA App execution

The research runner must work from a nested temp layout, e.g.:

```text
/tmp/yi-cr4b/tools/pppp_cleanroom/
/tmp/yi-cr4b/tools/phase3_pppp_probe/
```

Reuse the relocation-safe support loading added for CR-4.

Do not overwrite installed `/opt/yi-home/app` production files during manual testing.

### 12. Offline self-test

Add `--self-test` with strict isolation. It must report equivalent safe booleans showing:

```text
cloud_used=false
network_used=false
tnp_sent=false
media_requested=false
ffmpeg_started=false
ffprobe_started=false
runtime_support_imported=false
```

Self-test may use pure synthetic/fake objects only.

### 13. Support/mux smoke tests

Provide separate offline smoke modes where useful, for example:

```text
--support-smoke-test
--mux-support-smoke-test
```

`--support-smoke-test` may import installed parser/mux helpers but must not contact cloud/device.

A mux support smoke test may verify FFmpeg/ffprobe executable availability and process wiring without using real device/cloud/media. If real codec bytes would be required, use generated/synthetic test inputs created in test code only and never embed captured camera media.

## Security / privacy rules

Never commit, print, save, attach, or include in fixtures:

- YI APKs;
- `libPPPP_API.so` or proprietary binary bytes;
- pcap files;
- account credentials;
- UID / DID;
- InitString;
- License / device key;
- camera password;
- tokens;
- raw TNP authentication material;
- raw DRW payloads;
- raw H.264 NAL payloads;
- raw AAC payloads;
- raw MPEG-TS payloads;
- media files;
- real endpoint addresses;
- real stable IDs;
- exact absolute media timestamps tied to user activity.

Safe diagnostics include counts, durations, path class (`lan`/`wan`), model family/raw model, codec names, dimensions, AAC rate/channels/object type, NAL type numbers, retry/D2 counters, bounded buffer metrics, and child-process result categories.

## Clean-room / licensing rules

- Keep the same clean-room discipline already documented.
- Do not copy implementation code from repositories without a compatible explicit license.
- `frankzhangshcn/p2p_tnp` and `xen0bit/libPPCS_API` remain reference-only.
- `nosoop-onlyslop/p2pcam` remains feasibility/reference evidence only.
- Existing code inside this repository may be reused/refactored with tests.
- No decompiled proprietary implementation may be used as source code.

## Offline tests required

Add focused tests for at least:

### Regression

- existing CR-2/CR-3 tests pass;
- existing CR-4 tests pass;
- CR-4 runner success/failure semantics unchanged.

### Sustained collector

- activity-span criterion cannot pass before configured minimum duration;
- counts alone cannot fake a sustained PASS;
- valid I/P/AAC progress updates sustained state;
- media starvation yields `MEDIA_SUSTAIN_TIMEOUT` or equivalent;
- remote close/retry/buffer errors propagate stage-specifically;
- D2 remains observational;
- keepalive continues during sustained media reads;
- bounded queues never grow without limit.

### Mux integration

- video frames are fed only after parser/reorder acceptance;
- AAC is fed only after existing decrypt/ADTS validation;
- initial A/V offset calculation matches the existing relay semantics;
- mux starts once prerequisites are met and not before;
- mux output is continuously drained;
- ffprobe stdin path receives TS bytes without disk persistence;
- ffprobe metadata parsing accepts expected H.264 + AAC and rejects missing/wrong streams;
- EPIPE after successful validator completion is normalized safely;
- early mux exit before validation is a failure;
- child timeout is a failure;
- cleanup closes pipes, joins pumps, terminates/kills children when needed, and leaves no live child/thread.

### Relocation/isolation

- nested relocated runner self-test works;
- relocated support loader works with installed `/opt/yi-home/app` helpers;
- self-test imports no App/cloud/media runtime support;
- self-test performs no network, TNP, media, FFmpeg, or ffprobe activity;
- support smoke test performs no cloud/device traffic.

Do not require a live camera in CI.

## Manual live success criteria

Codex must prepare but **not execute** the live command.

For the first owned-camera run, use a bounded default such as 45 seconds with a 30-second sustained minimum.

A full CR-4B PASS should report secret-safe lines equivalent to:

```text
cr3_control_result=PASS
media_channels_enabled=true
sustained_media_started=true
media_active_seconds>=30
video_i_frames=<positive count>
video_p_frames=<positive count>
video_reordered_frames=<positive count>
audio_frames=<positive count>
aac_sample_rate=16000
aac_channels=1
mux_started=true
mpegts_bytes_observed=<positive count>
ffprobe_video_codec=h264
ffprobe_audio_codec=aac
ffprobe_video_size=1920x1080
ffprobe_audio_sample_rate=16000
ffprobe_audio_channels=1
sustained_media_result=PASS
mux_validation_result=PASS
stop_live_767_sent=true
transport_closed=true
cr4b_result=PASS
```

Exact names may differ, but the proof boundary must be unambiguous.

Do not claim PASS merely because FFmpeg started, because one TS sync byte appeared, or because CR-4 parsed one frame.

## Documentation updates

Update research/spec docs to reflect the actual state after implementation:

### PROVEN

- CR-2 live direct clean session on one owned `y291ga` path;
- CR-3 live reliable channel-0 TNP through valid `4882`;
- CR-4 live real H.264 I/P + AAC parser validation over clean channels;
- CR-4B offline mechanics only until the user manually runs it.

### INFERRED

- D2 interpretation;
- production timing/retry/window/chunk/buffer settings;
- any mux timing assumptions not already proven by the existing vendor-backed production relay.

### UNKNOWN until manual CR-4B run

- sustained clean-media duration on real hardware;
- clean-media -> mux/ffprobe live success;
- long-session D2 needs;
- loss behavior;
- RTSP/go2rtc/Frigate parity;
- other models/F2/relay/wakeup/IPv6.

Keep the existing roadmap milestone `CR-5 — YI server rendezvous`; do not silently renumber later milestones. Document this inserted sustained/mux gate as **CR-4B**.

## Production constraints

- Do not add or enable a production `clean_pppp` selector.
- Do not change production startup behavior.
- Do not change HA discovery.
- Do not change RTSP ports.
- Do not change go2rtc/Frigate configuration.
- Do not remove APK/vendor-runtime requirements.
- Do not modify HACS integration behavior.
- Do not create a release or tag.
- Do not merge PR #2.

## Live execution policy

Do not perform live device traffic from Codex.

At task completion:

1. all offline tests pass on Linux CI;
2. push only `pppp-cleanroom`;
3. keep PR #2 open and draft;
4. provide exact branch SHA;
5. confirm `main` SHA unchanged;
6. list changed files;
7. report focused and full CI results;
8. state explicitly that Codex performed **no live device traffic**;
9. provide nested `/tmp/yi-cr4b/...` copy/download instructions using the exact commit SHA;
10. provide self-test command first;
11. provide support/mux smoke commands next;
12. provide a live command template using placeholders only (`<CAMERA_STABLE_ID>`, `<YI_SERVER_IPV4>`), never real IDs/endpoints;
13. stop and wait for the user's sanitized live output.

## Final Codex report

Return a concise report with:

```text
branch SHA
main SHA (confirm unchanged)
PR #2 open/draft status
CI status
focused tests
full Linux tests
files changed
production behavior changed: no
live device traffic by Codex: no

PROVEN
INFERRED
UNKNOWN

manual relocation/copy commands
manual self-test command
manual support/mux smoke commands
manual CR-4B live command template
```
