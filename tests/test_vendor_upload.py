from __future__ import annotations

import io
import json
import struct
import sys
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "yi_home" / "rootfs" / "opt" / "yi-home" / "app"
sys.path.insert(0, str(APP_DIR))

from yi_vendor_bootstrap import COMPATIBILITY_GUEST_DIRS, LIBRARY_NAME  # noqa: E402
from yi_vendor_upload import Handler, UploadServer  # noqa: E402


def fake_arm64_library() -> bytes:
    raw = bytearray(4096)
    raw[:4] = b"\x7fELF"
    raw[4] = 2  # ELFCLASS64
    raw[5] = 1  # little endian
    raw[6] = 1  # ELF version
    struct.pack_into("<HHI", raw, 16, 3, 183, 1)  # ET_DYN, AArch64, EV_CURRENT
    struct.pack_into("<H", raw, 52, 64)  # ELF header size
    return bytes(raw)


def fake_apk() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"lib/arm64-v8a/{LIBRARY_NAME}", fake_arm64_library())
    return output.getvalue()


class VendorUploadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.data_dir = root / "data"
        self.runtime_root = root / "runtime"
        for guest_dir in COMPATIBILITY_GUEST_DIRS:
            (self.runtime_root / guest_dir).mkdir(parents=True, exist_ok=True)

        self.server = UploadServer(
            ("127.0.0.1", 0),
            Handler,
            data_dir=self.data_dir,
            runtime_root=self.runtime_root,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}/"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def get_json(self, path: str) -> dict[str, object]:
        with urllib.request.urlopen(self.base_url + path, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_upload_imports_vendor_and_deletes_apk(self) -> None:
        self.assertEqual(self.get_json("status"), {"installed": False})

        request = urllib.request.Request(
            self.base_url + "upload",
            data=fake_apk(),
            headers={"Content-Type": "application/vnd.android.package-archive"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["installed"])
        target = self.data_dir / "vendor" / LIBRARY_NAME
        self.assertTrue(target.is_file())
        self.assertEqual(target.stat().st_size, 4096)

        upload_dir = self.data_dir / "vendor-upload"
        self.assertEqual(list(upload_dir.iterdir()), [])
        for guest_dir in COMPATIBILITY_GUEST_DIRS:
            link = self.runtime_root / guest_dir / LIBRARY_NAME
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), target.resolve())

        status = self.get_json("status")
        self.assertTrue(status["installed"])
        self.assertEqual(status["size"], 4096)


if __name__ == "__main__":
    unittest.main()
