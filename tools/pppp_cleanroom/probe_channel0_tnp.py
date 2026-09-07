#!/usr/bin/env python3
"""CR-3 manual probe: clean reliable PPPP channel 0 carrying Phase 3E TNP."""
from __future__ import annotations

import argparse
import importlib.util
import struct
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve()
try:
    from .probe_legacy_punch import load_material, parse_camera_id, parse_server
    from .yi_pppp import DeviceId
    from .yi_pppp_session import CleanPpppSession, ExperimentalPolicy, TransportError
except ImportError:  # Direct execution from a relocated clean-room directory.
    from probe_legacy_punch import (  # type: ignore[no-redef]
        load_material,
        parse_camera_id,
        parse_server,
    )
    from yi_pppp import DeviceId  # type: ignore[no-redef]
    from yi_pppp_session import (  # type: ignore[no-redef]
        CleanPpppSession,
        ExperimentalPolicy,
        TransportError,
    )


def _app_source_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    for parent in HERE.parents:
        candidate = parent / "yi_home" / "rootfs" / "opt" / "yi-home" / "app"
        if candidate.is_dir():
            candidates.append(candidate)
    runtime = Path("/opt/yi-home/app")
    if runtime.is_dir():
        candidates.append(runtime)
    return tuple(dict.fromkeys(candidates))


