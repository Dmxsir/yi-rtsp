# Third-party notices

## Android Bionic runtime

The following files are redistributable Android Open Source Project (AOSP)
Bionic runtime components and are not covered by this repository's MIT license:

- `yi_home/rootfs/opt/yi-home/runtime/bionic-root/system/bin/linker64`
- `yi_home/rootfs/opt/yi-home/runtime/bionic-root/system/lib64/ld-android.so`
- `yi_home/rootfs/opt/yi-home/runtime/bionic-root/system/lib64/libc.so`
- `yi_home/rootfs/opt/yi-home/runtime/bionic-root/system/lib64/libdl.so`

Bionic includes components under Apache-2.0, BSD, ISC, MIT, legacy notice, and
unencumbered terms. The upstream notices are available from the AOSP Bionic
source tree:

- <https://android.googlesource.com/platform/bionic/+/master/libc/NOTICE>
- <https://android.googlesource.com/platform/bionic/+/master/linker/NOTICE>
- <https://android.googlesource.com/platform/bionic/+/master/libdl/NOTICE>

The remaining binaries under `data/local/tmp` are built from the MIT-licensed
YI RTSP project sources and contain no YI proprietary library bytes.

## Runtime downloads

The Docker build downloads FFmpeg 6.0.1 and go2rtc 1.9.14 with fixed SHA-256
checksums. Their own license terms apply. The image retains the FFmpeg GPLv3
license supplied with the pinned static archive.

## Proprietary YI components

No YI APK or `libPPPP_API.so` is distributed. Users must obtain the official
APK themselves and are responsible for complying with its terms.
