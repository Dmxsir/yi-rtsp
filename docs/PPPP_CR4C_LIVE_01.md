# PPPP Clean-Room CR-4C Live Validation 01

Date: 2026-09-07

Status: **PASS**

## Scope

This bounded manual HAOS experiment validated temporary RTSP publication for the clean-room PPPP/TNP/media path without changing the production transport, production go2rtc instance, Frigate configuration, or Home Assistant production behavior.

The test remained isolated to the research runner, temporary loopback-only go2rtc, synthetic stream name, and non-production ports.

No credentials, stable camera identifiers, server endpoints, raw media, pcap data, APK content, or proprietary YI library bytes are recorded here.

## Sanitized live result

```text
temporary_go2rtc_ready=true
cr3_control_result=PASS
media_channels_enabled=true
stop_live_767_sent=true
rtsp_consumer_stopped=true
temporary_go2rtc_stopped=true
transport_closed=true
source_media_started=true
source_media_active_seconds=31.033
source_video_i_frames=10
source_video_p_frames=510
source_video_reordered_frames=520
source_audio_frames=514
audio_validation_drops=5
channel1_tnp_units=514
channel2_tnp_units=10
channel3_tnp_units=510
source_aac_sample_rate=16000
source_aac_channels=1
initial_av_delta_ms=71
drw_retries=0
d2_observed=0
source_media_result=PASS
mpegts_mux_started=true
ingest_connected=true
mpegts_published_bytes=1193988
ingest_finalize_mode=go2rtc_eof_after_terminal
producer_registered=true
producer_media_ready=true
rtsp_consumer_connected=true
rtsp_video_codec=h264
rtsp_video_size=1920x1080
rtsp_audio_codec=aac
rtsp_audio_sample_rate=16000
rtsp_audio_channels=1
rtsp_video_packets=141
rtsp_audio_packets=125
rtsp_consumer_active_seconds=9.658
rtsp_consumer_result=PASS
cr4c_result=PASS
```

## What this proves

The following chain is now live-proven on one owned direct-LAN YI camera path:

1. clean-room PPPP transport establishes the session;
2. CR-3 TNP control/auth succeeds;
3. reliable media channels 1/2/3 carry sustained H.264 and AAC;
4. H.264 I/P frames and AAC 16 kHz mono are validated;
5. the media is copy-muxed to MPEG-TS in memory;
6. MPEG-TS is ingested into a temporary loopback-only go2rtc instance;
7. go2rtc registers the synthetic producer and reports media-ready;
8. an independent loopback RTSP consumer receives H.264 1920x1080 plus AAC 16 kHz mono with positive packet counts for more than the configured minimum duration;
9. STOP_LIVE 767 is sent;
10. RTSP consumer, temporary go2rtc, and clean transport all stop cleanly;
11. terminal HTTP 500 with exact bounded body `EOF` after the terminal chunk is classified as `go2rtc_eof_after_terminal`, matching go2rtc 1.9.14 finite-body EOF behavior;
12. the complete CR-4C gate returns `cr4c_result=PASS`.

## Audio validation note

This run used the explicit experimental override `--max-audio-validation-drops 12`. Five isolated `AudioUnitValidationError` records were dropped while 514 valid AAC records still passed the source gate. This override is a bounded research setting, not a protocol invariant and not a production default.

CR-4B remains strict by default and CR-4C's default drop allowance remains unchanged.

## Remaining unproven areas

CR-4C PASS does not yet prove:

- multi-hour or multi-day stability;
- adverse packet loss/reordering behavior;
- long-session D2 semantics;
- additional YI models/firmwares;
- F2 transport;
- relay/WAN paths;
- wakeup behavior;
- IPv6;
- production go2rtc/Frigate/Home Assistant consumer parity;
- production tuning or production transport replacement.

Production remains on the existing stable vendor-runtime path until later gates are explicitly completed and approved.
