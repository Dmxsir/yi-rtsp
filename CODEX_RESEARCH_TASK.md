# Codex Research Task — CR-4C temporary clean RTSP publication validation

## Scope

Work only in repository `Dmxsir/yi-rtsp` on branch `pppp-cleanroom`.

Do **not** modify, merge, rebase, tag, release, or otherwise change `main` or the installed production `0.2.0` vendor-runtime path. Keep PR #2 open and draft.

Call this gate **CR-4C** in code/docs. Do not rename the roadmap's existing `CR-5 — YI server rendezvous` milestone.

CR-2, CR-3, CR-4 and CR-4B are already live-proven on one owned direct-LAN `y291ga` / raw model `83` path. CR-4C is the next narrow research gate:

> Publish the already-proven clean PPPP → TNP → H.264/AAC → MPEG-TS path into a **temporary, isolated, loopback-only go2rtc instance**, consume that stream through a temporary RTSP endpoint, and validate the RTSP consumer view without changing the production App publisher, production ports, production stream registry, Home Assistant discovery, or Frigate.

Do **not** perform an unattended live-device experiment. Implement and test CR-4C offline, push only `pppp-cleanroom`, then stop with secret-safe manual commands for the user to run inside the Home Assistant App container.

## Read first

Read these files before changing code:

- `docs/PPPP_CLEANROOM_RESEARCH.md`
- `docs/PPPP_CLEANROOM_TRANSPORT.md`
- `docs/PPPP_CR2_LIVE_01.md`
- `docs/PPPP_CR3_LIVE_01.md`
- `docs/PPPP_CR4_LIVE_01.md`
- `docs/PPPP_CR4B_LIVE_01.md`
- `tools/pppp_cleanroom/yi_pppp.py`
- `tools/pppp_cleanroom/yi_pppp_session.py`
- `tools/pppp_cleanroom/tnp_stream.py`
- `tools/pppp_cleanroom/probe_channel0_tnp.py`
- `tools/pppp_cleanroom/probe_media_tnp.py`
- `tools/pppp_cleanroom/probe_sustained_mux.py`
- `tools/pppp_cleanroom/mux_pipe.py`
- `tests/test_pppp_cleanroom.py`
- `tests/test_pppp_cr4.py`
- `tests/test_pppp_cr4b.py`
- `yi_home/rootfs/opt/yi-home/app/yi_live_relay.py`
- `yi_home/rootfs/opt/yi-home/app/yi_native_av_relay.py`
- `yi_home/rootfs/opt/yi-home/app/yi_media_publisher.py`
- `yi_home/rootfs/opt/yi-home/app/yi_runtime_lifecycle.py`
- `yi_home/rootfs/opt/yi-home/app/yi_stream_identity.py`
- `yi_home/rootfs/opt/yi-home/app/tools/phase3_pppp_probe/run_phase3e_tnp.py`
- `yi_home/rootfs/opt/yi-home/app/yi_camera_manager.py`

Inspect the production publication path carefully before implementing anything. In particular understand and reuse semantics from:

- `YiGo2RTCPublisher._render_config(...)`;
- go2rtc incoming MPEG-TS endpoint `/api/stream.ts?dst=...`;
- the production lifecycle's first-chunk prebuffer before opening the HTTP POST;
- HTTP chunked MPEG-TS ingest;
- go2rtc `/api/streams` readiness/media reporting;
- the production RTSP path convention only as architecture reference;
- the existing FFmpeg H.264/AAC copy-mux and timestamp behavior already proven in CR-4B.

Do not call or start the normal App lifecycle/publisher as part of the CR-4C runner. Do not modify the currently running production go2rtc instance or its generated config. CR-4C must own a separate temporary go2rtc child with separate loopback ports and a synthetic research stream name.

## Current proven state

Treat the following as established evidence and do not regress it.

### CR-2 — LIVE PROVEN

