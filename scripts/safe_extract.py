#!/usr/bin/env python3
"""Validate a small static-site ZIP and extract it without following archive paths."""

from __future__ import annotations

import argparse
import json
import shutil
import stat
import sys
import unicodedata
import zipfile
import zlib
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 1 * 1024 * 1024
MAX_FILES = 20
MAX_ENTRIES = 100
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
ALLOWED_SUFFIXES = {
    ".css", ".gif", ".html", ".ico", ".jpeg", ".jpg", ".js", ".json",
    ".mp3", ".mp4", ".otf", ".pdf", ".png", ".svg", ".txt", ".webm",
    ".webp", ".woff", ".woff2", ".xml",
}


class ArchiveError(ValueError):
    pass


def fail(message: str) -> None:
    raise ArchiveError(message)


def normalized_member(info: zipfile.ZipInfo) -> tuple[str, bool]:
    raw = info.filename
    if not raw or "\x00" in raw or "\\" in raw:
        fail(f"invalid archive path: {raw!r}")
    is_dir = info.is_dir() or raw.endswith("/")
    path_text = raw[:-1] if is_dir and raw.endswith("/") else raw
    if path_text.startswith("/") or (len(path_text) >= 2 and path_text[1] == ":"):
        fail(f"absolute path is not allowed: {raw!r}")
    parts = path_text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        fail(f"unsafe path segment: {raw!r}")
    parts = [unicodedata.normalize("NFC", part) for part in parts]
    if any(part.startswith(".") for part in parts):
        fail(f"hidden/reserved paths are not allowed: {raw!r}")
    normalized = PurePosixPath(*parts).as_posix()
    return normalized, is_dir


def validate_archive(archive: Path) -> list[tuple[zipfile.ZipInfo, str, bool]]:
    if not archive.is_file():
        fail("ZIP file does not exist")
    archive_size = archive.stat().st_size
    if archive_size <= 0 or archive_size > MAX_ARCHIVE_BYTES:
        fail(f"ZIP size must be between 1 byte and {MAX_ARCHIVE_BYTES} bytes")

    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        fail(f"invalid ZIP archive: {exc}")

    validated: list[tuple[zipfile.ZipInfo, str, bool]] = []
    seen: dict[str, tuple[str, bool]] = {}
    files = 0
    total_size = 0
    with zf:
        for info in zf.infolist():
            if len(validated) >= MAX_ENTRIES:
                fail(f"archive exceeds {MAX_ENTRIES} total entries")
            path, is_dir = normalized_member(info)
            folded = path.casefold()
            previous = seen.get(folded)
            if previous is not None:
                fail(f"duplicate or case-colliding path: {path!r}")
            seen[folded] = (path, is_dir)

            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                fail(f"special files and symlinks are not allowed: {path!r}")
            if is_dir and file_type == stat.S_IFREG:
                fail(f"file/directory type conflict: {path!r}")
            if not is_dir and file_type == stat.S_IFDIR:
                fail(f"file/directory type conflict: {path!r}")
            if info.flag_bits & 0x1:
                fail(f"encrypted entries are not allowed: {path!r}")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                fail(f"unsupported compression method: {path!r}")
            if len(path.encode("utf-8")) > 240:
                fail(f"path is too long: {path!r}")

            if is_dir:
                validated.append((info, path, True))
                continue

            files += 1
            total_size += info.file_size
            suffix = Path(path).suffix.lower()
            if suffix not in ALLOWED_SUFFIXES:
                fail(f"file type is not allowed: {path!r}")
            if info.file_size < 0 or info.file_size > MAX_FILE_BYTES:
                fail(f"file exceeds the per-file size limit: {path!r}")
            if info.compress_size == 0 and info.file_size > 0:
                fail(f"invalid compressed size: {path!r}")
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                fail(f"compression ratio exceeds {MAX_COMPRESSION_RATIO}:1: {path!r}")
            validated.append((info, path, False))

        if files == 0 or files > MAX_FILES:
            fail(f"archive must contain 1 to {MAX_FILES} regular files")
        if total_size > MAX_TOTAL_BYTES:
            fail(f"expanded archive exceeds {MAX_TOTAL_BYTES} bytes")
        if "index.html" not in {path for _, path, is_dir in validated if not is_dir}:
            fail("archive must contain index.html at its root")

        # Disallow children below a regular file path (e.g. a.txt/child.js).
        file_paths = {path.casefold() for _, path, is_dir in validated if not is_dir}
        for path in seen:
            parts = path.split("/")
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index]).casefold()
                if parent in file_paths:
                    fail(f"a regular file is used as a directory: {'/'.join(parts)!r}")

    return validated


def extract(archive: Path, destination: Path) -> dict[str, int]:
    if destination.exists():
        fail(f"destination already exists: {destination}")
    members = validate_archive(archive)
    destination.mkdir(parents=True)
    extracted_bytes = 0
    file_count = 0
    try:
        with zipfile.ZipFile(archive) as zf:
            for info, relative, is_dir in members:
                target = destination.joinpath(*PurePosixPath(relative).parts)
                if is_dir:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with zf.open(info, "r") as source, target.open("xb") as output:
                    while True:
                        block = source.read(64 * 1024)
                        if not block:
                            break
                        written += len(block)
                        extracted_bytes += len(block)
                        if written > MAX_FILE_BYTES or extracted_bytes > MAX_TOTAL_BYTES:
                            fail("actual decompressed data exceeded a configured limit")
                        output.write(block)
                if written != info.file_size:
                    fail(f"actual size differs from ZIP metadata: {relative!r}")
                file_count += 1
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise

    return {"files": file_count, "expanded_bytes": extracted_bytes}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = extract(args.archive, args.dest)
    except (ArchiveError, OSError, EOFError, zipfile.BadZipFile, RuntimeError, zlib.error) as exc:
        print(f"ZIP validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
