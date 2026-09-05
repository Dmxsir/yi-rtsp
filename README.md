# YI RTSP

YI RTSP is a standalone Home Assistant App repository that provides RTSP
support for YI cameras using the validated native PPPP/TNP runtime.

This repository contains the App build context, startup entrypoint,
redistributable runtime components, and user documentation. The current App
supports `amd64` Home Assistant systems only. Phase 1 deliberately preserves
the internal App slug and discovery identifier `yi_home`, the `/data` layout,
runtime paths, backend API, and RTSP behavior.

## Installation concept

After this repository is published, add
`https://github.com/Dmxsir/yi-rtsp` to the Home Assistant App store and install
**YI RTSP**. Before starting it, supply your own official YI Home APK at:

```text
/share/yi_rtsp/yi-home.apk
```

The App's `/share` mount is read-only. At startup the App extracts and validates
the required ARM64 vendor library, stores it privately at
`/data/vendor/libPPPP_API.so`, and creates the existing runtime compatibility
symlinks. The private copy persists under `/data` across restarts.

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
