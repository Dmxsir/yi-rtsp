#!/usr/bin/with-contenv bashio
set -euo pipefail

APP_ROOT="/opt/yi-home/app"
RUNTIME_ROOT="/opt/yi-home/runtime/bionic-root"
API_PORT=8099
RTSP_PORT=8554
TOKEN_FILE="/data/backend-api-token"
ENV_FILE="/data/yi.env"
BACKEND_PID=""
BACKEND_STOP_TIMEOUT_SECONDS=20

mkdir -p /data
chmod 0700 /data 2>/dev/null || true

# Import the user-supplied official runtime once, then attach it at the two
# proven Bionic guest paths. Only symlinks enter the ephemeral packaged tree;
# the accepted vendor bytes remain under persistent private App data.
python3 "${APP_ROOT}/yi_vendor_bootstrap.py" \
  --data-dir /data \
  --share-dir /share/yi_rtsp \
  --runtime-root "${RUNTIME_ROOT}" \
  || bashio::exit.nok \
    "Vendor runtime unavailable. Place the official YI Home APK at /share/yi_rtsp/yi-home.apk."

TOKEN_STATE="reused"
if [[ ! -s "${TOKEN_FILE}" ]]; then
  TOKEN_STATE="created"
  umask 077
  python3 - <<'PY' >"${TOKEN_FILE}"
import secrets
print(secrets.token_urlsafe(48))
PY
fi
chmod 0600 "${TOKEN_FILE}"
TOKEN_MODE="$(python3 - "${TOKEN_FILE}" <<'PY'
import os
import stat
import sys

print(f"{stat.S_IMODE(os.stat(sys.argv[1]).st_mode):03o}")
PY
)"
if [[ "${TOKEN_MODE}" != "600" ]]; then
  bashio::exit.nok "Backend API token permissions are not restrictive."
fi
bashio::log.info "Backend API token ${TOKEN_STATE}; mode=${TOKEN_MODE}; value_exposed=false."
API_TOKEN="$(cat "${TOKEN_FILE}")"

# Phase 6D.2 will populate this file through the authenticated internal API.
# Keeping an empty restrictive file lets the App/API boot before account setup.
if [[ ! -e "${ENV_FILE}" ]]; then
  umask 077
  : >"${ENV_FILE}"
fi
chmod 0600 "${ENV_FILE}"

export YI_ADDON_API_TOKEN="${API_TOKEN}"
export PYTHONUNBUFFERED=1

terminate_backend() {
  local pid="${BACKEND_PID}"
  if [[ -z "${pid}" ]] || ! kill -0 "${pid}" 2>/dev/null; then
    return
  fi

  bashio::log.info "Stopping YI Home backend gracefully..."
  kill -TERM "${pid}" 2>/dev/null || true

  # Never let an App stop/restart block indefinitely on the backend. The
  # backend normally shuts down in a few seconds; this bounded grace period
  # still gives camera runtimes and go2rtc time to terminate cleanly.
  local ticks=$((BACKEND_STOP_TIMEOUT_SECONDS * 2))
  for _ in $(seq 1 "${ticks}"); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      wait "${pid}" 2>/dev/null || true
      bashio::log.info "YI Home backend stopped cleanly."
      return
    fi
    sleep 0.5
  done

  bashio::log.warning "YI Home backend exceeded the shutdown grace period; forcing termination."
  kill -KILL "${pid}" 2>/dev/null || true
  wait "${pid}" 2>/dev/null || true
}
trap terminate_backend TERM INT

# One-shot, secret-safe environment fingerprint. This intentionally runs before
# the backend starts and does not add a resident diagnostic process or touch the
# media path. It exists only to identify mutable base-image/APK runtime versions.
QEMU_VERSION="$(qemu-aarch64 --version 2>/dev/null | head -n 1 || true)"
PYTHON_VERSION="$(python3 --version 2>&1 | head -n 1 || true)"
QEMU_PACKAGE="$(apk info -v qemu-aarch64 2>/dev/null | head -n 1 || true)"
PYTHON_PACKAGE="$(apk info -v python3 2>/dev/null | head -n 1 || true)"
CRYPTO_PACKAGE="$(apk info -v py3-cryptography 2>/dev/null | head -n 1 || true)"
bashio::log.info "Runtime versions: qemu=${QEMU_VERSION:-unknown}; python=${PYTHON_VERSION:-unknown}; qemu_package=${QEMU_PACKAGE:-unknown}; python_package=${PYTHON_PACKAGE:-unknown}; cryptography_package=${CRYPTO_PACKAGE:-unknown}; secrets_exposed=false."

bashio::log.info "Starting YI Home backend..."
cd "${APP_ROOT}"
python3 yi_addon_service.py \
  --env-file "${ENV_FILE}" \
  --bind 0.0.0.0 \
  --port "${API_PORT}" \
  --data-dir /data \
  --runtime-root "${RUNTIME_ROOT}" \
  --worker-dir "${RUNTIME_ROOT}/data/local/tmp/yi-phase3g" \
  --go2rtc-bin /usr/local/bin/go2rtc \
  --go2rtc-state-dir /data/publisher \
  --go2rtc-api-port 1984 \
  --go2rtc-rtsp-port "${RTSP_PORT}" \
  --go2rtc-rtsp-bind 0.0.0.0 \
  --discovery-retry-interval 30 &
BACKEND_PID=$!

ready=false
for _ in $(seq 1 120); do
  if curl -fsS --max-time 2 \
      -H "Authorization: Bearer ${API_TOKEN}" \
      "http://127.0.0.1:${API_PORT}/api/v1/health" >/dev/null 2>&1; then
    ready=true
    break
  fi
  if ! kill -0 "${BACKEND_PID}" 2>/dev/null; then
    wait "${BACKEND_PID}" || true
    bashio::exit.nok "YI Home backend exited before its health API became ready."
  fi
  sleep 0.5
done

if [[ "${ready}" != true ]]; then
  terminate_backend
  bashio::exit.nok "YI Home backend health API did not become ready."
fi

# Supervisor discovery carries only the App-internal API credential and
# connection metadata. YI account/camera credentials are never included.
bashio::log.info "Publishing YI Home discovery endpoint host=local-yi-home port=${API_PORT}; credentials_exposed=false."
ha_config="$(
  bashio::var.json \
    host "local-yi-home" \
    port "^${API_PORT}" \
    api_version "v1" \
    api_token "${API_TOKEN}" \
    rtsp_port "^${RTSP_PORT}"
)"
if bashio::discovery "yi_home" "${ha_config}" >/dev/null; then
  bashio::log.info "Published YI Home discovery information to Home Assistant."
else
  bashio::log.warning "Could not publish YI Home discovery information yet; the backend remains available."
fi

bashio::log.info "YI Home backend is ready."
set +e
wait "${BACKEND_PID}"
rc=$?
set -e
BACKEND_PID=""
exit "${rc}"
