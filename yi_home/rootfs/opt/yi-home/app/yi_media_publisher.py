#!/usr/bin/env python3
"""Managed go2rtc publisher for the YI Home App/Add-on backend.

The publisher owns only media publication. Camera PPPP/TNP session lifecycle
remains in yi_runtime_lifecycle.py. Each discovered stable_id is represented as
an empty go2rtc stream and the lifecycle manager pushes MPEG-TS into that stream
through go2rtc's incoming HTTP MPEG-TS endpoint.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from yi_stream_identity import media_rtsp_path, media_stream_name, normalize_stable_id


@dataclass(frozen=True)
class Go2RTCPublisherConfig:
    binary: Path
    state_dir: Path
    api_host: str = "127.0.0.1"
    api_port: int = 1984
    rtsp_bind: str = "127.0.0.1"
    rtsp_port: int = 8554
    startup_timeout: float = 10.0
    terminate_grace: float = 5.0

    def validate(self) -> None:
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise RuntimeError(f"go2rtc binary missing or not executable: {self.binary}")
        if not 1 <= self.api_port <= 65535 or not 1 <= self.rtsp_port <= 65535:
            raise RuntimeError("go2rtc ports must be between 1 and 65535")
        if self.startup_timeout <= 0 or self.terminate_grace <= 0:
            raise RuntimeError("go2rtc timing values must be greater than zero")


class YiGo2RTCPublisher:
    """Own one shared go2rtc process and its generated empty stream registry."""

    def __init__(self, config: Go2RTCPublisherConfig) -> None:
        config.validate()
        self.config = config
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.config.state_dir, 0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._log_stream = None
        self._stable_ids: tuple[str, ...] = ()
        self._last_error: str | None = None
        self._generation = 0

    @property
    def config_path(self) -> Path:
        return self.config.state_dir / "go2rtc.generated.yaml"

    @property
    def log_path(self) -> Path:
        return self.config.state_dir / "go2rtc.log"

    @property
    def api_base(self) -> str:
        return f"http://{self.config.api_host}:{self.config.api_port}"

    @property
    def ingest_host(self) -> str:
        return self.config.api_host

    @property
    def ingest_port(self) -> int:
        return self.config.api_port

    def _render_config(self, stable_ids: Iterable[str]) -> str:
        keys = sorted({normalize_stable_id(value) for value in stable_ids})
        lines = [
            "api:",
            f'  listen: "{self.config.api_host}:{self.config.api_port}"',
            "rtsp:",
            f'  listen: "{self.config.rtsp_bind}:{self.config.rtsp_port}"',
            '  default_query: "video&audio"',
            "webrtc:",
            '  listen: ""',
            "log:",
            '  format: "text"',
            '  level: "info"',
            '  output: "stdout"',
            "streams:",
        ]
        if not keys:
            lines.append("  {}")
        else:
            for stable_id in keys:
                # YAML null value intentionally creates an empty go2rtc Stream.
                lines.append(f"  {media_stream_name(stable_id)}:")
        return "\n".join(lines) + "\n"

    def _write_config(self, stable_ids: Iterable[str]) -> tuple[str, ...]:
        keys = tuple(sorted({normalize_stable_id(value) for value in stable_ids}))
        raw = self._render_config(keys)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(raw, encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, self.config_path)
        try:
            os.chmod(self.config_path, 0o600)
        except OSError:
            pass
        return keys

    def _api_json(self, path: str, *, timeout: float = 1.5) -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(self.api_base + path, timeout=timeout) as response:
                if response.status != 200:
                    return None
                payload = json.loads(response.read().decode("utf-8"))
                return payload if isinstance(payload, dict) else None
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def _api_ready(self) -> bool:
        return self._api_json("/api/streams") is not None

    def _stream_snapshot(self, stream_name: str) -> dict[str, Any] | None:
        query = urllib.parse.quote(stream_name, safe="")
        return self._api_json(f"/api/streams?src={query}")

    @staticmethod
    def _registered_mpegts_producers(snapshot: dict[str, Any] | None) -> int:
        if not isinstance(snapshot, dict):
            return 0
        producers = snapshot.get("producers")
        if not isinstance(producers, list):
            return 0
        return sum(
            1
            for producer in producers
            if isinstance(producer, dict) and producer.get("format_name") == "mpegts"
        )

    @staticmethod
    def _ready_mpegts_producers(snapshot: dict[str, Any] | None) -> int:
        if not isinstance(snapshot, dict):
            return 0
        producers = snapshot.get("producers")
        if not isinstance(producers, list):
            return 0
        return sum(
            1
            for producer in producers
            if (
                isinstance(producer, dict)
                and producer.get("format_name") == "mpegts"
                and isinstance(producer.get("medias"), list)
                and len(producer["medias"]) > 0
            )
        )

    def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=self.config.terminate_grace)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
        if self._log_stream is not None:
            try:
                self._log_stream.close()
            except OSError:
                pass
            self._log_stream = None

    def sync_streams(self, stable_ids: Iterable[str]) -> dict[str, Any]:
        keys = tuple(sorted({normalize_stable_id(value) for value in stable_ids}))
        with self._lock:
            process_alive = self._process is not None and self._process.poll() is None
            if process_alive and keys == self._stable_ids and self._api_ready():
                return self.safe_status()

            self._stop_locked()
            self._stable_ids = self._write_config(keys)
            self._last_error = None
            self._generation += 1

            try:
                self._log_stream = self.log_path.open("ab", buffering=0)
                process = subprocess.Popen(
                    [str(self.config.binary), "-c", str(self.config_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=self._log_stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                self._last_error = "publisher_spawn_failed"
                if self._log_stream is not None:
                    self._log_stream.close()
                    self._log_stream = None
                raise RuntimeError("go2rtc publisher could not be started") from exc
            self._process = process

        deadline = time.monotonic() + self.config.startup_timeout
        while time.monotonic() < deadline:
            with self._lock:
                process = self._process
                if process is None:
                    break
                if process.poll() is not None:
                    self._last_error = "publisher_exited_during_startup"
                    break
            if self._api_ready():
                return self.safe_status()
            time.sleep(0.2)

        with self._lock:
            self._stop_locked()
            self._last_error = self._last_error or "publisher_startup_timeout"
        raise RuntimeError("go2rtc publisher did not become ready")

    def safe_status(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            alive = process is not None and process.poll() is None
            pid = process.pid if alive else None
            stable_ids = self._stable_ids
            last_error = self._last_error
            generation = self._generation
        return {
            "enabled": True,
            "ready": bool(alive and self._api_ready()),
            "process_alive": alive,
            "pid": pid,
            "generation": generation,
            "stream_count": len(stable_ids),
            "api_port": self.config.api_port,
            "rtsp_port": self.config.rtsp_port,
            "rtsp_bind": self.config.rtsp_bind,
            "last_error": last_error,
            "secrets_exposed": False,
        }

    def camera_endpoint(self, stable_id: str) -> dict[str, Any]:
        key = normalize_stable_id(stable_id)
        stream = media_stream_name(key)
        with self._lock:
            configured = key in self._stable_ids
        snapshot = self._stream_snapshot(stream) if configured else None
        producer_count = self._registered_mpegts_producers(snapshot)
        ready_producer_count = self._ready_mpegts_producers(snapshot)
        return {
            "stream_name": stream,
            "rtsp_path": media_rtsp_path(key),
            "rtsp_port": self.config.rtsp_port,
            "configured": configured,
            "producer_registered": producer_count > 0,
            "mpegts_producer_count": producer_count,
            "producer_media_ready": ready_producer_count > 0,
            "mpegts_ready_producer_count": ready_producer_count,
            "secrets_exposed": False,
        }

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()