On one owned YI `y291ga` / raw model `83` direct-LAN path, the clean client completed YI rendezvous, legacy punch, matching `P2P_RDY`, keepalive confirmation, and close without `libPPPP_API.so` performing the PPPP transport handshake.

### CR-3 — LIVE PROVEN

On the same path, reliable clean PPPP channel 0 carried the existing TNP startup:

```text
4881 -> 9029 -> 768 -> valid first 4882 -> 767 -> close
```

The 164-byte startup DRW was selectively acknowledged through D1.

### CR-4 — LIVE PROVEN

On the same path, clean reliable channels 1/2/3 carried real media accepted by the existing parsers:

- channel 2 H.264 I-frame;
- channel 3 H.264 P-frame;
- reordered video output;
- channel 1 AAC-LC/object type 2, 16 kHz, mono;
- `STOP_LIVE 767` and clean close.

### CR-4B — LIVE PROVEN

A bounded live run on 2026-09-07 proved sustained clean media plus the pipe-only mux boundary:

```text
cr3_control_result=PASS
media_channels_enabled=true
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
sustained_media_result=PASS
mux_started=true
mpegts_bytes_observed=1243808
ffprobe_video_codec=h264
ffprobe_audio_codec=aac
ffprobe_video_size=1920x1080
ffprobe_audio_sample_rate=16000
ffprobe_audio_channels=1
mux_validation_result=PASS
stop_live_767_sent=true
transport_closed=true
cr4b_result=PASS
```

Therefore, on this one path, the following chain is already live-proven:

```text
clean PPPP
-> TNP authentication/control
-> independent media channels
-> existing H.264/AAC parsers
-> existing video reorder
-> sustained media
-> FFmpeg copy-mux
-> MPEG-TS
-> ffprobe codec/format validation
```

CR-4B had zero DRW retransmissions and observed no D2 during the tested active window. This is evidence for that run only, not a protocol guarantee.

What remains unproven includes:

- RTSP publication/consumption over the clean path;
- go2rtc consumer parity;
- Home Assistant / Frigate parity;
- multi-hour stability;
- adverse packet loss/reordering;
- long-session D2 requirements;
- other camera models/firmware;
- F2, relay, wakeup, IPv6, or non-direct operation.

## CR-4C goal

Implement a separate research-only runner, for example:

```text
tools/pppp_cleanroom/probe_rtsp_publish.py
```

The exact filename may differ, but do not overload or weaken the CR-4B runner.

The intended **manual live** sequence is:

```text
start isolated temporary go2rtc on loopback-only non-production ports
-> register one synthetic empty research stream
-> clean CR-2 direct session
-> CR-3 TNP startup and valid 4882
-> enable clean channels 1/2/3
-> reuse CR-4/CR-4B parsers + reorder
-> FFmpeg copy-mux H.264/AAC to MPEG-TS
-> prebuffer first complete TS chunk
-> HTTP chunked POST MPEG-TS to temporary go2rtc /api/stream.ts?dst=<synthetic-name>
-> wait until go2rtc reports one media-ready MPEG-TS producer
-> consume the temporary loopback RTSP URL with a bounded local consumer
-> validate H.264 1920x1080 + AAC 16 kHz mono through RTSP
-> require real RTSP media activity for a bounded interval, not metadata-only startup
-> STOP_LIVE 767
-> end ingest
-> terminate RTSP consumer
-> terminate temporary go2rtc
-> clean PPPP CLOSE
```

No production port, stream, publisher, config, discovery, API contract, Home Assistant entity, go2rtc production process, Frigate configuration, or external Docker port mapping may be changed.

## Isolation requirements — mandatory

CR-4C must be incapable of silently attaching to or modifying the production publisher.

### 1. Loopback-only temporary publisher

The research go2rtc instance must bind only to loopback inside the App container:

```text
127.0.0.1:<temporary-api-port>
127.0.0.1:<temporary-rtsp-port>
```

Do not bind `0.0.0.0` or `[::]` in CR-4C.

