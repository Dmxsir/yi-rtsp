from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLEANROOM = ROOT / "tools" / "pppp_cleanroom"
sys.path.insert(0, str(CLEANROOM))

import probe_cr4d_frigate as cr4d  # noqa: E402
import probe_external_rtsp_consumer as consumer  # noqa: E402
import rtsp_publish as publish  # noqa: E402


class CR4DSourceIsolationTest(unittest.TestCase):
    def test_external_config_exposes_only_rtsp(self) -> None:
        config = cr4d.render_external_go2rtc_config(11984, 18554)
        self.assertIn('listen: "127.0.0.1:11984"', config)
        self.assertIn('listen: "0.0.0.0:18554"', config)
        self.assertNotIn('listen: "0.0.0.0:11984"', config)
        self.assertEqual(config.count(publish.STREAM_NAME), 1)
        for forbidden in ("stable_id", "did", "uid", "password", "license"):
            self.assertNotIn(forbidden, config.casefold())

    def test_production_ports_still_rejected(self) -> None:
        for api, rtsp in ((1984, 18554), (11984, 8554), (11984, 11984)):
            with self.subTest(api=api, rtsp=rtsp):
                with self.assertRaises(publish.CR4CError):
                    cr4d.render_external_go2rtc_config(api, rtsp)

    def test_preflight_binds_api_loopback_and_rtsp_container_network(self) -> None:
        first = mock.Mock()
        second = mock.Mock()
        controller = cr4d.ExternalTemporaryGo2RTC("go2rtc")
        with mock.patch.object(
            cr4d.socket, "socket", side_effect=(first, second)
        ):
            controller._preflight_ports()
        first.bind.assert_called_once_with(("127.0.0.1", 11984))
        second.bind.assert_called_once_with(("0.0.0.0", 18554))
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_cr4c_default_remains_loopback_only(self) -> None:
        config = publish.render_go2rtc_config(11984, 18554)
        self.assertIn('listen: "127.0.0.1:11984"', config)
        self.assertIn('listen: "127.0.0.1:18554"', config)
        self.assertNotIn("0.0.0.0", config)

    def test_self_test_is_process_and_network_safe(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(cr4d.subprocess, "Popen") as popen,
            mock.patch.object(cr4d.socket, "socket") as socket_factory,
            redirect_stdout(output),
        ):
            result = cr4d.self_test()
        self.assertEqual(result, 0)
        self.assertIn("CR4D_SELF_TEST=PASS", output.getvalue())
        popen.assert_not_called()
        socket_factory.assert_not_called()


class ExternalConsumerTest(unittest.TestCase):
    @staticmethod
    def _metadata(**changes: object) -> bytes:
        video = {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "nb_read_packets": "20",
        }
        audio = {
            "codec_type": "audio",
            "codec_name": "aac",
            "sample_rate": "16000",
            "channels": 1,
            "nb_read_packets": "30",
        }
        for key, value in changes.items():
            target, field = key.split("_", 1)
            (video if target == "video" else audio)[field] = value
        return json.dumps({"streams": [video, audio]}).encode("utf-8")

    def test_command_is_fixed_stream_private_ipv4_and_nonproduction_port(self) -> None:
        command = consumer.ffprobe_command(
            "ffprobe", "172.30.32.200", 18554, 8.0, 5.0
        )
        self.assertEqual(command[0], "ffprobe")
        self.assertEqual(
            command[-1], "rtsp://172.30.32.200:18554/yi_cr4c_probe"
        )
        with self.assertRaises(Exception):
            consumer.ffprobe_command("ffprobe", "8.8.8.8", 18554, 8.0, 5.0)
        with self.assertRaises(consumer.CR4DConsumerError):
            consumer.ffprobe_command(
                "ffprobe", "172.30.32.200", 8554, 8.0, 5.0
            )

    def test_valid_h264_aac_result_passes(self) -> None:
        result = consumer.parse_result(self._metadata(), 8.5, 8.0)
        self.assertEqual(result["video_codec"], "h264")
        self.assertEqual(result["audio_codec"], "aac")
        self.assertEqual(result["video_packets"], 20)
        self.assertEqual(result["audio_packets"], 30)

    def test_wrong_format_zero_packets_and_short_span_fail(self) -> None:
        cases = (
            (self._metadata(video_codec_name="hevc"), 8.5, "CR4D_RTSP_FORMAT_INVALID"),
            (self._metadata(video_nb_read_packets="0"), 8.5, "CR4D_RTSP_STARVED"),
            (self._metadata(), 2.0, "CR4D_RTSP_STARVED"),
        )
        for raw, active, category in cases:
            with self.subTest(category=category):
                with self.assertRaises(consumer.CR4DConsumerError) as raised:
                    consumer.parse_result(raw, active, 8.0)
                self.assertEqual(raised.exception.category, category)

    def test_consumer_self_test_starts_no_process(self) -> None:
        output = io.StringIO()
        with mock.patch.object(consumer.subprocess, "Popen") as popen, redirect_stdout(output):
            result = consumer.self_test()
        self.assertEqual(result, 0)
        self.assertIn("CR4D_EXTERNAL_CONSUMER_SELF_TEST=PASS", output.getvalue())
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
