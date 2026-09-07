# CR-4B sustained clean media + pipe-only mux validation 01

Date: 2026-09-07

Status: **PASS** on one owned YI `y291ga` / raw model `83` direct-LAN camera path.

This report is intentionally sanitized. It does not contain a camera name, stable ID, DID, UID, endpoint address, InitString, license, device key, password, token, raw TNP authentication material, raw DRW/TNP/media payload, pcap, APK, proprietary library bytes, or saved media.

## Purpose

Validate the next clean-room boundary after CR-4: sustain real H.264/AAC media over the clean PPPP transport for a meaningful active window and feed the already-validated media through a pipe-only FFmpeg MPEG-TS copy-mux and ffprobe validation path, without RTSP/go2rtc/Frigate publication and without changing the production transport default.

The controlled experiment was:

```text
clean PPPP CR-2 session
-> reliable channel 0 / proven CR-3 TNP startup
-> valid first 4882
-> enable reliable channels 1/2/3
-> sustained H.264 I/P + AAC collection
-> existing sequence reorder
-> pipe-only FFmpeg H.264/AAC copy-mux
-> ffprobe validation from stdin
-> STOP_LIVE 767
-> clean PPPP CLOSE
```

No RTSP, go2rtc, Frigate publication, media file output, pcap capture, or production backend switch was part of the experiment.

## Sanitized result

The live probe reported:

```text
cr3_control_result=PASS
media_channels_enabled=true
stop_live_767_sent=true
transport_closed=true
sustained_media_started=true
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
cr4b_result=PASS
```

## What this proves

### PROVEN by this live experiment

1. The clean-room PPPP transport sustained real media on the tested owned direct-LAN `y291ga` path for more than the configured 30-second active-media gate.
2. Sustained traffic included repeated channel-2 H.264 I-frame activity, channel-3 H.264 P-frame activity, and channel-1 AAC activity.
3. The existing video reorder path emitted hundreds of ordered frames during the bounded run.
4. The existing AAC parser continued to validate 16 kHz mono audio throughout the sustained window.
5. The run completed with zero DRW retransmissions observed under the test conditions.
6. No D2 packet was observed during this specific bounded run; this is an observation only and does not redefine D2 semantics.
7. The already-validated clean media path fed a pipe-only FFmpeg copy-mux successfully.
8. More than 1.2 MB of MPEG-TS data was observed in-memory through the pipe path without saving media to disk.
9. ffprobe accepted the piped MPEG-TS and identified H.264 video at 1920x1080 plus AAC audio at 16 kHz mono.
10. `STOP_LIVE 767` was sent and the clean PPPP session closed successfully.
11. No vendor-library PPPP transport fallback, RTSP, go2rtc, Frigate publication, or production transport switch was used.

The strongest conclusion is therefore:

> On the tested owned `y291ga` direct-LAN path, the clean-room PPPP transport can sustain real H.264/AAC media for a meaningful interval and feed that media successfully through the project's existing pipe-only MPEG-TS mux/ffprobe boundary.

## What this does not prove

The experiment does **not** establish full production parity. The following remain outside this result:

- multi-hour or multi-day sustained operation;
- packet-loss/reordering behavior under adverse network conditions;
- exact D2 semantics and whether D2 is required in longer or lossy sessions;
- direct RTSP publication through the clean transport;
- go2rtc / Home Assistant / Frigate parity through the clean transport;
- other camera models and firmware generations;
- F2 framing;
- relay paths;
- wakeup behavior;
- IPv6;
- operation when a direct LAN candidate is unavailable;
- final production retry/window/chunk/buffer/timeout tuning.

## Next gate

CR-4B is now live-proven for this direct `y291ga` path. A reasonable next gate is a development-only clean RTSP publication path using the already-proven clean transport + TNP + media + mux stack, while preserving the current production vendor path as the default and keeping go2rtc/Frigate integration opt-in until repeated parity tests pass.

Production default switching and removal of the APK/vendor fallback remain gated behind broader compatibility and repeated long-run validation.