### 2. Never use production go2rtc ports

The current production publisher uses its normal App ports (notably API 1984 and RTSP 8554 in the existing implementation). CR-4C must refuse those values and must not connect to them.

Use explicit research-only ports or safely selected free loopback ports. If fixed defaults are chosen, use clearly non-production defaults and perform a bind/preflight conflict check before spawning. A port already in use is a hard `CR4C_PORT_IN_USE` failure, not a reason to reuse an existing service.

### 3. Synthetic stream identity only

Do not put a real stable ID, camera name, DID, UID, or other device identifier into the temporary go2rtc config or RTSP path.

Use a fixed safe synthetic name such as:

```text
yi_cr4c_probe
```

or another constant clearly unrelated to real camera identity.

### 4. Temporary files only

Any generated go2rtc config/log needed by the research runner must live under a temporary CR-4C directory, preferably `/tmp/yi-cr4c/...`, with restrictive permissions where applicable.

Do not touch the production publisher state directory, `/data` go2rtc config, production generated YAML, or production logs.

The runner must remove its temporary config/state on normal exit. On failure it may retain only a secret-free textual diagnostic if explicitly justified; default is cleanup.

### 5. No external exposure

The live proof is entirely inside the App container. Do not add Docker host port mappings. Do not tell the user to expose the temporary RTSP port to the LAN.

The RTSP consumer must connect to `127.0.0.1` inside the container.

## Required implementation work

### 1. Preserve all existing gates

CR-2 / CR-3 / CR-4 / CR-4B focused tests and runners must continue to pass.

Do not weaken:

- handshake source gating;
- keepalive confirmation;
- reliable channel-0 D1 ACK/retry behavior;
- D2 observational-only handling;
- media-channel opt-in;
- bounded reliable/TNP/media buffers;
- H.264/AAC validation;
- video reorder semantics;
- CR-4B sustained duration gate;
- `STOP_LIVE 767` cleanup;
- transport close.

### 2. Reuse CR-4B media/mux logic

Do not duplicate a third media parser or timestamp model.

Prefer factoring the smallest neutral reusable boundary from CR-4B if needed. The flow before MPEG-TS must remain semantically identical to the already-proven CR-4B path:

```text
CleanPpppSession
 -> TnpUnitReader
 -> existing H264/AAC parser
 -> SequenceReorderBuffer
 -> existing SETTS/copy-mux semantics
```

Any refactor of CR-4B must have tests proving its existing behavior remains unchanged.

### 3. Research-only MPEG-TS sink abstraction

CR-4B currently validates FFmpeg MPEG-TS directly through a pipe/ffprobe path. CR-4C needs the same mux output to feed a go2rtc HTTP ingest instead.

Prefer a small research-only sink/pump abstraction that:

- reads FFmpeg MPEG-TS stdout continuously;
- validates at least TS sync/framing counters already used by CR-4B;
- prebuffers the first non-empty transport chunk before opening go2rtc ingest, matching the production lifecycle rationale;
- writes subsequent TS chunks incrementally with bounded memory;
- handles downstream EPIPE/connection close stage-specifically;
- does not retain the whole TS stream;
- does not write TS bytes to disk or stdout;
- exposes only sanitized counts/status.

Do not hash or print raw media/TS bytes.

### 4. Temporary go2rtc controller

Add a research-only helper/class that owns exactly one temporary go2rtc child.

It must:

- locate the existing go2rtc binary without changing production configuration;
- create a minimal synthetic config with one empty stream;
- bind API and RTSP only to `127.0.0.1`;
- disable unrelated listeners such as WebRTC where possible;
- start the child in its own process/session as appropriate;
- wait for `/api/streams` readiness with a bounded timeout;
- detect early exit;
- expose no secret data in logs;
- terminate then kill with bounded grace on cleanup;
- leave no child behind after PASS or FAIL.

