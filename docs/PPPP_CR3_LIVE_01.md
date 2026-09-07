# CR-3 live reliable channel-0 / TNP validation 01

Date: 2026-09-06

Status: **PASS** on one owned YI `y291ga` / raw model `83` camera.

This report is intentionally sanitized. It does not contain a camera name, stable ID, DID, UID, endpoint address, InitString, license, device key, password, token, raw TNP authentication material, raw DRW/TNP/media payload, pcap, APK, or proprietary library bytes.

## Purpose

Validate the next clean-room boundary after CR-2: carry the existing YI TNP v2 control/authentication exchange over the independently implemented reliable PPPP channel 0, without `libPPPP_API.so` performing the transport or channel I/O.

The controlled experiment was:

```text
clean PPPP CR-2 session established
-> reliable DRW channel 0
-> 4881
-> 9029
-> 768
-> selective D1 ACK confirms startup DRW
-> first valid 4882 authentication/control response
-> STOP_LIVE 767
-> clean PPPP CLOSE
```

No RTSP, go2rtc, Frigate, media decode, media save, or production transport change was part of the experiment.

## Sanitized result

The live probe reported:

```text
cr2_transport_established=true
selected_path=lan
target_selection=stable_id
secrets_exposed=false

channel0_drw_started=true
startup_tnp_bytes_sent=164
startup_drw_packets=1
startup_drw_acked=true

first_4882_valid=true
stop_live_767_sent=true
transport_closed=true
cr3_result=PASS
```

## What this proves

### PROVEN by this live experiment

1. The clean-room PPPP transport can transition from the already-proven direct CR-2 session into reliable DRW channel 0 on real YI hardware.
2. The three existing Phase 3E startup units (`4881`, `9029`, `768`) can be queued as adjacent byte-stream writes and transmitted as one 164-byte DRW payload, matching the earlier passive capture.
3. The camera acknowledged that startup DRW through the implemented selective D1 ACK path.
4. The clean transport received enough ordered channel-0 bytes to reconstruct the first TNP response header and body.
5. The existing Phase 3E validator accepted that response as the expected `4882`, request number `1`, with authentication result `0`.
6. `STOP_LIVE 767` was sent over the same clean channel-0 path.
7. The clean PPPP session then closed successfully.
8. `libPPPP_API.so` was not used to perform this CR-3 transport/channel exchange.

The strongest conclusion is therefore:

> On the tested owned `y291ga` direct-LAN path, YI TNP authentication/control works over the clean-room reliable PPPP channel-0 implementation through the first valid `4882` response, followed by stop-live and close.

## What this does not prove

The experiment does **not** establish full production parity. The following remain outside this result:

- sustained channel-0 operation under packet loss;
- exact D2 semantics or long-session D2 requirements;
- media decoding/parity on channels 1/2/3;
- sustained H264/AAC relay;
- RTSP/go2rtc/Frigate output through the clean transport;
- other camera models / firmware generations;
- F2 framing;
- relay paths;
- wakeup behavior;
- IPv6;
- production timing/window/retry limits;
- behavior when the direct-LAN candidate is unavailable.

## Next gate: CR-4

CR-3 is now live-proven for this direct `y291ga` path. The next controlled experiment may connect payload-blind/parsed media-channel delivery from the clean PPPP session into the existing media parser/relay without changing the production default.

The initial CR-4 success boundary should remain narrow:

```text
clean PPPP + proven TNP startup
-> receive channel 2/3 media records
-> existing parser recognizes valid H264 units
-> optional channel 1 AAC recognition where present
-> short bounded stream
-> STOP_LIVE 767
-> CLOSE
```

RTSP publication and production backend switching should remain gated until media parity is proven separately.
