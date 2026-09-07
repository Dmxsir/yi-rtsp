# Codex Research Task — CR-4C narrow audio-validation fix after live failure

## Scope

Work only in repository `Dmxsir/yi-rtsp` on branch `pppp-cleanroom`.

Do **not** modify, merge, rebase, tag, release, or otherwise change `main` or the installed production `0.2.0` vendor-runtime path. Keep PR #2 open and draft.

Do **not** perform live device traffic. This is a narrow corrective task for CR-4C after the first manual live attempt failed with a single audio-parser category.

## Live evidence to preserve

The first CR-4C manual run on 2026-09-07 produced this sanitized result:

```text
temporary_go2rtc_ready=true
cr3_control_result=PASS
media_channels_enabled=true
stop_live_767_sent=true
rtsp_consumer_stopped=true
temporary_go2rtc_stopped=true
transport_closed=true
producer_registered=true
producer_media_ready=true
cr4c_result=FAIL; failure_category=AUDIO_PARSE_INVALID
```

Important interpretation:

- temporary go2rtc startup succeeded;
- clean CR-3 control/authentication succeeded;
- media channels were enabled;
- go2rtc registered a real MPEG-TS producer and reported media-ready;
- cleanup succeeded;
- the run failed only because the shared sustained collector treated one channel-1 audio validation exception as fatal.

Do not claim CR-4C PASS from this result. Do not change RTSP/go2rtc isolation or cleanup merely because they already reached a useful stage.

## Root cause to inspect first

Read these files before editing:

- `tools/pppp_cleanroom/probe_sustained_mux.py`
- `tools/pppp_cleanroom/probe_rtsp_publish.py`
- `tests/test_pppp_cr4b.py`
- `tests/test_pppp_cr4c.py`
- `yi_home/rootfs/opt/yi-home/app/yi_native_av_relay.py`
- `docs/PPPP_CLEANROOM_RESEARCH.md`
- `docs/PPPP_CLEANROOM_TRANSPORT.md`

The current research `SustainedCollector._audio(...)` catches every exception from `decrypt_audio_unit(...)` and converts it to fatal `AUDIO_PARSE_INVALID`.

The existing production relay is intentionally more precise. `yi_native_av_relay.AudioUnitValidationError` is documented as an isolated malformed/corrupt audio record that may be dropped in a live stream, and the production loop catches **only that exception type**, increments a safe drop counter, and continues. Configuration/session invariant failures such as an invalid AES key length are not swallowed.

This task is to align the **CR-4C research behavior** with that proven production tolerance without weakening CR-4B or hiding real audio failures.

## Required fix

### 1. Preserve CR-4B behavior by default

Do not silently change the already-live-proven CR-4B success boundary.

The shared `SustainedCollector` may be extended with an explicit policy/limit, but default CR-4B behavior must remain strict unless the CR-4B runner explicitly opts into a new policy later.

A reasonable design is an optional argument/field such as:

```text
max_audio_validation_drops = 0
```

where zero preserves the existing strict CR-4B behavior.

CR-4C may explicitly opt into a small bounded nonzero limit.

### 2. Catch only the production-classified packet error

When audio decode raises exactly the production module's `AudioUnitValidationError` (or a safely equivalent type obtained from `support.audio`), CR-4C may drop that one unit and continue.

Do **not** broadly catch and ignore `Exception`.

The following must remain fatal:

- invalid/changed AAC format after successful decode;
- invalid session/config key length;
- unexpected runtime/parser exceptions;
- any error not explicitly classified as the production droppable audio-unit validation exception.

### 3. Bound the tolerance

CR-4C must not accept an unlimited stream of corrupt audio.

Add an explicit configurable research limit, with a conservative documented default for the first live retry. Use a separate failure category when exceeded, for example:

```text
AUDIO_VALIDATION_DROP_LIMIT
```

The exact default may be chosen after inspecting production behavior and existing test cadence, but it must be:

- nonzero for CR-4C;
- small relative to a normal 45-second / ~hundreds-of-AAC-frames run;
- configurable from the CR-4C CLI;
- documented as experimental rather than a protocol invariant.

Do not use a percentage-only threshold that can accidentally permit many drops before enough valid frames arrive. An absolute bound is required; a secondary ratio guard is optional.

### 4. Safe counter only

Track and print only a sanitized count:

```text
audio_validation_drops=<count>
```

Do not print the malformed TNP unit, decrypted bytes, hashes, timestamps, sequence values, credentials, or exception text containing packet data.

