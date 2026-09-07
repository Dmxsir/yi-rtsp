# Codex Research Task — CR-4C ingest-finalization hardening after second live run

## Scope

Work only in repository `Dmxsir/yi-rtsp` on branch `pppp-cleanroom`.

Do **not** modify, merge, rebase, tag, release, or otherwise change `main` or the installed production `0.2.0` vendor-runtime path. Keep PR #2 open and draft.

Do **not** perform live device traffic. This is a narrow CR-4C corrective task after the second manual live run proved the full source + go2rtc + RTSP consumer path but still ended with a teardown/finalization failure.

## Second live result to preserve

The manual run on 2026-09-07 produced this sanitized result:

```text
temporary_go2rtc_ready=true
cr3_control_result=PASS
media_channels_enabled=true
stop_live_767_sent=true
rtsp_consumer_stopped=true
temporary_go2rtc_stopped=true
transport_closed=true
source_media_started=true
source_media_active_seconds=32.759
source_video_i_frames=10
source_video_p_frames=531
source_video_reordered_frames=541
source_audio_frames=536
audio_validation_drops=3
channel1_tnp_units=536
channel2_tnp_units=10
channel3_tnp_units=531
source_aac_sample_rate=16000
source_aac_channels=1
initial_av_delta_ms=30
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
rtsp_video_packets=144
rtsp_audio_packets=125
rtsp_consumer_active_seconds=10.372
rtsp_consumer_result=PASS
cr4c_result=FAIL; failure_category=GO2RTC_INGEST_FAILED
```

Interpretation:

- CR-3 control/auth passed.
- sustained clean H.264/AAC source gate passed for >30 seconds.
- exactly 3 isolated `AudioUnitValidationError` records were dropped within the configured experimental bound; 536 valid AAC frames still passed the source gate.
- temporary go2rtc registered a real MPEG-TS producer and reported media-ready.
- a real loopback RTSP consumer successfully received H.264 1920x1080 + AAC 16 kHz mono for >10 seconds, with positive video/audio packet counts.
- STOP 767, RTSP consumer stop, temporary go2rtc stop, and clean transport close all completed.
- the only remaining failure category was `GO2RTC_INGEST_FAILED`, observed during finalization/teardown after the RTSP consumer gate had already passed.

Do **not** call CR-4C full PASS yet. The goal of this task is to determine and fix only this finalization false-negative without weakening streaming failure detection.

## Root cause area to inspect first

Read these files before editing:

- `tools/pppp_cleanroom/rtsp_publish.py`
- `tools/pppp_cleanroom/probe_rtsp_publish.py`
- `tools/pppp_cleanroom/mux_pipe.py`
- `tools/pppp_cleanroom/probe_sustained_mux.py`
- `tests/test_pppp_cr4c.py`
- `tests/test_pppp_cr4b.py`
- `yi_home/rootfs/opt/yi-home/app/yi_runtime_lifecycle.py`
- `yi_home/rootfs/opt/yi-home/app/yi_media_publisher.py`
- `docs/PPPP_CLEANROOM_RESEARCH.md`
- `docs/PPPP_CLEANROOM_TRANSPORT.md`

Current likely path:

```text
MpegTsIngestMux._pump_output()
 -> ChunkedIngestSink.finish()
 -> send terminal HTTP chunk `0\r\n\r\n`
 -> wait for HTTP response with getresponse()/read()
 -> any OSError/http.client.HTTPException
 -> GO2RTC_INGEST_FAILED
 -> mux.finish() fails despite source + producer + RTSP consumer already passing
```

Confirm the exact lifecycle in code before changing anything.

## Required behavior

### 1. Do not blanket-ignore GO2RTC_INGEST_FAILED

Streaming-phase ingest failure must remain fatal.

The following must still fail CR-4C:

- failure opening the POST;
- failure sending the first MPEG-TS chunk;
- failure sending any MPEG-TS chunk while source/consumer proof is still in progress;
- invalid TS framing;
- HTTP response with status >= 400 when a valid response is actually received;
- premature producer loss before RTSP proof completes;
- go2rtc process exit;
- mux failure/backpressure;
- any failure before `source_media_result=PASS` and `rtsp_consumer_result=PASS`.

### 2. Separate streaming failure from terminal-finalization outcome

Refactor `ChunkedIngestSink.finish()` / `MpegTsIngestMux._pump_output()` so the code can distinguish at least:

- normal HTTP final response success;
- exact peer EOF/`http.client.RemoteDisconnected` while waiting for the HTTP response **after the terminal chunk was successfully sent**;
- terminal-chunk send failure;
- timeout/reset/other socket or HTTP protocol failure;
- explicit HTTP >=400 response.

Do not collapse all of these to one generic category internally.

Use secret-safe stage names only. Example result/status names are acceptable:

```text
ingest_finalize_mode=http_response
ingest_finalize_mode=peer_closed_after_terminal
```

or equivalent booleans/enums.

Do not print exception text, endpoint data, payloads, headers, or media bytes.

### 3. Conservative acceptance rule

A peer close after the terminal chunk may be treated as a successful bounded-research finalization **only** when all of these are true:

