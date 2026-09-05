#!/usr/bin/env python3
"""Per-camera supervised runtime lifecycle manager for the YI Home App/Add-on.

Each camera is owned independently by secret-safe stable_id. The native
PPPP/TNP relay remains behind the existing media-stall supervisor. When a media
publisher is configured, supervised MPEG-TS is streamed into the App-owned
go2rtc incoming MPEG-TS endpoint; otherwise output is drained for development
lifecycle tests.
"""

from __future__ import annotations

import http.client
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

from yi_stream_identity import media_stream_name, normalize_stable_id


_SAFE_PROBE_PREFIXES = (
    "[phase3g-relay] native_record_probe=",
    "[phase3g-relay] native_video_payload_probe=",
)
_RUNTIME_SECRET_ENV_KEYS = frozenset(
    {
        "YI_REGION",
        "YI_COUNTRY",
        "YI_ACCOUNT",
        "YI_PASSWORD",
        "YI_DEVICE_BRAND",
        "YI_DEVICE_MODEL",
        "YI_ANDROID_VERSION",
        "YI_LANGUAGE",
        "YI_ADDON_API_TOKEN",
    }
)


def runtime_child_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    child = dict(os.environ if environment is None else environment)
    for key in _RUNTIME_SECRET_ENV_KEYS:
        child.pop(key, None)
    return child


