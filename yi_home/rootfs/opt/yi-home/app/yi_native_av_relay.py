#!/usr/bin/env python3
"""Integrated phoneless YI PPPP/TNP H264+AAC relay for go2rtc.

This folds the Phase 3H-3J continuous-pipe fixes into one relay: bounded FFmpeg
probe windows, explicit 90 kHz packet timestamps, an explicit MPEG-TS stdout
pump, and shutdown handling that treats only an observed consumer EPIPE as a
normal mux close. The finite --output path remains equivalent to the proven
Phase 3G file path. No ADB or Android phone is used at runtime.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import signal
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, BinaryIO

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parent
PROBE_DIR = ROOT / "tools" / "phase3_pppp_probe"
for path in (ROOT, PROBE_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import run_phase3e_tnp as phase3e
import yi_live_relay
import yi_tnp_oracle as oracle

VIDEO_FPS = 20
MPEGTS_TIME_BASE = 90000
VIDEO_TICKS = MPEGTS_TIME_BASE // VIDEO_FPS
AAC_SAMPLE_RATE = 16000
AAC_SAMPLES_PER_FRAME = 1024
AUDIO_TICKS = MPEGTS_TIME_BASE * AAC_SAMPLES_PER_FRAME // AAC_SAMPLE_RATE
MAX_RECORD = 2 * 1024 * 1024 + 32
NATIVE_RECORD_PROBE_LIMIT = 3
STREAM_MAGIC = b"YAV1"
CHILD_STATE_MARKER_ENV = "YI_PHASE3_CHILD_STATE_MARKER"

_STDOUT_CONSUMER_CLOSED = threading.Event()
_PUMPS: list[threading.Thread] = []


class AudioUnitValidationError(RuntimeError):
    """One malformed/corrupt audio record that may be dropped in a live stream."""


def log(message: str) -> None:
    try:
        print(f"[phase3g-relay] {message}", file=sys.stderr, flush=True)
    except (BrokenPipeError, OSError, ValueError):
        pass


def _write_child_state_marker(qemu_alive: bool, ffmpeg_alive: bool) -> None:
    """Publish only safe child-liveness booleans for the outer supervisor."""
    raw = os.getenv(CHILD_STATE_MARKER_ENV, "").strip()
    if not raw:
        return
    path = Path(raw)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            f"qemu_alive={1 if qemu_alive else 0}\n"
            f"ffmpeg_alive={1 if ffmpeg_alive else 0}\n",
            encoding="ascii",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except (OSError, UnicodeError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _write_worker_stderr(line: bytes) -> None:
    try:
        sys.stderr.buffer.write(b"[phase3g-worker] " + line)
        sys.stderr.buffer.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass


def payload(material: oracle.CameraMaterial, units: tuple[bytes, bytes, bytes, bytes]) -> bytes:
    did = phase3e._field(material.pppp_did)
    server = phase3e._field(material.server)
    key = phase3e._field(material.device_key)
    lengths = (len(did), len(server), len(key), *(len(unit) for unit in units))
    header = b"Y3F1" + bytes((1 if material.wakeup else 0, phase3e.CONNECTION_FLAG)) + struct.pack(">H", 0)
    header += struct.pack(">IIIIIII", *lengths)
    return header + did + server + key + b"".join(units)


def read_exact(stream: BinaryIO, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        chunk = stream.read(length - len(result))
        if not chunk:
            raise EOFError("native PPPP worker closed its media pipe")
        result.extend(chunk)
    return bytes(result)


def signed_delta32(current: int, base: int) -> int:
    value = (current - base) & 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _format_native_record_probe(
    channel: int,
    raw: bytes,
    channel_index: int,
    previous: tuple[int, int, int] | None,
) -> tuple[str, tuple[int, int, int]]:
    frame = raw[8:32]
    sequence = int.from_bytes(frame[6:8], "big")
    timestamp = int.from_bytes(frame[12:16], "big")
    timestamp_ms = int.from_bytes(frame[20:24], "big")
    current = (sequence, timestamp, timestamp_ms)
    if previous is None:
        sequence_delta: int | str = "first"
        timestamp_delta: int | str = "first"
        timestamp_ms_delta: int | str = "first"
    else:
        sequence_delta = (sequence - previous[0]) & 0xFFFF
        timestamp_delta = signed_delta32(timestamp, previous[1])
        timestamp_ms_delta = signed_delta32(timestamp_ms, previous[2])
    return (
        "native_record_probe=true; "
        f"channel={channel}; channel_index={channel_index}; "
        f"native_header=YAV1/{channel}/000000/{len(raw)}; "
        f"tnp_version={raw[0]}; io_type={raw[1]}; "
        f"declared_size={int.from_bytes(raw[4:8], 'big')}; "
        f"codec_id={int.from_bytes(frame[0:2], 'big')}; flags=0x{frame[2]:02x}; "
        f"sequence={sequence}; sequence_delta={sequence_delta}; "
        f"timestamp={timestamp}; timestamp_delta={timestamp_delta}; "
        f"timestamp_ms={timestamp_ms}; timestamp_ms_delta={timestamp_ms_delta}; "
        f"dimensions={int.from_bytes(frame[8:10], 'big')}x{int.from_bytes(frame[10:12], 'big')}; "
        f"live_flag={frame[3]}; use_count={frame[5]}; "
        f"out_loss={frame[18]}; in_loss={frame[19]}"
    ), current


def decrypt_audio_unit(raw: bytes, password: str) -> tuple[int, bytes, dict[str, int]]:
    if len(raw) < 39 or raw[0] < 2 or raw[1] != 2:
        raise AudioUnitValidationError("malformed TNP v2 audio unit")
    if int.from_bytes(raw[4:8], "big") != len(raw) - 8:
        raise AudioUnitValidationError("TNP audio size mismatch")
    media = raw[8:32]
    if int.from_bytes(media[0:2], "big") != 138:
        raise AudioUnitValidationError("native relay expected AAC codec id 138")

    access_unit = raw[32:]
    key = (password + "0").encode("ascii")
    if len(key) != 16:
        # This is a session/config invariant, not a packet-level corruption.
        raise RuntimeError("TNP audio AES key is not 16 bytes")
    aligned = (len(access_unit) // 16) * 16
    if aligned:
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        access_unit = decryptor.update(access_unit[:aligned]) + decryptor.finalize() + access_unit[aligned:]

    if len(access_unit) < 7 or access_unit[0] != 0xFF or (access_unit[1] & 0xF0) != 0xF0:
        raise AudioUnitValidationError("decrypted AAC payload has no native ADTS header")
    rates = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)
    freq_index = (access_unit[2] >> 2) & 0x0F
    if freq_index >= len(rates):
        raise AudioUnitValidationError("invalid native ADTS sample-rate index")
    channels = ((access_unit[2] & 0x01) << 2) | ((access_unit[3] >> 6) & 0x03)
    object_type = ((access_unit[2] >> 6) & 0x03) + 1
    timestamp_ms = int.from_bytes(media[20:24], "big")
    return timestamp_ms, access_unit, {
        "sample_rate": rates[freq_index],
        "channels": channels,
        "object_type": object_type,
    }


def _setts(kind: str, start_ms: int) -> str:
    start_ticks = int(start_ms) * MPEGTS_TIME_BASE // 1000
    if kind == "video":
        step = VIDEO_TICKS
    elif kind == "audio":
        step = AUDIO_TICKS
    else:
        raise ValueError(kind)
    return (
        "setts=time_base=1/90000:"
        f"pts=N*{step}+{start_ticks}:"
        f"dts=N*{step}+{start_ticks}:"
        f"duration={step}"
    )


class _MuxProcessProxy:
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    def poll(self) -> int | None:
        return self._proc.poll()

    def wait(self, timeout: float | None = None) -> int:
        rc = self._proc.wait(timeout=timeout)
        if rc != 0 and _STDOUT_CONSUMER_CLOSED.is_set():
            log(f"mpegts_mux_exit_rc={rc}; consumer_close_epipe=true; normalized_rc=0")
            return 0
        return rc

    def kill(self) -> None:
        self._proc.kill()


def _start_ffmpeg_file(ffmpeg: str, output: BinaryIO, video_offset_ms: int, audio_offset_ms: int):
    video_r, video_w = os.pipe()
    audio_r, audio_w = os.pipe()

    video_opts = ["-fflags", "+genpts"]
    if video_offset_ms:
        video_opts += ["-itsoffset", f"{video_offset_ms / 1000:.3f}"]
    video_opts += ["-r", str(VIDEO_FPS), "-f", "h264", "-i", f"pipe:{video_r}"]

    audio_opts: list[str] = []
    if audio_offset_ms:
        audio_opts += ["-itsoffset", f"{audio_offset_ms / 1000:.3f}"]
    audio_opts += ["-f", "aac", "-i", f"pipe:{audio_r}"]

    command = [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin",
        *video_opts, *audio_opts,
        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
        "-muxdelay", "0", "-muxpreload", "0",
        "-mpegts_flags", "+resend_headers", "-f", "mpegts", "pipe:1",
    ]
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=sys.stderr.buffer,
            pass_fds=(video_r, audio_r),
            close_fds=True,
        )
    finally:
        os.close(video_r)
        os.close(audio_r)
    return proc, os.fdopen(video_w, "wb", buffering=0), os.fdopen(audio_w, "wb", buffering=0)


def _start_ffmpeg_stdout(ffmpeg: str, video_offset_ms: int, audio_offset_ms: int):
    _STDOUT_CONSUMER_CLOSED.clear()
    video_r, video_w = os.pipe()
    audio_r, audio_w = os.pipe()
    ts_r, ts_w = os.pipe()
    ts_writer = os.fdopen(ts_w, "wb", buffering=0)

    video_opts = [
        "-thread_queue_size", "512",
        "-probesize", "262144",
        "-analyzeduration", "500000",
        "-fflags", "+nobuffer",
        "-r", str(VIDEO_FPS),
        "-f", "h264", "-i", f"pipe:{video_r}",
    ]
    audio_opts = [
        "-thread_queue_size", "512",
        "-probesize", "32768",
        "-analyzeduration", "200000",
        "-f", "aac", "-i", f"pipe:{audio_r}",
    ]
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin",
        *video_opts, *audio_opts,
        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
        "-bsf:v", _setts("video", video_offset_ms),
        "-bsf:a", _setts("audio", audio_offset_ms),
        "-flush_packets", "1", "-muxdelay", "0", "-muxpreload", "0",
        "-mpegts_flags", "+resend_headers", "-f", "mpegts", "pipe:1",
    ]

    log("mpegts_streaming_probe_tuning=video_probe_262144/video_analyze_500ms/audio_probe_32768/audio_analyze_200ms/thread_queue_512/flush_packets")
    log(
        "mpegts_timestamp_mode=SETTS_90KHZ; "
        f"video_step_ticks={VIDEO_TICKS}; audio_step_ticks={AUDIO_TICKS}; "
        f"video_offset_ms={video_offset_ms}; audio_offset_ms={audio_offset_ms}"
    )

    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=ts_writer,
            stderr=sys.stderr.buffer,
            pass_fds=(video_r, audio_r),
            close_fds=True,
        )
    except Exception:
        for fd in (video_r, video_w, audio_r, audio_w, ts_r):
            try:
                os.close(fd)
            except OSError:
                pass
        ts_writer.close()
        raise
    else:
        os.close(video_r)
        os.close(audio_r)
        ts_writer.close()

    def pump() -> None:
        total = 0
        first = True
        next_report = 1024 * 1024
        try:
            while True:
                chunk = os.read(ts_r, 65536)
                if not chunk:
                    break
                if first:
                    log(f"mpegts_stdout_first_chunk_bytes={len(chunk)}; sync_byte={'PASS' if chunk[0] == 0x47 else 'FAIL'}")
                    first = False
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                total += len(chunk)
                if total >= next_report:
                    log(f"mpegts_stdout_progress_bytes={total}")
                    next_report += 1024 * 1024
        except BrokenPipeError:
            _STDOUT_CONSUMER_CLOSED.set()
            log("mpegts_stdout_consumer_closed=true; reason=EPIPE")
        except OSError as exc:
            if exc.errno == errno.EPIPE:
                _STDOUT_CONSUMER_CLOSED.set()
                log("mpegts_stdout_consumer_closed=true; reason=EPIPE")
            else:
                log(f"mpegts_stdout_pump_error={type(exc).__name__}")
        finally:
            try:
                os.close(ts_r)
            except OSError:
                pass
            log(f"mpegts_stdout_pumped_bytes={total}")

    thread = threading.Thread(target=pump, name="yi-mpegts-stdout-pump", daemon=True)
    thread.start()
    _PUMPS.append(thread)
    return _MuxProcessProxy(proc), os.fdopen(video_w, "wb", buffering=0), os.fdopen(audio_w, "wb", buffering=0)


def start_ffmpeg(ffmpeg: str, output: BinaryIO | None, video_offset_ms: int, audio_offset_ms: int):
    if output is not None:
        return _start_ffmpeg_file(ffmpeg, output, video_offset_ms, audio_offset_ms)
    return _start_ffmpeg_stdout(ffmpeg, video_offset_ms, audio_offset_ms)


def validate_ts(path: Path, ffprobe: str) -> bool:
    proc = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "stream=codec_name,codec_type,width,height,sample_rate,channels",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=False, timeout=20,
    )
    if proc.returncode != 0:
        log("ffprobe_mpegts=FAIL")
        return False
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log("ffprobe_mpegts=FAIL")
        return False
    streams = parsed.get("streams", []) if isinstance(parsed, dict) else []
    safe = [
        {key: stream.get(key) for key in ("codec_name", "codec_type", "width", "height", "sample_rate", "channels") if key in stream}
        for stream in streams if isinstance(stream, dict)
    ]
    log("ffprobe_mpegts=" + json.dumps(safe, sort_keys=True, separators=(",", ":")))
    video_ok = any(s.get("codec_type") == "video" and s.get("codec_name") == "h264" and s.get("width") == 1920 and s.get("height") == 1080 for s in safe)
    audio_ok = any(s.get("codec_type") == "audio" and s.get("codec_name") == "aac" and str(s.get("sample_rate")) == "16000" and int(s.get("channels", 0)) == 1 for s in safe)
    return video_ok and audio_ok


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phoneless native YI H264+AAC MPEG-TS relay")
    p.add_argument("--env-file", type=Path, default=ROOT / ".env.local")
    p.add_argument("--skip-env-load", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--runtime", type=Path, default=Path("/opt/yi-home/runtime/bionic-root"))
    p.add_argument(
        "--worker-dir",
        type=Path,
        default=Path("/opt/yi-home/runtime/bionic-root/data/local/tmp/yi-phase3g"),
    )
    p.add_argument("--qemu", default="qemu-aarch64")
    p.add_argument("--ffmpeg", default="ffmpeg")
    p.add_argument("--ffprobe", default="ffprobe")
    p.add_argument("--duration", type=float, default=0.0, help="0 means continuous until consumer/signal")
    p.add_argument("--stdout", action="store_true", help="write MPEG-TS to stdout for go2rtc")
    p.add_argument("--output", type=Path, help="write MPEG-TS to a file for validation")
    return p


def _close_pipe(pipe: BinaryIO | None) -> None:
    if pipe is None or pipe.closed:
        return
    try:
        pipe.close()
    except (BrokenPipeError, OSError):
        pass


def main() -> int:
    args = parser().parse_args()
    if args.stdout == bool(args.output):
        raise SystemExit("choose exactly one of --stdout or --output")
    if not args.skip_env_load and not args.env_file.is_file():
        raise SystemExit(".env.local is missing")
    worker = args.worker_dir / "android_pppp_av_stream"
    library = args.worker_dir / "libPPPP_API.so"
    if not worker.is_file() or not library.is_file():
        raise SystemExit("Phase 3G worker is not built")

    if not args.skip_env_load:
        oracle.load_env_file(args.env_file)
    material: oracle.CameraMaterial | None = None
    child: subprocess.Popen[bytes] | None = None
    mux: Any | None = None
    video_pipe: BinaryIO | None = None
    audio_pipe: BinaryIO | None = None
    output_file: BinaryIO | None = None
    stop_lock = threading.Lock()
    stop_sent = False
    child_state_stop = threading.Event()
    child_state_thread: threading.Thread | None = None

    try:
        material, preflight = phase3e._fresh_exact_target(timeout=10.0)
        units = phase3e._build_units(material)
        config = payload(material, units)
        log(f"target={material.name}; raw_model={material.raw_model}; model={material.normalized_model}")
        log(f"cloud_online_reported={str(preflight['cloud_online_reported']).lower()}; runtime_reachability=PPPP")
        log("media=H264/1920x1080@20fps + native AAC-LC/16000/mono; transcoding=false")
        log("phone_required=false")

        command = [
            args.qemu, "-L", str(args.runtime),
            "-E", "LD_LIBRARY_PATH=/data/local/tmp/yi-phase3g:/system/lib64",
            str(worker), "/data/local/tmp/yi-phase3g/libPPPP_API.so",
        ]
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if child.stdin is None or child.stdout is None or child.stderr is None:
            raise RuntimeError("failed to create native worker pipes")
        child.stdin.write(config)
        child.stdin.flush()
        del config

        def report_child_state() -> None:
            while True:
                qemu_alive = child is not None and child.poll() is None
                ffmpeg_alive = mux is not None and mux.poll() is None
                _write_child_state_marker(qemu_alive, ffmpeg_alive)
                if child_state_stop.wait(0.25):
                    return

        child_state_thread = threading.Thread(
            target=report_child_state,
            name="yi-child-state-reporter",
            daemon=True,
        )
        child_state_thread.start()

        def drain_stderr() -> None:
            assert child is not None and child.stderr is not None
            for line in iter(child.stderr.readline, b""):
                _write_worker_stderr(line)

        threading.Thread(target=drain_stderr, daemon=True).start()

        def request_stop() -> None:
            nonlocal stop_sent
            with stop_lock:
                if stop_sent:
                    return
                stop_sent = True
                if child is not None and child.stdin is not None:
                    try:
                        child.stdin.write(b"\x00")
                        child.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass

        def signal_stop(_signum: int, _frame: object) -> None:
            request_stop()

        signal.signal(signal.SIGINT, signal_stop)
        signal.signal(signal.SIGTERM, signal_stop)
        timer = threading.Timer(args.duration, request_stop) if args.duration > 0 else None
        if timer is not None:
            timer.daemon = True
            timer.start()

        reorder = yi_live_relay.SequenceReorderBuffer(max_pending=24, max_wait_seconds=0.35)
        pre_video: list[tuple[int, bytes]] = []
        pre_audio: list[tuple[int, bytes]] = []
        first_video_ts: int | None = None
        first_audio_ts: int | None = None
        audio_format: dict[str, int] | None = None
        video_frames = 0
        audio_frames = 0
        audio_validation_drops = 0
        probe_counts = {1: 0, 2: 0, 3: 0}
        probe_previous: dict[int, tuple[int, int, int]] = {}

        def start_mux_if_ready() -> None:
            nonlocal mux, video_pipe, audio_pipe, output_file
            if mux is not None or first_video_ts is None or first_audio_ts is None:
                return
            delta = signed_delta32(first_audio_ts, first_video_ts)
            video_offset_ms = max(0, -delta)
            audio_offset_ms = max(0, delta)
            log(f"tnp_timebase=milliseconds; video_fps={VIDEO_FPS}; aac_frame_ms=64")
            log(f"initial_av_delta_ms={delta}; video_offset_ms={video_offset_ms}; audio_offset_ms={audio_offset_ms}")
            if audio_format is not None:
                log(f"native_aac=object_type_{audio_format['object_type']}/{audio_format['sample_rate']}Hz/{audio_format['channels']}ch")
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                output_file = args.output.open("wb")
                mux_output: BinaryIO | None = output_file
            else:
                mux_output = None
            mux, video_pipe, audio_pipe = start_ffmpeg(args.ffmpeg, mux_output, video_offset_ms, audio_offset_ms)
            for _, frame in pre_video:
                video_pipe.write(frame)
            for _, frame in pre_audio:
                audio_pipe.write(frame)
            pre_video.clear()
            pre_audio.clear()
            log("mpegts_mux=STARTED")

        try:
            log(
                "native_record_probe=armed; "
                f"per_channel_limit={NATIVE_RECORD_PROBE_LIMIT}; payload_logged=false"
            )
            while True:
                header = child.stdout.read(12)
                if not header:
                    break
                if len(header) != 12:
                    raise RuntimeError("truncated native stream header")
                if header[:4] != STREAM_MAGIC or header[5:8] != b"\0\0\0":
                    raise RuntimeError("invalid native stream framing")
                channel = header[4]
                length = int.from_bytes(header[8:12], "big")
                if channel not in (1, 2, 3) or length < 32 or length > MAX_RECORD:
                    raise RuntimeError("invalid native media record")
                raw = read_exact(child.stdout, length)
                probe_this_record = probe_counts[channel] < NATIVE_RECORD_PROBE_LIMIT
                if probe_this_record:
                    probe_counts[channel] += 1
                    probe_line, probe_previous[channel] = _format_native_record_probe(
                        channel,
                        raw,
                        probe_counts[channel],
                        probe_previous.get(channel),
                    )
                    log(probe_line)

                if channel == 1:
                    try:
                        timestamp_ms, aac, fmt = decrypt_audio_unit(raw, material.password)
                    except AudioUnitValidationError:
                        # A long-running live PPPP session may occasionally yield
                        # one malformed/corrupt channel-1 record. The native
                        # worker has already framed the record atomically, so one
                        # bad audio unit must not tear down otherwise healthy
                        # H.264 publication. Drop only the isolated audio record;
                        # session/config invariants still raise normal errors.
                        audio_validation_drops += 1
                        if audio_validation_drops <= 3 or (
                            audio_validation_drops & (audio_validation_drops - 1)
                        ) == 0:
                            log(
                                f"audio_validation_drop_count={audio_validation_drops}; "
                                "action=drop_and_continue"
                            )
                        continue
                    if audio_format is None:
                        audio_format = fmt
                    elif fmt != audio_format:
                        raise RuntimeError("AAC format changed during live session")
                    if first_audio_ts is None:
                        first_audio_ts = timestamp_ms
                    audio_frames += 1
                    if mux is None:
                        pre_audio.append((timestamp_ms, aac))
                        start_mux_if_ready()
                    else:
                        assert audio_pipe is not None
                        audio_pipe.write(aac)
                    continue

                frame = yi_live_relay._decode_video_unit(channel, raw, material.password, material.encrypted)
                if probe_this_record:
                    nal_types = ",".join(str(value) for value in frame["nal_unit_types"]) or "none"
                    log(
                        "native_video_payload_probe=true; "
                        f"channel={channel}; channel_index={probe_counts[channel]}; "
                        f"sequence={frame['sequence']}; frame_type={frame['frame_type']}; "
                        f"framing={frame['framing']}; nal_types={nal_types}; "
                        f"payload_bytes={len(frame['output_payload'])}; payload_logged=false"
                    )
                for ready in reorder.push(frame):
                    timestamp_ms = int(ready["timestamp_ms"])
                    if first_video_ts is None:
                        first_video_ts = timestamp_ms
                    video_frames += 1
                    data = ready["output_payload"]
                    if mux is None:
                        pre_video.append((timestamp_ms, data))
                        start_mux_if_ready()
                    else:
                        assert video_pipe is not None
                        video_pipe.write(data)

                if mux is not None and mux.poll() is not None:
                    if args.stdout:
                        log("consumer/mux closed output; stopping native source")
                        request_stop()
                        break
                    raise RuntimeError(f"FFmpeg MPEG-TS mux exited early with {mux.returncode}")
        except BrokenPipeError:
            if args.stdout:
                log("consumer closed MPEG-TS output")
                request_stop()
            else:
                raise
        finally:
            request_stop()
            if timer is not None:
                timer.cancel()

        child_rc = child.wait(timeout=20)
        _close_pipe(video_pipe)
        _close_pipe(audio_pipe)
        mux_rc = mux.wait(timeout=20) if mux is not None else 1
        if output_file is not None:
            _close_pipe(output_file)
            output_file = None

        if audio_validation_drops:
            log(f"audio_validation_drop_total={audio_validation_drops}")
        log(f"native_worker_exit={child_rc}; mpegts_mux_exit={mux_rc}; video_frames={video_frames}; audio_frames={audio_frames}")
        if child_rc != 0 or mux_rc != 0 or video_frames == 0 or audio_frames == 0:
            log("PHASE3G_NATIVE_AV=FAIL")
            return 1

        if args.output:
            ok = validate_ts(args.output, args.ffprobe)
            log("PHASE3G_NATIVE_AV=PASS" if ok else "PHASE3G_NATIVE_AV=FAIL")
            return 0 if ok else 1
        return 0
    finally:
        child_state_stop.set()
        if child_state_thread is not None:
            child_state_thread.join(timeout=0.5)
        _close_pipe(video_pipe)
        _close_pipe(audio_pipe)
        _close_pipe(output_file)
        if child is not None and child.poll() is None:
            child.kill()
        if mux is not None and mux.poll() is None:
            mux.kill()
        _write_child_state_marker(False, False)
        if material is not None:
            material.clear()


def _write_exit_marker(rc: int) -> None:
    raw = os.getenv("YI_PHASE3_EXIT_MARKER", "").strip()
    if not raw:
        return
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"relay_exit_rc={rc}\n", encoding="utf-8")


if __name__ == "__main__":
    exit_rc = 1
    try:
        exit_rc = main()
    finally:
        try:
            _write_exit_marker(exit_rc)
        except Exception as exc:
            log(f"exit_marker_write=FAIL:{type(exc).__name__}")
    raise SystemExit(exit_rc)
