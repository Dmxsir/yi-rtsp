# CR-4 live media-channel validation 01

Date: 2026-09-06

Status: **PASS** on one owned YI `y291ga` / raw model `83` direct-LAN camera path.

This report is intentionally sanitized. It does not contain a camera name, stable ID, DID, UID, endpoint address, InitString, license, device key, password, token, raw TNP authentication material, raw DRW/TNP/media payload, pcap, APK, proprietary library bytes, or saved media.

## Purpose

Validate the next clean-room boundary after CR-3: receive bounded YI TNP media on independent reliable PPPP channels 1/2/3, reconstruct complete media units from the channel byte streams, and pass them through the project's existing H.264/AAC parsers without `libPPPP_API.so` performing the transport/channel I/O.

The controlled experiment was:

```text
clean PPPP CR-2 session
-> reliable channel 0
-> 4881 -> 9029 -> 768
-> valid first 4882
-> enable bounded reliable channels 1/2/3
-> reconstruct TNP media units
-> validate channel-2 H.264 I-frame
-> validate channel-3 H.264 P-frame
-> validate channel-1 AAC
-> STOP_LIVE 767
-> clean PPPP CLOSE
```

No FFmpeg, RTSP, go2rtc, Frigate publication, media file output, or production transport change was part of the experiment.

## Sanitized result

The live probe reported:

```text
cr3_control_result=PASS
media_channels_enabled=true
channel2_tnp_units_valid=1
channel3_tnp_units_valid=1
h264_i_frame_valid=true
h264_p_frame_valid=true
h264_framing_valid=true
h264_frames_reordered=2
h264_nal_types=1,5,7,8
channel1_tnp_units_valid=1
aac_valid=true
aac_sample_rate=16000
aac_channels=1
aac_object_type=2
stop_live_767_sent=true
transport_closed=true
cr4_result=PASS
```

## What this proves

### PROVEN by this live experiment

1. The clean-room PPPP transport can continue from the already-proven CR-3 control boundary into reliable receive streams for media channels 1, 2, and 3 on real YI hardware.
2. The independent channel streams delivered complete TNP media records that the bounded stream reader could reconstruct without relying on DRW datagram boundaries.
3. The existing project video parser accepted a real channel-2 I-frame and a real channel-3 P-frame from the clean transport.
4. H.264 framing validation succeeded and the observed sanitized NAL types included 1, 5, 7, and 8.
5. The existing sequence reorder logic accepted and emitted two ordered video frames in the bounded probe.
6. The existing audio parser/decrypt path accepted a real channel-1 AAC record and reported AAC object type 2, 16 kHz, mono.
7. `STOP_LIVE 767` was sent and the clean PPPP session closed successfully.
8. `libPPPP_API.so` was not used to perform the CR-4 PPPP transport/channel exchange.
9. No media payload bytes were logged or persisted by the research probe.

The strongest conclusion is therefore:

> On the tested owned `y291ga` direct-LAN path, real H.264 I/P video and AAC media can be received and parsed through the clean-room PPPP transport after successful clean TNP authentication/control.

## What this does not prove

The experiment does **not** establish full production parity. The following remain outside this result:

- sustained long-duration media transport;
- behavior under packet loss/reordering beyond the short controlled run;
- exact D2 semantics and long-session D2 requirements;
- RTSP/MPEG-TS publication through the clean transport;
- go2rtc / Home Assistant / Frigate parity through the clean transport;
- production retry/window/chunk/buffer/timeout tuning;
- other camera models and firmware generations;
- F2 framing;
- relay paths;
- wakeup behavior;
- IPv6;
- behavior when a direct LAN candidate is unavailable.

## Next gate

CR-4 is now live-proven for this direct `y291ga` path. The next research gate should remain opt-in and bounded: run the clean transport for a longer sustained media window and/or connect its already-validated media output into the existing relay/mux boundary, while keeping production on the vendor-library path.

A practical next success boundary is:

```text
clean PPPP + proven TNP startup
-> sustained ordered H.264/AAC media
-> existing relay/mux accepts the clean media stream
-> bounded validation output only
-> STOP_LIVE 767
-> CLOSE
```

Production transport switching, default RTSP publication, and removal of the APK/vendor fallback remain gated until repeated parity testing succeeds.
