#!/usr/bin/env python3
"""Persistent secret-safe camera runtime intent for the YI Home App.

Only immutable stable_id values whose desired state is running are persisted.
No YI UID/DID, credentials, tokens, camera passwords or transport material are
stored here. The Home Assistant App will place this file under /data.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
STABLE_ID_RE = re.compile(r"^[0-9a-f]{20}$")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_stable_id(stable_id: str) -> str:
    value = stable_id.strip().casefold()
    if not STABLE_ID_RE.fullmatch(value):
        raise ValueError("stable_id must be exactly 20 lowercase hexadecimal characters")
    return value


def default_runtime_policy_path() -> Path:
    override = os.getenv("YI_RUNTIME_POLICY")
    if override:
        return Path(override).expanduser()
    state_home = os.getenv("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "yi-cam-integration" / "runtime-policy.json"


class YiRuntimePolicyStore:
    """Atomic persistent set of stable_ids that should be running."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_runtime_policy_path()
        self._lock = threading.RLock()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": None,
            "desired_running": [],
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("runtime policy is unreadable") from exc
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("unsupported runtime policy schema")
        desired = value.get("desired_running")
        if not isinstance(desired, list):
            raise RuntimeError("runtime policy desired_running is invalid")
        normalized = sorted({_validate_stable_id(str(item)) for item in desired})
        updated_at = value.get("updated_at")
        if updated_at is not None and not isinstance(updated_at, str):
            raise RuntimeError("runtime policy updated_at is invalid")
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": updated_at,
            "desired_running": normalized,
        }

    def _write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        fd, temporary = tempfile.mkstemp(prefix=".runtime-policy-", suffix=".tmp", dir=self.path.parent)
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

    def desired_running_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._load()["desired_running"])

    def desired_running(self, stable_id: str) -> bool:
        key = _validate_stable_id(stable_id)
        return key in set(self.desired_running_ids())

    def set_desired_running(self, stable_id: str, desired: bool) -> dict[str, Any]:
        key = _validate_stable_id(stable_id)
        with self._lock:
            payload = self._load()
            values = set(payload["desired_running"])
            if desired:
                values.add(key)
            else:
                values.discard(key)
            result = {
                "schema_version": SCHEMA_VERSION,
                "updated_at": _utc_now(),
                "desired_running": sorted(values),
            }
            self._write(result)
            return self.safe_status_from(result)

    def safe_status_from(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        desired = payload.get("desired_running", [])
        return {
            "enabled": True,
            "schema_version": SCHEMA_VERSION,
            "desired_running_count": len(desired) if isinstance(desired, list) else 0,
            "updated_at": payload.get("updated_at"),
            "secrets_exposed": False,
        }

    def safe_status(self) -> dict[str, Any]:
        with self._lock:
            return self.safe_status_from(self._load())
