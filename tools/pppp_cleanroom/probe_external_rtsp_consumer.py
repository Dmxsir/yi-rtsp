#!/usr/bin/env python3
"""Standalone CR-4D RTSP consumer verifier for execution inside Frigate.

The helper never mutates Frigate/go2rtc/Home Assistant configuration. It only
uses the container's existing ffprobe binary to consume the temporary research
RTSP endpoint and emits sanitized codec/count/duration evidence.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import shutil
import subprocess
import time
from typing import Any


STREAM_NAME = "yi_cr4c_probe"
PRODUCTION_PORTS = frozenset((1984, 8554))


class CR4DConsumerError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def parse_private_ipv4(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("private IPv4 required") from exc
    if address.version != 4 or not address.is_private or address.is_loopback:
        raise argparse.ArgumentTypeError("private non-loopback IPv4 required")
    return str(address)


def validate_port(port: int) -> int:
    if not 1 <= port <= 65535 or port in PRODUCTION_PORTS:
        raise CR4DConsumerError("CR4D_SETUP")
    return port


def ffprobe_command(
    ffprobe: str,
    host: str,
    port: int,
    minimum_seconds: float,
    io_timeout: float,
) -> list[str]:
    validate_port(port)
    parse_private_ipv4(host)
    if minimum_seconds <= 0 or io_timeout <= 0:
        raise CR4DConsumerError("CR4D_SETUP")
    return [
        ffprobe,
        "-v",
        "error",
        "-rtsp_transport",
        "tcp",
        "-rw_timeout",
        str(int(io_timeout * 1_000_000)),
        "-read_intervals",
        f"%+{minimum_seconds:g}",
        "-count_packets",
        "-show_entries",
        "stream=codec_name,codec_type,width,height,sample_rate,channels,nb_read_packets",
        "-of",
        "json",
        f"rtsp://{host}:{port}/{STREAM_NAME}",
    ]


def parse_result(raw: bytes, active_seconds: float, minimum_seconds: float) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
        streams = payload["streams"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CR4DConsumerError("CR4D_RTSP_FORMAT_INVALID") from exc
    if not isinstance(streams, list):
        raise CR4DConsumerError("CR4D_RTSP_FORMAT_INVALID")
    video = next(
        (
            item
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == "video"
        ),
        None,
    )
    audio = next(
        (
            item
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == "audio"
        ),
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
        raise CR4DConsumerError("CR4D_RTSP_FORMAT_INVALID")
    try:
        video_packets = int(video.get("nb_read_packets", 0))
        audio_packets = int(audio.get("nb_read_packets", 0))
    except (TypeError, ValueError) as exc:
        raise CR4DConsumerError("CR4D_RTSP_STARVED") from exc
    if (
        video_packets <= 0
        or audio_packets <= 0
        or active_seconds < minimum_seconds
    ):
        raise CR4DConsumerError("CR4D_RTSP_STARVED")
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


def self_test() -> int:
    command = ffprobe_command("ffprobe", "172.30.32.200", 18554, 8.0, 5.0)
    synthetic = json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "nb_read_packets": "20",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "16000",
                    "channels": 1,
                    "nb_read_packets": "30",
                },
            ]
        }
    ).encode("utf-8")
    result = parse_result(synthetic, 8.5, 8.0)
    assert STREAM_NAME in command[-1]
    assert result["video_packets"] == 20 and result["audio_packets"] == 30
    print(
        "CR4D_EXTERNAL_CONSUMER_SELF_TEST=PASS; network_used=false; "
        "processes_started=false; configuration_changed=false"
    )
    return 0


def run(args: argparse.Namespace) -> int:
    if shutil.which(args.ffprobe) is None:
        raise CR4DConsumerError("CR4D_FFPROBE_MISSING")
    command = ffprobe_command(
        args.ffprobe, args.host, args.port, args.min_seconds, args.io_timeout
    )
    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            stdout, _stderr = process.communicate(
                timeout=args.min_seconds + args.io_timeout
            )
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait(timeout=args.io_timeout)
            raise CR4DConsumerError("CR4D_RTSP_TIMEOUT") from exc
        active = time.monotonic() - started
        if process.returncode != 0:
            raise CR4DConsumerError("CR4D_RTSP_CONNECT_FAILED")
        result = parse_result(stdout, active, args.min_seconds)
    except CR4DConsumerError:
        raise
    except OSError as exc:
        raise CR4DConsumerError("CR4D_RTSP_CONNECT_FAILED") from exc
    finally:
        if process is not None and process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=args.io_timeout)
            except (OSError, subprocess.TimeoutExpired):
                pass

    print("external_consumer_context=frigate_container", flush=True)
    print("external_rtsp_connected=true", flush=True)
    print(f"external_rtsp_video_codec={result['video_codec']}", flush=True)
    print(f"external_rtsp_video_size={result['video_size']}", flush=True)
    print(f"external_rtsp_audio_codec={result['audio_codec']}", flush=True)
    print(
        f"external_rtsp_audio_sample_rate={result['audio_sample_rate']}", flush=True
    )
    print(f"external_rtsp_audio_channels={result['audio_channels']}", flush=True)
    print(f"external_rtsp_video_packets={result['video_packets']}", flush=True)
    print(f"external_rtsp_audio_packets={result['audio_packets']}", flush=True)
    print(
        f"external_rtsp_active_seconds={result['active_seconds']:.3f}", flush=True
    )
    print("cr4d_frigate_consumer_result=PASS", flush=True)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CR-4D external RTSP verifier for the Frigate container"
    )
    parser.add_argument("--host", type=parse_private_ipv4)
    parser.add_argument("--port", type=int, default=18554)
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--min-seconds", type=float, default=8.0)
    parser.add_argument("--io-timeout", type=float, default=5.0)
    parser.add_argument("--self-test", action="store_true")
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.host:
        parser.error("--host is required")
    try:
        return run(args)
    except CR4DConsumerError as exc:
        print(f"cr4d_frigate_consumer_result=FAIL; failure_category={exc.category}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
