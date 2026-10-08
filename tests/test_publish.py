from __future__ import annotations

import importlib.util
import io
import struct
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAFE_EXTRACT = ROOT / "scripts" / "safe_extract.py"
CHECK_ASSETS = ROOT / "scripts" / "check_relative_assets.py"
FIXTURE = ROOT / "fixtures" / "demo-work.zip"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


safe_extract = load_module("safe_extract", SAFE_EXTRACT)
asset_check = load_module("check_relative_assets", CHECK_ASSETS)


def make_zip(entries: list[tuple[str, bytes, int | None]], *, compression=zipfile.ZIP_DEFLATED) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=compression) as archive:
        for name, data, unix_mode in entries:
            info = zipfile.ZipInfo(name)
            info.compress_type = compression
            if unix_mode is not None:
                info.create_system = 3
                info.external_attr = unix_mode << 16
            archive.writestr(info, data)
    return result.getvalue()


class SafeExtractTests(unittest.TestCase):
    def test_valid_fixture_extracts_all_static_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "work"
            result = safe_extract.extract(FIXTURE, destination)
            self.assertEqual(result["files"], 4)
            self.assertTrue((destination / "index.html").is_file())
            self.assertTrue((destination / "assets/site.css").is_file())
            self.assertTrue((destination / "assets/site.js").is_file())
            self.assertTrue((destination / "assets/mark.svg").is_file())
            self.assertGreater(result["expanded_bytes"], 0)
            self.assertEqual(asset_check.check_tree(destination), 3)

    def assert_rejected(self, entries, expected_message: str, *, compression=zipfile.ZIP_DEFLATED):
        data = make_zip(entries, compression=compression)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "input.zip"
            archive.write_bytes(data)
            with self.assertRaisesRegex(safe_extract.ArchiveError, expected_message):
                safe_extract.validate_archive(archive)

    def test_rejects_zip_slip(self):
        self.assert_rejected(
            [("index.html", b"ok", None), ("../escape.js", b"bad", None)],
            "unsafe path segment",
        )

    def test_rejects_absolute_and_backslash_paths(self):
        self.assert_rejected([("index.html", b"ok", None), ("/root.js", b"bad", None)], "absolute path")
        self.assert_rejected([("index.html", b"ok", None), ("..\\escape.js", b"bad", None)], "invalid archive path")

    def test_rejects_symlink(self):
        symlink_mode = stat.S_IFLNK | 0o777
        self.assert_rejected(
            [("index.html", b"ok", None), ("assets/link.js", b"target", symlink_mode)],
            "symlinks are not allowed",
        )

    def test_rejects_duplicate_case_collision_and_hidden_paths(self):
        self.assert_rejected(
            [("index.html", b"1", None), ("INDEX.HTML", b"2", None)],
            "duplicate or case-colliding",
        )
        self.assert_rejected(
            [("index.html", b"ok", None), (".github/workflows/evil.yml", b"bad", None)],
            "hidden/reserved",
        )

    def test_rejects_missing_root_entry_and_disallowed_extension(self):
        self.assert_rejected([("nested/index.html", b"no root", None)], "root")
        self.assert_rejected(
            [("index.html", b"ok", None), ("server.py", b"no", None)],
            "file type is not allowed",
        )

    def test_rejects_compression_bomb_ratio(self):
        payload = b"0" * 100_000
        self.assert_rejected(
            [("index.html", b"ok", None), ("assets/large.txt", payload, None)],
            "compression ratio exceeds",
        )

    def test_rejects_archive_entry_file_and_expanded_size_limits(self):
        old_archive = safe_extract.MAX_ARCHIVE_BYTES
        old_file = safe_extract.MAX_FILE_BYTES
        old_total = safe_extract.MAX_TOTAL_BYTES
        old_entries = safe_extract.MAX_ENTRIES
        try:
            safe_extract.MAX_ARCHIVE_BYTES = 100
            with tempfile.TemporaryDirectory() as tmp:
                oversized = Path(tmp) / "oversized.zip"
                oversized.write_bytes(make_zip([("index.html", b"x" * 200, None)]))
                with self.assertRaisesRegex(safe_extract.ArchiveError, "ZIP size"):
                    safe_extract.validate_archive(oversized)

            safe_extract.MAX_ARCHIVE_BYTES = old_archive
            safe_extract.MAX_FILE_BYTES = 8
            self.assert_rejected([("index.html", b"x" * 9, None)], "per-file size")

            safe_extract.MAX_FILE_BYTES = old_file
            safe_extract.MAX_TOTAL_BYTES = 4
            self.assert_rejected([("index.html", b"12345", None)], "expanded archive")

            safe_extract.MAX_TOTAL_BYTES = old_total
            safe_extract.MAX_ENTRIES = 2
            self.assert_rejected(
                [("index.html", b"x", None), ("assets/a.js", b"x", None), ("assets/b.js", b"x", None)],
                "total entries",
            )
        finally:
            safe_extract.MAX_ARCHIVE_BYTES = old_archive
            safe_extract.MAX_FILE_BYTES = old_file
            safe_extract.MAX_TOTAL_BYTES = old_total
            safe_extract.MAX_ENTRIES = old_entries

    def test_corrupt_compressed_data_is_rejected_and_partial_output_removed(self):
        data = bytearray(make_zip([
            ("index.html", b"valid", None),
            ("assets/app.js", b"second file data" * 100, None),
        ]))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("assets/app.js")
            header_offset = info.header_offset
            filename_length, extra_length = struct.unpack_from("<HH", data, header_offset + 26)
            data_offset = header_offset + 30 + filename_length + extra_length
            data[data_offset] ^= 0xFF
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "corrupt.zip"
            destination = Path(tmp) / "work"
            archive_path.write_bytes(data)
            with self.assertRaises((zipfile.BadZipFile, RuntimeError, zlib.error, EOFError)):
                safe_extract.extract(archive_path, destination)
            self.assertFalse(destination.exists())

    def test_rejects_file_count_and_file_directory_conflicts(self):
        entries = [("index.html", b"ok", None)] + [
            (f"assets/{index}.txt", b"x", None) for index in range(safe_extract.MAX_FILES)
        ]
        self.assert_rejected(entries, "regular files")
        self.assert_rejected(
            [("index.html", b"ok", None), ("assets.txt", b"x", None), ("assets.txt/child.js", b"y", None)],
            "regular file is used as a directory",
        )

    def test_rejects_casefolded_parent_file_directory_conflict(self):
        self.assert_rejected(
            [("index.html", b"ok", None), ("Assets.txt", b"x", None), ("assets.txt/child.js", b"y", None)],
            "regular file is used as a directory",
        )

    def test_existing_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "work"
            destination.mkdir()
            sentinel = destination / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            with self.assertRaises(safe_extract.ArchiveError):
                safe_extract.extract(FIXTURE, destination)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")


