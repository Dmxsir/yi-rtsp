# YI RTSP

YI RTSP is an experimental Home Assistant App that runs the validated native
YI PPPP/TNP camera runtime and publishes managed RTSP streams. The current App
supports `amd64` Home Assistant systems only.

## First start

The App does not distribute or download the YI Home APK or `libPPPP_API.so`.
You provide your own official YI Home APK once through Home Assistant Ingress:

1. Install and start **YI RTSP**.
2. Open the App's **Web UI**.
3. Select the official YI Home APK and upload it.
4. The App validates the APK, extracts only the required ARM64
   `libPPPP_API.so`, stores it privately under `/data/vendor`, and continues
   startup automatically.

The uploaded APK is written only to temporary private App storage while it is
processed and is deleted afterwards. The extracted library persists under
`/data/vendor/libPPPP_API.so` with restrictive permissions and is reused on
future starts.

For backward compatibility, the App also accepts an official APK at
`/share/yi_rtsp/yi-home.apk` (or a direct `libPPPP_API.so` in the same folder).
The `/share` mount remains read-only.

If no private library or fallback artifact is available, the App remains alive
with its Ingress setup UI available instead of terminating. Once a valid APK is
uploaded, the backend and Supervisor discovery start automatically.

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

The setup Web UI is exposed only through Home Assistant Ingress. Its internal
port is not mapped to the host network.

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