Do not instantiate the normal App publisher in a way that could discover or manipulate production cameras. Reusing neutral config-rendering concepts is fine; the research controller should remain isolated and synthetic.

### 5. go2rtc MPEG-TS ingest

Use the same incoming endpoint semantics already present in production:

```text
POST /api/stream.ts?dst=yi_cr4c_probe
Content-Type: video/mp2t
Transfer-Encoding: chunked
Cache-Control: no-store
```

Important:

- do not open the POST before there is actual MPEG-TS data available;
- prebuffer the first chunk so go2rtc receives real TS immediately after its probe clock starts;
- use bounded connect/read/write timeouts;
- count published bytes safely;
- do not print request bodies;
- handle producer disconnect and HTTP errors stage-specifically.

Suggested failure categories:

```text
GO2RTC_START_FAILED
GO2RTC_START_TIMEOUT
GO2RTC_EARLY_EXIT
GO2RTC_INGEST_FAILED
GO2RTC_PRODUCER_NOT_READY
GO2RTC_PORT_IN_USE
```

Exact names may differ, but failures must be secret-safe and distinguishable.

### 6. Verify media-ready producer before RTSP consumer

Do not declare publication success just because the HTTP POST socket is open.

Poll the temporary go2rtc API for the synthetic stream and require:

- exactly the intended synthetic stream exists;
- at least one registered producer with `format_name == "mpegts"`;
- that producer reports non-empty media information before the RTSP consumer gate proceeds.

Use only sanitized counts/booleans in output.

Suggested diagnostics:

```text
go2rtc_ready=true
ingest_connected=true
mpegts_published_bytes=<count>
producer_registered=true
producer_media_ready=true
```

### 7. RTSP consumer validation

The consumer must connect to the **temporary loopback RTSP endpoint**, not to production `8554` and not to a host-mapped external port.

Use an available local tool such as ffprobe and/or ffmpeg. Pick the smallest robust bounded approach after inspecting tool behavior in the App image.

A metadata-only connect is not enough. CR-4C PASS should establish both:

1. correct stream identity/format through RTSP; and
2. actual media packets continue to arrive for a bounded consumer interval.

For the tested `y291ga` path require sanitized RTSP-observed facts:

- video codec H.264;
- video dimensions 1920x1080;
- audio codec AAC;
- audio sample rate 16000;
- audio channels 1;
- positive video packet/frame count through the RTSP consumer;
- positive audio packet/frame count through the RTSP consumer;
- consumer active/read span of at least a configurable minimum, recommended default 8–10 seconds.

Do not make exact FPS or bitrate a protocol invariant.

If ffprobe alone cannot reliably provide a bounded live packet-count interval, use ffprobe for metadata plus a second bounded ffmpeg null-output consumer for continued media evidence. The consumer must not save media.

Suggested sanitized output:

```text
rtsp_consumer_connected=true
rtsp_video_codec=h264
rtsp_video_size=1920x1080
rtsp_audio_codec=aac
rtsp_audio_sample_rate=16000
rtsp_audio_channels=1
rtsp_video_packets=<positive count>
rtsp_audio_packets=<positive count>
rtsp_consumer_active_seconds=<bounded decimal>
rtsp_consumer_result=PASS
```

Do not print an RTSP URL containing any real device identity. The synthetic local path itself is safe but printing it is unnecessary.

### 8. Sustained clean-source gate remains required

CR-4C must not reduce the source-side proof to a brief burst merely because RTSP starts.

Recommended defaults for the first live run:

```text
--duration 45
--min-active-seconds 30
--min-rtsp-consumer-seconds 8
```

The source-side I/P/AAC duration/count conditions proven by CR-4B remain part of PASS.

It is acceptable for the RTSP consumer to start after the producer becomes media-ready and overlap the remaining source window.

### 9. Cleanup ordering

Cleanup must work on every exit path.

Preferred order after startup was sent:

