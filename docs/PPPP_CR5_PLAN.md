# PPPP Clean-Room CR-5 — Sustained Frigate-Side Stability Gate

Status: **READY FOR MANUAL PVE VALIDATION**

## Goal

Extend the successful CR-4D bounded parity proof into a longer, side-by-side
clean-room stability run before any persistent Frigate camera configuration is
created or any production YI RTSP path is replaced.

CR-5 is intentionally still isolated from production configuration. It is a
soak/stability gate, not a cutover gate.

## CR-5A first gate

The first CR-5 run targets:

- at least 300 seconds of clean source media activity;
- at least 240 seconds of independent RTSP consumption from the real running
  Frigate container;
- H.264 1920x1080 plus AAC 16 kHz mono throughout the validated path;
- positive and sustained I-frame, P-frame, and audio counts;
- no media stall of 5 seconds or more;
- successful STOP 767 and clean transport/process cleanup;
- production YI RTSP remaining reachable after cleanup.

The existing CR-4D source and external-consumer probes are reused. No Frigate,
Home Assistant, or production go2rtc configuration is changed.

## Audio validation-drop policy

Short CR-4C/CR-4D live evidence showed a small recurring number of narrowly
classified `AudioUnitValidationError` drops. The long-run gate must not simply
raise the old short-run absolute limit without a criterion.

For CR-5A, the temporary safety ceiling is 100 validation drops over the
300-second source gate. The final evidence must also satisfy a validation-drop
ratio no greater than 2.0%, calculated as:

`drops / (accepted_audio_frames + drops)`

The 2.0% gate is deliberately above the roughly 0.8-1.1% observed in the
successful short live runs, while still bounding degradation. Generic audio
parse errors remain fatal; only the already-classified validation exception may
be dropped.

This is a research acceptance criterion only, not a production policy.

## Capacity adjustments for the longer run

The short CR-4 probes used 4096-record count ceilings. A 300-second run can
legitimately exceed those counts at the observed frame cadence, so CR-5A raises
only the test record-count ceilings to 12000 for video and 12000 for audio.
These are count guards, not media persistence buffers.

All existing byte bounds, pre-mux bounds, channel buffers, retry limits,
keepalive behavior, and no-persistence rules remain unchanged.

## PASS condition

CR-5A PASS requires evidence from the same run that:

1. `cr4d_source_publication_result=PASS`;
2. `cr4d_frigate_consumer_result=PASS` from the actual Frigate container;
3. source active time is at least 300 seconds;
4. external Frigate-container consumer active time is at least 240 seconds;
5. H.264 remains 1920x1080 and AAC remains 16 kHz mono;
6. source I/P/audio counts remain positive and sustained;
7. validation-drop ratio is <= 2.0%;
8. `drw_retries` and `d2_observed` are recorded for evidence, without assuming
   either must be zero;
9. STOP 767 is sent;
10. temporary go2rtc/consumer resources stop and the clean transport closes;
11. production RTSP remains reachable after cleanup.

## What CR-5A will not prove

Even a CR-5A PASS does not yet prove:

- multi-hour/day stability;
- persisted Frigate configuration, detection, recording, snapshots, or events;
- Home Assistant camera/WebRTC parity through the clean path;
- WAN/relay, wakeup, F2, IPv6, or other camera models;
- production cutover readiness.

Those remain later explicit gates.

## Safety

Do not commit credentials, stable camera identifiers, YI rendezvous endpoints,
raw media, packet captures, APK/proprietary library bytes, or local host/container
addresses in CR-5 evidence.
