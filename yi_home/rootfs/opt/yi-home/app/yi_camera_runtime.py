#!/usr/bin/env python3
"""Reusable Phase 6 camera runtime material provider for the future Add-on.

This module bridges generic account discovery to the already-proven native
PPPP/TNP relay without selecting cameras by display name or raw model number.
The public interface consumes a secret-safe stable_id and returns one
secret-bearing CameraMaterial object plus secret-safe runtime metadata.

It intentionally does not start media processes itself. Process lifecycle,
capability probing and Add-on service orchestration are layered above this core.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from yi_camera_manager import CameraDevice, YiCameraManager
from yi_capability_cache import YiCapabilityCache
from yi_tnp_oracle import CameraMaterial


PROVEN_PROFILE = "tnp_v2_resolution_1_h264_aac"
SUPPORTED_PROFILES = frozenset({PROVEN_PROFILE})


@dataclass(frozen=True)
class RuntimeDescriptor:
    stable_id: str
    stream_id: str
    name: str
    raw_model: str
    normalized_model: str
    transport: str
    cloud_online_reported: bool
    encrypted: bool | None
    wakeup: bool
    profile_candidate: str
    profile_source: str

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


def _select_device(devices: list[CameraDevice], stable_id: str) -> CameraDevice:
    selected = next((device for device in devices if device.stable_id == stable_id), None)
    if selected is None:
        raise RuntimeError("Unknown camera stable_id")
    if selected.transport != "tnp" or selected.p2p_type != 2:
        raise RuntimeError("Selected camera is not a TNP transport device")
    if selected.has_pincode:
        raise RuntimeError("Selected camera requires PIN-gated credentials")
    if selected.credential_status != "decrypted":
        raise RuntimeError(
            f"Selected camera credentials are not runtime-ready: {selected.credential_status}"
        )
    return selected


def _profile_for(stable_id: str) -> tuple[str, str]:
    """Prefer a previously proven profile, otherwise return a safe probe candidate."""
    try:
        cached = YiCapabilityCache().get_success(stable_id)
    except RuntimeError:
        # Cache corruption must never make a previously working camera unusable.
        return PROVEN_PROFILE, "default_probe_candidate_cache_error"
    if cached is not None and cached.profile in SUPPORTED_PROFILES:
        return cached.profile, "capability_cache"
    return PROVEN_PROFILE, "default_probe_candidate"


def runtime_material_for(stable_id: str, *, timeout: float = 10.0) -> tuple[CameraMaterial, RuntimeDescriptor]:
    """Resolve one camera by stable_id and return fresh TNP runtime material.

    No model whitelist is consulted. A cached profile is trusted only when it
    was previously recorded after observed H264/AAC success and is implemented
    by this runtime. Otherwise the proven TNP-v2 profile is merely a probe
    candidate; actual support is established by observed media/control behavior.
    """

    manager = YiCameraManager(timeout=timeout)
    material: CameraMaterial | None = None
    try:
        material, descriptor = runtime_material_from_manager(manager, stable_id)
        return material, descriptor
    except Exception:
        if material is not None:
            material.clear()
        raise
    finally:
        manager.close()


def runtime_material_from_manager(
    manager: YiCameraManager,
    stable_id: str,
) -> tuple[CameraMaterial, RuntimeDescriptor]:
    """Resolve runtime material through an already authenticated manager."""
    devices = manager.discover(fetch_tnp=False)
    selected = _select_device(devices, stable_id)
    material = manager.material_for(stable_id)
    try:
        profile, profile_source = _profile_for(stable_id)
        descriptor = RuntimeDescriptor(
            stable_id=selected.stable_id,
            stream_id=selected.stream_id,
            name=selected.name,
            raw_model=selected.raw_model,
            normalized_model=selected.normalized_model,
            transport=selected.transport,
            cloud_online_reported=selected.cloud_online_reported,
            encrypted=selected.encrypted,
            wakeup=selected.wakeup,
            profile_candidate=profile,
            profile_source=profile_source,
        )
        return material, descriptor
    except Exception:
        material.clear()
        raise