```text
attempt STOP_LIVE 767
close/finish FFmpeg input pipes
finish HTTP chunked ingest if possible
terminate RTSP consumer
terminate temporary go2rtc
close clean PPPP transport
remove temporary config/state
```

If another order is required to avoid deadlock/EPIPE, document and test it. The invariant is that no child/process/socket/thread remains after the runner exits.

A consumer disconnect must not accidentally convert a failed media/source run into PASS.

### 10. Timeouts, backpressure, and process lifecycle

All waits must be bounded:

- go2rtc startup/API readiness;
- HTTP ingest connect/send;
- producer-media readiness;
- RTSP consumer startup;
- RTSP consumer active interval;
- FFmpeg/go2rtc/consumer child shutdown;
- pump thread joins.

Handle at least:

- go2rtc binary missing;
- selected research port already in use;
- go2rtc early exit;
- HTTP ingest refusal/reset;
- producer registered but never media-ready;
- RTSP connect failure;
- RTSP wrong codec/format;
- RTSP consumer starvation;
- FFmpeg mux early exit;
- MPEG-TS pump backpressure/EPIPE;
- remote camera close;
- media stall;
- STOP failure;
- cleanup timeout.

Use stage-specific safe failure categories. Never include secrets or raw payloads in exceptions printed to the user.

## Self-test / smoke modes

### `--self-test`

Must remain completely isolated and report at least:

```text
cloud_used=false
network_used=false
tnp_sent=false
media_requested=false
ffmpeg_started=false
ffprobe_started=false
go2rtc_started=false
rtsp_consumer_started=false
runtime_support_imported=false
```

Self-test may test state machines/config rendering with synthetic values only. It must not open sockets or spawn processes.

### `--support-smoke-test`

May import the existing App parser/mux helpers but must not use cloud/device traffic and must not start processes.

### `--rtsp-support-smoke-test`

Add a separate smoke mode that verifies only local binary availability and safe command/config construction for:

- ffmpeg;
- ffprobe;
- go2rtc.

It must not start any process and must not bind/open network sockets.

Expected style:

```text
CR4C_RTSP_SUPPORT_SMOKE=PASS
ffmpeg_available=true
ffprobe_available=true
go2rtc_available=true
processes_started=false
network_used=false
```

## Offline tests required

Add focused tests, e.g. `tests/test_pppp_cr4c.py`, using synthetic/fake data only.

Cover at minimum:

### Existing regression gates

- CR-2/CR-3/CR-4/CR-4B tests remain green;
- CR-4B sustained proof semantics remain unchanged;
- D2 remains observational only.

### Temporary go2rtc config/controller

- config binds only `127.0.0.1`;
- config does not contain real/stable device identifiers;
- production ports are rejected;
- port-conflict preflight produces the correct failure;
- startup readiness success;
- startup timeout;
- early child exit;
- terminate/kill cleanup;
- temporary files removed;
- no orphan child after exception.

Use mocked sockets/processes where necessary so offline tests do not actually bind or spawn.

### MPEG-TS ingest

- first TS chunk is obtained before opening ingest;
- correct HTTP path uses only synthetic stream name;
- chunked framing for first/subsequent/final chunks;
- byte counter;
- HTTP refusal/reset;
- downstream EPIPE;
- bounded send/backpressure failure;
- no payload printed or persisted.

### Producer readiness

- stream absent;
- producer absent;
- MPEG-TS producer registered but medias empty;
- producer media-ready;
- unexpected extra/mismatched stream does not satisfy the gate.

### RTSP consumer

With fake subprocess outputs or synthetic metadata only:

- correct H.264/AAC metadata accepted;
- wrong video codec rejected;
- wrong resolution rejected;
- wrong audio codec/rate/channels rejected;
- zero video packet/frame count rejected;
- zero audio packet/frame count rejected;
- insufficient consumer active span rejected;
- consumer early exit;
- consumer timeout/starvation;
- process cleanup after all failures.

### End-to-end runner orchestration with fakes

