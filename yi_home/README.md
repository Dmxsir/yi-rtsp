# YI RTSP

Home Assistant App packaging for the validated native YI PPPP/TNP camera
runtime and managed RTSP publication.

The current build supports `amd64` only. It keeps the existing `yi_home`
internal identifiers and runtime paths for compatibility. Proprietary YI
components are not distributed.

On first start, open the App Web UI and upload your own official YI Home APK.
The App extracts only the required ARM64 vendor library into private `/data`
storage, deletes the temporary APK, and continues startup automatically.

See `DOCS.md` for runtime and fallback installation details.
