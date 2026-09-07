# Codex Research Task — CR-4C exact go2rtc EOF-finalization classification

## Scope

Work only in repository `Dmxsir/yi-rtsp` on branch `pppp-cleanroom`.

Do **not** modify, merge, rebase, tag, release, or otherwise change `main` or the installed production `0.2.0` vendor-runtime path. Keep PR #2 open and draft.

Do **not** perform live camera/device traffic. This is a narrow CR-4C corrective task after the latest manual run proved the sustained source and RTSP consumer gates but ended with `GO2RTC_INGEST_REJECTED` only after the chunked MPEG-TS request was finalized.

## Latest live evidence to preserve

The latest manual run on 2026-09-07 used an explicit experimental retry override `--max-audio-validation-drops 12` and produced this sanitized result:

```text
temporary_go2rtc_ready=true
cr3_control_result=PASS
media_channels_enabled=true
stop_live_767_sent=true
rtsp_consumer_stopped=true
temporary_go2rtc_stopped=true
transport_closed=true
source_media_started=true
source_media_active_seconds=32.168
source_video_i_frames=10
source_video_p_frames=509
source_video_reordered_frames=519
source_audio_frames=516
audio_validation_drops=2
channel1_tnp_units=516
channel2_tnp_units=10
channel3_tnp_units=509
source_aac_sample_rate=16000
source_aac_channels=1
initial_av_delta_ms=17
drw_retries=0
d2_observed=0
source_media_result=PASS
producer_registered=true
producer_media_ready=true
rtsp_consumer_connected=true
rtsp_video_codec=h264
rtsp_video_size=1920x1080
rtsp_audio_codec=aac
rtsp_audio_sample_rate=16000
rtsp_audio_channels=1
rtsp_video_packets=160
rtsp_audio_packets=125
rtsp_consumer_active_seconds=10.540
rtsp_consumer_result=PASS
cr4c_result=FAIL; failure_category=GO2RTC_INGEST_REJECTED
```

Interpretation:

- clean CR-3 control/auth passed;
- sustained valid source media passed for >30 seconds;
- only 2 isolated classified audio-validation records were dropped, while 516 valid AAC records passed;
- temporary go2rtc producer registered and became media-ready;
- the local RTSP consumer passed with H.264 1920x1080 + AAC 16 kHz mono and positive packet counts for >10 seconds;
- STOP 767, consumer stop, temporary go2rtc stop, and clean PPPP close succeeded;
- the only failure occurred after terminal HTTP finalization returned an HTTP status currently classified as `GO2RTC_INGEST_REJECTED`.

CR-4C remains **LIVE UNPROVEN** until one manual run returns `cr4c_result=PASS`.

## Verified go2rtc v1.9.14 behavior — treat as source-grounded evidence

The App image pins:

```text
GO2RTC_VERSION=1.9.14
```

Verify this remains true in `yi_home/Dockerfile` before editing anything.

For upstream go2rtc v1.9.14, tag commit:

```text
b5948cfb25404cc5cb37b166ecaa2dca20b11d4b
```

Relevant upstream files:

```text
internal/mpegts/mpegts.go
pkg/mpegts/producer.go
```

The v1.9.14 `POST /api/stream.ts?dst=...` handler does:

```text
mpegts.Open(r.Body)
stream.AddProducer(client)
client.Start()
if Start returns any error -> http.Error(..., HTTP 500)
```

The v1.9.14 MPEG-TS `Producer.Start()` is an infinite read loop and returns the first `ReadPacket(...)` error; it has no normal `nil` completion path. Therefore a finite chunked request that has streamed successfully and then reaches clean body EOF can surface at the HTTP layer as a 500 carrying the read error, even though the producer was valid and consumers already received media.

This is the key difference from a genuine mid-stream ingest rejection. Do **not** solve it by accepting arbitrary HTTP 500 responses.

