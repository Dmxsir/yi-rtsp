#!/usr/bin/env python3
"""CR-4D source-side probe for temporary Frigate-container RTSP parity.

This reuses the already-proven CR-4C clean PPPP/TNP/media/mux pipeline, but
changes only the temporary research RTSP listener from loopback to the
container network. The go2rtc API remains loopback-only, production ports are
still rejected, and no Frigate/Home Assistant configuration is modified.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

try:
    from . import probe_rtsp_publish as cr4c
    from . import rtsp_publish as publish
except ImportError:  # Direct execution from a relocated clean-room directory.
    import probe_rtsp_publish as cr4c
    import rtsp_publish as publish


EXTERNAL_RTSP_BIND = "0.0.0.0"
TEMP_ROOT = Path("/tmp/yi-cr4d")


def render_external_go2rtc_config(api_port: int, rtsp_port: int) -> str:
    """Render a research config with loopback API and container-network RTSP."""
    publish.validate_research_ports(api_port, rtsp_port)
    return "\n".join(
        (
            "api:",
            f'  listen: "{publish.LOOPBACK}:{api_port}"',
            "rtsp:",
            f'  listen: "{EXTERNAL_RTSP_BIND}:{rtsp_port}"',
            '  default_query: "video&audio"',
            "webrtc:",
            '  listen: ""',
            "log:",
            '  format: "text"',
            '  level: "info"',
            '  output: "stdout"',
            "streams:",
            f"  {publish.STREAM_NAME}:",
            "",
        )
    )


class ExternalTemporaryGo2RTC(publish.TemporaryGo2RTC):
    """Temporary go2rtc with API on loopback and RTSP on container network."""

    def _preflight_ports(self) -> None:
        held: list[socket.socket] = []
        try:
            for host, port in (
                (publish.LOOPBACK, self.api_port),
                (EXTERNAL_RTSP_BIND, self.rtsp_port),
            ):
                candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                held.append(candidate)
                candidate.bind((host, port))
        except OSError as exc:
            raise publish.CR4CError("CR4C_PORT_IN_USE") from exc
        finally:
            for candidate in held:
                candidate.close()

    def start(self) -> None:
        executable = shutil.which(self.binary)
        if executable is None:
            raise publish.CR4CError("GO2RTC_START_FAILED")
        self._preflight_ports()
        try:
            if self.temp_root.is_symlink():
                raise publish.CR4CError("CR4C_SETUP")
            self.temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.temp_root.is_symlink() or not self.temp_root.is_dir():
                raise publish.CR4CError("CR4C_SETUP")
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
                render_external_go2rtc_config(self.api_port, self.rtsp_port),
                encoding="utf-8",
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
                    raise publish.CR4CError("GO2RTC_EARLY_EXIT")
                if self._registry_is_exact():
                    self.ready = True
                    return
                self.sleeper(min(0.1, max(0.0, deadline - self.clock())))
            raise publish.CR4CError("GO2RTC_START_TIMEOUT")
        except publish.CR4CError:
            self.stop()
            raise
        except Exception as exc:
            self.stop()
            raise publish.CR4CError("GO2RTC_START_FAILED") from exc


class CR4DController(ExternalTemporaryGo2RTC):
    """Adapter that forces CR-4D temporary state under /tmp/yi-cr4d."""

    def __init__(self, *controller_args: object, **controller_kwargs: object) -> None:
        if len(controller_args) >= 7:
            controller_args = (*controller_args[:6], TEMP_ROOT, *controller_args[7:])
        else:
            controller_kwargs["temp_root"] = TEMP_ROOT
        super().__init__(*controller_args, **controller_kwargs)


def self_test() -> int:
    config = render_external_go2rtc_config(
        publish.DEFAULT_API_PORT, publish.DEFAULT_RTSP_PORT
    )
    assert f'listen: "{publish.LOOPBACK}:{publish.DEFAULT_API_PORT}"' in config
    assert f'listen: "{EXTERNAL_RTSP_BIND}:{publish.DEFAULT_RTSP_PORT}"' in config
    assert config.count(publish.STREAM_NAME) == 1
    assert 'listen: "127.0.0.1:1984"' not in config
    assert 'listen: "0.0.0.0:1984"' not in config
    assert 'listen: "127.0.0.1:8554"' not in config
    assert 'listen: "0.0.0.0:8554"' not in config
    print(
        "CR4D_SELF_TEST=PASS; network_used=false; processes_started=false; "
        "device_traffic=false; production_config_changed=false; "
        "api_scope=loopback; rtsp_scope=container_network"
    )
    return 0


def main() -> int:
    parser = cr4c._parser()
    parser.description = (
        "CR-4D temporary container-network RTSP source probe for Frigate parity"
    )
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if args.support_smoke_test:
        return cr4c.support_smoke_test(args.app_source)
    if args.rtsp_support_smoke_test:
        return cr4c.rtsp_support_smoke_test(
            args.ffmpeg, args.ffprobe, args.go2rtc, args.app_source
        )
    if not args.servers:
        parser.error("at least one --server is required")
    if not args.camera_id:
        parser.error("--camera-id is required for a live probe")

    original_controller = cr4c.TemporaryGo2RTC
    try:
        cr4c.TemporaryGo2RTC = CR4DController
        print("cr4d_external_rtsp_enabled=true", flush=True)
        print("cr4d_api_scope=loopback", flush=True)
        print("cr4d_rtsp_scope=container_network", flush=True)
        result = cr4c._run_live(args)
    finally:
        cr4c.TemporaryGo2RTC = original_controller

    print(
        f"cr4d_source_publication_result={'PASS' if result == 0 else 'FAIL'}",
        flush=True,
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