Test the stage sequence:

```text
temporary go2rtc ready
-> clean control PASS
-> source media PASS
-> mux/ingest active
-> producer media-ready
-> RTSP consumer PASS
-> STOP
-> cleanup all children
-> transport close
-> CR4C PASS
```

Also test failures at each major stage and verify cleanup still runs.

### Relocation

The actual manual runner must work from nested temporary layout such as:

```text
/tmp/yi-cr4c/tools/pppp_cleanroom/
/tmp/yi-cr4c/tools/phase3_pppp_probe/
```

Add a relocated self-test/support-loader test for the actual live support path. Do not rely on a shallow fixed `Path.parents[n]` assumption.

## Security / privacy rules

Never commit, print, save, attach, or place in fixtures:

- YI APKs;
- `libPPPP_API.so` or proprietary binary bytes;
- pcap files;
- account credentials;
- UID / DID;
- InitString;
- license/device key;
- camera password;
- login token/secret;
- raw TNP auth material;
- raw DRW payloads;
- raw H.264/AAC payloads;
- MPEG-TS payloads;
- media files;
- real YI server addresses;
- real camera stable IDs;
- real camera names;
- production RTSP credentials if any.

Do not print hashes of secret/auth/media material as a workaround.

Safe diagnostics may include:

- stage booleans;
- model family/raw model number;
- selected path class `lan` / `wan`;
- channel numbers;
- DRW retry/D2 counters;
- frame/unit counts;
- bounded byte counts;
- sanitized codec metadata;
- loopback-only research port numbers if needed for debugging;
- process-alive booleans;
- bounded elapsed durations;
- stage-specific error categories.

## Clean-room / licensing rules

- Protocol constants/shapes must come from independently observed project evidence or compatible public documentation.
- Do not copy implementation code from repositories without a compatible explicit license.
- `frankzhangshcn/p2p_tnp` and `xen0bit/libPPCS_API` remain reference-only.
- `nosoop-onlyslop/p2pcam` remains feasibility/reference evidence only because its provenance includes proprietary decompilation; do not copy or derive from it.
- Existing repository App/parser/publisher code may be reused/refactored within this repository, provided production behavior is preserved and tests cover the change.

## Production isolation audit required

Before finishing, explicitly verify and report:

1. no change to `main`;
2. no merge/tag/release;
3. PR #2 remains draft/open;
4. no production App default changed;
5. no production go2rtc port/default changed;
6. no production stream registry/discovery contract changed;
7. no Home Assistant/Frigate config change;
8. no `libPPPP_API.so`, APK, pcap, or media artifact added;
9. CR-4C live runner uses synthetic stream identity;
10. CR-4C temporary publisher binds loopback only;
11. Codex performed no live device traffic.

## Documentation update

Update research docs on `pppp-cleanroom` only:

- mark CR-4B as **LIVE PROVEN for one owned direct-LAN y291ga path**;
- include the sanitized CR-4B metrics from `docs/PPPP_CR4B_LIVE_01.md`;
- mark CR-4C as **OFFLINE IMPLEMENTED / LIVE UNPROVEN** after implementation/tests;
- document the temporary loopback-only publisher architecture;
- keep RTSP/go2rtc real-device parity under UNKNOWN until the user manually runs the probe;
- keep Home Assistant/Frigate parity UNKNOWN;
- keep other models/F2/relay/wakeup/IPv6/non-direct paths UNKNOWN;
- keep D2 meaning INFERRED/UNKNOWN as currently documented.

Do not create a `PPPP_CR4C_LIVE_01.md` before an actual user-run PASS occurs.

## Manual App-container workflow to provide after offline completion

Codex must finish by giving secret-safe commands that the user can run from PVE through the existing repository App container.

Use exact commit-pinned downloads and preserve nested layout under `/tmp/yi-cr4c`.

The manual sequence must be:

