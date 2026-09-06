# YI RTSP

YI RTSP is a Home Assistant App that provides RTSP access to supported YI Home cameras without requiring an SD-card hack such as `yi-hack`.

The project is split into two repositories:

- **`Dmxsir/yi-rtsp`** — this repository. The Home Assistant App that runs the YI camera PPPP/TNP runtime and publishes the RTSP streams.
- **`Dmxsir/yi-cam-integration`** — the matching Home Assistant custom integration, installed through HACS.

> **Current status:** experimental but working end-to-end on the validated YI Home camera setup. The current App build supports `amd64` Home Assistant systems.

## What you get

After installation, the YI Home integration can expose your cameras in Home Assistant and the App can publish stable RTSP endpoints for use by Frigate/go2rtc.

Typical flow:

```text
YI cameras / YI cloud
        |
        v
YI RTSP Home Assistant App
        |
        | internal authenticated API + RTSP
        v
YI Home HACS integration
        |
        +--> Home Assistant camera/entities
        |
        +--> Frigate RTSP sensor / go2rtc configuration
```

---

# Installation

## 1. Add the YI RTSP App repository

In Home Assistant open:

**Settings → Apps → App Store → ⋮ → Repositories**

Add:

```text
https://github.com/Dmxsir/yi-rtsp
```

Reload the App Store if necessary, then install **YI RTSP**.

Start the App.

## 2. Import the official YI Home APK

The proprietary YI PPPP library is **not included in this repository**. On first start the App remains running and exposes a setup page through Home Assistant Ingress.

Open:

**YI RTSP → Open Web UI**

You should see the **Vendor runtime setup** screen.

Select your own official **YI Home APK** and press **Upload APK**.

The App will:

1. upload the APK only into temporary private App storage;
2. find the required ARM64 `libPPPP_API.so`;
3. validate the library;
4. store only that library under `/data/vendor/libPPPP_API.so`;
5. delete the temporary APK;
6. continue startup automatically.

A successful import shows:

```text
Runtime installed. The App will continue startup automatically.
```

Afterwards the Web UI should show something similar to:

```text
Vendor runtime installed (238 KiB).
```

The extracted library is stored in the App's persistent `/data` directory, so the APK normally needs to be supplied **only once**, including after App restarts and upgrades.

### APK error: `official APK contains no ARM64 vendor library`

The selected file does not contain the ARM64 YI vendor library required by the App.

This commonly happens with modern Android **split APK** installations, where the base APK contains the application code but native libraries are stored in a separate architecture APK.

Use either:

- a complete/universal official YI Home APK that contains `arm64-v8a`, or
- the ARM64 split APK that contains `libPPPP_API.so`.

Do not upload a random third-party library or rename an unrelated APK.

### Legacy import method

For backward compatibility the App can still import:

```text
/share/yi_rtsp/yi-home.apk
```

or:

```text
/share/yi_rtsp/libPPPP_API.so
```

The Web UI method is recommended for new installations.

## 3. Install the YI Home HACS integration

Open HACS and add this custom repository:

```text
https://github.com/Dmxsir/yi-cam-integration
```

Repository type:

```text
Integration
```

Install **YI Home**, then perform a **full Home Assistant restart**.

## 4. Configure YI Home from Discovery

Do **not** use **Add integration → YI Home** for the initial setup. Manual setup is intentionally disabled.

After Home Assistant restarts and the YI RTSP App is running, open:

**Settings → Devices & services**

Home Assistant should show **YI Home** under **Discovered**.

Press **Configure** on the discovered YI Home card.

Enter:

- your YI account/email;
- your YI account password;
- country code, for example `IL`;
- region matching your YI account, for example `Europe`.

The credentials are passed to the YI RTSP App and are not stored in the Home Assistant integration config entry.

After configuration, the integration should create the available camera entities, runtime sensors and RTSP export sensors.

---

# Frigate / external RTSP

The App publishes RTSP internally on TCP port `8554`.

