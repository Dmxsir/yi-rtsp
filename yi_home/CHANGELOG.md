# Changelog

## 0.2.0-beta.3

- Added HTTP chunked-transfer decoding for APK uploads proxied through Home Assistant `ingress_stream`.
- Kept streamed uploads bounded to 512 MiB and processed directly into private App storage.
- Added a chunked-upload integration test that mirrors Supervisor streaming behavior.

## 0.2.0-beta.2

- Fixed Ingress-relative `status` and `upload` requests when Home Assistant opens the Web UI without a trailing slash.
- Added backward-compatible Supervisor hostname resolution for both modern `bashio::app.*` and older `bashio::addon.*` runtimes.
- Discovery hostname lookup no longer forces the App into a restart loop if Supervisor metadata cannot be resolved immediately.

## 0.2.0-beta.1

- Added a Home Assistant Ingress setup UI for uploading the user's official YI Home APK.
- The App now remains running and waits for first-time APK setup instead of exiting when the private vendor runtime is missing.
- APK uploads are processed only in private App storage; only the validated ARM64 `libPPPP_API.so` is persisted and the temporary APK is deleted.
- Existing `/share/yi_rtsp/yi-home.apk` and direct-library import paths remain supported for backward compatibility.
- The backend and Supervisor discovery continue automatically after a valid APK is imported.

## 0.1.2

- Fixed Home Assistant Supervisor discovery hostname for the repository-installed App (`7adb5cbc-yi-home`).
- Bumped the App version so Home Assistant can detect and rebuild the corrected package.

## 0.1.0

- Initial experimental Home Assistant App scaffold.
- amd64 packaging target.
- Persistent `/data` backend state.
- App-generated internal API token and Supervisor discovery.
- Managed go2rtc RTSP publication on TCP 8554.
- Prepared packaging path for proven Bionic/QEMU PPPP/TNP runtime and authoritative online-status worker.
