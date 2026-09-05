#!/usr/bin/env python3
"""Bounded live capability probe used by the YI Home Add-on backend.

The probe resolves a camera only by secret-safe stable_id and uses the same
continuous MPEG-TS stdout path as the production runtime. It waits for actual
stream bytes, captures a bounded media window, terminates the dedicated relay
process group, validates H264/AAC with ffprobe, and only then atomically updates
the capability cache.

A failed probe never overwrites a previously proven capability record. Every
attempt owns a dedicated process group so timeouts or Add-on shutdown cannot
leave relay/QEMU/FFmpeg descendants behind.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from yi_camera_runtime import PROVEN_PROFILE
from yi_capability_cache import CapabilityRecord, YiCapabilityCache
from yi_runtime_lifecycle import RuntimeLifecycleConfig, runtime_child_environment

STABLE_ID_RE = re.compile(r"^[0-9a-f]{20}$")
MIN_STREAM_BYTES = 64 * 1024


class CapabilityProbeError(RuntimeError):
    def __init__(self, category: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.category = category
        self.safe_message = safe_message


@dataclass(frozen=True)
class CapabilityProbeResult:
    stable_id: str
    profile: str
    capability: CapabilityRecord
    duration_seconds: float
    attempts_used: int
    forced_shutdown_after_media: bool

    def safe_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "profile": self.profile,
            "duration_seconds": self.duration_seconds,
            "attempts_used": self.attempts_used,
            "capture_mode": "streaming_stdout",
            "forced_shutdown_after_media": self.forced_shutdown_after_media,
            "capability": self.capability.safe_dict(),
            "secrets_exposed": False,
        }


def _stable_id(value: str) -> str:
    normalized = value.strip().casefold()
    if not STABLE_ID_RE.fullmatch(normalized):
        raise ValueError("stable_id must be exactly 20 lowercase hexadecimal characters")
    return normalized


def _validated_media(probe_json: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        payload = json.loads(probe_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityProbeError(
            "media_validation_failed",
            "The live probe did not produce readable media metadata.",
        ) from exc

    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        raise CapabilityProbeError(
            "media_validation_failed",
            "The live probe did not produce a valid media stream list.",
        )

    video = next(
        (
            item
            for item in streams
            if isinstance(item, dict)
            and item.get("codec_type") == "video"
            and item.get("codec_name") == "h264"
            and int(item.get("width", 0) or 0) > 0
            and int(item.get("height", 0) or 0) > 0
        ),
        None,
    )
    audio = next(
        (
            item
            for item in streams
            if isinstance(item, dict)
            and item.get("codec_type") == "audio"
            and item.get("codec_name") == "aac"
            and int(item.get("sample_rate", 0) or 0) > 0
            and int(item.get("channels", 0) or 0) > 0
        ),
        None,
    )
    if video is None or audio is None:
        raise CapabilityProbeError(
            "media_validation_failed",
            "The live probe did not prove both H264 video and AAC audio.",
        )
    return video, audio


class YiCapabilityProbe:
    """Run bounded secret-safe capability proofs and own their child processes."""

    def __init__(
        self,
        config: RuntimeLifecycleConfig,
        capability_cache: YiCapabilityCache,
        *,
        duration_seconds: float = 8.0,
        media_start_timeout_seconds: float = 20.0,
        attempts: int = 2,
        session_settle_seconds: float = 3.0,
        retry_delay_seconds: float = 2.0,
        terminate_grace_seconds: float = 4.0,
    ) -> None:
        config.validate()
        if duration_seconds <= 0 or media_start_timeout_seconds <= 0:
            raise ValueError("probe timing values must be greater than zero")
        if attempts < 1 or attempts > 3:
            raise ValueError("probe attempts must be between 1 and 3")
        if session_settle_seconds < 0 or retry_delay_seconds < 0:
            raise ValueError("probe handoff delays must not be negative")
        if terminate_grace_seconds <= 0:
            raise ValueError("probe terminate grace must be greater than zero")
        self.config = config
        self.capability_cache = capability_cache
        self.duration_seconds = float(duration_seconds)
        self.media_start_timeout_seconds = float(media_start_timeout_seconds)
        self.attempts = int(attempts)
        self.session_settle_seconds = float(session_settle_seconds)
        self.retry_delay_seconds = float(retry_delay_seconds)
        self.terminate_grace_seconds = float(terminate_grace_seconds)
        self._active_lock = threading.RLock()
        self._active: dict[int, subprocess.Popen[bytes]] = {}
        self._shutdown = threading.Event()

    @property
    def attempt_timeout_seconds(self) -> float:
        return self.media_start_timeout_seconds + self.duration_seconds + self.terminate_grace_seconds + 2.0

    def _relay_command(self, stable_id: str) -> list[str]:
        cfg = self.config
        command = [
            cfg.python,
            str(cfg.stable_relay),
            "--stable-id",
            stable_id,
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

    def _kill_group(self, process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            if process.poll() is None:
                try:
                    process.send_signal(sig)
                except ProcessLookupError:
                    pass

    def _terminate_group(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        self._kill_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=self.terminate_grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        self._kill_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass

    def _stop_after_media(self, process: subprocess.Popen[bytes]) -> bool:
        """Ask only the relay to stop first; kill the group only if shutdown stalls."""
        if process.poll() is not None:
            return False
        try:
            process.terminate()
        except ProcessLookupError:
            return False
        try:
            process.wait(timeout=self.terminate_grace_seconds)
            return False
        except subprocess.TimeoutExpired:
            self._kill_group(process, signal.SIGKILL)
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
            return True

    def _register(self, process: subprocess.Popen[bytes]) -> None:
        with self._active_lock:
            if self._shutdown.is_set():
                self._terminate_group(process)
                raise CapabilityProbeError(
                    "probe_cancelled",
                    "The live capability probe was cancelled because the Add-on is shutting down.",
                )
            self._active[process.pid] = process

    def _unregister(self, process: subprocess.Popen[bytes]) -> None:
        with self._active_lock:
            self._active.pop(process.pid, None)

    def _sleep_interruptible(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._shutdown.wait(seconds):
            raise CapabilityProbeError(
                "probe_cancelled",
                "The live capability probe was cancelled because the Add-on is shutting down.",
            )

    def _run_stream_attempt(
        self,
        key: str,
        media_path: Path,
        log_stream: BinaryIO,
        attempt: int,
    ) -> bool:
        marker = (
            f"\n=== reprobe attempt {attempt}/{self.attempts}; "
            f"mode=streaming_stdout; media_start_timeout={self.media_start_timeout_seconds:g}s; "
            f"capture={self.duration_seconds:g}s ===\n"
        ).encode("utf-8")
        log_stream.write(marker)
        log_stream.flush()

        try:
            media_stream = media_path.open("wb", buffering=0)
        except OSError as exc:
            raise CapabilityProbeError(
                "probe_output_failed",
                "The live capability probe could not create its temporary media output.",
            ) from exc

        try:
            try:
                process = subprocess.Popen(
                    self._relay_command(key),
                    stdin=subprocess.DEVNULL,
                    stdout=media_stream,
                    stderr=log_stream,
                    start_new_session=True,
                    env=runtime_child_environment(),
                )
            except OSError as exc:
                raise CapabilityProbeError(
                    "probe_spawn_failed",
                    "The live camera capability probe could not be started.",
                ) from exc

            self._register(process)
            try:
                launched = time.monotonic()
                media_started_at: float | None = None

                while True:
                    if self._shutdown.is_set():
                        self._terminate_group(process)
                        raise CapabilityProbeError(
                            "probe_cancelled",
                            "The live capability probe was cancelled because the Add-on is shutting down.",
                        )

                    rc = process.poll()
                    try:
                        size = media_path.stat().st_size
                    except OSError:
                        size = 0

                    now = time.monotonic()
                    if media_started_at is None and size >= MIN_STREAM_BYTES:
                        media_started_at = now
                        log_stream.write(
                            f"reprobe_stream_bytes_ready={size}\n".encode("utf-8")
                        )
                        log_stream.flush()

                    if media_started_at is not None and now - media_started_at >= self.duration_seconds:
                        forced = self._stop_after_media(process)
                        log_stream.write(
                            f"reprobe_capture_complete=true; bytes={size}; forced_shutdown_after_media={str(forced).lower()}\n".encode("utf-8")
                        )
                        log_stream.flush()
                        return forced

                    if rc is not None:
                        if size >= MIN_STREAM_BYTES:
                            log_stream.write(
                                f"reprobe_runtime_exit_after_media_rc={rc}; bytes={size}\n".encode("utf-8")
                            )
                            log_stream.flush()
                            return False
                        raise CapabilityProbeError(
                            "probe_runtime_failed",
                            "The camera runtime exited before producing enough media for capability validation.",
                        )

                    if media_started_at is None and now - launched >= self.media_start_timeout_seconds:
                        self._terminate_group(process)
                        raise CapabilityProbeError(
                            "probe_timeout",
                            "The live camera capability probe timed out before producing media.",
                        )

                    self._sleep_interruptible(0.1)
            finally:
                if process.poll() is None:
                    self._terminate_group(process)
                self._unregister(process)
        finally:
            media_stream.close()

    def _probe_media(self, media_path: Path, probe_json: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        if not media_path.is_file() or media_path.stat().st_size < MIN_STREAM_BYTES:
            raise CapabilityProbeError(
                "media_validation_failed",
                "The live probe did not collect enough MPEG-TS media for validation.",
            )
        try:
            with probe_json.open("wb") as output:
                metadata = subprocess.run(
                    [
                        self.config.ffprobe,
                        "-v",
                        "error",
                        "-show_entries",
                        "stream=codec_name,codec_type,width,height,sample_rate,channels",
                        "-of",
                        "json",
                        str(media_path),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=10.0,
                    check=False,
                )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise CapabilityProbeError(
                "media_validation_failed",
                "The live probe media could not be validated.",
            ) from exc
        if metadata.returncode != 0:
            raise CapabilityProbeError(
                "media_validation_failed",
                "The live probe media could not be validated.",
            )
        return _validated_media(probe_json)

    def probe(self, stable_id: str, *, session_handoff: bool = False) -> CapabilityProbeResult:
        if self._shutdown.is_set():
            raise CapabilityProbeError(
                "probe_cancelled",
                "The live capability probe is unavailable because the Add-on is shutting down.",
            )
        key = _stable_id(stable_id)
        probe_dir = self.config.state_dir / "probes"
        probe_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(probe_dir, 0o700)
        except OSError:
            pass
        last_log = probe_dir / f"{key}-last.log"

        if session_handoff:
            self._sleep_interruptible(self.session_settle_seconds)

        with tempfile.TemporaryDirectory(prefix=f"yi-probe-{key[:8]}-", dir=probe_dir) as temporary:
            work = Path(temporary)
            media_path = work / "probe.ts"
            probe_json = work / "ffprobe.json"
            attempts_used = 0
            forced_shutdown_after_media = False
            video: dict[str, Any] | None = None
            audio: dict[str, Any] | None = None

            with last_log.open("wb", buffering=0) as log_stream:
                try:
                    os.chmod(last_log, 0o600)
                except OSError:
                    pass

                for attempt in range(1, self.attempts + 1):
                    attempts_used = attempt
                    media_path.unlink(missing_ok=True)
                    probe_json.unlink(missing_ok=True)
                    try:
                        forced_shutdown_after_media = self._run_stream_attempt(
                            key, media_path, log_stream, attempt
                        )
                        video, audio = self._probe_media(media_path, probe_json)
                        log_stream.write(b"reprobe_media_validation=PASS\n")
                        log_stream.flush()
                        break
                    except CapabilityProbeError as exc:
                        log_stream.write(
                            f"reprobe_attempt_result={exc.category}\n".encode("utf-8")
                        )
                        log_stream.flush()
                        retryable = exc.category in {
                            "probe_runtime_failed",
                            "probe_timeout",
                            "media_validation_failed",
                        }
                        if attempt >= self.attempts or not retryable:
                            raise
                        self._sleep_interruptible(self.retry_delay_seconds)

            if video is None or audio is None:
                raise CapabilityProbeError(
                    "media_validation_failed",
                    "The live capability probe did not produce validated media.",
                )

            record = self.capability_cache.record_success(
                stable_id=key,
                profile=PROVEN_PROFILE,
                video_codec="h264",
                video_width=int(video["width"]),
                video_height=int(video["height"]),
                audio_codec="aac",
                audio_sample_rate=int(audio["sample_rate"]),
                audio_channels=int(audio["channels"]),
                source="addon_api_reprobe",
            )
            return CapabilityProbeResult(
                stable_id=key,
                profile=PROVEN_PROFILE,
                capability=record,
                duration_seconds=self.duration_seconds,
                attempts_used=attempts_used,
                forced_shutdown_after_media=forced_shutdown_after_media,
            )

    def shutdown(self) -> None:
        self._shutdown.set()
        with self._active_lock:
            processes = list(self._active.values())
        for process in processes:
            self._terminate_group(process)
