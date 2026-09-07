# PPPP Clean-Room CR-4D — Live Frigate Container RTSP Parity

Status: **LIVE PASS**

Date: 2026-09-07

## Scope

This run validated the bounded CR-4D gate against the real running Frigate
container while leaving production configuration unchanged.

The source side reused the proven clean-room PPPP -> TNP -> H.264/AAC ->
MPEG-TS -> temporary go2rtc path. The external verifier ran inside the actual
Frigate container and consumed the temporary RTSP stream over the host/LAN
path used for the isolated test.

No persistent Frigate camera configuration was created, no Home Assistant
camera configuration was changed, no production go2rtc configuration was
changed, and no production YI RTSP mapping was replaced.

## Sanitized source evidence

```text
cr4d_external_rtsp_enabled=true
cr4d_api_scope=loopback
cr4d_rtsp_scope=container_network
temporary_go2rtc_ready=true
cr3_control_result=PASS
media_channels_enabled=true
source_media_started=true
source_media_active_seconds=31.457
source_video_i_frames=10
source_video_p_frames=531
source_video_reordered_frames=541
source_audio_frames=533
audio_validation_drops=6
channel1_tnp_units=533
channel2_tnp_units=10
channel3_tnp_units=531
source_aac_sample_rate=16000
source_aac_channels=1
initial_av_delta_ms=33
drw_retries=0
d2_observed=0
source_media_result=PASS
mpegts_mux_started=true
ingest_connected=true
mpegts_published_bytes=1258096
ingest_finalize_mode=go2rtc_eof_after_terminal
producer_registered=true
producer_media_ready=true
rtsp_consumer_connected=true
rtsp_video_codec=h264
rtsp_video_size=1920x1080
rtsp_audio_codec=aac
rtsp_audio_sample_rate=16000
rtsp_audio_channels=1
rtsp_video_packets=160
rtsp_audio_packets=109
rtsp_consumer_active_seconds=8.612
rtsp_consumer_result=PASS
cr4c_result=PASS
cr4d_source_publication_result=PASS
stop_live_767_sent=true
rtsp_consumer_stopped=true
temporary_go2rtc_stopped=true
transport_closed=true
```

## Sanitized Frigate-container evidence

```text
external_consumer_context=frigate_container
external_rtsp_connected=true
external_rtsp_video_codec=h264
external_rtsp_video_size=1920x1080
external_rtsp_audio_codec=aac
external_rtsp_audio_sample_rate=16000
external_rtsp_audio_channels=1
external_rtsp_video_packets=160
external_rtsp_audio_packets=110
external_rtsp_active_seconds=10.174
cr4d_frigate_consumer_result=PASS
```

## Result

CR-4D is a **LIVE PASS** for the scoped gate.

The same bounded run proved all of the following:

1. clean-room PPPP session establishment and TNP control remained valid;
2. H.264 1920x1080 and AAC 16 kHz mono were produced by the clean path;
3. MPEG-TS publication reached temporary go2rtc;
4. an independent verifier running inside the real Frigate container connected
   to the temporary RTSP endpoint;
5. that verifier observed positive video and audio packet counts for more than
   the required minimum duration;
6. STOP 767 was sent and temporary source-side resources were stopped;
7. the clean transport closed normally;
8. production YI RTSP remained reachable after test cleanup.

## What remains unproven

This PASS does not yet prove:

- a persisted Frigate camera configuration;
- Frigate detection, recording, snapshots, or event pipelines;
- Home Assistant camera entity/WebRTC parity through the clean transport;
- multi-hour/day clean-transport stability;
- long-session D2 behavior or adverse packet-loss recovery;
- other YI models/firmwares;
- relay/WAN, wakeup, F2, or IPv6;
- production transport replacement readiness.

Those remain later explicit gates.

## Safety

No credentials, stable camera identifier, YI rendezvous endpoints, raw media,
packet captures, APK bytes, proprietary YI library bytes, host IP addresses, or
container IP addresses are recorded here.