If useful, independently re-check the exact upstream v1.9.14 sources. Do not copy upstream implementation code; this task only relies on endpoint semantics.

## Required fix

### 1. Classify exact terminal go2rtc EOF, not arbitrary HTTP errors

Update the research-only `ChunkedIngestSink.finish()` so it reads a **small bounded response body** before classifying HTTP status.

Add a distinct safe finalization mode, for example:

```text
ingest_finalize_mode=go2rtc_eof_after_terminal
```

The mode may be returned **only** when all low-level conditions are true:

- the terminal chunk `0\r\n\r\n` was sent successfully;
- a valid HTTP response was received;
- HTTP status is exactly `500`;
- the bounded response body, after ASCII-safe whitespace trimming, is exactly the known EOF marker `EOF`;
- no exception occurred while reading that bounded body.

Do not print or persist the response body. Only print the safe mode and, optionally, an integer field such as:

```text
ingest_finalize_http_status=500
```

Do not hash the body.

If actual local unit/integration evidence shows the exact v1.9.14 clean EOF body differs, stop and report rather than broadening the matcher. Do not accept `unexpected EOF`, prefixes/suffixes, arbitrary text containing `EOF`, empty bodies, HTML, or other 500 bodies without a separately justified task.

### 2. Preserve existing accepted terminal modes

Keep the already-implemented modes:

```text
http_response
peer_closed_after_terminal
```

A normal 2xx/3xx response remains `http_response`.

Exact `http.client.RemoteDisconnected` from `getresponse()` after a successfully sent terminal chunk remains `peer_closed_after_terminal` under the existing narrow rule.

The new go2rtc EOF mode is a third narrow outcome, not a replacement.

### 3. All other HTTP rejection cases stay fatal

Keep fatal:

- any HTTP 4xx;
- HTTP 500 with any body other than exact trimmed `EOF`;
- HTTP 501–599;
- malformed HTTP response;
- timeout;
- connection reset;
- failure reading response body;
- terminal-chunk send failure;
- first-chunk failure;
- any mid-stream send failure;
- producer loss before proof;
- TS framing failure;
- FFmpeg/mux/backpressure failure.

Use existing stage-specific categories. `GO2RTC_INGEST_REJECTED` is still appropriate for a real HTTP rejection that does not match the exact terminal EOF classification.

### 4. High-level CR-4C PASS gate stays strict

The low-level sink should only classify the terminal outcome. The top-level `probe_rtsp_publish.py` may accept `go2rtc_eof_after_terminal` only if **all** existing proof/cleanup gates are true:

- sustained source PASS;
- valid H.264 I/P and valid AAC source activity for the configured minimum span;
- valid TS framing and positive published MPEG-TS bytes;
- producer registered + media-ready;
- RTSP consumer PASS;
- RTSP H.264 1920x1080;
- RTSP AAC 16 kHz mono;
- positive RTSP video/audio packet counts;
- minimum RTSP consumer active span;
- no previous ingest/mux/coordinator failure;
- STOP 767 sent;
- RTSP consumer stopped;
- temporary go2rtc stopped;
- clean PPPP session closed.

Do not weaken any of these conditions.

### 5. Audio-drop policy

Do not change the shared CR-4B default:

```text
max_audio_validation_drops=0
```

Do not silently change the CR-4C parser default of `3` in this task.

The latest manual run used `12` only as an explicit bounded retry override and observed 2 drops. Document that distinction. The next manual live command may again pass `--max-audio-validation-drops 12` explicitly so finalization can be tested without the earlier small-limit variability. `12` is not a protocol invariant or production default.

### 6. Isolation and production stability

Do not change:

- temporary research go2rtc only;
- loopback-only binds;
- synthetic `yi_cr4c_probe` identity;
- research API/RTSP defaults 11984/18554;
- production-port rejection for 1984/8554;
- no Docker host mapping;
- no Home Assistant/Frigate integration in this gate;
- no saved H.264/AAC/MPEG-TS;
- no production App lifecycle/publisher changes;
- no proprietary binary or APK additions.

