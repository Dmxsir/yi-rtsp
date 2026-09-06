#!/usr/bin/env python3
"""Bounded FFmpeg-to-ffprobe pipe validation for CR-4B research."""
from __future__ import annotations

import errno
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any, BinaryIO

TS_PACKET_BYTES = 188


class MuxError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def ffmpeg_command(
    executable: str,
    video_fd: int,
    audio_fd: int,
    video_offset_ms: int,
    audio_offset_ms: int,
    setts: Callable[[str, int], str],
) -> list[str]:
    """Match the existing streaming relay's copy-mux and SETTS semantics."""
    return [
        executable,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-thread_queue_size",
        "512",
        "-probesize",
        "262144",
        "-analyzeduration",
        "500000",
        "-fflags",
        "+nobuffer",
        "-r",
        "20",
        "-f",
        "h264",
        "-i",
        f"pipe:{video_fd}",
        "-thread_queue_size",
        "512",
        "-probesize",
        "32768",
        "-analyzeduration",
        "200000",
        "-f",
        "aac",
        "-i",
        f"pipe:{audio_fd}",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        "-bsf:v",
        setts("video", video_offset_ms),
        "-bsf:a",
        setts("audio", audio_offset_ms),
        "-flush_packets",
        "1",
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "-mpegts_flags",
        "+resend_headers",
        "-f",
        "mpegts",
        "pipe:1",
    ]


def ffprobe_command(executable: str) -> list[str]:
    return [
        executable,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_name,codec_type,width,height,sample_rate,channels",
        "-of",
        "json",
        "-i",
        "pipe:0",
    ]


def parse_ffprobe_metadata(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MuxError("MPEGTS_STREAM_INVALID") from exc
    streams = value.get("streams") if isinstance(value, dict) else None
    if not isinstance(streams, list):
        raise MuxError("MPEGTS_STREAM_INVALID")
    video = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict)
            and stream.get("codec_type") == "video"
            and stream.get("codec_name") == "h264"
            and stream.get("width") == 1920
            and stream.get("height") == 1080
        ),
        None,
    )
    audio = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict)
            and stream.get("codec_type") == "audio"
            and stream.get("codec_name") == "aac"
            and str(stream.get("sample_rate")) == "16000"
            and stream.get("channels") in (1, "1")
        ),
        None,
    )
    if video is None or audio is None:
        raise MuxError("MPEGTS_STREAM_INVALID")
    return {
        "video_codec": "h264",
        "video_size": "1920x1080",
        "audio_codec": "aac",
        "audio_sample_rate": 16000,
        "audio_channels": 1,
    }


def _close(stream: BinaryIO | None) -> None:
    if stream is None or stream.closed:
        return
    try:
        stream.close()
    except (BrokenPipeError, OSError):
        pass