def _runtime_probe_lines(path: Path, offset: int) -> list[str]:
    """Read only the relay's fixed, secret-safe probe records."""
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            return [
                line
                for raw in stream
                if (line := raw.decode("utf-8", errors="replace").rstrip()).startswith(
                    _SAFE_PROBE_PREFIXES
                )
            ]
    except OSError:
        return []


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_runtime_state_dir() -> Path:
    override = os.getenv("YI_RUNTIME_STATE_DIR")
    if override:
        return Path(override).expanduser()
    state_home = os.getenv("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "yi-cam-integration" / "runtime"


@dataclass(frozen=True)
class RuntimeLifecycleConfig:
    python: str
    env_file: Path
    runtime_root: Path
    worker_dir: Path
    stable_relay: Path
    supervisor: Path
    state_dir: Path
    qemu: str = "qemu-aarch64"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    startup_timeout: float = 45.0
    stall_timeout: float = 12.0
    terminate_grace: float = 3.0
    restart_delay: float = 1.0
    max_restart_delay: float = 30.0
    media_ingest_host: str | None = None
    media_ingest_port: int | None = None
    media_ingest_timeout: float = 5.0
    material_socket: Path | None = None

    def validate(self) -> None:
        if not Path(self.python).is_file():
            raise RuntimeError(f"runtime python missing: {self.python}")
        paths = [
            (self.stable_relay, "stable relay"),
            (self.supervisor, "supervisor"),
        ]
        if self.material_socket is None:
            paths.insert(0, (self.env_file, "env file"))
        for path, label in paths:
            if not path.is_file():
                raise RuntimeError(f"{label} missing: {path}")
        if not self.runtime_root.is_dir():
            raise RuntimeError(f"runtime root missing: {self.runtime_root}")
        if not self.worker_dir.is_dir():
            raise RuntimeError(f"worker directory missing: {self.worker_dir}")
        if self.startup_timeout <= 0 or self.stall_timeout <= 0:
            raise RuntimeError("runtime timeouts must be greater than zero")
        if self.terminate_grace <= 0 or self.restart_delay < 0 or self.max_restart_delay <= 0:
            raise RuntimeError("runtime lifecycle timing configuration is invalid")
        if (self.media_ingest_host is None) != (self.media_ingest_port is None):
            raise RuntimeError("media ingest host and port must be configured together")
        if self.media_ingest_port is not None and not 1 <= self.media_ingest_port <= 65535:
            raise RuntimeError("media ingest port must be between 1 and 65535")
        if self.media_ingest_timeout <= 0:
            raise RuntimeError("media ingest timeout must be greater than zero")

    @property
    def media_publisher_enabled(self) -> bool:
        return self.media_ingest_host is not None and self.media_ingest_port is not None


class _CameraRuntimeController:
    def __init__(self, stable_id: str, config: RuntimeLifecycleConfig) -> None:
        self.stable_id = normalize_stable_id(stable_id)
        self.config = config
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.desired_running = False
        self.state = "stopped"
        self.pid: int | None = None
        self.generation = 0
        self.restart_count = 0
        self.last_exit_code: int | None = None
        self.last_error: str | None = None
        self.last_reason: str | None = None
        self.started_at: str | None = None
        self.updated_at = _utc_now()
        self.publisher_connected = False
        self.published_bytes = 0
        self.publisher_error: str | None = None

    @property
    def log_path(self) -> Path:
        return self.config.state_dir / f"{self.stable_id}.log"

    def _set_state(self, state: str, *, reason: str | None = None, error: str | None = None) -> None:
        with self.lock:
            self.state = state
            self.last_reason = reason
            self.last_error = error
            self.updated_at = _utc_now()

    def safe_status(self) -> dict[str, Any]:
        with self.lock:
            process_alive = self.process is not None and self.process.poll() is None
            return {
                "stable_id": self.stable_id,
                "runtime_state": self.state,
                "desired_running": self.desired_running,
                "process_alive": process_alive,
                "pid": self.pid if process_alive else None,
                "generation": self.generation,
                "restart_count": self.restart_count,
                "last_exit_code": self.last_exit_code,
                "last_reason": self.last_reason,
                "last_error": self.last_error,
                "started_at": self.started_at,
                "updated_at": self.updated_at,
                "media_publisher_enabled": self.config.media_publisher_enabled,
                "media_publisher_attached": bool(self.publisher_connected and process_alive),
                "published_bytes": self.published_bytes,
                "publisher_error": self.publisher_error,
                "stream_name": media_stream_name(self.stable_id),
                "secrets_exposed": False,
            }

    def _command(self) -> list[str]:
        cfg = self.config
        command = [
            cfg.python,
            str(cfg.supervisor),
            "--startup-timeout",
            f"{cfg.startup_timeout:g}",
            "--stall-timeout",
            f"{cfg.stall_timeout:g}",
            "--terminate-grace",
            f"{cfg.terminate_grace:g}",
            "--",
            cfg.python,
            str(cfg.stable_relay),
            "--stable-id",
            self.stable_id,
        ]
        if cfg.material_socket is not None:
            command.extend(("--material-socket", str(cfg.material_socket)))
        else:
            command.extend(("--env-file", str(cfg.env_file)))
        command.extend([
            "--runtime",
            str(cfg.runtime_root),
            "--worker-dir",
            str(cfg.worker_dir),
            "--qemu",
            cfg.qemu,
            "--ffmpeg",
            cfg.ffmpeg,
            "--ffprobe",
            cfg.ffprobe,
            "--stdout",
        ])
        return command

    def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=self.config.terminate_grace + 5.0)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass

    def _publish_mpegts(self, stream: BinaryIO, process: subprocess.Popen[bytes]) -> None:
        cfg = self.config
        assert cfg.media_ingest_host is not None and cfg.media_ingest_port is not None
        connection: http.client.HTTPConnection | None = None
        try:
            # go2rtc starts its MPEG-TS ProbeTimeout clock as soon as the POST
            # handler calls mpegts.Open(r.Body). PPPP/TNP camera startup can take
            # most or all of that five-second window, so opening the HTTP request
            # before the first TS bytes exist can register a producer with no
            # medias. Prebuffer one full transport chunk before opening the POST
            # so go2rtc gets the complete probe window for actual MPEG-TS data.
            try:
                first_chunk = stream.read(65536)
            except ValueError:
                # The lifecycle thread may close stdout after the supervised
                # runtime exits. Treat that as EOF, not a publisher failure.
                if self.stop_event.is_set() or process.poll() is not None:
                    return
                raise
            if not first_chunk:
                return

            connection = http.client.HTTPConnection(
                cfg.media_ingest_host,
                cfg.media_ingest_port,
                timeout=cfg.media_ingest_timeout,
            )
            path = "/api/stream.ts?dst=" + quote(media_stream_name(self.stable_id), safe="")
            connection.putrequest("POST", path)
            connection.putheader("Content-Type", "video/mp2t")
            connection.putheader("Transfer-Encoding", "chunked")
            connection.putheader("Cache-Control", "no-store")
            connection.endheaders()
            connection.send(f"{len(first_chunk):X}\r\n".encode("ascii"))
            connection.send(first_chunk)
            connection.send(b"\r\n")
            with self.lock:
                self.publisher_connected = True
                self.publisher_error = None
                self.published_bytes += len(first_chunk)
                self.updated_at = _utc_now()

            while not self.stop_event.is_set():
                try:
                    chunk = stream.read(65536)
                except ValueError:
                    # A closed pipe after process exit is a normal EOF race.
                    if self.stop_event.is_set() or process.poll() is not None:
                        break
                    raise
                if not chunk:
                    break
                connection.send(f"{len(chunk):X}\r\n".encode("ascii"))
                connection.send(chunk)
                connection.send(b"\r\n")
                with self.lock:
                    self.published_bytes += len(chunk)
                    self.updated_at = _utc_now()

            try:
                connection.send(b"0\r\n\r\n")
                response = connection.getresponse()
                response.read(1024)
                if response.status >= 400:
                    raise OSError("go2rtc ingest rejected MPEG-TS producer")
            except (OSError, http.client.HTTPException):
                if not self.stop_event.is_set():
                    raise
        except (OSError, ValueError, http.client.HTTPException):
            with self.lock:
                self.publisher_error = "media_publish_failed"
                self.publisher_connected = False
                self.updated_at = _utc_now()
            if not self.stop_event.is_set() and process.poll() is None:
                self._terminate_process(process)
        finally:
            with self.lock:
                self.publisher_connected = False
                self.updated_at = _utc_now()
            try:
                stream.close()
            except OSError:
                pass
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass

    def _run(self) -> None:
        first_launch = True
        consecutive_restarts = 0
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    if not self.desired_running:
                        break
                    self.publisher_connected = False
                    self.published_bytes = 0
                    self.publisher_error = None
                self._set_state("starting" if first_launch else "restarting")
                self.config.state_dir.mkdir(parents=True, exist_ok=True)
                pump_thread: threading.Thread | None = None
                generation_log_offset = 0
                try:
                    log_stream = self.log_path.open("ab", buffering=0)
                    generation_log_offset = log_stream.tell()
                    try:
                        process = subprocess.Popen(
                            self._command(),
                            stdin=subprocess.DEVNULL,
                            stdout=(subprocess.PIPE if self.config.media_publisher_enabled else subprocess.DEVNULL),
                            stderr=log_stream,
                            start_new_session=True,
                            env=runtime_child_environment(),
                        )
                    except Exception:
                        log_stream.close()
                        raise
                except OSError:
                    self._set_state(
                        "error",
                        reason="spawn_failed",
                        error="The supervised camera runtime could not be started.",
                    )
                    with self.lock:
                        self.desired_running = False
                    break

                if self.config.media_publisher_enabled:
                    if process.stdout is None:
                        log_stream.close()
                        self._terminate_process(process)
                        self._set_state("error", reason="publisher_pipe_failed", error="Media publisher pipe is unavailable.")
                        with self.lock:
                            self.desired_running = False
                        break
                    pump_thread = threading.Thread(
                        target=self._publish_mpegts,
                        args=(process.stdout, process),
                        name=f"yi-publish-{self.stable_id[:8]}",
                        daemon=True,
                    )
                    pump_thread.start()

                launched_mono = time.monotonic()
                with self.lock:
                    self.process = process
                    self.pid = process.pid
                    self.generation += 1
                    self.started_at = _utc_now()
                    self.last_error = None
                    self.last_reason = "started" if first_launch else "recreated_after_exit"
                    self.state = "running"
                    self.updated_at = _utc_now()
                first_launch = False

                rc = process.wait()

                # Let the publisher consume the pipe EOF before closing stdout.
                # Closing stdout first races with stream.read() in the publisher
                # thread and previously produced "ValueError: read of closed file".
                if pump_thread is not None and pump_thread is not threading.current_thread():
                    pump_thread.join(timeout=self.config.media_ingest_timeout + 2.0)

                if process.stdout is not None:
                    try:
                        process.stdout.close()
                    except OSError:
                        pass

                # If a reader was still blocked, closing the pipe above wakes it;
                # give it a final bounded chance to terminate cleanly.
                if (
                    pump_thread is not None
                    and pump_thread is not threading.current_thread()
                    and pump_thread.is_alive()
                ):
                    pump_thread.join(timeout=1.0)

                log_stream.close()
                for probe_line in _runtime_probe_lines(self.log_path, generation_log_offset):
                    print(
                        f"[yi-runtime-diagnostic] camera={self.stable_id[:12]} {probe_line}",
                        file=sys.stderr,
                        flush=True,
                    )
                runtime_seconds = time.monotonic() - launched_mono
                with self.lock:
                    self.process = None
                    self.pid = None
                    self.publisher_connected = False
                    self.last_exit_code = rc
                    should_continue = self.desired_running and not self.stop_event.is_set()

                if not should_continue:
                    break

                consecutive_restarts = 0 if runtime_seconds >= 60.0 else consecutive_restarts + 1
                with self.lock:
                    self.restart_count += 1
                    self.state = "restarting"
                    if self.publisher_error is not None:
                        self.last_reason = "publisher_disconnect"
                    else:
                        self.last_reason = "media_stall" if rc == 75 else "runtime_exit"
                    self.updated_at = _utc_now()

                delay = min(
                    self.config.restart_delay * (2 ** min(max(consecutive_restarts - 1, 0), 5)),
                    self.config.max_restart_delay,
                )
                if self.stop_event.wait(delay):
                    break
        finally:
            with self.lock:
                process = self.process
            if process is not None and process.poll() is None:
                self._terminate_process(process)
            with self.lock:
                self.process = None
                self.pid = None
                self.publisher_connected = False
                if self.state != "error":
                    self.state = "stopped"
                    self.last_reason = "stopped"
                self.updated_at = _utc_now()

    def start(self) -> dict[str, Any]:
        with self.lock:
            if self.desired_running and self.thread is not None and self.thread.is_alive():
                return self.safe_status()
            self.stop_event = threading.Event()
            self.desired_running = True
            self.last_error = None
            self.state = "starting"
            self.last_reason = "start_requested"
            self.updated_at = _utc_now()
            self.thread = threading.Thread(
                target=self._run,
                name=f"yi-runtime-{self.stable_id[:8]}",
                daemon=True,
            )
            thread = self.thread
        thread.start()
        return self.safe_status()

    def stop(self) -> dict[str, Any]:
        with self.lock:
            self.desired_running = False
            self.stop_event.set()
            process = self.process
            thread = self.thread
            if self.state not in {"stopped", "error"}:
                self.state = "stopping"
                self.last_reason = "stop_requested"
                self.updated_at = _utc_now()
        if process is not None and process.poll() is None:
            self._terminate_process(process)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.config.terminate_grace + self.config.media_ingest_timeout + 8.0)
        with self.lock:
            if self.thread is thread and (thread is None or not thread.is_alive()):
                self.thread = None
            if self.process is None and self.state != "error":
                self.state = "stopped"
                self.last_reason = "stopped"
                self.updated_at = _utc_now()
        return self.safe_status()

    def restart(self) -> dict[str, Any]:
        self.stop()
        with self.lock:
            self.last_reason = "restart_requested"
        return self.start()


