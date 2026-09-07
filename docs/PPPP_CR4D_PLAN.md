# PPPP Clean-Room CR-4D — Frigate Container RTSP Parity

Status: **READY FOR MANUAL PVE VALIDATION**

## Goal

Validate that the already-proven clean-room media path can be consumed from the
running Frigate container across the Home Assistant internal Docker network,
without changing production Frigate configuration, Home Assistant camera
configuration, the production go2rtc instance, or the stable vendor-runtime
transport.

This is intentionally narrower than production Frigate integration. The first
CR-4D gate proves container/network/codec parity using Frigate's own runtime
container and ffprobe. It does not create or persist a Frigate camera.

## Source side

`tools/pppp_cleanroom/probe_cr4d_frigate.py` reuses the CR-4C orchestrator and
all existing source, mux, producer, RTSP-format, STOP 767, and cleanup gates.
The only publication change is the temporary research RTSP listener:

- go2rtc API: `127.0.0.1:11984` only;
- temporary RTSP: `0.0.0.0:18554` inside the App container;
- no Docker host port is published by this code;
- production ports `1984` and `8554` remain rejected;
- WebRTC remains disabled;
- the synthetic research stream remains `yi_cr4c_probe`;
- temporary state lives under `/tmp/yi-cr4d`;
- no media is saved.

The production App/container configuration is not changed. When the process
exits, the temporary go2rtc child and its temporary files are removed.

## Frigate-side verifier

`tools/pppp_cleanroom/probe_external_rtsp_consumer.py` is a standalone helper
intended to be copied to `/tmp` inside the running Frigate container. It:

- accepts only a private, non-loopback IPv4 source address;
- rejects production ports `1984` and `8554`;
- uses the Frigate container's existing `ffprobe` binary;
- connects over RTSP/TCP to the temporary research endpoint;
- requires H.264 1920x1080 plus AAC 16 kHz mono;
- requires positive video and audio packet counts;
- requires the configured minimum active duration;
- prints only sanitized codec/count/duration evidence;
- does not mutate Frigate/go2rtc/Home Assistant configuration.

## Offline validation

GitHub Actions validated the implementation on the exact research branch head
before manual PVE testing:

- full Linux unittest suite: `107/107 PASS`;
- existing CR-4C tests remained green;
- CR-4D external-bind isolation tests passed;
- Docker App image build passed;
- App-image CR-4C self/support/RTSP smokes passed;
- App-image `CR4D_SELF_TEST=PASS`;
- App-image `CR4D_EXTERNAL_CONSUMER_SELF_TEST=PASS`;
- no live device traffic was performed by CI.

## PASS condition

CR-4D PASS requires evidence from the same bounded run that:

1. the CR-4D source probe reaches the existing CR-4C PASS gates;
2. `cr4d_source_publication_result=PASS` is printed;
3. the external verifier is executed inside the running Frigate container;
4. it reaches the temporary App-container RTSP endpoint over the internal
   container network;
5. it validates H.264 1920x1080 and AAC 16 kHz mono;
6. video and audio packet counts are both positive;
7. the external consumer active duration is at least the configured minimum;
8. it prints `cr4d_frigate_consumer_result=PASS`;
9. STOP 767 and all temporary source-side cleanup still complete.

## What this does not prove

A CR-4D PASS will not yet prove:

- a persisted Frigate camera configuration;
- Frigate detection/recording/event pipelines;
- Home Assistant camera entity/WebRTC parity;
- multi-hour/day stability;
- adverse-loss or D2 long-session behavior;
- other YI models/firmwares;
- relay/WAN, wakeup, F2, or IPv6;
- production transport replacement readiness.

Those remain later, explicitly opt-in gates.

## Safety

No credentials, stable camera identifiers, YI server endpoints, raw media,
pcap data, APK bytes, proprietary YI library bytes, or host/container IP
addresses belong in committed CR-4D evidence.