def _phase3e_support() -> ModuleType:
    """Load the authoritative Phase 3E builders/parser only for a live run."""
    relative = Path("tools/phase3_pppp_probe/run_phase3e_tnp.py")
    app_dirs = _app_source_candidates()
    for app_dir in app_dirs:
        if str(app_dir) not in sys.path:
            sys.path.insert(0, str(app_dir))
    module_paths = (HERE.with_name("run_phase3e_tnp.py"),) + tuple(
        app_dir / relative for app_dir in app_dirs
    )
    for module_path in dict.fromkeys(module_paths):
        if not module_path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("_yi_phase3e_tnp", module_path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if hasattr(module, "_build_units") and hasattr(module, "validate_first_4882"):
            return module
    raise RuntimeError("Phase 3E TNP helpers not found")


def _read_exact(session: CleanPpppSession, size: int, timeout: float) -> bytes:
    output = bytearray()
    deadline = time.monotonic() + timeout
    while len(output) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransportError("CHANNEL0_READ_TIMEOUT")
        output.extend(session.read_channel(0, size - len(output), remaining))
    return bytes(output)


def self_test() -> int:
    """Prove the command can start without cloud, sockets, TNP, or media I/O."""
    forbidden = ("yi_camera_manager", "yi_tnp_oracle", "yi_cloud_probe", "_yi_phase3e_tnp")
    assert not any(name in sys.modules for name in forbidden)
    assert parse_camera_id("0123456789ABCDEFabcd") == "0123456789abcdefabcd"
    policy = ExperimentalPolicy()
    assert policy.max_payload >= 164 and policy.max_attempts >= 1
    print(
        "CR3_SELF_TEST=PASS; cloud_used=false; network_used=false; tnp_sent=false; "
        "media_requested=false; runtime_support_imported=false"
    )
    return 0


def _run_live(args: argparse.Namespace) -> int:
    material: Any | None = None
    session: CleanPpppSession | None = None
    stop_unit: bytes | None = None
    startup_started = False
    startup_acked = False
    first_valid = False
    stop_attempted = False
    stop_sent = False
    failure: str | None = None
    try:
        material, report = load_material(args.env_file, args.cloud_timeout, args.camera_id)
        phase3e = _phase3e_support()
        units = phase3e._build_units(material)
        if tuple(map(len, units)) != (56, 52, 56, 56):
            raise RuntimeError("unexpected Phase 3E unit shape")
        stop_unit = units[3]
        session = CleanPpppSession(
            tuple(args.servers),
            ExperimentalPolicy(
                handshake_timeout=args.handshake_timeout,
                keepalive_timeout=args.keepalive_timeout,
                keepalive_interval=args.keepalive_interval,
                request_retry_after=args.request_retry_after,
                retry_after=args.retry_after,
                max_attempts=args.max_attempts,
                punch_repeat=args.punch_repeat,
                max_payload=args.max_drw_payload,
                receive_window=args.receive_window,
            ),
        )
        session.connect(DeviceId.from_text(material.pppp_did))
        print(
            "cr2_transport_established=true; "
            f"selected_path={session.selected_path}; target_selection=stable_id; "
            "secrets_exposed=false",
            flush=True,
        )

        for unit in units[:3]:
            session.write_channel(0, unit)
        startup_started = True
        packet_count = session.flush_channel(0)
        startup_bytes = sum(len(unit) for unit in units[:3])
        print(
            f"channel0_drw_started=true; startup_tnp_bytes_sent={startup_bytes}; "
            f"startup_drw_packets={packet_count}",
            flush=True,
        )
        session.wait_channel_acked(0, args.ack_timeout)
        startup_acked = True
        print("startup_drw_acked=true", flush=True)

        header = _read_exact(session, 8, args.read_timeout)
        body_size = struct.unpack(">I", header[4:8])[0] if len(header) == 8 else 0
        if not 40 <= body_size <= 4096 - 8:
            raise TransportError("TNP_RESPONSE_INVALID")
        body = _read_exact(session, body_size, args.read_timeout)
        if not phase3e.validate_first_4882(header, body):
            raise TransportError("TNP_RESPONSE_INVALID")
        first_valid = True
        print("first_4882_valid=true", flush=True)

        stop_attempted = True
        session.write_channel(0, stop_unit)
        stop_sent = session.flush_channel(0) > 0
        print(f"stop_live_767_sent={str(stop_sent).lower()}", flush=True)
    except TransportError as exc:
        failure = exc.category
    except Exception:
        failure = "CR3_SETUP"
    finally:
        if startup_started and not stop_attempted and session is not None:
            stop_attempted = True
            try:
                if stop_unit is not None:
                    session.write_channel(0, stop_unit)
                    stop_sent = session.flush_channel(0) > 0
            except Exception:
                stop_sent = False
            print(f"stop_live_767_sent={str(stop_sent).lower()}", flush=True)
        if session is not None:
            session.close()
            print("transport_closed=true", flush=True)
        if material is not None:
            material.clear()

    passed = bool(startup_acked and first_valid and stop_sent and session and session.closed)
    if passed:
        print("cr3_result=PASS", flush=True)
        return 0
    print(f"cr3_result=FAIL; failure_category={failure or 'STOP_LIVE_FAILED'}", flush=True)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CR-3 manual clean PPPP channel-0 TNP probe for an owned camera"
    )
    parser.add_argument("--server", dest="servers", action="append", type=parse_server)
    parser.add_argument("--camera-id", type=parse_camera_id)
    parser.add_argument("--env-file", type=Path, default=Path("/data/yi.env"))
    parser.add_argument("--cloud-timeout", type=float, default=10.0)
    parser.add_argument("--handshake-timeout", type=float, default=4.0)
    parser.add_argument("--keepalive-timeout", type=float, default=0.8)
    parser.add_argument("--keepalive-interval", type=float, default=0.5)
    parser.add_argument("--request-retry-after", type=float, default=0.7)
    parser.add_argument("--retry-after", type=float, default=0.25)
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--punch-repeat", type=int, default=3)
    parser.add_argument("--max-drw-payload", type=int, default=1024)
    parser.add_argument("--receive-window", type=int, default=4096)
    parser.add_argument("--ack-timeout", type=float, default=2.0)
    parser.add_argument("--read-timeout", type=float, default=4.0)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.servers:
        parser.error("at least one --server is required")
    if not args.camera_id:
        parser.error("--camera-id is required for a live probe")
    return _run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