## Offline tests required

Add or adjust focused tests proving at least:

1. HTTP 200 final response still yields `http_response`.
2. exact `RemoteDisconnected` after terminal send still yields `peer_closed_after_terminal`.
3. exact HTTP 500 + bounded body `EOF\n` yields the new `go2rtc_eof_after_terminal` mode.
4. exact HTTP 500 + body `EOF` also matches after whitespace trim if that is the chosen normalization.
5. HTTP 500 + empty body remains fatal.
6. HTTP 500 + `unexpected EOF` remains fatal.
7. HTTP 500 + `some EOF` remains fatal.
8. HTTP 500 + oversized body is not accepted.
9. HTTP 400 + body `EOF` remains fatal.
10. HTTP 503 + body `EOF` remains fatal.
11. response-body read timeout remains fatal.
12. response-body protocol/reset error remains fatal.
13. terminal send failure remains fatal.
14. first-chunk and mid-stream send failures remain fatal.
15. top-level CR-4C accepts the new EOF mode only when every source/TS/producer/RTSP/STOP/cleanup condition is PASS.
16. the new EOF mode alone cannot rescue a failed source gate, failed RTSP gate, zero TS bytes, failed STOP, or failed cleanup.
17. current audio-drop-policy tests remain green.
18. CR-4B stays strict by default and all CR-2/3/4/4B/4C focused tests remain green.
19. self-test/support/RTSP-support smoke modes remain free of live camera traffic.
20. relocated `/tmp/yi-cr4c/...` execution remains valid.

Use synthetic fakes only for unit tests. Do not add raw camera media fixtures.

If practical without complicating the runner, an additional App-image **loopback-only, no-camera** integration check against the pinned `/usr/local/bin/go2rtc` may be added to verify that a finite valid synthetic MPEG-TS POST ends with the expected 500/EOF behavior. This check must use synthetic FFmpeg-generated media, temporary research ports, no external network, no camera material, and full cleanup. It is optional; do not make the patch large just to add it.

## Documentation

Update `docs/PPPP_CLEANROOM_RESEARCH.md` and `docs/PPPP_CLEANROOM_TRANSPORT.md` narrowly:

- record the latest source PASS: 32.168 s, 10 I, 509 P, 519 reordered, 516 valid AAC, 2 bounded drops;
- record RTSP PASS: H.264 1920x1080 + AAC 16 kHz mono, 160 video packets, 125 audio packets, 10.540 s;
- record final failure `GO2RTC_INGEST_REJECTED`;
- explain the pinned go2rtc 1.9.14 handler/producer EOF semantics without copying code;
- state that only exact HTTP 500 + exact EOF body after successful terminal send is newly classifiable;
- keep CR-4C **LIVE UNPROVEN** until a subsequent manual run returns PASS.

Never add real stable ID, DID/UID, YI server endpoints, credentials, packet/media bytes, pcap, APK, or proprietary library content.

## Validation before push

Run at minimum:

```text
python3 -m unittest tests.test_pppp_cleanroom tests.test_pppp_cr4 tests.test_pppp_cr4b tests.test_pppp_cr4c -v
python3 -m py_compile tools/pppp_cleanroom/*.py
python3 tools/pppp_cleanroom/probe_rtsp_publish.py --self-test
```

Preserve Linux CI, Docker build/startup validation, and App-image support smokes.

## Stop condition

After tests and CI pass:

1. commit and push only `pppp-cleanroom`;
2. keep PR #2 open and draft;
3. do not run live camera traffic;
4. report exact commit SHA and test totals;
5. state the exact HTTP status/body pair accepted and every case that remains fatal;
6. provide the next manual HAOS copy/self-test/smoke/live commands, with `--max-audio-validation-drops 12` explicitly in the live command;
7. do not merge, tag, release, change production transport, or restart/alter the installed App.
