#!/usr/bin/env python3
"""Install a user-supplied YI ARM64 runtime library into private App data."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import struct
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Sequence


LIBRARY_NAME = "libPPPP_API.so"
APK_NAME = "yi-home.apk"
MIN_LIBRARY_SIZE = 4096
MAX_LIBRARY_SIZE = 64 * 1024 * 1024
AARCH64_MACHINE = 183
COMPATIBILITY_GUEST_DIRS = (
    "data/local/tmp/yi-phase3g",
    "data/local/tmp/yi-online-status",
)


class VendorRuntimeError(RuntimeError):
    """A supplied vendor artifact cannot be accepted safely."""


@dataclass(frozen=True)
class VendorLibrary:
    path: Path
    source: str
    size: int
    sha256: str
    reused: bool


def validate_vendor_library(path: Path) -> tuple[int, str]:
    """Validate the minimum ELF64/AArch64 shared-object structure and hash it."""
    try:
        status = path.stat()
        size = status.st_size
        if not stat.S_ISREG(status.st_mode):
            raise VendorRuntimeError("vendor library is not a regular file")
        if size < MIN_LIBRARY_SIZE:
            raise VendorRuntimeError("vendor library is truncated")
        if size > MAX_LIBRARY_SIZE:
            raise VendorRuntimeError("vendor library is unexpectedly large")
        with path.open("rb") as stream:
            header = stream.read(64)
            digest = hashlib.sha256(header)
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise VendorRuntimeError("vendor library is not readable") from exc

    if len(header) < 64 or header[:4] != b"\x7fELF":
        raise VendorRuntimeError("vendor library is not an ELF shared object")
    if header[4] != 2:
        raise VendorRuntimeError("vendor library is not a 64-bit ELF shared object")
    if header[5] != 1 or header[6] != 1:
        raise VendorRuntimeError("vendor library has an unsupported ELF encoding")

    elf_type, machine, version = struct.unpack_from("<HHI", header, 16)
    header_size = struct.unpack_from("<H", header, 52)[0]
    if elf_type != 3:
        raise VendorRuntimeError("vendor library is not an ELF shared object")
    if machine != AARCH64_MACHINE:
        raise VendorRuntimeError("vendor library is not AArch64/ARM64")
    if version != 1 or header_size < 64 or header_size > size:
        raise VendorRuntimeError("vendor library has a malformed ELF header")
    return size, digest.hexdigest()


def persist_vendor_library(source: BinaryIO, target: Path) -> tuple[int, str]:
    """Validate and atomically replace the private vendor library."""
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{LIBRARY_NAME}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            size = 0
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_LIBRARY_SIZE:
                    raise VendorRuntimeError("vendor library is unexpectedly large")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        size, digest = validate_vendor_library(temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        try:
            directory_fd = os.open(target.parent, getattr(os, "O_DIRECTORY", 0))
        except OSError:
            pass
        else:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return size, digest
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _select_apk_library(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    candidates = [
        info
        for info in archive.infolist()
        if not info.is_dir()
        and PurePosixPath(info.filename).name.casefold() == LIBRARY_NAME.casefold()
        and "arm64-v8a" in PurePosixPath(info.filename).parts
    ]
    candidates.sort(
        key=lambda info: (
            info.filename != f"lib/arm64-v8a/{LIBRARY_NAME}",
            len(PurePosixPath(info.filename).parts),
            info.filename,
        )
    )
    if not candidates:
        raise VendorRuntimeError("official APK contains no ARM64 vendor library")
    selected = candidates[0]
    if selected.flag_bits & 1:
        raise VendorRuntimeError("official APK vendor library is encrypted")
    if not MIN_LIBRARY_SIZE <= selected.file_size <= MAX_LIBRARY_SIZE:
        raise VendorRuntimeError("official APK vendor library has an invalid size")
    return selected


def bootstrap_vendor_runtime(data_dir: Path, share_dir: Path) -> VendorLibrary:
    """Reuse a valid private library or import one official local artifact once."""
    data_dir = data_dir.expanduser().resolve()
    share_dir = share_dir.expanduser().resolve()
    target = data_dir / "vendor" / LIBRARY_NAME
    if target.exists() and not target.is_symlink():
        try:
            size, digest = validate_vendor_library(target)
        except VendorRuntimeError:
            pass
        else:
            os.chmod(target.parent, 0o700)
            os.chmod(target, 0o600)
            return VendorLibrary(target, "private_data", size, digest, True)

    apk = share_dir / APK_NAME
    direct = share_dir / LIBRARY_NAME
    if apk.is_file():
        try:
            with zipfile.ZipFile(apk) as archive:
                selected = _select_apk_library(archive)
                with archive.open(selected) as source:
                    size, digest = persist_vendor_library(source, target)
        except VendorRuntimeError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise VendorRuntimeError("official YI Home APK is not a readable APK/ZIP") from exc
        source_kind = "official_apk"
    elif direct.is_file():
        try:
            with direct.open("rb") as source:
                size, digest = persist_vendor_library(source, target)
        except OSError as exc:
            raise VendorRuntimeError("direct vendor library could not be read") from exc
        source_kind = "direct_library"
    else:
        raise VendorRuntimeError(
            f"place the official YI Home APK at {share_dir / APK_NAME} "
            f"(or {share_dir / LIBRARY_NAME})"
        )
    return VendorLibrary(target, source_kind, size, digest, False)


def install_compatibility_links(vendor_library: Path, runtime_root: Path) -> None:
    """Keep the two proven Bionic guest library paths without copying vendor bytes."""
    vendor_library = vendor_library.expanduser().resolve()
    runtime_root = runtime_root.expanduser().resolve()
    for guest_dir in COMPATIBILITY_GUEST_DIRS:
        directory = runtime_root / guest_dir
        if not directory.is_dir():
            raise VendorRuntimeError("proven Bionic runtime layout is incomplete")
        link = directory / LIBRARY_NAME
        if link.is_symlink() and link.resolve() == vendor_library.resolve():
            continue
        if link.exists() or link.is_symlink():
            raise VendorRuntimeError("packaged runtime unexpectedly contains a vendor library")
        try:
            os.symlink(vendor_library, link)
        except OSError as exc:
            raise VendorRuntimeError("could not attach the private vendor runtime") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the private YI vendor runtime")
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--share-dir", type=Path, default=Path("/share/yi_rtsp"))
    parser.add_argument(
        "--runtime-root", type=Path, default=Path("/opt/yi-home/runtime/bionic-root")
    )
    args = parser.parse_args(argv)
    try:
        installed = bootstrap_vendor_runtime(args.data_dir, args.share_dir)
        install_compatibility_links(installed.path, args.runtime_root)
    except VendorRuntimeError as exc:
        print(f"YI vendor runtime unavailable: {exc}", file=sys.stderr)
        return 1

    state = "reused" if installed.reused else "installed"
    print(
        f"vendor_runtime={state}; source={installed.source}; size={installed.size}; "
        f"sha256={installed.sha256}; proprietary_bytes_exposed=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
