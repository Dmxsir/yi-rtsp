# CR-2 live transport-only validation 01

Date: 2026-09-06

Status: **PASS** on one owned YI `y291ga` / raw model `83` camera.

This report is intentionally sanitized. It does not contain a camera name, stable ID, DID, UID, endpoint address, InitString, license, device key, password, token, TNP payload, media payload, pcap, APK, or proprietary library bytes.

## Purpose

Validate the clean-room CR-2 direct F1 session path on real YI hardware without using `libPPPP_API.so` for transport.

The experiment was transport-only:

```text
cloud material
-> YI server rendezvous
-> HELLO / P2P_REQ
-> PUNCH_TO
-> legacy 20-byte PUNCH_PKT
-> P2P_RDY
-> ALIVE / ALIVE_ACK
-> CLOSE
```

No DRW, TNP command, video, audio, RTSP, Frigate, or production runtime change was part of this experiment.

## Environment

- Camera family: `y291ga`
- Raw model: `83`
- Transport family reported by cloud: TNP / PPPP type `2`
- Region/country: EU / IL account configuration
- Cloud material status: available
- Cloud `online` was treated only as a hint; UDP transport determined success.
- Probe ran from a temporary copy inside the Home Assistant App container.
- Production `main` and the installed production transport remained unchanged.

## Sanitized result

The live probe reported:

```text
cloud_material=ok
target_selection=stable_id
did_transport_bytes=20
connectivity_decision=udp_transport

rendezvous_sent=true
server_count=9

hello_ack=3
p2p_req_ack=3
candidates=2
lan_candidates=1
wan_candidates=1

legacy_punch_result=PASS
p2p_rdy=true
selected_path=lan
elapsed_ms=2568

alive_seen=true
alive_ack_seen=true

transport_result=PASS
tnp_gate=closed
media_gate=closed
close_sent=true
```

## What this proves

### PROVEN by this live experiment

1. The clean-room client can obtain fresh connection material for a selected owned camera without exposing secret-bearing fields in output.
2. The implemented YI F1 rendezvous path receives valid server replies and server-supplied LAN/WAN candidates.
3. This `y291ga` camera accepts the independently observed **legacy 20-byte `PUNCH_PKT`** even though YI extended punch forms also exist in captured traffic.
4. A matching `P2P_RDY` can be established without the proprietary PPPP library.
5. The selected direct path can exchange both `ALIVE` and `ALIVE_ACK` and reach the CR-2 `ESTABLISHED` state.
6. The session can be closed with the observed best-effort `CLOSE` behavior.
7. No TNP or media traffic is required to prove this transport establishment boundary.

The strongest conclusion is therefore:

> A real owned YI `y291ga` camera can establish a direct F1 PPPP session through the clean-room transport using the legacy 20-byte punch form, without `libPPPP_API.so` performing the transport handshake.

## What this does not prove

The experiment does **not** establish that every YI model or every network path accepts the legacy punch form. The following remain outside this result:

- other camera models / firmware generations;
- relay paths;
- F2 framing;
- wakeup behavior;
- IPv6;
- exact extended punch suffix construction;
- production timing/retry limits;
- long-session D2 behavior;
- live reliable DRW behavior;
- TNP authentication over the clean transport;
- media channels and sustained streaming.

## Next gate: CR-3

CR-2 is now live-proven for this direct `y291ga` path. The next controlled experiment may connect the already offline-tested channel-0 reliability code to the existing TNP builders.

The intended minimal CR-3 sequence is:

```text
clean PPPP session
-> reliable DRW channel 0
-> 4881
-> 9029
-> 768
-> first valid 4882 response
-> STOP_LIVE 767
-> CLOSE
```

CR-3 must remain a separate research-only experiment. It must not enable RTSP/media or change the production default transport.
