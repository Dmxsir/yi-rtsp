#!/usr/bin/env python3
"""Isolated MPEG-TS publication helpers for the CR-4C research gate."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

try:
    from .mux_pipe import (
        TS_PACKET_BYTES,
        MuxError,
        TsFramingCounter,
        start_ffmpeg_mux,
        write_mux_input,
    )
except ImportError:  # Direct execution from a relocated clean-room directory.
    from mux_pipe import (
        TS_PACKET_BYTES,
        MuxError,
        TsFramingCounter,
        start_ffmpeg_mux,
        write_mux_input,
    )


LOOPBACK = "127.0.0.1"
STREAM_NAME = "yi_cr4c_probe"
DEFAULT_API_PORT = 11984
DEFAULT_RTSP_PORT = 18554
PRODUCTION_PORTS = frozenset((1984, 8554))
INGEST_FINALIZE_HTTP_RESPONSE = "http_response"
INGEST_FINALIZE_PEER_CLOSED = "peer_closed_after_terminal"
INGEST_FINALIZE_GO2RTC_EOF = "go2rtc_eof_after_terminal"
FINALIZE_RESPONSE_BODY_LIMIT = 64


class CR4CError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def _validate_research_port(port: int) -> None:
    if not 1 <= port <= 65535 or port in PRODUCTION_PORTS:
        raise CR4CError("CR4C_PORT_INVALID")


def validate_research_ports(api_port: int, rtsp_port: int) -> None:
    _validate_research_port(api_port)
    _validate_research_port(rtsp_port)
    if api_port == rtsp_port:
        raise CR4CError("CR4C_PORT_INVALID")


def render_go2rtc_config(api_port: int, rtsp_port: int) -> str:
    validate_research_ports(api_port, rtsp_port)
    return "\n".join(
        (
            "api:",
            f'  listen: "{LOOPBACK}:{api_port}"',
            "rtsp:",
            f'  listen: "{LOOPBACK}:{rtsp_port}"',
            '  default_query: "video&audio"',
            "webrtc:",
            '  listen: ""',
            "log:",
            '  format: "text"',
            '  level: "info"',
            '  output: "stdout"',
            "streams:",
            f"  {STREAM_NAME}:",
            "",
        )
    )


def _registered_and_ready(snapshot: dict[str, Any] | None) -> tuple[bool, bool]:
    producers = snapshot.get("producers") if isinstance(snapshot, dict) else None
    if not isinstance(producers, list):
        return False, False
    matching = [
        producer
        for producer in producers
        if isinstance(producer, dict) and producer.get("format_name") == "mpegts"
    ]
    ready = any(
        isinstance(producer.get("medias"), list) and bool(producer["medias"])
        for producer in matching
    )
    return bool(matching), ready


class TemporaryGo2RTC:
    """Own exactly one loopback-only go2rtc child and its temporary files."""

    def __init__(
        self,
        binary: str,
        api_port: int = DEFAULT_API_PORT,
        rtsp_port: int = DEFAULT_RTSP_PORT,
        startup_timeout: float = 10.0,
        producer_timeout: float = 10.0,
        terminate_grace: float = 3.0,
        temp_root: Path = Path("/tmp/yi-cr4c"),
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        validate_research_ports(api_port, rtsp_port)
        if min(startup_timeout, producer_timeout, terminate_grace) <= 0:
            raise CR4CError("CR4C_SETUP")
        self.binary = binary
        self.api_port = api_port
        self.rtsp_port = rtsp_port
        self.startup_timeout = startup_timeout
        self.producer_timeout = producer_timeout
        self.terminate_grace = terminate_grace
        self.temp_root = temp_root
        self.clock = clock
        self.sleeper = sleeper
        self.process: subprocess.Popen[bytes] | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._log: BinaryIO | None = None
        self.ready = False
        self.stopped = False

    @property
    def run_dir(self) -> Path | None:
        return Path(self._temporary.name) if self._temporary is not None else None

    @property
    def api_base(self) -> str:
        return f"http://{LOOPBACK}:{self.api_port}"

    def _preflight_ports(self) -> None:
        held: list[socket.socket] = []
        try:
            for port in (self.api_port, self.rtsp_port):
                candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                held.append(candidate)
                candidate.bind((LOOPBACK, port))
        except OSError as exc:
            raise CR4CError("CR4C_PORT_IN_USE") from exc
        finally:
            for candidate in held:
                candidate.close()

    def _api_json(self, path: str, timeout: float = 1.0) -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(self.api_base + path, timeout=timeout) as response:
                if response.status != 200:
                    return None
                raw = response.read(256 * 1024 + 1)
                if len(raw) > 256 * 1024:
                    return None
                payload = json.loads(raw.decode("utf-8"))
                return payload if isinstance(payload, dict) else None
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def _registry_is_exact(self) -> bool:
        registry = self._api_json("/api/streams")
        return isinstance(registry, dict) and set(registry) == {STREAM_NAME}

    def start(self) -> None:
        executable = shutil.which(self.binary)
        if executable is None:
            raise CR4CError("GO2RTC_START_FAILED")
        self._preflight_ports()
        try:
            if self.temp_root.is_symlink():
                raise CR4CError("CR4C_SETUP")
            self.temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.temp_root.is_symlink() or not self.temp_root.is_dir():
                raise CR4CError("CR4C_SETUP")
            try:
                os.chmod(self.temp_root, 0o700)
            except OSError:
                pass
            self._temporary = tempfile.TemporaryDirectory(
                prefix="run-", dir=str(self.temp_root)
            )
            run_dir = Path(self._temporary.name)
            config_path = run_dir / "go2rtc.yaml"
            log_path = run_dir / "go2rtc.log"
            config_path.write_text(
                render_go2rtc_config(self.api_port, self.rtsp_port), encoding="utf-8"
            )
            os.chmod(config_path, 0o600)
            self._log = log_path.open("ab", buffering=0)
            os.chmod(log_path, 0o600)
            self.process = subprocess.Popen(
                [executable, "-c", str(config_path)],
                stdin=subprocess.DEVNULL,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = self.clock() + self.startup_timeout
            while self.clock() < deadline:
                if self.process.poll() is not None:
                    raise CR4CError("GO2RTC_EARLY_EXIT")
                if self._registry_is_exact():
                    self.ready = True
                    return
                self.sleeper(min(0.1, max(0.0, deadline - self.clock())))
            raise CR4CError("GO2RTC_START_TIMEOUT")
        except CR4CError:
            self.stop()
            raise
        except Exception as exc:
            self.stop()
            raise CR4CError("GO2RTC_START_FAILED") from exc

    def wait_producer_ready(
        self, cancel: threading.Event | None = None
    ) -> dict[str, bool]:
        deadline = self.clock() + self.producer_timeout
        registered = False
        while self.clock() < deadline and not (cancel and cancel.is_set()):
            if self.process is None or self.process.poll() is not None:
                raise CR4CError("GO2RTC_EARLY_EXIT")
            if self._registry_is_exact():
                query = urllib.parse.quote(STREAM_NAME, safe="")
                current_registered, current_ready = _registered_and_ready(
                    self._api_json(f"/api/streams?src={query}")
                )
                registered = registered or current_registered
                if current_ready:
                    return {
                        "producer_registered": True,
                        "producer_media_ready": True,
                    }
            self.sleeper(min(0.1, max(0.0, deadline - self.clock())))
        if cancel and cancel.is_set():
            raise CR4CError("CR4C_CANCELLED")
        raise CR4CError(
            "GO2RTC_PRODUCER_NOT_READY"
            if registered
            else "GO2RTC_PRODUCER_NOT_REGISTERED"
        )

    def stop(self) -> None:
        process = self.process
        self.process = None
        cleanup_failed = False
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=self.terminate_grace)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=self.terminate_grace)
                except subprocess.TimeoutExpired:
                    cleanup_failed = process.poll() is None
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None
        if self._temporary is not None:
            try:
                self._temporary.cleanup()
            except OSError:
                cleanup_failed = True
            else:
                self._temporary = None
        self.ready = False
        self.stopped = not cleanup_failed
        if cleanup_failed:
            raise CR4CError("CR4C_CLEANUP_TIMEOUT")


class ChunkedIngestSink:
    """Bounded loopback HTTP chunked MPEG-TS sink with no payload retention."""

    def __init__(
        self,
        api_port: int,
        timeout: float,
        connection_factory: Callable[..., http.client.HTTPConnection] = http.client.HTTPConnection,
    ) -> None:
        _validate_research_port(api_port)
        if timeout <= 0:
            raise CR4CError("CR4C_SETUP")
        self.api_port = api_port
        self.timeout = timeout
        self.connection_factory = connection_factory
        self.connection: http.client.HTTPConnection | None = None
        self.connected = False
        self.finished = False
        self.published_bytes = 0
        self.finalize_mode: str | None = None

    @property
    def path(self) -> str:
        return "/api/stream.ts?dst=" + urllib.parse.quote(STREAM_NAME, safe="")

    def _send_chunk(self, chunk: bytes) -> None:
        if self.connection is None or not chunk:
            raise CR4CError("GO2RTC_INGEST_FAILED")
        try:
            self.connection.send(f"{len(chunk):X}\r\n".encode("ascii"))
            self.connection.send(chunk)
            self.connection.send(b"\r\n")
        except (OSError, http.client.HTTPException) as exc:
            raise CR4CError("GO2RTC_INGEST_FAILED") from exc
        self.published_bytes += len(chunk)

    def open(self, first_chunk: bytes) -> None:
        if self.connection is not None or not first_chunk:
            raise CR4CError("GO2RTC_INGEST_FAILED")
        try:
            connection = self.connection_factory(LOOPBACK, self.api_port, timeout=self.timeout)
            self.connection = connection
            connection.putrequest("POST", self.path)
            connection.putheader("Content-Type", "video/mp2t")
            connection.putheader("Transfer-Encoding", "chunked")
            connection.putheader("Cache-Control", "no-store")
            connection.endheaders()
            self._send_chunk(first_chunk)
            self.connected = True
        except CR4CError:
            self.abort()
            raise
        except (OSError, http.client.HTTPException) as exc:
            self.abort()
            raise CR4CError("GO2RTC_INGEST_FAILED") from exc

    def send(self, chunk: bytes) -> None:
        if not self.connected or self.finished:
            raise CR4CError("GO2RTC_INGEST_FAILED")
        self._send_chunk(chunk)

    def finish(self) -> str:
        if self.connection is None or not self.connected or self.finished:
            raise CR4CError("GO2RTC_INGEST_FAILED")
        connection = self.connection
        try:
            try:
                connection.send(b"0\r\n\r\n")
            except socket.timeout as exc:
                raise CR4CError("GO2RTC_INGEST_FINALIZE_TIMEOUT") from exc
            except OSError as exc:
                raise CR4CError("GO2RTC_INGEST_FINALIZE_SEND_FAILED") from exc
            except http.client.HTTPException as exc:
                raise CR4CError("GO2RTC_INGEST_FINALIZE_PROTOCOL_FAILED") from exc

            try:
                response = connection.getresponse()
            except http.client.RemoteDisconnected:
                self.finalize_mode = INGEST_FINALIZE_PEER_CLOSED
            except socket.timeout as exc:
                raise CR4CError("GO2RTC_INGEST_FINALIZE_TIMEOUT") from exc
            except (OSError, http.client.HTTPException) as exc:
                raise CR4CError("GO2RTC_INGEST_FINALIZE_PROTOCOL_FAILED") from exc
            else:
                try:
                    body = response.read(FINALIZE_RESPONSE_BODY_LIMIT + 1)
                except socket.timeout as exc:
                    raise CR4CError("GO2RTC_INGEST_FINALIZE_TIMEOUT") from exc
                except (OSError, http.client.HTTPException) as exc:
                    raise CR4CError(
                        "GO2RTC_INGEST_FINALIZE_PROTOCOL_FAILED"
                    ) from exc
                if (
                    response.status == 500
                    and len(body) <= FINALIZE_RESPONSE_BODY_LIMIT
                    and body.strip(b" \t\r\n") == b"EOF"
                ):
                    self.finalize_mode = INGEST_FINALIZE_GO2RTC_EOF
                elif response.status >= 400:
                    raise CR4CError("GO2RTC_INGEST_REJECTED")
                else:
                    self.finalize_mode = INGEST_FINALIZE_HTTP_RESPONSE
            self.finished = True
            return self.finalize_mode
        finally:
            connection.close()

    def abort(self) -> None:
        connection = self.connection
        self.connection = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass


class MpegTsIngestMux:
    """Reuse the CR-4B copy-mux and pump complete TS packets into go2rtc."""

    def __init__(
        self,
        ffmpeg: str,
        setts: Callable[[str, int], str],
        sink: ChunkedIngestSink,
        child_timeout: float = 20.0,
        pump_chunk_bytes: int = 65536,
    ) -> None:
        if (
            child_timeout <= 0
            or not TS_PACKET_BYTES <= pump_chunk_bytes <= 1024 * 1024
        ):
            raise CR4CError("CR4C_SETUP")
        self.ffmpeg = ffmpeg
        self.setts = setts
        self.sink = sink
        self.child_timeout = child_timeout
        self.pump_chunk_bytes = pump_chunk_bytes
        self.started = False
        self.finished = False
        self.ingest_connected = threading.Event()
        self._mux: subprocess.Popen[bytes] | None = None
        self._video: BinaryIO | None = None
        self._audio: BinaryIO | None = None
        self._pump: threading.Thread | None = None
        self._pump_error: str | None = None
        self._result: dict[str, int] | None = None
        self._finalize_mode: str | None = None

    @property
    def failure_category(self) -> str | None:
        return self._pump_error

    @property
    def pump_alive(self) -> bool:
        return bool(self._pump and self._pump.is_alive())

    @property
    def finalize_mode(self) -> str | None:
        return self._finalize_mode

    def start(self, video_offset_ms: int, audio_offset_ms: int) -> None:
        if self.started:
            return
        try:
            self._mux, self._video, self._audio = start_ffmpeg_mux(
                self.ffmpeg, video_offset_ms, audio_offset_ms, self.setts
            )
            if self._mux.stdout is None:
                raise CR4CError("MUX_START_FAILED")
            self.started = True
            self._pump = threading.Thread(
                target=self._pump_output, name="yi-cr4c-ts-pump"
            )
            self._pump.start()
        except CR4CError:
            self.abort()
            raise
        except Exception as exc:
            self.abort()
            raise CR4CError("MUX_START_FAILED") from exc

    def _pump_output(self) -> None:
        assert self._mux is not None and self._mux.stdout is not None
        counter = TsFramingCounter()
        pending = bytearray()
        try:
            while True:
                chunk = self._mux.stdout.read(self.pump_chunk_bytes)
                if not chunk:
                    break
                pending.extend(chunk)
                complete = (
                    len(pending) // TS_PACKET_BYTES * TS_PACKET_BYTES
                )
                if not complete:
                    continue
                framed = bytes(pending[:complete])
                del pending[:complete]
                counter.observe(framed)
                if not counter.valid:
                    raise MuxError("MPEGTS_STREAM_INVALID")
                if not self.sink.connected:
                    self.sink.open(framed)
                    self.ingest_connected.set()
                else:
                    self.sink.send(framed)
            if pending:
                raise MuxError("MPEGTS_STREAM_INVALID")
            self._result = counter.finish()
            self._finalize_mode = self.sink.finish()
        except (CR4CError, MuxError) as exc:
            self._pump_error = exc.category
            self.sink.abort()
        except (OSError, ValueError):
            self._pump_error = "MUX_PIPE_BACKPRESSURE"
            self.sink.abort()
        finally:
            try:
                self._mux.stdout.close()
            except OSError:
                pass

    def _feed(self, pipe: BinaryIO | None, payload: bytes) -> None:
        if not self.started or self.finished or self._mux is None or pipe is None:
            raise CR4CError("MUX_START_FAILED")
        write_mux_input(self._mux, pipe, payload, self.child_timeout)

    def feed_video(self, payload: bytes) -> None:
        self._feed(self._video, payload)

    def feed_audio(self, payload: bytes) -> None:
        self._feed(self._audio, payload)

    @staticmethod
    def _close(stream: BinaryIO | None) -> None:
        if stream is not None and not stream.closed:
            try:
                stream.close()
            except (BrokenPipeError, OSError):
                pass

    def finish(self) -> dict[str, Any]:
        if not self.started or self.finished or self._mux is None:
            raise CR4CError("MUX_START_FAILED")
        self._close(self._video)
        self._close(self._audio)
        try:
            returncode = self._mux.wait(timeout=self.child_timeout)
        except subprocess.TimeoutExpired as exc:
            self._mux.kill()
            try:
                self._mux.wait(timeout=self.child_timeout)
            except subprocess.TimeoutExpired as cleanup_exc:
                raise CR4CError("CR4C_CLEANUP_TIMEOUT") from cleanup_exc
            raise CR4CError("MUX_PIPE_BACKPRESSURE") from exc
        if self._pump is not None:
            self._pump.join(timeout=self.child_timeout)
        if self.pump_alive:
            raise CR4CError("MUX_PIPE_BACKPRESSURE")
        if self._pump_error:
            raise CR4CError(self._pump_error)
        if returncode != 0:
            raise CR4CError("MUX_EARLY_EXIT")
        if (
            self._result is None
            or self.sink.published_bytes <= 0
            or self._finalize_mode
            not in (
                INGEST_FINALIZE_HTTP_RESPONSE,
                INGEST_FINALIZE_PEER_CLOSED,
                INGEST_FINALIZE_GO2RTC_EOF,
            )
        ):
            raise CR4CError("MPEGTS_STREAM_INVALID")
        self.finished = True
        return {
            **self._result,
            "mpegts_published_bytes": self.sink.published_bytes,
            "ingest_finalize_mode": self._finalize_mode,
        }

    def abort(self) -> None:
        self._close(self._video)
        self._close(self._audio)
        self.sink.abort()
        process = self._mux
        cleanup_failed = False
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=self.child_timeout)
            except subprocess.TimeoutExpired:
                cleanup_failed = process.poll() is None
        if process is not None and process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
        if self._pump is not None:
            self._pump.join(timeout=self.child_timeout)
            cleanup_failed = cleanup_failed or self._pump.is_alive()
        if cleanup_failed:
            raise CR4CError("CR4C_CLEANUP_TIMEOUT")


def rtsp_consumer_command(
    ffprobe: str, rtsp_port: int, min_seconds: float, io_timeout: float
) -> list[str]:
    _validate_research_port(rtsp_port)
    if min_seconds <= 0 or io_timeout <= 0:
        raise CR4CError("CR4C_SETUP")
    return [
        ffprobe,
        "-v",
        "error",
        "-rtsp_transport",
        "tcp",
        "-rw_timeout",
        str(int(io_timeout * 1_000_000)),
        "-read_intervals",
        f"%+{min_seconds:g}",
        "-count_packets",
        "-show_entries",
        "stream=codec_name,codec_type,width,height,sample_rate,channels,nb_read_packets",
        "-of",
        "json",
        f"rtsp://{LOOPBACK}:{rtsp_port}/{STREAM_NAME}",
    ]


def parse_rtsp_result(raw: bytes, active_seconds: float, minimum: float) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
        streams = payload["streams"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CR4CError("RTSP_FORMAT_INVALID") from exc
    if not isinstance(streams, list):
        raise CR4CError("RTSP_FORMAT_INVALID")
    video = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"),
        None,
    )
    if (
        video is None
        or audio is None
        or video.get("codec_name") != "h264"
        or video.get("width") != 1920
        or video.get("height") != 1080
        or audio.get("codec_name") != "aac"
        or str(audio.get("sample_rate")) != "16000"
        or audio.get("channels") not in (1, "1")
    ):
        raise CR4CError("RTSP_FORMAT_INVALID")
    try:
        video_packets = int(video.get("nb_read_packets", 0))
        audio_packets = int(audio.get("nb_read_packets", 0))
    except (TypeError, ValueError) as exc:
        raise CR4CError("RTSP_CONSUMER_STARVED") from exc
    if video_packets <= 0 or audio_packets <= 0 or active_seconds < minimum:
        raise CR4CError("RTSP_CONSUMER_STARVED")
    return {
        "video_codec": "h264",
        "video_size": "1920x1080",
        "audio_codec": "aac",
        "audio_sample_rate": 16000,
        "audio_channels": 1,
        "video_packets": video_packets,
        "audio_packets": audio_packets,
        "active_seconds": active_seconds,
    }


class RtspConsumer:
    def __init__(
        self,
        ffprobe: str,
        rtsp_port: int,
        min_seconds: float,
        io_timeout: float = 5.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.command = rtsp_consumer_command(
            ffprobe, rtsp_port, min_seconds, io_timeout
        )
        self.min_seconds = min_seconds
        self.io_timeout = io_timeout
        self.clock = clock
        self.process: subprocess.Popen[bytes] | None = None
        self.connected = False
        self.stopped = False

    def run(self) -> dict[str, Any]:
        started = self.clock()
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                stdout, _stderr = self.process.communicate(
                    timeout=self.min_seconds + self.io_timeout
                )
            except subprocess.TimeoutExpired as exc:
                self.abort()
                raise CR4CError("RTSP_CONSUMER_TIMEOUT") from exc
            active = self.clock() - started
            if self.process.returncode != 0:
                raise CR4CError("RTSP_CONNECT_FAILED")
            result = parse_rtsp_result(stdout, active, self.min_seconds)
            self.connected = True
            self.stopped = True
            return result
        except CR4CError:
            self.abort()
            raise
        except OSError as exc:
            self.abort()
            raise CR4CError("RTSP_CONNECT_FAILED") from exc

    def abort(self) -> None:
        process = self.process
        cleanup_failed = False
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=self.io_timeout)
            except subprocess.TimeoutExpired:
                cleanup_failed = process.poll() is None
        if process is not None and process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
        self.stopped = not cleanup_failed
        if cleanup_failed:
            raise CR4CError("CR4C_CLEANUP_TIMEOUT")


class PublicationCoordinator:
    """Wait for ingest/producer readiness, then run the bounded RTSP consumer."""

    def __init__(
        self,
        controller: TemporaryGo2RTC,
        mux: MpegTsIngestMux,
        consumer: RtspConsumer,
        ingest_timeout: float,
    ) -> None:
        if ingest_timeout <= 0:
            raise CR4CError("CR4C_SETUP")
        self.controller = controller
        self.mux = mux
        self.consumer = consumer
        self.ingest_timeout = ingest_timeout
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self.producer: dict[str, bool] | None = None
        self.result: dict[str, Any] | None = None
        self.error: str | None = None

    def start(self) -> None:
        if self.thread is not None:
            raise CR4CError("CR4C_SETUP")
        self.thread = threading.Thread(target=self._run, name="yi-cr4c-coordinator")
        self.thread.start()

    def _run(self) -> None:
        try:
            deadline = time.monotonic() + self.ingest_timeout
            while not self.mux.ingest_connected.wait(0.05):
                if self.cancel.is_set():
                    raise CR4CError("CR4C_CANCELLED")
                if self.mux.failure_category:
                    raise CR4CError(self.mux.failure_category)
                if time.monotonic() >= deadline:
                    raise CR4CError("GO2RTC_INGEST_FAILED")
            self.producer = self.controller.wait_producer_ready(self.cancel)
            self.result = self.consumer.run()
        except CR4CError as exc:
            self.error = exc.category
        except Exception:
            self.error = "CR4C_SETUP"

    @property
    def done(self) -> bool:
        return self.result is not None and self.error is None

    def raise_if_failed(self) -> None:
        if self.error:
            raise CR4CError(self.error)

    def stop(self) -> None:
        self.cancel.set()
        failure: CR4CError | None = None
        try:
            self.consumer.abort()
        except CR4CError as exc:
            failure = exc
        if self.thread is not None:
            self.thread.join(timeout=self.ingest_timeout)
            if self.thread.is_alive():
                failure = CR4CError("CR4C_CLEANUP_TIMEOUT")
        if failure is not None:
            raise failure
