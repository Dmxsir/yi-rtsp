# Codex Research Task — CR-3 live reliable channel 0 / TNP

## Scope

Work only in repository `Dmxsir/yi-rtsp` on branch `pppp-cleanroom`.

Do **not** modify, merge, rebase, tag, release, or otherwise change `main` or the installed production `0.2.0` vendor-runtime path. Keep PR #2 open and draft.

The goal of this task is to prepare the next clean-room gate after the successful CR-2 live proof: connect the already offline-tested reliable PPPP channel-0 implementation to the existing TNP v2 control builders/parsers, then provide a safe manual live probe for an owned camera.

Do not perform an unattended live-device experiment. Build and test the CR-3 implementation offline first, then stop with a manual command/instructions for the user to run inside the Home Assistant App container.

## Read first

Read these files before changing code:

- `docs/PPPP_CLEANROOM_RESEARCH.md`
- `docs/PPPP_CLEANROOM_TRANSPORT.md`
- `docs/PPPP_CR1_CAPTURE_01.md`
- `docs/PPPP_CR1_CAPTURE_02_STARTUP.md`
- `docs/PPPP_CR2_LIVE_01.md`
- `tools/pppp_cleanroom/yi_pppp.py`
- `tools/pppp_cleanroom/probe_legacy_punch.py`
- `tests/test_pppp_cleanroom.py`
- `yi_home/rootfs/opt/yi-home/app/tools/phase3_pppp_probe/run_phase3e_tnp.py`
- `yi_home/rootfs/opt/yi-home/app/yi_tnp_oracle.py`
- `yi_home/rootfs/opt/yi-home/app/yi_camera_manager.py`

Also inspect any existing TNP builders/parsers/helpers used by Phase 3E rather than duplicating them.

## Current proven state

Treat the following as established evidence:

### CR-1 direct F1 capture

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

Observed DRW facts:

- outer `F1 D0` carries inner `D1 <channel> <seq:u16-be> <stream bytes>`;
- outer `F1 D1` carries selective ACK lists;
- outer `F1 D2` uses four payload bytes: `D2 <channel> <value:u16-be>`;
- D2 meaning is still inferred and must not be treated as proven cumulative ACK behavior;
- channel 0 carries TNP control;
- channels 1/2/3 carry audio / realtime I-frame / realtime P-frame traffic;
- application writes are a byte stream, not record boundaries;
- the three proven startup writes of 56 + 52 + 56 bytes appeared as one 164-byte channel-0 DRW payload.

### CR-2 live result

CR-2 is **LIVE PROVEN** on one owned `y291ga` / raw model `83` camera:

- YI server rendezvous succeeded;
- server supplied LAN + WAN candidates;
- the clean-room client deliberately sent the legacy 20-byte `PUNCH_PKT`;
- matching `P2P_RDY` was received;
- direct LAN path selected;
- both `ALIVE` and `ALIVE_ACK` were observed;
- `transport_result=PASS`;
- best-effort `CLOSE` was sent;
- no DRW, TNP, media, RTSP, or Frigate traffic was used for the CR-2 proof;
- proprietary `libPPPP_API.so` did not perform that transport handshake.

The CR-2 implementation selects the camera by secret-safe `stable_id` through `yi_camera_manager`; do not reintroduce a hard-coded camera name.

## CR-3 goal

Implement a **research-only** channel-0 TNP probe on top of the clean PPPP transport.

The minimal intended sequence is:

```text
clean PPPP session established
-> reliable DRW channel 0
-> TNP 4881
-> TNP 9029
-> TNP 768
-> receive and validate the first expected 4882 authentication/control response
-> send STOP_LIVE 767
-> close clean PPPP session
```

The existing Phase 3E TNP builders/parsers are authoritative for the application-layer construction and validation. Reuse them. Do not independently redesign TNP authentication.

## Required implementation work

1. **Do not weaken the existing CR-2 state machine.** Reuse the proven rendezvous/punch/ready/keepalive path.

2. Add a research-only clean transport session abstraction sufficient for CR-3. It should expose only the semantics needed by the experiment, for example:

```text
connect(...)
write_channel(channel, bytes)
read_channel(channel, max_bytes, timeout)
close()
```

The implementation does not need to mimic the proprietary C ABI.

3. Wire channel 0 to the existing `ReliableChannel` logic:

- ordered byte-stream semantics;
- 16-bit sequence wraparound;
- selective D1 ACK processing;
- retransmission of unacknowledged packets with bounded retry policy;
- out-of-order receive buffering;
- duplicate suppression while still acknowledging duplicates;
- byte-stream reads independent of DRW packet boundaries.

4. Preserve D2 as observational/parsed data unless live evidence proves a required behavior. Do **not** silently apply inferred D2 semantics to send state.

5. Continue transport keepalives during the short CR-3 session when required. Keep all timers and retry limits explicit/configurable; mark defaults as experimental.

6. Reuse existing Phase 3E TNP construction for exactly:

- `4881` set resolution;
- `9029` start realtime;
- `768` start audio;
- validation of the first expected `4882` response;
- `767` stop live.

Do not copy secret values into source or fixtures.

7. Treat the three startup commands as adjacent application writes to the same byte stream. The clean transport may coalesce them exactly as observed. Do not require one DRW packet per TNP write.

8. The CR-3 runner must select a camera by secret-safe stable ID through the generic camera manager. No hard-coded camera name and no fallback to an arbitrary camera.

9. The live runner must be safe to execute from a relocated temporary directory inside the HA App container, similarly to the CR-2 probe. Resolve runtime helpers robustly; do not assume a fixed `Path.parents[N]` depth.

10. The live runner must have a `--self-test` or equivalent offline mode that performs no cloud request and no network/device I/O.