class YiRuntimeLifecycleManager:
    """Own independent supervised camera runtimes by stable_id."""

    def __init__(self, config: RuntimeLifecycleConfig) -> None:
        config.validate()
        self.config = config
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.config.state_dir, 0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._controllers: dict[str, _CameraRuntimeController] = {}

    def _controller(self, stable_id: str) -> _CameraRuntimeController:
        key = normalize_stable_id(stable_id)
        with self._lock:
            controller = self._controllers.get(key)
            if controller is None:
                controller = _CameraRuntimeController(key, self.config)
                self._controllers[key] = controller
            return controller

    def start(self, stable_id: str) -> dict[str, Any]:
        return self._controller(stable_id).start()

    def stop(self, stable_id: str) -> dict[str, Any]:
        return self._controller(stable_id).stop()

    def restart(self, stable_id: str) -> dict[str, Any]:
        return self._controller(stable_id).restart()

    def status(self, stable_id: str) -> dict[str, Any]:
        key = normalize_stable_id(stable_id)
        with self._lock:
            controller = self._controllers.get(key)
        if controller is None:
            return {
                "stable_id": key,
                "runtime_state": "stopped",
                "desired_running": False,
                "process_alive": False,
                "pid": None,
                "generation": 0,
                "restart_count": 0,
                "last_exit_code": None,
                "last_reason": None,
                "last_error": None,
                "started_at": None,
                "updated_at": None,
                "media_publisher_enabled": self.config.media_publisher_enabled,
                "media_publisher_attached": False,
                "published_bytes": 0,
                "publisher_error": None,
                "stream_name": media_stream_name(key),
                "secrets_exposed": False,
            }
        return controller.safe_status()

    def managed_count(self) -> int:
        with self._lock:
            return sum(
                1
                for controller in self._controllers.values()
                if controller.safe_status()["desired_running"]
            )

    def shutdown_all(self) -> None:
        with self._lock:
            controllers = list(self._controllers.values())
        for controller in controllers:
            controller.stop()


