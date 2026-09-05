#!/usr/bin/env python3
"""Supervise a continuous native YI relay and fail fast on media stalls.

The proven relay remains responsible for PPPP/TNP and MPEG-TS generation. This
wrapper only forwards its stdout to go2rtc and watches for forward progress. If
the child stops producing bytes after startup, the wrapper terminates the full
relay process group and exits non-zero so a persistent go2rtc preload consumer
can recreate a fresh producer/session without leaving QEMU/FFmpeg orphans.

For HA OS diagnosis, startup stalls use a distinct exit code. Post-start media
stalls prefer a relay-owned, secret-safe child-state marker that reports only
whether the qemu-aarch64 worker and ffmpeg mux are alive plus one fixed blocking
stage. A /proc descendant snapshot remains a development fallback. No command
lines, PIDs or runtime material are surfaced.
"""

from __future__ import annotations

import argparse
import errno
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO


EXIT_STARTUP_STALL = 74
EXIT_MEDIA_STALL = 75
EXIT_MEDIA_STALL_QEMU_FFMPEG_ALIVE = 76
EXIT_MEDIA_STALL_QEMU_ALIVE_FFMPEG_MISSING = 77
EXIT_MEDIA_STALL_QEMU_MISSING_FFMPEG_ALIVE = 78
EXIT_MEDIA_STALL_QEMU_FFMPEG_MISSING = 79
EXIT_MEDIA_STALL_NATIVE_HEADER_WAIT = 100
EXIT_MEDIA_STALL_NATIVE_PAYLOAD_WAIT = 101
EXIT_MEDIA_STALL_AUDIO_PIPE_WRITE = 102
EXIT_MEDIA_STALL_VIDEO_PIPE_WRITE = 103
EXIT_MEDIA_STALL_MUX_STARTING = 104
EXIT_MEDIA_STALL_RELAY_PROCESSING = 105
CHILD_STATE_MARKER_ENV = "YI_PHASE3_CHILD_STATE_MARKER"

_ALLOWED_RELAY_STAGES = {
    "native_header_read",
    "native_payload_read",
    "audio_pipe_write",
    "video_pipe_write",
    "mux_starting",
    "audio_processing",
    "video_processing",
}


def log(message: str) -> None:
    try:
        print(f"[yi-session-supervisor] {message}", file=sys.stderr, flush=True)
    except (BrokenPipeError, OSError, ValueError):
        pass


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Watch a native YI relay for media stalls")
    p.add_argument("--startup-timeout", type=float, default=45.0)
    p.add_argument("--stall-timeout", type=float, default=12.0)
    p.add_argument("--terminate-grace", type=float, default=3.0)
    p.add_argument("command", nargs=argparse.REMAINDER)
    return p


