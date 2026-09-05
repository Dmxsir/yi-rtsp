# YI RTSP

Home Assistant App project for publishing YI Home cameras as local RTSP streams for Home Assistant and Frigate.

> This is an independent community project and is not affiliated with or endorsed by YI Technology.

## Status

The public repository is being prepared from the proven development runtime. **The installable App is not published yet.**

The current development build is working on Home Assistant OS and provides:

- YI cloud account discovery.
- App-owned, reusable YI cloud session.
- Per-camera PPPP/TNP runtime lifecycle.
- H264/AAC media relay.
- App-owned go2rtc RTSP publication.
- Persistent stream on/off intent.
- Authoritative online/offline checks.
- Private runtime-material IPC over a Unix-domain socket.
- Frigate-compatible RTSP export.

The remaining public-release blocker is packaging the vendor runtime without committing proprietary YI APK-derived binaries to this repository. See `docs/PUBLIC_PACKAGING.md`.

## Companion Home Assistant integration

Install **YI Camera Connect** through HACS after the App is available:

https://github.com/Dmxsir/yi-camera-connect

The internal Supervisor discovery service remains `yi_home` for compatibility between the App and the integration.

## Security model

- YI account credentials stay inside the App and are not exposed as Home Assistant entities.
- Cloud session tokens are memory-only.
- Camera runtime children do not receive the YI account/password in argv or environment.
- Runtime camera material is handed to child processes through a private Unix-domain socket.

## Development source

The historical reverse-engineering and development repository remains:

https://github.com/Dmxsir/yi-cam-integration

The public App repository will contain only material that is suitable for redistribution and reproducible installation.