class PipeMuxValidator:
    """Incrementally mux parsed media and validate TS metadata without a file."""

    def __init__(
        self,
        ffmpeg: str,
        ffprobe: str,
        setts: Callable[[str, int], str],
        child_timeout: float = 20.0,
        pump_chunk_bytes: int = 65536,
    ) -> None:
        if child_timeout <= 0 or not 188 <= pump_chunk_bytes <= 1024 * 1024:
            raise ValueError("invalid mux limits")
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.setts = setts
        self.child_timeout = child_timeout
        self.pump_chunk_bytes = pump_chunk_bytes
        self.started = False
        self.finished = False
        self._mux: subprocess.Popen[bytes] | None = None
        self._probe: subprocess.Popen[bytes] | None = None
        self._video: BinaryIO | None = None
        self._audio: BinaryIO | None = None
        self._pump: threading.Thread | None = None
        self._pump_error: str | None = None
        self._probe_epipe = False
        self._ts_bytes = 0
        self._ts_packets = 0
        self._ts_valid = True
        self._ts_remainder = 0

    def start(self, video_offset_ms: int, audio_offset_ms: int) -> None:
        if self.started:
            return
        video_r, video_w = os.pipe()
        audio_r, audio_w = os.pipe()
        try:
            self._probe = subprocess.Popen(
                ffprobe_command(self.ffprobe),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self._mux = subprocess.Popen(
                ffmpeg_command(
                    self.ffmpeg,
                    video_r,
                    audio_r,
                    video_offset_ms,
                    audio_offset_ms,
                    self.setts,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                pass_fds=(video_r, audio_r),
                close_fds=True,
            )
            if self._mux.stdout is None or self._probe.stdin is None:
                raise MuxError("MUX_START_FAILED")
            os.set_blocking(video_w, False)
            os.set_blocking(audio_w, False)
            self._video = os.fdopen(video_w, "wb", buffering=0)
            self._audio = os.fdopen(audio_w, "wb", buffering=0)
            video_w = audio_w = -1
            self.started = True
            self._pump = threading.Thread(
                target=self._pump_output,
                name="yi-cr4b-ts-pump",
            )
            self._pump.start()
        except MuxError:
            self.abort()
            raise
        except Exception as exc:
            self.abort()
            raise MuxError("MUX_START_FAILED") from exc
        finally:
            for fd in (video_r, audio_r, video_w, audio_w):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def _pump_output(self) -> None:
        assert self._mux is not None and self._mux.stdout is not None
        assert self._probe is not None and self._probe.stdin is not None
        remainder = bytearray()
        probe_input: BinaryIO | None = self._probe.stdin
        try:
            while True:
                chunk = self._mux.stdout.read(self.pump_chunk_bytes)
                if not chunk:
                    break
                self._ts_bytes += len(chunk)
                remainder.extend(chunk)
                complete = len(remainder) // TS_PACKET_BYTES * TS_PACKET_BYTES
                for offset in range(0, complete, TS_PACKET_BYTES):
                    if remainder[offset] != 0x47:
                        self._ts_valid = False
                    self._ts_packets += 1
                del remainder[:complete]
                if probe_input is not None:
                    try:
                        probe_input.write(chunk)
                        probe_input.flush()
                    except BrokenPipeError:
                        self._probe_epipe = True
                        _close(probe_input)
                        probe_input = None
                    except OSError as exc:
                        if exc.errno == errno.EPIPE:
                            self._probe_epipe = True
                        else:
                            self._pump_error = "MUX_PIPE_BACKPRESSURE"
                        _close(probe_input)
                        probe_input = None
        except OSError:
            self._pump_error = "MUX_PIPE_BACKPRESSURE"
        finally:
            self._ts_remainder = len(remainder)
            _close(probe_input)
            _close(self._mux.stdout)

    def _feed(self, pipe: BinaryIO | None, payload: bytes) -> None:
        if not self.started or self.finished or pipe is None:
            raise MuxError("MUX_START_FAILED")
        assert self._mux is not None
        remaining = memoryview(payload)
        deadline = time.monotonic() + self.child_timeout
        while remaining:
            if self._mux.poll() is not None:
                raise MuxError("MUX_EARLY_EXIT")
            try:
                written = os.write(pipe.fileno(), remaining)
            except BlockingIOError:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    raise MuxError("MUX_PIPE_BACKPRESSURE")
                time.sleep(min(0.01, wait))
                continue
            except (BrokenPipeError, OSError) as exc:
                raise MuxError("MUX_EARLY_EXIT") from exc
            if written <= 0:
                raise MuxError("MUX_EARLY_EXIT")
            remaining = remaining[written:]

    def feed_video(self, payload: bytes) -> None:
        self._feed(self._video, payload)

    def feed_audio(self, payload: bytes) -> None:
        self._feed(self._audio, payload)

    def finish(self) -> dict[str, Any]:
        if not self.started or self.finished:
            raise MuxError("MUX_START_FAILED")
        assert self._mux is not None and self._probe is not None
        _close(self._video)
        _close(self._audio)
        try:
            mux_rc = self._mux.wait(timeout=self.child_timeout)
        except subprocess.TimeoutExpired as exc:
            self._mux.kill()
            self._mux.wait()
            raise MuxError("MUX_PIPE_BACKPRESSURE") from exc
        if self._pump is not None:
            self._pump.join(timeout=self.child_timeout)
        if self._mux is not None:
            _close(self._mux.stdout)
        if self._pump is not None and self._pump.is_alive():
            raise MuxError("MUX_PIPE_BACKPRESSURE")
        if self._pump_error:
            raise MuxError(self._pump_error)
        if mux_rc != 0:
            raise MuxError("MUX_EARLY_EXIT")
        if self._ts_bytes == 0 or self._ts_packets == 0 or not self._ts_valid or self._ts_remainder:
            raise MuxError("MPEGTS_STREAM_INVALID")

        self._probe.stdin = None
        try:
            stdout, _stderr = self._probe.communicate(timeout=self.child_timeout)
        except subprocess.TimeoutExpired as exc:
            self._probe.kill()
            self._probe.communicate()
            raise MuxError("FFPROBE_TIMEOUT") from exc
        if self._probe.returncode != 0:
            raise MuxError("FFPROBE_FAILED")
        metadata = parse_ffprobe_metadata(stdout)
        self.finished = True
        return {
            **metadata,
            "mpegts_bytes": self._ts_bytes,
            "mpegts_packets": self._ts_packets,
            "ffprobe_epipe_normalized": self._probe_epipe,
        }

    def abort(self) -> None:
        _close(self._video)
        _close(self._audio)
        if self._probe is not None:
            _close(self._probe.stdin)
            _close(self._probe.stdout)
        for process in (self._mux, self._probe):
            if process is not None and process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=self.child_timeout)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        if self._pump is not None:
            self._pump.join(timeout=self.child_timeout)

    @property
    def pump_alive(self) -> bool:
        return bool(self._pump and self._pump.is_alive())
