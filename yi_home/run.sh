#!/usr/bin/with-contenv bashio
set -euo pipefail

APP_ROOT="/opt/yi-home/app"
RUNTIME_ROOT="/opt/yi-home/runtime/bionic-root"
API_PORT=8099
UPLOAD_PORT=8098
RTSP_PORT=8554
TOKEN_FILE="/data/backend-api-token"
ENV_FILE="/data/yi.env"
BACKEND_PID=""
UPLOAD_PID=""
BACKEND_STOP_TIMEOUT_SECONDS=20
BOOTSTRAP_LOG="/tmp/yi-vendor-bootstrap.log"

mkdir -p /data
chmod 0700 /data 2>/dev/null || true

terminate_backend() {
  local pid="${BACKEND_PID}"
  if [[ -z "${pid}" ]] || ! kill -0 "${pid}" 2>/dev/null; then
    return
  fi

  bashio::log.info "Stopping YI Home backend gracefully..."
  kill -TERM "${pid}" 2>/dev/null || true

  local ticks=$((BACKEND_STOP_TIMEOUT_SECONDS * 2))
  for _ in $(seq 1 "${ticks}"); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      wait "${pid}" 2>/dev/null || true
      BACKEND_PID=""
      bashio::log.info "YI Home backend stopped cleanly."
      return
    fi
    sleep 0.5
  done

  bashio::log.warning "YI Home backend exceeded the shutdown grace period; forcing termination."
  kill -KILL "${pid}" 2>/dev/null || true
  wait "${pid}" 2>/dev/null || true
  BACKEND_PID=""
}

terminate_upload_ui() {
  local pid="${UPLOAD_PID}"
  if [[ -z "${pid}" ]] || ! kill -0 "${pid}" 2>/dev/null; then
    return
  fi
  kill -TERM "${pid}" 2>/dev/null || true
  wait "${pid}" 2>/dev/null || true
  UPLOAD_PID=""
}

terminate_all() {
  terminate_backend
  terminate_upload_ui
}
trap terminate_all TERM INT

bashio::log.info "Starting YI RTSP setup Web UI on Home Assistant Ingress..."
python3 "${APP_ROOT}/yi_vendor_upload.py" \
  --bind 0.0.0.0 \
  --port "${UPLOAD_PORT}" \
  --data-dir /data \
  --runtime-root "${RUNTIME_ROOT}" &
UPLOAD_PID=$!
sleep 0.2
if ! kill -0 "${UPLOAD_PID}" 2>/dev/null; then
  wait "${UPLOAD_PID}" 2>/dev/null || true
  bashio::exit.nok "YI RTSP setup Web UI failed to start."
fi

# Reuse an already imported private vendor library, or keep the App alive while
# the user uploads the official YI Home APK through Home Assistant Ingress.
if ! python3 "${APP_ROOT}/yi_vendor_bootstrap.py" \
    --data-dir /data \
    --share-dir /share/yi_rtsp \
    --runtime-root "${RUNTIME_ROOT}"; then
  bashio::log.warning "YI vendor runtime is not installed yet. Open the YI RTSP Web UI and upload the official YI Home APK."
  bashio::log.info "The App will continue startup automatically after a valid APK is imported."

  while true; do
    if ! kill -0 "${UPLOAD_PID}" 2>/dev/null; then
      wait "${UPLOAD_PID}" 2>/dev/null || true
      bashio::exit.nok "YI RTSP setup Web UI exited before the vendor runtime was installed."
    fi

    if python3 "${APP_ROOT}/yi_vendor_bootstrap.py" \
        --data-dir /data \
        --share-dir /share/yi_rtsp \
        --runtime-root "${RUNTIME_ROOT}" >"${BOOTSTRAP_LOG}" 2>&1; then
      cat "${BOOTSTRAP_LOG}"
      break
    fi
    sleep 1
  done
fi
rm -f "${BOOTSTRAP_LOG}"

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

if [[ ! -e "${ENV_FILE}" ]]; then
  umask 077
  : >"${ENV_FILE}"
fi
chmod 0600 "${ENV_FILE}"

export YI_ADDON_API_TOKEN="${API_TOKEN}"
export PYTHONUNBUFFERED=1

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
    BACKEND_PID=""
    terminate_upload_ui
    bashio::exit.nok "YI Home backend exited before its health API became ready."
  fi
  sleep 0.5
done

if [[ "${ready}" != true ]]; then
  terminate_all
  bashio::exit.nok "YI Home backend health API did not become ready."
fi

resolve_app_hostname() {
  local value=""

  # /addons/self/* is available to an App without broad Supervisor API access.
  # Prefer the API response directly so this works across Bashio generations.
  if [[ -n "${SUPERVISOR_TOKEN:-}" ]]; then
    value="$(
      curl -fsS --max-time 5 \
        -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
        http://supervisor/addons/self/info 2>/dev/null \
      | python3 -c 'import json,sys; payload=json.load(sys.stdin); print(((payload.get("data") or {}).get("hostname")) or "")' \
        2>/dev/null || true
    )"
  fi

  # Home Assistant renamed add-on terminology to App. Older base images can
  # still ship Bashio with bashio::addon.* while newer releases use app.*.
  if [[ -z "${value}" ]] && declare -F bashio::app.hostname >/dev/null 2>&1; then
    value="$(bashio::app.hostname 2>/dev/null || true)"
  fi
  if [[ -z "${value}" ]] && declare -F bashio::addon.hostname >/dev/null 2>&1; then
    value="$(bashio::addon.hostname 2>/dev/null || true)"
  fi

  printf '%s' "${value}"
}

APP_HOSTNAME="$(resolve_app_hostname)"
if [[ -n "${APP_HOSTNAME}" ]]; then
  bashio::log.info "Publishing YI Home discovery endpoint host=${APP_HOSTNAME} port=${API_PORT}; credentials_exposed=false."
  ha_config="$(
    bashio::var.json \
      host "${APP_HOSTNAME}" \
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
else
  bashio::log.warning "Could not resolve this App's Supervisor hostname yet; discovery skipped without stopping the backend."
fi

bashio::log.info "YI Home backend is ready."
set +e
wait "${BACKEND_PID}"
rc=$?
set -e
BACKEND_PID=""
terminate_upload_ui
exit "${rc}"