- the terminal chunk was sent successfully;
- TS framing remained valid;
- positive MPEG-TS bytes were published;
- go2rtc producer was previously registered and media-ready;
- the RTSP consumer result is already PASS with expected codecs/formats and positive packet counts;
- the sustained source gate is PASS;
- no earlier ingest/mux/pump failure occurred;
- STOP 767 is sent;
- consumer, go2rtc, and clean transport cleanup complete.

Do not infer those high-level conditions inside the low-level HTTP sink if that makes layering worse. Prefer returning a classified finalization result upward and letting `probe_rtsp_publish.py` apply the CR-4C PASS policy.

### 4. Keep exact `RemoteDisconnected` scope narrow

If the implementation uses `http.client.RemoteDisconnected`, catch/classify it separately before broad OSError/HTTPException handling.

Do **not** automatically accept:

- `BrokenPipeError` while sending the terminal chunk;
- `ConnectionResetError` before terminal send is confirmed;
- socket timeout;
- malformed HTTP response;
- HTTP status >=400;
- any mid-stream send failure.

If offline inspection shows go2rtc has another deterministic clean-close behavior, model it explicitly and test it. Do not broadly accept all close/reset conditions.

### 5. Preserve CR-4B and audio-drop fix

Do not change:

- CR-4B strict default `max_audio_validation_drops=0`;
- CR-4C default `--max-audio-validation-drops 3`;
- only `AudioUnitValidationError` being droppable;
- dropped units not affecting valid counts/timing/mux;
- `AUDIO_VALIDATION_DROP_LIMIT` behavior.

### 6. Preserve isolation

Do not change:

- loopback-only go2rtc;
- synthetic stream `yi_cr4c_probe`;
- rejection of production ports 1984/8554;
- defaults 11984/18554;
- no host mapping;
- no production publisher/lifecycle/HA/Frigate changes;
- no saved media.

### 7. Better safe diagnostics

For the next manual run, print enough sanitized state to classify finalization without packet data. Suggested fields:

```text
mpegts_mux_started=true
ingest_connected=true
mpegts_published_bytes=<count>
ingest_finalize_mode=<safe-enum>
```

If finalization is fatal, use a stage-specific category such as:

```text
GO2RTC_INGEST_FINALIZE_SEND_FAILED
GO2RTC_INGEST_FINALIZE_TIMEOUT
GO2RTC_INGEST_FINALIZE_PROTOCOL_FAILED
GO2RTC_INGEST_REJECTED
```

Exact names may differ, but avoid reverting to an ambiguous generic category for terminal-only failures.

## Offline tests required

Add tests proving at least:

1. mid-stream POST send failure remains fatal.
2. first-chunk send failure remains fatal.
3. terminal chunk send failure remains fatal.
4. exact `RemoteDisconnected` from `getresponse()` after terminal chunk send is classified separately.
5. that classified peer-close result is accepted by CR-4C only when source PASS + producer-ready + RTSP consumer PASS + positive published bytes + clean mux/cleanup are all present.
6. the same peer-close is **not** enough to pass if RTSP consumer failed or source gate failed.
7. HTTP >=400 remains fatal.
8. timeout/reset/protocol errors remain fatal unless explicitly justified by deterministic go2rtc behavior and covered by a narrow test.
9. normal HTTP response success still passes.
10. `MpegTsIngestMux.finish()` still validates FFmpeg exit, pump completion, TS framing, and positive published bytes.
11. CR-4B tests remain unchanged/green.
12. audio-validation-drop tests remain green.
13. all CR-2/3/4/4B/4C tests remain green.
14. self-test/support/RTSP-support smoke remain process/network-safe as currently defined.
15. relocated `/tmp/yi-cr4c/...` execution remains valid.

Use fakes/synthetic bytes only. No real media fixtures and no live network/device traffic.

## Documentation

Update docs narrowly with the second live evidence:

- source sustained gate PASS;
- 3 bounded audio validation drops;
- producer registered/media-ready;
- RTSP consumer PASS for >10 seconds with H.264 1920x1080 + AAC 16k mono and positive packet counts;
- final CR-4C result remained FAIL only due terminal `GO2RTC_INGEST_FAILED`;
- CR-4C remains `LIVE UNPROVEN` until the finalization path is fixed and a subsequent manual run returns `cr4c_result=PASS`.

Do not record real stable IDs, server endpoints, credentials, or raw media.

## Validation before push

Run at minimum:

```text
python3 -m unittest tests.test_pppp_cleanroom tests.test_pppp_cr4 tests.test_pppp_cr4b tests.test_pppp_cr4c -v
python3 -m py_compile tools/pppp_cleanroom/*.py
python3 tools/pppp_cleanroom/probe_rtsp_publish.py --self-test
```

Also preserve current Linux App-image smoke tests, startup validation, and Docker build.

## Stop condition

After tests and CI pass:

1. commit and push only `pppp-cleanroom`;
2. keep PR #2 open and draft;
3. do not run live device traffic;
4. report exact commit SHA, changed files, test totals, and exact new safe output/finalization classification;
5. provide manual copy/self-test/smoke/live commands for one next HAOS retry;
6. explicitly state what exact terminal condition is now accepted and which terminal/mid-stream failures remain fatal.

Do not merge, tag, release, switch production transport, or alter the running App.