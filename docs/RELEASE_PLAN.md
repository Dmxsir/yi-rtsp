# YI RTSP public release plan

## Goal

Publish an installable Home Assistant App named **YI RTSP** in this repository without redistributing YI proprietary binaries or changing the proven PPPP/TNP media pipeline.

## Public package contract

- Public App folder/slug: `yi_rtsp`
- Public name: `YI RTSP`
- Supervisor discovery service remains `yi_home`
- Companion integration: `Dmxsir/yi-camera-connect`
- Initial App release target: `0.1.0`
- Initial architecture: `amd64`

## Preserve unchanged

The public packaging work must not alter the proven runtime behavior merely to make the project distributable:

- App-owned cloud-session reuse
- runtime-material Unix-domain-socket broker
- PPPP/TNP timing and watchdog behavior
- native H264/AAC relay
- pinned FFmpeg 6.0.1 path
- go2rtc publication contract
- RTSP path format `/yi_<12-char-stable-id-prefix>`
- Home Assistant / Frigate API contract

## Main blocker

The development build currently stages a local Bionic/AArch64 runtime tree which includes YI client-derived `libPPPP_API.so`. Redistribution rights for that library are not established, so it must not be committed to this public repository or embedded in a public release/image.

## Implementation direction

Implement a clean bootstrap/acquisition boundary for vendor material:

1. Keep all project-owned Python/App code in Git.
2. Build reproducible AOSP/Bionic/QEMU support pieces from documented, redistributable sources or obtain them from pinned redistributable packages.
3. Do not ship YI APKs or APK-extracted YI libraries.
4. If `libPPPP_API.so` remains required, make it user-supplied from an official YI artifact and keep it private under `/data`.
5. Validate supplied material before enabling camera runtimes:
   - expected architecture;
   - expected ELF type;
   - expected required symbols/properties;
   - checksum/status recorded locally without uploading the artifact.
6. Fail closed with a clear App log/status when vendor material is missing or invalid.
7. Never expose account credentials, tokens, camera material or App bearer tokens in logs, argv, child environment, GitHub or Home Assistant entities.

## Clean-install release gate

Before publishing the installable `yi_rtsp/` directory, validate on a clean Home Assistant OS instance:

1. Repository can be added as a Home Assistant App repository.
2. YI RTSP installs and builds from Git without access to the development `.analysis` tree.
3. Missing vendor material produces an actionable, secret-safe state instead of a crash loop.
4. Supplying the supported official vendor artifact completes bootstrap locally.
5. Account configuration succeeds.
6. At least two known cameras can start concurrently.
7. Existing RTSP paths work from Home Assistant and Frigate.
8. App restart preserves required private state but creates only the expected single cloud login for the new App process.
9. No YI proprietary binary or secret is present in the Git tree, GitHub release source archive or public container layers produced by the repository itself.

## Release sequence

1. Implement bootstrap/acquisition path in the development repository first.
2. Deploy and validate it on the existing HA OS test installation without changing media behavior.
3. Re-test from a clean installation path.
4. Copy only redistributable project-owned sources into this repository.
5. Add `yi_rtsp/config.yaml`, Dockerfile, run script, AppArmor profile, docs and CI.
6. Publish `v0.1.0` only after the clean-install gate passes.