## Media handling boundary

The TNP startup commands may cause the camera to begin sending data on non-zero channels. For CR-3:

- do not decode, save, publish, forward, or log media payloads;
- do not start RTSP/go2rtc/Frigate output;
- non-zero-channel DRW may be acknowledged or safely discarded only as needed to keep the short control experiment healthy;
- do not print raw packet payloads;
- stop immediately after the first valid expected 4882 response is proven, send 767, then close.

If the protocol requires behavior on non-zero channels to keep channel 0 alive, document exactly what is observed and keep processing payload-blind.

## Security / privacy rules

Never commit or print:

- YI APKs;
- `libPPPP_API.so` or any proprietary binary bytes;
- pcap files;
- account credentials;
- UID / DID;
- InitString;
- License or device key;
- camera password;
- login token / token secret;
- raw TNP authentication material;
- raw DRW/TNP/media payloads;
- real endpoint addresses from live sessions;
- real stable IDs in documentation, tests, or example commands.

Safe diagnostics may report counts, stages, boolean results, model family, raw model number, selected path class (`lan`/`wan`), sequence/ACK counters, byte counts, retry counts, and elapsed timings, provided they do not expose secret-bearing values.

## Clean-room / licensing rules

- Protocol constants and shapes must come from the project's independently observed evidence or compatible public documentation.
- Do not copy implementation code from repositories without a compatible explicit license.
- Treat `frankzhangshcn/p2p_tnp` and `xen0bit/libPPCS_API` as reference-only.
- Treat `nosoop-onlyslop/p2pcam` as feasibility/reference evidence only because its provenance includes proprietary decompilation; do not copy or derive implementation code from it.
- Compatible references already documented include `devbis/aiopppp` (Apache-2.0), `elastic/camera-hacks` (MIT), and `magicus/pppp-dissector` (MIT), but YI wire behavior must remain grounded in our own evidence.

## Offline tests required

Extend focused tests to cover at least:

- transport session moves from established CR-2 state into channel-0 DRW mode only after keepalive-confirmed establishment;
- exact D0/D1 codec round trips used by the runner;
- queueing 56 + 52 + 56 application bytes can become one 164-byte DRW stream payload;
- selective ACK removal;
- retransmission timeout and bounded retry failure;
- receive ordering and duplicate suppression;
- sequence wraparound;
- partial `read_channel()` boundaries that do not align with DRW packets;
- channel mismatch handling;
- malformed D0/D1/D2 rejection or safe ignore behavior;
- D2 parsed without changing reliability state;
- non-zero channel traffic cannot contaminate channel-0 reads;
- CR-3 self-test runs from repository checkout;
- relocated `/tmp` execution works with only the required clean-room files plus the installed App runtime helpers;
- self-test does not import cloud/runtime support or send network/TNP/media traffic.

Do not require a live camera in CI.

## Live runner success criteria

Create a separate, explicit research command/tool. A live CR-3 run is considered **PASS** only if all of the following are observed on an owned camera:

```text
CR2 transport established
channel0_drw_started=true
startup_tnp_bytes_sent=<sanitized byte count>
startup_drw_acked=true
first_4882_valid=true
stop_live_767_sent=true
transport_closed=true
cr3_result=PASS
```

Exact output naming may differ, but it must be secret-safe and unambiguous.

If the transport reaches CR-2 but channel 0 fails, return a stage-specific nonzero result instead of falling back to the vendor library.

Examples of useful failure categories:

```text
NO_DRW_ACK
CHANNEL0_READ_TIMEOUT
TNP_RESPONSE_INVALID
RETRY_LIMIT
REMOTE_CLOSE
```

Do not claim CR-3 success on `P2P_RDY`, keepalive, or DRW ACK alone. The success boundary is a valid first expected 4882 response over the clean transport followed by the stop-live attempt and clean close.

## Live execution policy

Do **not** run the live experiment automatically from Codex.

At the end of this task:

1. ensure all offline tests pass;
2. push only to `pppp-cleanroom`;
3. keep PR #2 draft;
4. provide the exact commit SHA;
5. provide a concise list of files changed;
6. provide a secret-safe manual command for the user to copy the required files into `/tmp` inside the Home Assistant App container and run self-test first;
7. provide the subsequent live command template using a placeholder such as `<CAMERA_STABLE_ID>` rather than a real stable ID;
8. stop and wait for the user's live output.

## Documentation updates

Update the research/specification docs to distinguish clearly:

### PROVEN

- CR-2 live direct F1 establishment on one owned `y291ga` path;
- any additional facts proven by offline tests only, labeled as such.

### INFERRED

- D2 cumulative/next-expected interpretation;
- retransmission/window/chunk/timing defaults until measured live.

### UNKNOWN

- CR-3 live TNP success until the user runs the manual probe;
- other models;
- F2;
- relay;
- wakeup;
- IPv6;
- long-session D2 behavior;
- media parity;
- production timing limits.

Do not mark CR-3 live as proven based only on implementation or offline tests.

## Production constraints

- Do not modify production startup behavior.
- Do not add a production `clean_pppp` selector yet.
- Do not remove the APK/vendor-library path.
- Do not change Home Assistant discovery, RTSP ports, go2rtc, Frigate, or HACS integration behavior.
- Do not create a release or tag.
- Do not merge PR #2.

## Final Codex report

Return a concise report with:

```text
branch SHA
main SHA (confirm unchanged)
PR #2 draft/open status
CI status
focused test result
files changed

PROVEN
INFERRED
UNKNOWN

manual self-test command
manual live CR-3 command template
```

The final report must explicitly state whether any live device traffic was performed by Codex. The expected answer for this task is **no**.