def build_default_config(
    *,
    env_file: Path,
    root: Path | None = None,
    python: str | None = None,
    runtime_root: Path | None = None,
    worker_dir: Path | None = None,
    state_dir: Path | None = None,
    startup_timeout: float = 45.0,
    stall_timeout: float = 12.0,
    terminate_grace: float = 3.0,
    restart_delay: float = 1.0,
    max_restart_delay: float = 30.0,
    media_ingest_host: str | None = None,
    media_ingest_port: int | None = None,
    media_ingest_timeout: float = 5.0,
    material_socket: Path | None = None,
) -> RuntimeLifecycleConfig:
    project_root = (root or Path(__file__).resolve().parent).resolve()
    selected_runtime = (
        runtime_root or Path("/opt/yi-home/runtime/bionic-root")
    ).resolve()
    selected_worker = (
        worker_dir or selected_runtime / "data" / "local" / "tmp" / "yi-phase3g"
    ).resolve()
    return RuntimeLifecycleConfig(
        python=python or sys.executable,
        env_file=env_file.resolve(),
        runtime_root=selected_runtime,
        worker_dir=selected_worker,
        stable_relay=project_root / "yi_native_av_relay_stable.py",
        supervisor=project_root / "yi_native_session_supervisor.py",
        state_dir=(state_dir or default_runtime_state_dir()).resolve(),
        material_socket=material_socket.resolve() if material_socket is not None else None,
        startup_timeout=startup_timeout,
        stall_timeout=stall_timeout,
        terminate_grace=terminate_grace,
        restart_delay=restart_delay,
        max_restart_delay=max_restart_delay,
        media_ingest_host=media_ingest_host,
        media_ingest_port=media_ingest_port,
        media_ingest_timeout=media_ingest_timeout,
    )