def signal_child_group(child: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    """Signal the relay and every descendant in its dedicated process group."""
    try:
        os.killpg(child.pid, sig)
        return
    except ProcessLookupError:
        return
    except (PermissionError, OSError):
        if child.poll() is None:
            try:
                child.send_signal(sig)
            except ProcessLookupError:
                pass


def terminate_child(child: subprocess.Popen[bytes], grace: float) -> None:
    if child.poll() is not None:
        return
    log("terminate_scope=process_group; signal=SIGTERM")
    signal_child_group(child, signal.SIGTERM)
    try:
        child.wait(timeout=max(0.1, grace))
        return
    except subprocess.TimeoutExpired:
        pass
    log("terminate_scope=process_group; signal=SIGKILL")
    signal_child_group(child, signal.SIGKILL)
    try:
        child.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def write_all(output: BinaryIO, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = output.write(view)
        if written is None:
            output.flush()
            return
        view = view[written:]
    output.flush()


def _create_child_state_marker() -> Path | None:
    """Create one per-supervisor 0600 marker path, best effort."""
    try:
        with tempfile.NamedTemporaryFile(
            prefix="yi-child-state-",
            dir="/tmp",
            delete=False,
        ) as handle:
            path = Path(handle.name)
        path.chmod(0o600)
        return path
    except OSError:
        return None


def _read_child_state_marker(
    path: Path | None,
) -> tuple[set[str], str | None] | None:
    """Read fixed child booleans and optional allowlisted relay stage."""
    if path is None:
        return None
    try:
        raw = path.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None

    values: dict[str, bool] = {}
    relay_stage: str | None = None
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        if key in {"qemu_alive", "ffmpeg_alive"}:
            if value not in {"0", "1"}:
                return None
            values[key] = value == "1"
            continue
        if key == "relay_stage":
            if value not in _ALLOWED_RELAY_STAGES:
                return None
            relay_stage = value

    if set(values) != {"qemu_alive", "ffmpeg_alive"}:
        return None

    comms: set[str] = set()
    if values["qemu_alive"]:
        comms.add("qemu-aarch64")
    if values["ffmpeg_alive"]:
        comms.add("ffmpeg")
    return comms, relay_stage


def _read_proc_children(pid: int) -> list[int] | None:
    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    if not raw:
        return []
    result: list[int] = []
    for value in raw.split():
        try:
            result.append(int(value))
        except ValueError:
            continue
    return result


def _read_proc_comm(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _descendant_comms(root_pid: int) -> set[str] | None:
    """Return descendant process names, or None when /proc cannot be inspected."""
    first = _read_proc_children(root_pid)
    if first is None:
        return None
    pending = list(first)
    seen: set[int] = set()
    comms: set[str] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        comm = _read_proc_comm(pid)
        if comm:
            comms.add(comm)
        children = _read_proc_children(pid)
        if children:
            pending.extend(children)
    return comms


def _classify_stall_processes(comms: set[str] | None) -> tuple[int, str]:
    """Map a process snapshot to a secret-safe stall diagnostic exit code."""
    if comms is None:
        return EXIT_MEDIA_STALL, "process_state_unavailable"
    qemu_alive = "qemu-aarch64" in comms
    ffmpeg_alive = "ffmpeg" in comms
    if qemu_alive and ffmpeg_alive:
        return EXIT_MEDIA_STALL_QEMU_FFMPEG_ALIVE, "qemu_alive_ffmpeg_alive"
    if qemu_alive:
        return EXIT_MEDIA_STALL_QEMU_ALIVE_FFMPEG_MISSING, "qemu_alive_ffmpeg_missing"
    if ffmpeg_alive:
        return EXIT_MEDIA_STALL_QEMU_MISSING_FFMPEG_ALIVE, "qemu_missing_ffmpeg_alive"
    return EXIT_MEDIA_STALL_QEMU_FFMPEG_MISSING, "qemu_missing_ffmpeg_missing"


def _classify_alive_relay_stage(relay_stage: str | None) -> tuple[int, str] | None:
    """Refine qemu+ffmpeg-alive stalls by the relay's fixed blocking stage."""
    mapping = {
        "native_header_read": (
            EXIT_MEDIA_STALL_NATIVE_HEADER_WAIT,
            "qemu_alive_ffmpeg_alive_native_header_wait",
        ),
        "native_payload_read": (
            EXIT_MEDIA_STALL_NATIVE_PAYLOAD_WAIT,
            "qemu_alive_ffmpeg_alive_native_payload_wait",
        ),
        "audio_pipe_write": (
            EXIT_MEDIA_STALL_AUDIO_PIPE_WRITE,
            "qemu_alive_ffmpeg_alive_audio_pipe_write",
        ),
        "video_pipe_write": (
            EXIT_MEDIA_STALL_VIDEO_PIPE_WRITE,
            "qemu_alive_ffmpeg_alive_video_pipe_write",
        ),
        "mux_starting": (
            EXIT_MEDIA_STALL_MUX_STARTING,
            "qemu_alive_ffmpeg_alive_mux_starting",
        ),
        "audio_processing": (
            EXIT_MEDIA_STALL_RELAY_PROCESSING,
            "qemu_alive_ffmpeg_alive_relay_processing",
        ),
        "video_processing": (
            EXIT_MEDIA_STALL_RELAY_PROCESSING,
            "qemu_alive_ffmpeg_alive_relay_processing",
        ),
    }
    return mapping.get(relay_stage)


def _media_stall_diagnostic(
    root_pid: int,
    marker_path: Path | None,
) -> tuple[int, str, str]:
    marker = _read_child_state_marker(marker_path)
    if marker is not None:
        marker_comms, relay_stage = marker
        rc, state = _classify_stall_processes(marker_comms)
        if rc == EXIT_MEDIA_STALL_QEMU_FFMPEG_ALIVE:
            refined = _classify_alive_relay_stage(relay_stage)
            if refined is not None:
                rc, state = refined
        return rc, state, "relay_marker"

    proc_comms = _descendant_comms(root_pid)
    rc, state = _classify_stall_processes(proc_comms)
    return rc, state, "proc_fallback" if proc_comms is not None else "unavailable"


def main() -> int:
    args = parser().parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("missing relay command after --")
    if args.startup_timeout <= 0 or args.stall_timeout <= 0:
        raise SystemExit("timeouts must be greater than zero")

    log(
        f"START startup_timeout={args.startup_timeout:g}s; "
        f"stall_timeout={args.stall_timeout:g}s"
    )

    marker_path = _create_child_state_marker()
    child_env = os.environ.copy()
    if marker_path is not None:
        child_env[CHILD_STATE_MARKER_ENV] = str(marker_path)

    child = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=None,
        bufsize=0,
        start_new_session=True,
        env=child_env,
    )
    if child.stdout is None:
        raise RuntimeError("failed to capture relay stdout")

    stop_requested = False

    def handle_signal(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True
        terminate_child(child, args.terminate_grace)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    fd = child.stdout.fileno()
    os.set_blocking(fd, False)
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_READ)

    launched = time.monotonic()
    last_data = launched
    started = False
    total = 0

    try:
        while True:
            now = time.monotonic()
            limit = args.stall_timeout if started else args.startup_timeout
            elapsed = now - last_data
            wait_for = max(0.05, min(1.0, limit - elapsed))
            events = selector.select(wait_for)

            if events:
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    chunk = b""
                if chunk:
                    if not started:
                        started = True
                        log(f"media_started=true; first_chunk_bytes={len(chunk)}")
                    last_data = time.monotonic()
                    total += len(chunk)
                    try:
                        write_all(sys.stdout.buffer, chunk)
                    except BrokenPipeError:
                        log("consumer_closed=true; reason=EPIPE")
                        terminate_child(child, args.terminate_grace)
                        return 0
                    except OSError as exc:
                        if exc.errno == errno.EPIPE:
                            log("consumer_closed=true; reason=EPIPE")
                            terminate_child(child, args.terminate_grace)
                            return 0
                        raise
                    continue

                rc = child.poll()
                if rc is not None:
                    log(f"relay_exit_rc={rc}; forwarded_bytes={total}")
                    return rc if rc != 0 else 0

            rc = child.poll()
            if rc is not None:
                log(f"relay_exit_rc={rc}; forwarded_bytes={total}")
                return rc if rc != 0 else 0

            now = time.monotonic()
            elapsed = now - last_data
            if not started and elapsed >= args.startup_timeout:
                log(
                    f"startup_stall_detected=true; silence_seconds={elapsed:.1f}; "
                    f"diagnostic_exit_code={EXIT_STARTUP_STALL}; action=terminate_and_recreate"
                )
                terminate_child(child, args.terminate_grace)
                return EXIT_STARTUP_STALL
            if started and elapsed >= args.stall_timeout:
                diagnostic_rc, process_state, state_source = _media_stall_diagnostic(
                    child.pid,
                    marker_path,
                )
                log(
                    f"media_stall_detected=true; silence_seconds={elapsed:.1f}; "
                    f"forwarded_bytes={total}; stall_process_state={process_state}; "
                    f"stall_state_source={state_source}; "
                    f"diagnostic_exit_code={diagnostic_rc}; action=terminate_and_recreate"
                )
                terminate_child(child, args.terminate_grace)
                return diagnostic_rc
            if stop_requested:
                return 0
    finally:
        selector.close()
        if child.poll() is None:
            terminate_child(child, args.terminate_grace)
        if marker_path is not None:
            try:
                marker_path.unlink(missing_ok=True)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
