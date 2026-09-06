# YI RTSP

YI RTSP is a standalone Home Assistant App repository that provides RTSP
support for YI cameras using the validated native PPPP/TNP runtime.

This repository contains the App build context, startup entrypoint,
redistributable runtime components, and user documentation. The current App
supports `amd64` Home Assistant systems only and keeps the internal App slug
and discovery identifier `yi_home` for compatibility.

## Installation concept

Add `https://github.com/Dmxsir/yi-rtsp` to the Home Assistant App store and
install **YI RTSP**.

On first start, if the private vendor runtime is not installed yet, the App
stays running and exposes a Home Assistant Ingress Web UI. Open **YI RTSP →
Open Web UI** and upload your own official YI Home APK. The upload is processed
locally inside the App.

The App extracts and validates only the required ARM64 `libPPPP_API.so`, stores
it privately at `/data/vendor/libPPPP_API.so`, creates the runtime compatibility
links, and deletes the temporary APK upload. The private library persists under
`/data` across restarts, so the APK is normally required only once.

The previous `/share/yi_rtsp/yi-home.apk` import path remains supported as a
backward-compatible fallback.

Neither the official APK nor proprietary YI libraries are distributed by this
repository, included in its Docker build context or image, or intended for
release assets.

## Build

The repository owns the complete redistributable build context. A fresh clone
does not depend on `yi-cam-integration`, a developer analysis directory, or a
local Documents folder:

```bash
docker build yi_home
```

The build downloads checksum-pinned FFmpeg and go2rtc artifacts. See
[`yi_home/DOCS.md`](yi_home/DOCS.md) for App runtime and usage details.

## Home Assistant integration

The matching Home Assistant integration remains maintained separately in
[`Dmxsir/yi-cam-integration`](https://github.com/Dmxsir/yi-cam-integration).

## Licensing

Repository code and documentation are MIT licensed. Bundled AOSP/Bionic
runtime components retain their upstream licenses; see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). No license or rights to YI
proprietary components are granted.
