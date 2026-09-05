#!/usr/bin/env python3
"""Versioned HTTP service for the YI Home App/Add-on engine.

The service exposes only secret-safe backend/runtime state. Optional managed
go2rtc publication keeps media publishing inside the App while PPPP/TNP session
ownership remains in the per-camera lifecycle manager. The App owns the YI
cloud session and hands only per-camera runtime material to scrubbed children.
When --data-dir is set,
non-secret capability/runtime intent is persisted there for App restarts.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import signal
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yi_tnp_oracle as oracle
from yi_account_credentials import YiAccountCredentialError, YiAccountCredentialStore
from yi_addon_backend import YiAddonBackend
from yi_capability_cache import YiCapabilityCache
from yi_capability_probe_runtime import YiCapabilityProbe
from yi_cloud_session import YiCloudSession, YiMaterialBroker
from yi_media_publisher import Go2RTCPublisherConfig, YiGo2RTCPublisher
from yi_persistent_backend import YiPersistentAddonBackend
from yi_runtime_lifecycle import (
    YiRuntimeLifecycleManager,
    build_default_config,
    default_runtime_state_dir,
)
from yi_runtime_policy import YiRuntimePolicyStore

CAMERA_RE = re.compile(r"^/api/v1/cameras/([0-9a-f]{20})$")
STATUS_RE = re.compile(r"^/api/v1/cameras/([0-9a-f]{20})/status$")
START_RE = re.compile(r"^/api/v1/cameras/([0-9a-f]{20})/start$")
STOP_RE = re.compile(r"^/api/v1/cameras/([0-9a-f]{20})/stop$")
RESTART_RE = re.compile(r"^/api/v1/cameras/([0-9a-f]{20})/restart$")
REPROBE_RE = re.compile(r"^/api/v1/cameras/([0-9a-f]{20})/reprobe$")
ACCOUNT_PATH = "/api/v1/account"
MAX_JSON_BODY = 16 * 1024


def _is_loopback(bind: str) -> bool:
    return bind in {"127.0.0.1", "::1", "localhost"}


def _credential_http_status(code: str) -> int:
    return {
        "invalid_request": HTTPStatus.BAD_REQUEST,
        "invalid_credentials": HTTPStatus.UNAUTHORIZED,
        "mfa_or_challenge": HTTPStatus.CONFLICT,
        "rate_limit": HTTPStatus.TOO_MANY_REQUESTS,
        "configuration_error": HTTPStatus.BAD_REQUEST,
        "transport_error": HTTPStatus.BAD_GATEWAY,
        "unsupported_api": HTTPStatus.BAD_GATEWAY,
        "server_rejection": HTTPStatus.BAD_GATEWAY,
        "unexpected_response_schema": HTTPStatus.BAD_GATEWAY,
    }.get(code, HTTPStatus.BAD_GATEWAY)


class YiAddonHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        backend: YiAddonBackend,
        api_token: str | None,
        credential_store: YiAccountCredentialStore | None,
        account_timeout: float,
    ) -> None:
        super().__init__(address, YiAddonRequestHandler)
        self.backend = backend
        self.api_token = api_token
        self.credential_store = credential_store
        self.account_timeout = account_timeout
        self.account_lock = threading.RLock()


class YiAddonRequestHandler(BaseHTTPRequestHandler):
    server: YiAddonHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _authorized(self) -> bool:
        token = self.server.api_token
        if token is None:
            return True
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):], token)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(
            status,
            {
                "ok": False,
                "error": {"code": code, "message": message},
                "secrets_exposed": False,
            },
        )

    def _preflight(self) -> str | None:
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", "A valid App API token is required.")
            return None
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            self._error(HTTPStatus.BAD_REQUEST, "query_not_supported", "Query strings are not supported by this API.")
            return None
        return parsed.path

    def _read_json_body(self) -> dict[str, Any] | None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "json_required", "This endpoint requires application/json.")
            return None
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_JSON_BODY:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_body_length", "The JSON request body length is invalid.")
            return None
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_json", "The request body is not valid UTF-8 JSON.")
            return None
        if not isinstance(payload, dict):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_json", "The request body must be a JSON object.")
            return None
        return payload

    def do_GET(self) -> None:  # noqa: N802
        path = self._preflight()
        if path is None:
            return
        if path == "/api/v1/health":
            self._json(HTTPStatus.OK, self.server.backend.health())
            return
        if path == ACCOUNT_PATH:
            store = self.server.credential_store
            if store is None:
                self._error(HTTPStatus.CONFLICT, "account_store_unavailable", "Persistent YI account storage is not enabled.")
            else:
                self._json(HTTPStatus.OK, store.status())
            return
        if path == "/api/v1/cameras":
            self._json(HTTPStatus.OK, self.server.backend.cameras())
            return

        match = CAMERA_RE.fullmatch(path)
        if match:
            item = self.server.backend.camera(match.group(1))
            if item is None:
                self._error(HTTPStatus.NOT_FOUND, "camera_not_found", "Unknown camera stable_id.")
            else:
                self._json(HTTPStatus.OK, item)
            return

        match = STATUS_RE.fullmatch(path)
        if match:
            item = self.server.backend.camera_status(match.group(1))
            if item is None:
                self._error(HTTPStatus.NOT_FOUND, "camera_not_found", "Unknown camera stable_id.")
            else:
                self._json(HTTPStatus.OK, item)
            return

        self._error(HTTPStatus.NOT_FOUND, "not_found", "Unknown API endpoint.")

    def do_POST(self) -> None:  # noqa: N802
        path = self._preflight()
        if path is None:
            return

        if path == ACCOUNT_PATH:
            payload = self._read_json_body()
            if payload is None:
                return
            store = self.server.credential_store
            if store is None:
                self._error(HTTPStatus.CONFLICT, "account_store_unavailable", "Persistent YI account storage is not enabled.")
                return
            with self.server.account_lock:
                try:
                    account_result = store.configure(
                        payload,
                        timeout=self.server.account_timeout,
                        cloud_session=self.server.backend.cloud_session,
                    )
                except YiAccountCredentialError as exc:
                    self._error(_credential_http_status(exc.code), exc.code, exc.safe_message)
                    return
                try:
                    discovery = self.server.backend.discover(fetch_tnp=True, reason="account_replace")
                except Exception:
                    self._json(
                        HTTPStatus.BAD_GATEWAY,
                        {
                            "ok": False,
                            "configured": True,
                            "credentials_persisted": True,
                            "error": {
                                "code": "discovery_failed",
                                "message": "The YI account was validated and stored, but camera discovery failed.",
                            },
                            "secrets_exposed": False,
                        },
                    )
                    return
            self._json(
                HTTPStatus.OK,
                {
                    **account_result,
                    "discovery_ok": True,
                    "camera_count": discovery.get("camera_count", account_result.get("camera_count", 0)),
                    "discovery_generation": discovery.get("generation"),
                    "secrets_exposed": False,
                },
            )
            return

        length = self.headers.get("Content-Length")
        if length not in {None, "", "0"}:
            self._error(HTTPStatus.BAD_REQUEST, "body_not_supported", "This endpoint does not accept a request body.")
            return

        if path == "/api/v1/discover":
            try:
                result = self.server.backend.discover(fetch_tnp=True, reason="ha_discovery")
            except Exception:
                self._error(HTTPStatus.BAD_GATEWAY, "discovery_failed", "YI camera discovery failed.")
            else:
                self._json(HTTPStatus.OK, result)
            return

        for regex, operation in (
            (START_RE, "start"),
            (STOP_RE, "stop"),
            (RESTART_RE, "restart"),
        ):
            match = regex.fullmatch(path)
            if match:
                stable_id = match.group(1)
                if operation == "start":
                    status, response = self.server.backend.start_camera(stable_id)
                elif operation == "stop":
                    status, response = self.server.backend.stop_camera(stable_id)
                else:
                    status, response = self.server.backend.restart_camera(stable_id)
                self._json(status, response)
                return

        match = REPROBE_RE.fullmatch(path)
        if match:
            status, response = self.server.backend.reprobe_camera(match.group(1))
            self._json(status, response)
            return

        self._error(HTTPStatus.NOT_FOUND, "not_found", "Unknown API endpoint.")


def main() -> int:
    parser = argparse.ArgumentParser(description="YI Home App/Add-on backend API service")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--bind", default=os.getenv("YI_ADDON_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("YI_ADDON_PORT", "8099")))
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--no-initial-discovery", action="store_true")
    parser.add_argument("--discovery-retry-interval", type=float, default=30.0)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--disable-lifecycle", action="store_true")
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--worker-dir", type=Path)
    parser.add_argument("--runtime-state-dir", type=Path)
    parser.add_argument("--startup-timeout", type=float, default=45.0)
    parser.add_argument("--stall-timeout", type=float, default=12.0)
    parser.add_argument("--terminate-grace", type=float, default=3.0)
    parser.add_argument("--restart-delay", type=float, default=1.0)
    parser.add_argument("--max-restart-delay", type=float, default=30.0)
    parser.add_argument("--probe-duration", type=float, default=8.0)
    parser.add_argument("--go2rtc-bin", type=Path)
    parser.add_argument("--go2rtc-state-dir", type=Path)
    parser.add_argument("--go2rtc-api-port", type=int, default=1984)
    parser.add_argument("--go2rtc-rtsp-port", type=int, default=8554)
    parser.add_argument("--go2rtc-rtsp-bind", default="127.0.0.1")
    args = parser.parse_args()

    if args.env_file is not None:
        if not args.env_file.is_file():
            raise SystemExit(f"env file not found: {args.env_file}")
        oracle.load_env_file(args.env_file)

    for value, label in (
        (args.port, "--port"),
        (args.go2rtc_api_port, "--go2rtc-api-port"),
        (args.go2rtc_rtsp_port, "--go2rtc-rtsp-port"),
    ):
        if not 1 <= value <= 65535:
            raise SystemExit(f"{label} must be between 1 and 65535")
    if args.probe_duration <= 0:
        raise SystemExit("--probe-duration must be greater than zero")
    if args.discovery_retry_interval < 0:
        raise SystemExit("--discovery-retry-interval must not be negative")

    token = os.getenv("YI_ADDON_API_TOKEN") or None
    if not _is_loopback(args.bind) and token is None:
        raise SystemExit("non-loopback bind requires YI_ADDON_API_TOKEN")

    data_dir = args.data_dir.expanduser().resolve() if args.data_dir is not None else None
    if data_dir is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(data_dir, 0o700)
        except OSError:
            pass

    credential_store = YiAccountCredentialStore(args.env_file) if args.env_file is not None else None
    cloud_session = YiCloudSession(timeout=args.timeout)

    selected_runtime_state = args.runtime_state_dir
    if selected_runtime_state is None and data_dir is not None:
        selected_runtime_state = data_dir / "runtime"

    media_publisher: YiGo2RTCPublisher | None = None
    if args.go2rtc_bin is not None:
        if args.go2rtc_state_dir is not None:
            publisher_state = args.go2rtc_state_dir
        elif data_dir is not None:
            publisher_state = data_dir / "publisher"
        else:
            publisher_state = (selected_runtime_state or default_runtime_state_dir()) / "publisher"
        media_publisher = YiGo2RTCPublisher(
            Go2RTCPublisherConfig(
                binary=args.go2rtc_bin.expanduser().resolve(),
                state_dir=publisher_state.expanduser().resolve(),
                api_host="127.0.0.1",
                api_port=args.go2rtc_api_port,
                rtsp_bind=args.go2rtc_rtsp_bind,
                rtsp_port=args.go2rtc_rtsp_port,
            )
        )

    capability_cache = YiCapabilityCache(data_dir / "capabilities.json" if data_dir is not None else None)
    lifecycle: YiRuntimeLifecycleManager | None = None
    capability_probe: YiCapabilityProbe | None = None
    material_broker: YiMaterialBroker | None = None
    if not args.disable_lifecycle:
        if args.env_file is None:
            raise SystemExit("--env-file is required unless --disable-lifecycle is used")
        broker_parent = data_dir or (selected_runtime_state or default_runtime_state_dir())
        material_socket = broker_parent / "cloud-material.sock"
        config = build_default_config(
            env_file=args.env_file,
            root=Path(__file__).resolve().parent,
            runtime_root=args.runtime_root,
            worker_dir=args.worker_dir,
            state_dir=selected_runtime_state,
            startup_timeout=args.startup_timeout,
            stall_timeout=args.stall_timeout,
            terminate_grace=args.terminate_grace,
            restart_delay=args.restart_delay,
            max_restart_delay=args.max_restart_delay,
            media_ingest_host=(media_publisher.ingest_host if media_publisher is not None else None),
            media_ingest_port=(media_publisher.ingest_port if media_publisher is not None else None),
            material_socket=material_socket,
        )
        lifecycle = YiRuntimeLifecycleManager(config)
        capability_probe = YiCapabilityProbe(
            config,
            capability_cache,
            duration_seconds=args.probe_duration,
        )

    backend_kwargs = {
        "timeout": args.timeout,
        "capability_cache": capability_cache,
        "lifecycle": lifecycle,
        "capability_probe": capability_probe,
        "media_publisher": media_publisher,
        "cloud_session": cloud_session,
    }
    if data_dir is not None:
        backend: YiAddonBackend = YiPersistentAddonBackend(
            runtime_policy=YiRuntimePolicyStore(data_dir / "runtime-policy.json"),
            **backend_kwargs,
        )
    else:
        backend = YiAddonBackend(**backend_kwargs)

    if lifecycle is not None:
        material_socket = lifecycle.config.material_socket
        if material_socket is None:
            backend.shutdown()
            raise SystemExit("private runtime-material socket is not configured")
        material_broker = YiMaterialBroker(material_socket, cloud_session)
        try:
            material_broker.start()
        except RuntimeError:
            backend.shutdown()
            raise SystemExit("private runtime-material broker failed to start")

    initial_discovery_ok = args.no_initial_discovery
    if not args.no_initial_discovery:
        try:
            backend.discover(fetch_tnp=True, reason="initial_discovery")
        except Exception:
            # Keep the service alive. Persistent runtime intent remains pending
            # and the retry loop below will reconcile it after cloud recovery.
            initial_discovery_ok = False
        else:
            initial_discovery_ok = True

    server = YiAddonHTTPServer(
        (args.bind, args.port),
        backend,
        token,
        credential_store,
        args.timeout,
    )
    stopping = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        threading.Thread(target=server.shutdown, name="yi-addon-shutdown", daemon=True).start()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    if (
        not args.no_initial_discovery
        and not initial_discovery_ok
        and args.discovery_retry_interval > 0
    ):
        def retry_initial_discovery() -> None:
            while not stopping.wait(args.discovery_retry_interval):
                try:
                    backend.discover(fetch_tnp=True, reason="initial_discovery")
                except Exception:
                    continue
                return

        threading.Thread(
            target=retry_initial_discovery,
            name="yi-initial-discovery-retry",
            daemon=True,
        ).start()

    account_configured = credential_store.status().get("configured", False) if credential_store is not None else False
    print(
        json.dumps(
            {
                "service": backend.SERVICE_NAME,
                "api_version": backend.API_VERSION,
                "bind": args.bind,
                "port": args.port,
                "authentication": "bearer" if token is not None else "loopback_only",
                "runtime_lifecycle_ready": lifecycle is not None,
                "runtime_material_broker_ready": material_broker is not None,
                "reprobe_ready": capability_probe is not None,
                "media_publisher_enabled": media_publisher is not None,
                "persistence_enabled": data_dir is not None,
                "managed_runtime_count": lifecycle.managed_count() if lifecycle is not None else 0,
                "account_configured": bool(account_configured),
                "initial_discovery_ok": initial_discovery_ok,
                "discovery_retry_enabled": bool(
                    not args.no_initial_discovery and args.discovery_retry_interval > 0
                ),
                "secrets_exposed": False,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stopping.set()
        backend.shutdown()
        if material_broker is not None:
            material_broker.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
