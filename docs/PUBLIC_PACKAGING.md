# Public App packaging boundary

## Why the proven development App is not copied here verbatim

The working Home Assistant App build in `Dmxsir/yi-cam-integration` is prepared by `tools/prepare_ha_app_context.py`. That development preparer stages an existing local Bionic/AArch64 runtime tree from `.analysis/phase3/bionic-root` into the Docker build context.

That staged runtime includes vendor/client-derived native material used by the PPPP/TNP transport path. In particular, the development runtime contains YI client native libraries such as `libPPPP_API.so`. Their redistribution rights are not established by this project.

For that reason this public repository must not simply commit or publish the staged `rootfs` from the development machine.

## Public-release requirements

An installable YI RTSP release must satisfy all of the following:

1. **No secrets**
   - no YI account/password;
   - no camera UID/DID/InitString/license/device key;
   - no App bearer token;
   - no cloud session token/token_secret.

2. **No unreviewed proprietary redistribution**
   - no YI APK;
   - no APK-extracted YI native library unless redistribution permission is established;
   - no opaque development capture or reverse-engineering artifact.

3. **Reproducible open-source runtime pieces**
   - document the source/license for the Bionic/AOSP runtime pieces used by QEMU;
   - pin versions/checksums for downloaded build dependencies;
   - keep the already-pinned FFmpeg/go2rtc versions or update them only through explicit validation.

4. **User-supplied vendor component if required**
   - if the transport still requires a proprietary library, prefer a flow where the user supplies an official YI Home APK or supported vendor artifact locally;
   - extract only the required library inside the App/private persistent data area;
   - validate architecture and expected file properties before use;
   - never upload or redistribute that artifact through GitHub or project infrastructure.

5. **Home Assistant App lifecycle**
   - public folder/slug: `yi_rtsp`;
   - public name: `YI RTSP`;
   - keep Supervisor discovery service `yi_home` for integration compatibility unless both projects are migrated together;
   - support clean install, restart, update and uninstall;
   - persist only necessary private configuration/state under `/data`.

## Proven architecture to preserve

```text
YI camera
 -> YI RTSP App
    -> one App-owned YI cloud session
    -> private per-camera runtime-material broker (UDS)
    -> supervised PPPP/TNP media runtime
    -> FFmpeg MPEG-TS
    -> App-owned go2rtc
 -> RTSP
    -> Home Assistant / YI Camera Connect
    -> Frigate
```

Do not change the media pipeline, PPPP/TNP timings, watchdog values or Frigate contract merely to solve public packaging. Public packaging should be isolated from the already-proven runtime behavior.

## Release gate

Do not add an installable `yi_rtsp/` App directory to the public repository until the vendor-runtime acquisition/bootstrap path is implemented and tested on a clean Home Assistant OS installation.
