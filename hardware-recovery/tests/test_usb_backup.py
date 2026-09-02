from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


TOOL_PATH = Path(__file__).parents[1] / "cc2_sig_tool.py"
SPEC = importlib.util.spec_from_file_location("cc2_sig_tool", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def usb_manifest(image: bytes) -> dict:
    boot_hash = tool.sha256(image[:0x40000])
    return {
        "format": tool.USB_BACKUP_FORMAT,
        **tool.image_hashes(image),
        "required_identical_reads": 3,
        "consecutive_identical_reads": 3,
        "read_passes": 3,
        "maximum_read_attempts": 5,
        "bootloader": {
            "partition": "boot",
            "size": 0x40000,
            "sha256": boot_hash,
            "known_sha256": tool.KNOWN_BOOTLOADER_SHA256,
            "known_reference": boot_hash == tool.KNOWN_BOOTLOADER_SHA256,
            "acceptance": (
                "known-reference"
                if boot_hash == tool.KNOWN_BOOTLOADER_SHA256
                else "explicit-hash"
            ),
        },
        "partitions": [
            {
                "index": index,
                "size": size,
                "erase_size": erase_size,
                "name": name,
            }
            for index, size, erase_size, name in tool.EXPECTED_USB_PARTITIONS
        ],
    }


def write_backup(path: Path, image: bytes, manifest: dict) -> None:
    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(tool.USB_BACKUP_IMAGE_MEMBER, image)
        archive.writestr(
            tool.USB_BACKUP_MANIFEST_MEMBER,
            json.dumps(manifest),
        )


class UsbBackupInputTests(unittest.TestCase):
    def test_valid_v2_backup_supplies_three_read_evidence(self):
        image = b"\0" * tool.FLASH_SIZE
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.zip"
            write_backup(path, image, usb_manifest(image))
            loaded, evidence = tool.read_image_source(path)
        self.assertEqual(loaded, image)
        self.assertEqual(evidence["format"], tool.USB_BACKUP_FORMAT)
        self.assertEqual(evidence["image_member"], "flash.bin")
        self.assertEqual(evidence["evidenced_identical_reads"], 3)
        self.assertEqual(evidence["read_passes"], 3)

    def test_manifest_hash_must_match_flash_member(self):
        image = b"\0" * tool.FLASH_SIZE
        manifest = usb_manifest(image)
        manifest["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.zip"
            write_backup(path, image, manifest)
            with self.assertRaisesRegex(tool.ValidationError, "sha256"):
                tool.read_image_source(path)

    def test_zip_members_are_exact(self):
        image = b"\0" * tool.FLASH_SIZE
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.zip"
            write_backup(path, image, usb_manifest(image))
            with zipfile.ZipFile(path, mode="a") as archive:
                archive.writestr("extra.txt", "unexpected")
            with self.assertRaisesRegex(tool.ValidationError, "exactly"):
                tool.read_image_source(path)


if __name__ == "__main__":
    unittest.main()