To allow Frigate or another LAN client to connect, map that container port to an unused host port.

Open the YI RTSP App **Network** configuration and map:

```text
8554/tcp -> 28554
```

`28554` is only an example. Any free TCP port can be used. This is useful when Home Assistant/Frigate/go2rtc already uses host port `8554`.

After changing the mapping:

1. restart the **YI RTSP** App;
2. reload the **YI Home** integration, or restart Home Assistant.

For each camera, the integration exposes a **Frigate RTSP** sensor.

The sensor state contains the complete ready-to-copy RTSP URL, for example:

```text
rtsp://HOME_ASSISTANT_IP:28554/yi_<camera-id>
```

Do not construct the path manually. Copy the value produced by the integration because the camera path is based on its stable ID.

The same sensor also exposes these useful attributes:

```text
external_rtsp_port
external_host
frigate_stream_name
frigate_go2rtc
```

`frigate_go2rtc` contains a ready-to-copy go2rtc YAML snippet for that camera.

Example structure:

```yaml
go2rtc:
  streams:
    my_yi_camera:
      - rtsp://HOME_ASSISTANT_IP:28554/yi_<camera-id>
      - "ffmpeg:my_yi_camera#audio=opus"
```

Prefer copying the generated `frigate_go2rtc` attribute rather than typing this example manually.

---

# Verification

A normal App startup should eventually contain messages similar to:

```text
vendor_runtime=reused; source=private_data
Starting YI Home backend...
Publishing YI Home discovery endpoint ...
Published YI Home discovery information to Home Assistant.
YI Home backend is ready.
```

The exact Supervisor-generated hostname is installation-specific and is resolved automatically by the App.

## Quick checklist

- YI RTSP App is **Running**.
- Web UI reports **Vendor runtime installed**.
- App log ends with **YI Home backend is ready**.
- YI Home appears under **Settings → Devices & services**.
- Cameras/entities are created after login.
- If Frigate is used, `8554/tcp` has a host mapping and the **Frigate RTSP** sensor is available.

---

# Troubleshooting

## YI Home does not appear under Discovered

Confirm that:

- YI RTSP is running;
- its log contains `Published YI Home discovery information to Home Assistant`;
- the HACS integration is installed;
- Home Assistant was fully restarted after the integration installation.

If Discovery still does not appear, restart the YI RTSP App once and then restart Home Assistant.

## `Failed setup, will retry: YI Home App is not ready`

This normally means the integration cannot reach the App backend described by Supervisor discovery.

First confirm that the App log contains:

```text
YI Home backend is ready.
```

If this installation was upgraded from an old development version, remove only the stale **YI Home integration config entry** and allow current Supervisor Discovery to create a fresh setup flow. Do not manually edit Home Assistant `.storage` files unless you know exactly what you are doing.

## Frigate RTSP sensor is unavailable

The external RTSP sensor requires a host port mapping for the App's `8554/tcp` port.

Map it to a free port such as `28554`, restart the App, and reload the YI Home integration.

## The Web UI keeps logging `method=GET`

This is expected while the Web UI is open. The page polls the runtime status periodically. The requests stop when the page is closed.

---

# Security and proprietary components

The official YI Home APK and YI proprietary libraries are **not distributed by this repository**, are not included in the Docker build context/image, and are not included in release assets.

The user supplies their own official APK. The App extracts the required library locally and keeps it in private persistent App storage.

Repository code and documentation are MIT licensed. Bundled AOSP/Bionic runtime components retain their upstream licenses. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

This is an independent community project and is not affiliated with or endorsed by YI Technology. YI and YI Home are trademarks of their respective owners.

---

# Development / build

The repository contains the redistributable build context required for the App. From a fresh clone:

```bash
docker build yi_home
```

The build downloads checksum-pinned FFmpeg and go2rtc artifacts.

Additional App runtime documentation is available in [`yi_home/DOCS.md`](yi_home/DOCS.md).

The companion Home Assistant integration is maintained at:

https://github.com/Dmxsir/yi-cam-integration