class RelativeAssetTests(unittest.TestCase):
    def test_rejects_missing_root_absolute_and_escape_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text('<link rel="stylesheet" href="/assets/site.css">', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "root-absolute"):
                asset_check.check_tree(root)
            (root / "index.html").write_text('<script src="../escape.js"></script>', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "escapes"):
                asset_check.check_tree(root)
            (root / "index.html").write_text('<script src="assets/missing.js"></script>', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not found"):
                asset_check.check_tree(root)


class WorkflowTests(unittest.TestCase):
    def test_workflow_is_manual_and_permissions_are_scoped(self):
        workflow = (ROOT / ".github" / "workflows" / "publish-pages.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("permissions: {}", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("pages: read", workflow)
        self.assertIn("pages: write", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("needs: validate-and-package", workflow)
        self.assertLess(workflow.index("actions/upload-pages-artifact@v4"), workflow.index("actions/deploy-pages@v4"))
        self.assertIn("actions/checkout@v6", workflow)
        self.assertIn("github.event.repository.default_branch", workflow)
        self.assertIn("python3 scripts/safe_extract.py", workflow)
        self.assertIn("fixtures/demo-work.zip", workflow)
        self.assertIn("--dest _site/works/zip-demo", workflow)
        self.assertIn("python3 scripts/check_relative_assets.py _site/works/zip-demo", workflow)
        self.assertIn("path: _site", workflow)
        self.assertNotIn("github-pages-poc/", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertNotIn("push:", workflow)

    def test_workflow_yaml_parses_with_ruby_when_available(self):
        import shutil

        ruby = shutil.which("ruby")
        if ruby is None:
            self.skipTest("Ruby is unavailable; workflow structural checks still run")
        workflow_path = ROOT / ".github" / "workflows" / "publish-pages.yml"
        completed = subprocess.run(
            [ruby, "-e", 'require "yaml"; YAML.load_file(ARGV.fetch(0)); puts "YAML OK"', str(workflow_path)],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("YAML OK", completed.stdout)


if __name__ == "__main__":
    unittest.main()
