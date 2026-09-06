#!/usr/bin/env python3
"""Ingress-only UI for importing the user-supplied official YI Home APK."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from yi_vendor_bootstrap import (
    LIBRARY_NAME,
    VendorRuntimeError,
    import_vendor_apk,
    install_compatibility_links,
    validate_vendor_library,
)

MAX_APK_SIZE = 512 * 1024 * 1024
ALLOWED_CLIENTS = {"127.0.0.1", "::1", "172.30.32.2"}

_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YI RTSP</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
body{margin:0;background:#f6f7f9;color:#1f2937}main{max-width:760px;margin:0 auto;padding:32px 18px 48px}
.card{background:white;border-radius:18px;padding:24px;box-shadow:0 8px 30px rgba(0,0,0,.08)}
h1{margin:0 0 8px;font-size:30px}.muted{color:#6b7280}.status{margin:20px 0;padding:14px 16px;border-radius:12px;background:#eef2ff}
.status.ok{background:#ecfdf5;color:#065f46}.status.warn{background:#fff7ed;color:#9a3412}
.drop{margin-top:18px;border:2px dashed #94a3b8;border-radius:14px;padding:24px;text-align:center}
input[type=file]{max-width:100%}button{margin-top:16px;border:0;border-radius:10px;padding:11px 18px;font-weight:700;cursor:pointer;background:#2563eb;color:white}
button:disabled{opacity:.45;cursor:not-allowed}.small{font-size:13px;line-height:1.5}.progress{margin-top:14px}code{background:#f1f5f9;padding:2px 6px;border-radius:6px}
@media(prefers-color-scheme:dark){body{background:#111827;color:#e5e7eb}.card{background:#1f2937}.muted{color:#9ca3af}.status{background:#312e81}.status.ok{background:#064e3b;color:#d1fae5}.status.warn{background:#7c2d12;color:#ffedd5}code{background:#374151}}
</style>
</head>
<body>
<main><div class="card">
<h1>YI RTSP</h1><p class="muted">Vendor runtime setup</p>
<div id="status" class="status">Checking runtime…</div>
<div class="drop">
<p><strong>Upload the official YI Home APK</strong></p>
<input id="apk" type="file" accept=".apk,application/vnd.android.package-archive"><br>
<button id="upload" disabled>Upload APK</button><div id="progress" class="progress muted"></div>
</div>
<p class="small muted">The APK is processed locally inside this Home Assistant App. Only the required ARM64 <code>libPPPP_API.so</code> is copied to the App's private <code>/data</code> storage. The uploaded APK itself is deleted after processing.</p>
</div></main>
<script>
const statusEl=document.getElementById("status"),fileEl=document.getElementById("apk"),button=document.getElementById("upload"),progress=document.getElementById("progress");
fileEl.addEventListener("change",()=>{button.disabled=!fileEl.files.length;});
async function refresh(){try{const r=await fetch("status",{cache:"no-store"}),s=await r.json();if(s.installed){statusEl.className="status ok";statusEl.textContent=`Vendor runtime installed (${Math.round(s.size/1024)} KiB).`;}else{statusEl.className="status warn";statusEl.textContent="Vendor runtime not installed. Upload the official YI Home APK.";}}catch(e){statusEl.className="status warn";statusEl.textContent="Could not read runtime status.";}}
button.addEventListener("click",async()=>{const file=fileEl.files[0];if(!file)return;if(!file.name.toLowerCase().endsWith(".apk")){progress.textContent="Please select an APK file.";return;}button.disabled=true;progress.textContent="Uploading and validating…";try{const r=await fetch("upload",{method:"POST",headers:{"Content-Type":"application/vnd.android.package-archive"},body:file}),data=await r.json();if(!r.ok||!data.ok)throw new Error(data.error||`HTTP ${r.status}`);progress.textContent="Runtime installed. The App will continue startup automatically.";fileEl.value="";await refresh();}catch(e){progress.textContent=`Upload failed: ${e.message}`;}finally{button.disabled=!fileEl.files.length;}});
refresh();setInterval(refresh,5000);
</script>
</body></html>
"""


class UploadServer(ThreadingHTTPServer):
    """HTTP server carrying App-private runtime paths."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        data_dir: Path,
        runtime_root: Path,
    ) -> None:
        super().__init__(address, handler)
        self.data_dir = data_dir
        self.runtime_root = runtime_root


class Handler(BaseHTTPRequestHandler):
    """Serve the small ingress UI and stream APK uploads to private storage."""

    server: UploadServer

    def _deny_if_needed(self) -> bool:
        if self.client_address[0] in ALLOWED_CLIENTS:
            return False
        self.send_error(HTTPStatus.FORBIDDEN)
        return True

    def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _runtime_status(self) -> dict[str, object]:
        target = self.server.data_dir / "vendor" / LIBRARY_NAME
        if not target.is_file() or target.is_symlink():
            return {"installed": False}
        try:
            size, digest = validate_vendor_library(target)
        except VendorRuntimeError:
            return {"installed": False}
        return {"installed": True, "size": size, "sha256": digest}

    def do_GET(self) -> None:
        if self._deny_if_needed():
            return
        path = urlsplit(self.path).path.rstrip("/")
        if path.endswith("/status") or path == "status":
            self._json(HTTPStatus.OK, self._runtime_status())
            return
        raw = _PAGE.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        if self._deny_if_needed():
            return
        path = urlsplit(self.path).path.rstrip("/")
        if not (path.endswith("/upload") or path == "upload"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            self._json(HTTPStatus.LENGTH_REQUIRED, {"ok": False, "error": "APK upload is empty."})
            return
        if length > MAX_APK_SIZE:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "APK is too large."})
            return

        upload_dir = self.server.data_dir / "vendor-upload"
        upload_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(upload_dir, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".yi-home.", suffix=".apk", dir=upload_dir)
        temporary = Path(temporary_name)
        try:
            remaining = length
            with os.fdopen(descriptor, "wb") as output:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise VendorRuntimeError("APK upload ended before all bytes were received")
                    output.write(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)

            installed = import_vendor_apk(temporary, self.server.data_dir)
            install_compatibility_links(installed.path, self.server.runtime_root)
            self._json(
                HTTPStatus.OK,
                {"ok": True, "installed": True, "size": installed.size, "sha256": installed.sha256},
            )
            print(
                f"vendor_upload=installed; size={installed.size}; sha256={installed.sha256}; "
                "apk_persisted=false; proprietary_bytes_exposed=false",
                flush=True,
            )
        except VendorRuntimeError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except (OSError, RuntimeError) as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "Could not process the APK."})
            print(f"vendor_upload=error; type={type(exc).__name__}", flush=True)
        finally:
            temporary.unlink(missing_ok=True)

    def log_message(self, format: str, *args: object) -> None:
        print(f"vendor_ui client={self.client_address[0]} method={self.command}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve YI APK import UI through Home Assistant Ingress")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8098)
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--runtime-root", type=Path, default=Path("/opt/yi-home/runtime/bionic-root"))
    args = parser.parse_args()

    server = UploadServer(
        (args.bind, args.port),
        Handler,
        data_dir=args.data_dir.expanduser().resolve(),
        runtime_root=args.runtime_root.expanduser().resolve(),
    )
    print(f"vendor_ui=ready; bind={args.bind}; port={args.port}; ingress_only=true", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
