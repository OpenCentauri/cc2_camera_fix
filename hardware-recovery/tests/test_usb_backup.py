from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile


TOOL_PATH = Path(__file__).parents[1] / "cc2_sig_tool.py"
SPEC = importlib.util.spec_from_file_location("cc2_sig_tool", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool
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
                "erase_size": 0x4000,
                "name": "HWCONFIG" if index == 4 else name,
            }
            for index, size, name in tool.EXPECTED_USB_PARTITIONS
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


class ConfigModeTests(unittest.TestCase):
    SERIAL = b"serial=12PSSSS4TEST000000000000000000000000\n"

    @staticmethod
    def config_file(name: bytes, data: bytes, inode: int, mode: int = 0x81A4):
        return tool.ConfigFile(
            name=name,
            data=data,
            inode=inode,
            mode=mode,
            uid=0,
            gid=0,
            atime=18,
            mtime=19,
            ctime=20,
            flags=0,
            dirent_mctime=21,
            dtype=8,
        )

    def test_clean_data_rejects_unknown_name_without_override(self):
        config = tool.build_preserved_config(
            [
                self.config_file(b"serial.cfg", self.SERIAL, 2),
                self.config_file(b"system.sh", b"/bin/adbd &\n", 3, 0x81ED),
            ]
        )
        with self.assertRaisesRegex(tool.ValidationError, "unexpected"):
            tool.extract_serial_and_config_info(config)
        accepted = tool.extract_serial_and_config_info(
            config, allow_unknown_names=True
        )
        self.assertEqual(accepted["unexpected_names"], ["system.sh"])

    def test_preserve_data_rebuild_has_one_node_pair_per_live_file(self):
        files = [
            self.config_file(b"serial.cfg", self.SERIAL, 7),
            self.config_file(b"system.sh", b"/bin/adbd &\n", 9, 0x81ED),
            self.config_file(b"uvc.attr", b"alpha\0beta", 11),
        ]
        config = tool.build_preserved_config(files)
        parsed = tool.extract_serial_and_config_info(
            config,
            allow_unknown_names=True,
            preserve_live_files=True,
        )
        self.assertEqual(parsed["obsolete_node_count"], 0)
        self.assertEqual(
            parsed["kind_counts"],
            {"cleanmarker": 1, "dirent": 3, "inode": 3},
        )
        rebuilt = {item.name: item for item in parsed["live_config_files"]}
        self.assertEqual(set(rebuilt), {b"serial.cfg", b"system.sh", b"uvc.attr"})
        for expected in files:
            self.assertEqual(rebuilt[expected.name].data, expected.data)
            self.assertEqual(rebuilt[expected.name].mode, expected.mode)

    def test_wipe_override_is_clean_data_only(self):
        with self.assertRaisesRegex(tool.ValidationError, "clean-data"):
            tool.build_recovery(
                Path("unused.bin"),
                Path("unused-output"),
                confirmation_paths=[],
                keep_config=False,
                config_mode="preserve-data",
                wipe_unknown_config=True,
                overwrite=False,
                show_serial=False,
                allow_fewer_reads=False,
            )

    def test_keep_config_cannot_be_combined_with_preserve_data(self):
        with self.assertRaisesRegex(tool.ValidationError, "cannot be combined"):
            tool.build_recovery(
                Path("unused.bin"),
                Path("unused-output"),
                confirmation_paths=[],
                keep_config=True,
                config_mode="preserve-data",
                wipe_unknown_config=False,
                overwrite=False,
                show_serial=False,
                allow_fewer_reads=False,
            )

if __name__ == "__main__":
    unittest.main()
