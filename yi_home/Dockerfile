FROM ghcr.io/home-assistant/base:3.23

ARG BUILD_VERSION=0.1.0
ARG BUILD_ARCH=amd64
ARG FFMPEG_VERSION=6.0.1
ARG FFMPEG_SHA256=28268bf402f1083833ea269331587f60a242848880073be8016501d864bd07a5
ARG GO2RTC_VERSION=1.9.14
ARG GO2RTC_SHA256=32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6

SHELL ["/bin/ash", "-o", "pipefail", "-c"]

# FFmpeg is intentionally pinned to the proven 6.0.1 static build. Alpine 3.23's
# FFmpeg 8.0.1 stalls the native YI H264/AAC relay after roughly 20-30 seconds,
# while the same App image and full lifecycle -> go2rtc pipeline remain stable
# with FFmpeg 6.0.1. Keep the archive checksum pinned so base-image package
# updates cannot silently reintroduce the media-stall regression.
RUN apk add --no-cache \
      ca-certificates \
      curl \
      py3-cryptography \
      python3 \
      qemu-aarch64 \
      xz \
    && curl -fsSL \
      -o /tmp/ffmpeg-static.tar.xz \
      "https://software.frc971.org/Build-Dependencies/www.johnvansickle.com/ffmpeg/old-releases/ffmpeg-${FFMPEG_VERSION}-amd64-static.tar.xz" \
    && echo "${FFMPEG_SHA256}  /tmp/ffmpeg-static.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/ffmpeg-static.tar.xz -C /tmp \
    && install -m 0755 \
      "/tmp/ffmpeg-${FFMPEG_VERSION}-amd64-static/ffmpeg" \
      /usr/local/bin/ffmpeg \
    && install -m 0755 \
      "/tmp/ffmpeg-${FFMPEG_VERSION}-amd64-static/ffprobe" \
      /usr/local/bin/ffprobe \
    && mkdir -p /usr/share/licenses/ffmpeg-static \
    && install -m 0644 \
      "/tmp/ffmpeg-${FFMPEG_VERSION}-amd64-static/GPLv3.txt" \
      /usr/share/licenses/ffmpeg-static/GPLv3.txt \
    && rm -rf \
      /tmp/ffmpeg-static.tar.xz \
      "/tmp/ffmpeg-${FFMPEG_VERSION}-amd64-static" \
    && curl -fsSL \
      -o /usr/local/bin/go2rtc \
      "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_amd64" \
    && echo "${GO2RTC_SHA256}  /usr/local/bin/go2rtc" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/go2rtc

# The repository-owned rootfs contains only the required project sources and
# the proven redistributable Bionic/native runtime. Proprietary YI bytes are
# supplied by the user at runtime and must never enter this build context.
COPY rootfs/ /
COPY run.sh /run.sh
RUN chmod 0755 /run.sh \
    && test -f /opt/yi-home/app/yi_addon_service.py \
    && test -x /opt/yi-home/runtime/bionic-root/system/bin/linker64 \
    && test -x /opt/yi-home/runtime/bionic-root/data/local/tmp/yi-online-status/android_pppp_online_probe \
    && test -x /usr/local/bin/ffmpeg \
    && test -x /usr/local/bin/ffprobe \
    && python3 -m py_compile /opt/yi-home/app/*.py

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}" \
  org.opencontainers.image.title="Home Assistant App: YI RTSP" \
  org.opencontainers.image.description="YI camera RTSP support for Home Assistant" \
  org.opencontainers.image.source="https://github.com/Dmxsir/yi-rtsp"

CMD ["/run.sh"]
