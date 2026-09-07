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


class TsFramingCounter:
    """Count complete MPEG-TS packets while retaining only one partial packet."""

    def __init__(self) -> None:
        self.bytes = 0
        self.packets = 0
        self.valid = True
        self._remainder = bytearray()

    def observe(self, chunk: bytes) -> None:
        self.bytes += len(chunk)
        self._remainder.extend(chunk)
        complete = len(self._remainder) // TS_PACKET_BYTES * TS_PACKET_BYTES
        for offset in range(0, complete, TS_PACKET_BYTES):
            if self._remainder[offset] != 0x47:
                self.valid = False
            self.packets += 1
        del self._remainder[:complete]

    def finish(self) -> dict[str, int]:
        if self.bytes == 0 or self.packets == 0 or not self.valid or self._remainder:
            raise MuxError("MPEGTS_STREAM_INVALID")
        return {"mpegts_bytes": self.bytes, "mpegts_packets": self.packets}


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


def start_ffmpeg_mux(
    executable: str,
    video_offset_ms: int,
    audio_offset_ms: int,
    setts: Callable[[str, int], str],
) -> tuple[subprocess.Popen[bytes], BinaryIO, BinaryIO]:
    """Start the existing H.264/AAC copy-mux with two bounded input pipes."""
    video_r, video_w = os.pipe()
    audio_r, audio_w = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            ffmpeg_command(
                executable,
                video_r,
                audio_r,
                video_offset_ms,
                audio_offset_ms,
                setts,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            pass_fds=(video_r, audio_r),
            close_fds=True,
        )
        if process.stdout is None:
            raise OSError("FFmpeg stdout pipe unavailable")
        os.set_blocking(video_w, False)
        os.set_blocking(audio_w, False)
        video = os.fdopen(video_w, "wb", buffering=0)
        audio = os.fdopen(audio_w, "wb", buffering=0)
        video_w = audio_w = -1
        return process, video, audio
    except Exception:
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=2.0)
            except (subprocess.TimeoutExpired, OSError):
                pass
        raise
    finally:
        for fd in (video_r, audio_r, video_w, audio_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def write_mux_input(
    process: subprocess.Popen[bytes],
    pipe: BinaryIO,
    payload: bytes,
    timeout: float,
) -> None:
    """Write one parsed frame without allowing FFmpeg input backpressure to hang."""
    remaining = memoryview(payload)
    deadline = time.monotonic() + timeout
    while remaining:
        if process.poll() is not None:
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
        self._ts = TsFramingCounter()

    def start(self, video_offset_ms: int, audio_offset_ms: int) -> None:
        if self.started:
            return
        try:
            self._probe = subprocess.Popen(
                ffprobe_command(self.ffprobe),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self._mux, self._video, self._audio = start_ffmpeg_mux(
                self.ffmpeg,
                video_offset_ms,
                audio_offset_ms,
                self.setts,
            )
            if self._mux.stdout is None or self._probe.stdin is None:
                raise MuxError("MUX_START_FAILED")
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

    def _pump_output(self) -> None:
        assert self._mux is not None and self._mux.stdout is not None
        assert self._probe is not None and self._probe.stdin is not None
        probe_input: BinaryIO | None = self._probe.stdin
        try:
            while True:
                chunk = self._mux.stdout.read(self.pump_chunk_bytes)
                if not chunk:
                    break
                self._ts.observe(chunk)
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
            _close(probe_input)
            _close(self._mux.stdout)

    def _feed(self, pipe: BinaryIO | None, payload: bytes) -> None:
        if not self.started or self.finished or pipe is None:
            raise MuxError("MUX_START_FAILED")
        assert self._mux is not None
        write_mux_input(self._mux, pipe, payload, self.child_timeout)

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
            try:
                self._mux.wait(timeout=self.child_timeout)
            except subprocess.TimeoutExpired as cleanup_exc:
                raise MuxError("MUX_PIPE_BACKPRESSURE") from cleanup_exc
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
        ts_result = self._ts.finish()

        self._probe.stdin = None
        try:
            stdout, _stderr = self._probe.communicate(timeout=self.child_timeout)
        except subprocess.TimeoutExpired as exc:
            self._probe.kill()
            try:
                self._probe.communicate(timeout=self.child_timeout)
            except subprocess.TimeoutExpired as cleanup_exc:
                raise MuxError("FFPROBE_TIMEOUT") from cleanup_exc
            raise MuxError("FFPROBE_TIMEOUT") from exc
        if self._probe.returncode != 0:
            raise MuxError("FFPROBE_FAILED")
        metadata = parse_ffprobe_metadata(stdout)
        self.finished = True
        return {
            **metadata,
            **ts_result,
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