Dropped units must:

- not increment valid `audio_frames`;
- not advance sustained valid-audio timing;
- not be fed to FFmpeg/go2rtc;
- not initialize or alter `audio_format`;
- not initialize `first_audio_ts`;
- not count toward the CR-4C source PASS gate.

### 5. Preserve the source proof gate

CR-4C PASS must still require the existing valid AAC sustained-media conditions:

- valid AAC format 16 kHz mono/object type 2;
- minimum valid audio frame count;
- valid audio activity spanning the configured source minimum interval;
- valid H.264 I/P sustained activity;
- video reorder output;
- no media stall/overflow/retry-limit failure.

A run containing only malformed/dropped audio must fail.

### 6. Do not touch RTSP isolation

Do not alter these already-correct CR-4C constraints:

- temporary go2rtc only;
- loopback bind only;
- synthetic `yi_cr4c_probe` stream only;
- production ports 1984/8554 rejected;
- research defaults 11984/18554;
- hard fail on occupied research ports;
- no Docker host mapping;
- no production publisher/lifecycle/discovery/HA/Frigate changes;
- no saved media.

### 7. Cleanup remains mandatory

All existing cleanup guarantees must remain:

```text
STOP_LIVE 767
RTSP consumer stop
MPEG-TS ingest/mux stop
Temporary go2rtc stop
Clean PPPP close
```

The new audio-drop path must not bypass `finally` cleanup.

## Offline tests required

Add focused tests proving at least:

1. CR-4B default remains strict: one `AudioUnitValidationError` still produces the existing fatal behavior unless an explicit tolerance is enabled.
2. CR-4C with tolerance enabled drops one isolated `AudioUnitValidationError` and continues to later valid AAC.
3. A dropped audio unit does not increment valid audio counters or active span.
4. A dropped unit is never fed to the mux.
5. Valid AAC after one dropped unit can still initialize audio timestamp/format and proceed normally.
6. Repeated droppable audio errors exceeding the configured absolute limit fail with a dedicated category.
7. A generic `RuntimeError` from `decrypt_audio_unit(...)` remains fatal and is never treated as droppable.
8. AAC format mismatch remains fatal.
9. A run with only dropped audio cannot satisfy the sustained source gate.
10. Existing CR-2/3/4/4B/4C tests remain green.
11. CR-4C self-test/support-smokes still perform no live device/network/process work beyond their existing documented smoke semantics.
12. Relocated `/tmp/yi-cr4c/...` execution still works.

Use synthetic bytes/fakes only. Do not add real media fixtures.

## Documentation

Update the research/spec docs narrowly:

- record that the first CR-4C live attempt reached temporary go2rtc readiness, clean control, media enablement, producer registration/media-ready, then failed on `AUDIO_PARSE_INVALID`;
- state that production already tolerates isolated `AudioUnitValidationError` records;
- classify the CR-4C audio-drop bound as experimental;
- keep CR-4C status **LIVE UNPROVEN** until a subsequent manual run passes the full RTSP consumer gate.

Do not add any real camera identifier, endpoint, credential, payload, or packet dump to docs.

## Security / privacy

Never commit or print:

- stable IDs from the manual run;
- DID/UID;
- YI server endpoints;
- InitString/license/device key/password/token;
- raw TNP/DRW/H.264/AAC/MPEG-TS;
- pcap/APK/proprietary binary bytes.

Safe output is limited to booleans, stage/failure categories, counts, bounded durations, codec/format metadata already used by the research probes, and the new `audio_validation_drops` count.

## Validation before push

Run at minimum:

```text
python3 -m unittest tests.test_pppp_cleanroom tests.test_pppp_cr4 tests.test_pppp_cr4b tests.test_pppp_cr4c -v
python3 -m py_compile tools/pppp_cleanroom/*.py
python3 tools/pppp_cleanroom/probe_rtsp_publish.py --self-test
```

Also run the App-image support smoke tests on Linux CI as currently configured. Preserve Docker build/startup validation.

## Stop condition

After tests and CI pass:

1. commit and push only `pppp-cleanroom`;
2. keep PR #2 open and draft;
3. do not perform live camera traffic;
4. report the exact commit SHA, changed files, test totals, and the new CR-4C manual copy/self-test/smoke/live commands;
5. explicitly state the configured default audio validation-drop limit used for the next manual retry.

Do not merge, tag, release, switch production transport, or alter the running App.