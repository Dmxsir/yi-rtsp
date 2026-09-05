#!/usr/bin/env python3
"""Persistent, secret-safe capability/profile cache for YI camera runtimes.

The future Home Assistant Add-on uses this cache to remember a profile only
after live media observation proves that profile works for a camera stable_id.
The cache contains no YI UID, DID, credentials, tokens, licenses or InitString.

On a development host the default path follows XDG state conventions. The
Add-on can point YI_CAPABILITY_CACHE at a persistent /data path without changing
this module.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
STABLE_ID_RE = re.compile(r"^[0-9a-f]{20}$")
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,95}$")


def default_cache_path() -> Path:
    override = os.getenv("YI_CAPABILITY_CACHE")
    if override:
        return Path(override).expanduser()
    state_home = os.getenv("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "yi-cam-integration" / "capabilities.json"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_stable_id(stable_id: str) -> str:
    value = stable_id.strip().casefold()
    if not STABLE_ID_RE.fullmatch(value):
        raise ValueError("stable_id must be exactly 20 lowercase hexadecimal characters")
    return value


def _validate_profile(profile: str) -> str:
    value = profile.strip().casefold()
    if not PROFILE_RE.fullmatch(value):
        raise ValueError("invalid capability profile identifier")
    return value


@dataclass(frozen=True)
class CapabilityRecord:
    stable_id: str
    profile: str
    status: str
    observed_at: str
    source: str
    video_codec: str
    video_width: int
    video_height: int
    audio_codec: str
    audio_sample_rate: int
    audio_channels: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CapabilityRecord":
        return cls(
            stable_id=_validate_stable_id(str(value.get("stable_id", ""))),
            profile=_validate_profile(str(value.get("profile", ""))),
            status=str(value.get("status", "")),
            observed_at=str(value.get("observed_at", "")),
            source=str(value.get("source", "")),
            video_codec=str(value.get("video_codec", "")),
            video_width=int(value.get("video_width", 0) or 0),
            video_height=int(value.get("video_height", 0) or 0),
            audio_codec=str(value.get("audio_codec", "")),
            audio_sample_rate=int(value.get("audio_sample_rate", 0) or 0),
            audio_channels=int(value.get("audio_channels", 0) or 0),
        )

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


class YiCapabilityCache:
    """Atomic persistent mapping from camera stable_id to proven capability."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_cache_path()

    def _empty(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "cameras": {}}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"capability cache is unreadable: {self.path}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("unsupported capability cache schema")
        cameras = value.get("cameras")
        if not isinstance(cameras, dict):
            raise RuntimeError("capability cache cameras mapping is invalid")
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".capabilities-", suffix=".tmp", dir=self.path.parent)
        temp_path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
            try:
                dir_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def get(self, stable_id: str) -> CapabilityRecord | None:
        key = _validate_stable_id(stable_id)
        cameras = self._load()["cameras"]
        value = cameras.get(key)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RuntimeError("invalid capability cache camera record")
        record = CapabilityRecord.from_mapping(value)
        if record.stable_id != key:
            raise RuntimeError("capability cache stable_id mismatch")
        return record

    def get_success(self, stable_id: str) -> CapabilityRecord | None:
        record = self.get(stable_id)
        return record if record is not None and record.status == "success" else None

    def record_success(
        self,
        *,
        stable_id: str,
        profile: str,
        video_codec: str,
        video_width: int,
        video_height: int,
        audio_codec: str,
        audio_sample_rate: int,
        audio_channels: int,
        source: str = "live_probe",
    ) -> CapabilityRecord:
        key = _validate_stable_id(stable_id)
        selected_profile = _validate_profile(profile)
        if video_codec != "h264" or video_width <= 0 or video_height <= 0:
            raise ValueError("successful profile requires observed H264 video dimensions")
        if audio_codec != "aac" or audio_sample_rate <= 0 or audio_channels <= 0:
            raise ValueError("successful profile requires observed AAC audio parameters")
        record = CapabilityRecord(
            stable_id=key,
            profile=selected_profile,
            status="success",
            observed_at=_utc_now(),
            source=source,
            video_codec=video_codec,
            video_width=int(video_width),
            video_height=int(video_height),
            audio_codec=audio_codec,
            audio_sample_rate=int(audio_sample_rate),
            audio_channels=int(audio_channels),
        )
        payload = self._load()
        cameras = dict(payload["cameras"])
        cameras[key] = record.safe_dict()
        payload = {"schema_version": SCHEMA_VERSION, "cameras": cameras}
        self._write(payload)
        return record


def _media_from_ffprobe(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("ffprobe JSON is unreadable") from exc
    if not isinstance(value, dict) or not isinstance(value.get("streams"), list):
        raise RuntimeError("ffprobe JSON does not contain streams")
    streams = [item for item in value["streams"] if isinstance(item, dict)]
    video = next(
        (
            item
            for item in streams
            if item.get("codec_type") == "video"
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
            if item.get("codec_type") == "audio"
            and item.get("codec_name") == "aac"
            and int(item.get("sample_rate", 0) or 0) > 0
            and int(item.get("channels", 0) or 0) > 0
        ),
        None,
    )
    if video is None or audio is None:
        raise RuntimeError("ffprobe did not prove both H264 video and AAC audio")
    return video, audio


def main() -> int:
    parser = argparse.ArgumentParser(description="Secret-safe YI capability/profile cache")
    parser.add_argument("--cache", type=Path, help="override cache path")
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show")
    show.add_argument("--stable-id", required=True)

    record = sub.add_parser("record-ffprobe")
    record.add_argument("--stable-id", required=True)
    record.add_argument("--profile", required=True)
    record.add_argument("--ffprobe-json", type=Path, required=True)
    record.add_argument("--source", default="live_probe")

    args = parser.parse_args()
    cache = YiCapabilityCache(args.cache)

    if args.command == "show":
        item = cache.get(args.stable_id)
        print(
            json.dumps(
                {
                    "ok": True,
                    "cache_path": str(cache.path),
                    "found": item is not None,
                    "record": item.safe_dict() if item is not None else None,
                    "secrets_exposed": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    video, audio = _media_from_ffprobe(args.ffprobe_json)
    item = cache.record_success(
        stable_id=args.stable_id,
        profile=args.profile,
        video_codec="h264",
        video_width=int(video["width"]),
        video_height=int(video["height"]),
        audio_codec="aac",
        audio_sample_rate=int(audio["sample_rate"]),
        audio_channels=int(audio["channels"]),
        source=args.source,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "cache_path": str(cache.path),
                "record": item.safe_dict(),
                "secrets_exposed": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
