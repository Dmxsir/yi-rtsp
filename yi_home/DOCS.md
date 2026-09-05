# YI RTSP

YI RTSP is an experimental Home Assistant App that runs the validated native
YI PPPP/TNP camera runtime and publishes managed RTSP streams. The current App
supports `amd64` Home Assistant systems only.

## Before starting

Obtain the official YI Home APK yourself and place it at:

```text
/share/yi_rtsp/yi-home.apk
```

The App does not distribute or download the APK or `libPPPP_API.so`. On first
start it extracts the ARM64 library from the APK, validates it, and stores it
privately at `/data/vendor/libPPPP_API.so` with restrictive permissions.
Subsequent starts reuse that private copy. Runtime compatibility symlinks are
created only inside the running App.

The `/share` mount is read-only. If no valid private library or official APK is
available, startup stops with an actionable error.

## Runtime

The App keeps the validated internal layout and behavior:

```text
/opt/yi-home/app/                     Python engine
/opt/yi-home/runtime/bionic-root/     AArch64/Bionic guest runtime
/data/vendor/libPPPP_API.so           private persistent vendor library
/data/                                other persistent App state
```

The internal backend uses Supervisor discovery identifier `yi_home`. RTSP is
available on TCP 8554 inside the App network and can optionally be mapped to a
host port. The backend API is not exposed as a host port.

## Building

The complete redistributable build context is committed under `rootfs`; no
developer analysis tree, connected phone, or neighboring repository is needed.
From a fresh clone, build with:

```bash
docker build yi_home
```

Proprietary YI artifacts must never be placed in the repository. The Docker
ignore rules exclude them if one is added accidentally.

## Integration

The Home Assistant integration is maintained separately at
<https://github.com/Dmxsir/yi-cam-integration>.
