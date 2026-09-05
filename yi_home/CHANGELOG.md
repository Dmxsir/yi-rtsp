# Changelog

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