1. create `/tmp/yi-cr4c/tools/pppp_cleanroom` and `/tmp/yi-cr4c/tools/phase3_pppp_probe`;
2. copy/download only the required research files from the exact CR-4C commit;
3. run `--self-test`;
4. run `--support-smoke-test --app-source /opt/yi-home/app`;
5. run `--rtsp-support-smoke-test --app-source /opt/yi-home/app`;
6. stop and return the smoke output for review;
7. provide, but do **not** execute, the live command template.

The live command should accept:

- `--camera-id <stable-id>`;
- one or more `--server <IPv4:32100>`;
- `--env-file /data/yi.env`;
- `--app-source /opt/yi-home/app`;
- explicit or safe defaults for loopback research API/RTSP ports;
- `--duration 45`;
- `--min-active-seconds 30`;
- `--min-rtsp-consumer-seconds 8` (or a justified nearby value).

Do not use production ports 1984 or 8554 in the live template.

The user will supply the known owned-camera stable ID and known server list outside the repository. Do not commit them.

## Suggested sanitized live output

A successful manual run should produce only safe evidence similar to:

```text
cr3_control_result=PASS
media_channels_enabled=true
source_media_active_seconds=...
source_video_i_frames=...
source_video_p_frames=...
source_audio_frames=...
source_media_result=PASS
mpegts_mux_started=true
temporary_go2rtc_ready=true
ingest_connected=true
mpegts_published_bytes=...
producer_registered=true
producer_media_ready=true
rtsp_consumer_connected=true
rtsp_video_codec=h264
rtsp_video_size=1920x1080
rtsp_audio_codec=aac
rtsp_audio_sample_rate=16000
rtsp_audio_channels=1
rtsp_video_packets=...
rtsp_audio_packets=...
rtsp_consumer_active_seconds=...
rtsp_consumer_result=PASS
stop_live_767_sent=true
temporary_go2rtc_stopped=true
rtsp_consumer_stopped=true
transport_closed=true
cr4c_result=PASS
```

Exact field names may differ, but the runner must make it obvious whether source media, publisher ingest, producer readiness, RTSP consumer, STOP, and cleanup each passed.

## CR-4C PASS definition

Do not report PASS unless all are true in one manual run:

1. clean CR-3 authentication/control succeeds;
2. clean H.264/AAC source media satisfies the CR-4B sustained gate;
3. FFmpeg MPEG-TS mux starts and produces bytes;
4. temporary loopback-only go2rtc starts on non-production ports;
5. MPEG-TS ingest connects and publishes positive bytes;
6. go2rtc reports the synthetic producer media-ready;
7. RTSP consumer connects to the temporary RTSP endpoint;
8. RTSP consumer sees H.264 1920x1080;
9. RTSP consumer sees AAC 16 kHz mono;
10. positive video and audio packets/frames are observed through RTSP;
11. RTSP consumer remains active for the configured minimum interval;
12. no source/mux/ingest/consumer buffer or timeout failure occurs;
13. `STOP_LIVE 767` is sent/attempted successfully according to the proven cleanup contract;
14. temporary RTSP consumer and go2rtc child are stopped;
15. clean PPPP transport closes;
16. no media is written to disk;
17. no production publisher/port/config/stream is touched;
18. no vendor PPPP transport fallback occurs.

If any condition fails, report a stage-specific `CR4C` failure and do not broaden/refactor unrelated code.

## Final Codex report

After implementation/offline tests, report:

- branch and final commit SHA;
- confirmation `main` unchanged;
- PR #2 state/draft status;
- changed files and purpose;
- focused test counts;
- full Linux CI result;
- self-test output;
- support smoke output;
- RTSP-support smoke output;
- confirmation no live device traffic by Codex;
- confirmation no production behavior/default changed;
- confirmation no proprietary/media artifacts committed;
- exact secret-safe manual App-container commands;
- remaining PROVEN / INFERRED / UNKNOWN boundaries.

Do not merge, tag, release, or execute the live device test.