from __future__ import annotations

import hashlib
import io
import json
import lzma
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
import zipfile

from cc2flash.adb_backup import (
    AdbClient,
    AdbUnavailable,
    BACKUP_IMAGE_MEMBER,
    BACKUP_MANIFEST_MEMBER,
    EXPECTED_PARTITIONS,
    KNOWN_BOOTLOADER_SHA256,
    MAX_READ_ATTEMPTS,
    REQUIRED_IDENTICAL_READS,
    acquire_stable,
    bootloader_reference,
    hashes,
    load_preserved_backup,
    parse_proc_mtd,
    save_backup,
    validate_backup_archive_path,
    validate_partition_map,
    validate_preserved_backup,
    validate_replacement_against_backup,
)
from cc2flash import cli
from cc2flash import hid_transport
from cc2flash.protocol import (
    BOOT_DATA_SIZE,
    BOOT_REPORT_SIZE,
    FLASH_SIZE,
    NORMAL_REPORT_SIZE,
    ProtocolError,
    UPGRADE_FLAG_OFFSET,
    UPGRADE_FLAG_0,
    UpdatePlan,
    additive_checksum,
    build_boot_frame,
    build_metadata_frame,
    build_normal_report,
    build_update_blob,
    crc16_normal,
    from_boot_hid_report,
    iter_data_frames,
    parse_boot_frame,
    parse_normal_report,
    to_boot_hid_report,
    upgrade_flag_payload,
    validate_full_restore_image,
)


def response_frame(frame_type: int, payload: bytes) -> bytes:
    total = len(payload) + 7
    result = bytearray(total)
    result[:3] = b"\x81\x00\xee"
    result[3] = frame_type
    struct.pack_into("<H", result, 4, total)
    result[6:-1] = payload
    result[-1] = additive_checksum(result[:-1])
    return bytes(result)


def normal_response(command: int, status: int = 0) -> bytes:
    result = bytearray(NORMAL_REPORT_SIZE)
    result[0] = 1
    result[1:3] = b"\x5a\x5a"
    struct.pack_into("<H", result, 3, command)
    result[5] = 0
    struct.pack_into("<H", result, 6, 0)
    struct.pack_into("<I", result, 8, status)
    struct.pack_into("<H", result, 12, crc16_normal(bytes(result[:12])))
    return bytes(result)


class ProtocolTests(unittest.TestCase):
    def test_normal_crc_vector(self):
        self.assertEqual(crc16_normal(b"123456789"), 0x1DBA)

    def test_upgrade_transition_report_vector(self):
        self.assertEqual(upgrade_flag_payload().hex(), "54445055a1030201")
        report = build_normal_report(0x4000, upgrade_flag_payload())
        self.assertEqual(len(report), 1024)
        self.assertEqual(
            report[:22].hex(),
            "015a5a004001080000000000f80e54445055a1030201",
        )
        parsed = parse_normal_report(report)
        self.assertEqual(parsed.command, 0x4000)
        self.assertEqual(parsed.status, 0)
        self.assertEqual(parsed.payload, upgrade_flag_payload())

    def test_normal_crc_rejects_corruption(self):
        report = bytearray(build_normal_report(1, b"version"))
        report[15] ^= 1
        with self.assertRaisesRegex(ProtocolError, "CRC"):
            parse_normal_report(bytes(report))

    def test_boot_metadata_vector(self):
        plan = UpdatePlan(0, 512, 640, "00" * 16, 1, BOOT_DATA_SIZE, 1)
        frame = build_metadata_frame(plan)
        self.assertEqual(frame.hex(), "8000ee011100f40b800200000100000002")
        parsed = parse_boot_frame(frame, device_response=False)
        self.assertEqual(parsed.frame_type, 1)
        self.assertEqual(parsed.total_length, 17)
        self.assertEqual(struct.unpack("<HIHH", parsed.payload), (3060, 640, 1, 0))

    def test_boot_checksum_rejects_corruption(self):
        frame = bytearray(build_boot_frame(3, b"abc"))
        frame[6] ^= 1
        with self.assertRaisesRegex(ProtocolError, "checksum"):
            parse_boot_frame(bytes(frame), device_response=False)

    def test_image_header_and_packet_numbers(self):
        image = bytes(range(256)) * 16
        blob, plan = build_update_blob(image, flash_offset=0x1000)
        self.assertEqual(len(blob), len(image) + 128)
        self.assertEqual(struct.unpack_from("<II", blob, 4), (0x1000, len(image)))
        self.assertEqual(blob[12:28], hashlib.md5(image).digest())
        frames = list(iter_data_frames(blob))
        self.assertEqual(len(frames), plan.packet_count)
        for expected, (sequence, frame) in enumerate(frames):
            self.assertEqual(sequence, expected)
            parsed = parse_boot_frame(frame, device_response=False)
            self.assertEqual(parsed.frame_type, 3)
            self.assertEqual(struct.unpack_from("<I", parsed.payload)[0], expected)

    def test_boot_hid_envelope(self):
        frame = build_boot_frame(1, b"0123456789")
        report = to_boot_hid_report(frame)
        self.assertEqual(len(report), BOOT_REPORT_SIZE)
        self.assertEqual(report[0], 1)
        response = response_frame(2, b"\0" * 4)
        parsed = from_boot_hid_report(b"\x01" + response.ljust(3071, b"\0"))
        self.assertEqual((parsed.frame_type, parsed.payload), (2, b"\0" * 4))

    def test_full_restore_guards(self):
        with self.assertRaisesRegex(ProtocolError, "exactly"):
            validate_full_restore_image(b"\xff" * 10)
        image = bytearray(b"\xff" * FLASH_SIZE)
        struct.pack_into("<I", image, UPGRADE_FLAG_OFFSET, UPGRADE_FLAG_0)
        with self.assertRaisesRegex(ProtocolError, "boot-loop"):
            validate_full_restore_image(bytes(image))


PROC_MTD = """dev:    size   erasesize  name
mtd0: 00040000 00008000 "boot"
mtd1: 00150000 00008000 "kernel"
mtd2: 00158000 00008000 "root"
mtd3: 004e8000 00008000 "system"
mtd4: 00010000 00008000 "hwconfig"
mtd5: 00020000 00008000 "config"
"""


class BackupTests(unittest.TestCase):
    def test_partition_map(self):
        parts = parse_proc_mtd(PROC_MTD)
        validate_partition_map(parts)
        self.assertEqual(sum(part.size for part in parts), FLASH_SIZE)

    def test_partition_map_rejects_wrong_size(self):
        parts = parse_proc_mtd(PROC_MTD.replace("004e8000", "004f0000"))
        with self.assertRaisesRegex(ProtocolError, "partition sizes"):
            validate_partition_map(parts)

    def test_three_consecutive_reads_build_v2_manifest(self):
        parts = parse_proc_mtd(PROC_MTD)
        image = b"".join(
            bytes((index,)) * size for index, (_name, size) in enumerate(EXPECTED_PARTITIONS)
        )

        class FakeAdb:
            serial = "fake"
            calls = 0

            def identity_and_partitions(self):
                return "uid=0(root)", parts

            def read_flash_once(self, _parts):
                self.calls += 1
                return image

        fake_adb = FakeAdb()
        progress = mock.Mock()
        acquired, manifest = acquire_stable(fake_adb, progress=progress)
        self.assertEqual(acquired, image)
        self.assertEqual(fake_adb.calls, 3)
        self.assertEqual(manifest["format"], "cc2flash-backup-v2")
        self.assertEqual(manifest["read_passes"], 3)
        self.assertEqual(manifest["required_identical_reads"], 3)
        self.assertEqual(manifest["consecutive_identical_reads"], 3)
        self.assertEqual(manifest["maximum_read_attempts"], 5)
        self.assertEqual(progress.call_count, 3)
        manifest["bootloader"]["acceptance"] = "explicit-hash"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.zip"
            archive_path = save_backup(path, acquired, manifest)
            self.assertEqual(archive_path, path)
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {BACKUP_IMAGE_MEMBER, BACKUP_MANIFEST_MEMBER},
                )
                self.assertEqual(archive.read(BACKUP_IMAGE_MEMBER), acquired)
            self.assertEqual(validate_preserved_backup(path)["sha256"], manifest["sha256"])
            with self.assertRaisesRegex(ProtocolError, "overwrite"):
                save_backup(path, acquired, manifest)

    def test_backup_requires_zip_destination(self):
        with self.assertRaisesRegex(ProtocolError, "must be a .zip"):
            validate_backup_archive_path(Path("backup.bin"))

    def test_hardware_recovery_regions_are_restore_compatible(self):
        preserved = b"\0" * FLASH_SIZE
        replacement = bytearray(preserved)
        replacement[0x463000] ^= 0xFF
        replacement[0x46AFFF] ^= 0xFF
        replacement[0x7E0000] ^= 0xFF
        replacement[0x7FFFFF] ^= 0xFF
        validate_replacement_against_backup(bytes(replacement), preserved)

    def test_restore_rejects_difference_outside_recovery_regions(self):
        preserved = b"\0" * FLASH_SIZE
        replacement = bytearray(preserved)
        replacement[0x7D2011] = 1
        with self.assertRaisesRegex(ProtocolError, "0x7d2011.*wrong-unit"):
            validate_replacement_against_backup(bytes(replacement), preserved)

    def test_failed_archive_publish_leaves_no_final_or_half_backup(self):
        image = b"\0" * FLASH_SIZE
        manifest = {"format": "test", **hashes(image)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.zip"
            with (
                mock.patch(
                    "cc2flash.adb_backup.os.link",
                    side_effect=OSError("publish failed"),
                ),
                self.assertRaisesRegex(OSError, "publish failed"),
            ):
                save_backup(path, image, manifest)
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_concurrent_archive_publish_is_not_overwritten(self):
        image = b"\0" * FLASH_SIZE
        manifest = {"format": "test", **hashes(image)}
        competing_contents = b"another process published this"

        def publish_competing_archive(_source, destination):
            Path(destination).write_bytes(competing_contents)
            raise FileExistsError("destination appeared concurrently")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.zip"
            with (
                mock.patch(
                    "cc2flash.adb_backup.os.link",
                    side_effect=publish_competing_archive,
                ),
                self.assertRaisesRegex(ProtocolError, "overwrite"),
            ):
                save_backup(path, image, manifest)
            self.assertEqual(path.read_bytes(), competing_contents)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_reads_settle_after_initial_mismatch(self):
        parts = parse_proc_mtd(PROC_MTD)
        values = [1, 2, 2, 2]

        class FakeAdb:
            serial = "fake"
            calls = 0

            def identity_and_partitions(self):
                return "uid=0(root)", parts

            def read_flash_once(self, _parts):
                value = values[self.calls]
                self.calls += 1
                return bytes((value,)) * FLASH_SIZE

        fake_adb = FakeAdb()
        progress = mock.Mock()
        acquired, manifest = acquire_stable(fake_adb, progress=progress)
        self.assertEqual(acquired[:1], b"\x02")
        self.assertEqual(manifest["read_passes"], 4)
        self.assertEqual(
            [call.args[2] for call in progress.call_args_list],
            [1, 1, 2, 3],
        )

    def test_five_unstable_reads_are_rejected_before_boot_hash_check(self):
        parts = parse_proc_mtd(PROC_MTD)
        values = [1, 1, 2, 2, 1]

        class FakeAdb:
            serial = "fake"
            calls = 0

            def identity_and_partitions(self):
                return "uid=0(root)", parts

            def read_flash_once(self, _parts):
                value = values[self.calls]
                self.calls += 1
                return bytes((value,)) * FLASH_SIZE

        fake_adb = FakeAdb()
        with (
            mock.patch("cc2flash.adb_backup.bootloader_reference") as reference,
            self.assertRaisesRegex(
                ProtocolError, "five complete.*three consecutive"
            ),
        ):
            acquire_stable(fake_adb)
        self.assertEqual(fake_adb.calls, MAX_READ_ATTEMPTS)
        reference.assert_not_called()

    def test_bootloader_reference_records_observed_and_known_hashes(self):
        image = b"\0" * FLASH_SIZE
        result = bootloader_reference(image)
        expected_observed = hashlib.sha256(image[:0x40000]).hexdigest()
        self.assertEqual(result["partition"], "boot")
        self.assertEqual(result["size"], 0x40000)
        self.assertEqual(result["sha256"], expected_observed)
        self.assertEqual(result["known_sha256"], KNOWN_BOOTLOADER_SHA256)
        self.assertEqual(
            result["known_reference"],
            expected_observed == KNOWN_BOOTLOADER_SHA256,
        )

    def test_restore_rejects_legacy_two_read_manifest(self):
        image = b"\0" * FLASH_SIZE
        manifest = {
            "format": "cc2flash-backup-v1",
            **hashes(image),
            "read_passes": 2,
            "partitions": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.zip"
            save_backup(path, image, manifest)
            with self.assertRaisesRegex(ProtocolError, "v2 manifest"):
                validate_preserved_backup(path)

    def test_malformed_consecutive_read_count_is_rejected_cleanly(self):
        image = b"\0" * FLASH_SIZE
        manifest = {
            "format": "cc2flash-backup-v2",
            **hashes(image),
            "required_identical_reads": 3,
            "consecutive_identical_reads": "three",
            "read_passes": 3,
            "maximum_read_attempts": 5,
            "bootloader": {
                **bootloader_reference(image),
                "acceptance": "explicit-hash",
            },
            "partitions": [
                {
                    "index": index,
                    "size": size,
                    "erase_size": 0x8000,
                    "name": name,
                }
                for index, (name, size) in enumerate(EXPECTED_PARTITIONS)
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.zip"
            save_backup(path, image, manifest)
            with self.assertRaisesRegex(ProtocolError, "three consecutive"):
                validate_preserved_backup(path)

    def test_impossible_consecutive_read_count_is_rejected(self):
        image = b"\0" * FLASH_SIZE
        manifest = {
            "format": "cc2flash-backup-v2",
            **hashes(image),
            "required_identical_reads": 3,
            "consecutive_identical_reads": 999,
            "read_passes": 3,
            "maximum_read_attempts": 5,
            "bootloader": {
                **bootloader_reference(image),
                "acceptance": "explicit-hash",
            },
            "partitions": [
                {
                    "index": index,
                    "size": size,
                    "erase_size": 0x8000,
                    "name": name,
                }
                for index, (name, size) in enumerate(EXPECTED_PARTITIONS)
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.zip"
            save_backup(path, image, manifest)
            with self.assertRaisesRegex(ProtocolError, "three consecutive"):
                validate_preserved_backup(path)

    def test_unsupported_zip_compression_is_rejected_cleanly(self):
        image = b"\0" * FLASH_SIZE
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.zip"
            save_backup(path, image, {"format": "test", **hashes(image)})
            with (
                mock.patch.object(
                    zipfile.ZipFile,
                    "read",
                    side_effect=NotImplementedError("unsupported compression"),
                ),
                self.assertRaisesRegex(ProtocolError, "unreadable or corrupt"),
            ):
                validate_preserved_backup(path)

    def test_corrupt_compressed_zip_member_is_rejected_cleanly(self):
        image = b"\0" * FLASH_SIZE
        manifest = {"format": "test", **hashes(image)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.zip"
            with zipfile.ZipFile(
                path, mode="w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr(BACKUP_IMAGE_MEMBER, image)
                archive.writestr(BACKUP_MANIFEST_MEMBER, json.dumps(manifest))

            damaged = bytearray(path.read_bytes())
            with zipfile.ZipFile(io.BytesIO(damaged)) as archive:
                info = archive.getinfo(BACKUP_IMAGE_MEMBER)
                header_size = 30 + len(info.filename.encode()) + len(info.extra)
                compressed_start = info.header_offset + header_size
            damaged[compressed_start] = 0x06  # reserved DEFLATE block type
            path.write_bytes(damaged)

            with self.assertRaisesRegex(ProtocolError, "unreadable or corrupt"):
                validate_preserved_backup(path)

    def test_lzma_corruption_is_rejected_cleanly(self):
        image = b"\0" * FLASH_SIZE
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.zip"
            save_backup(path, image, {"format": "test", **hashes(image)})
            with (
                mock.patch.object(
                    zipfile.ZipFile,
                    "read",
                    side_effect=lzma.LZMAError("damaged LZMA stream"),
                ),
                self.assertRaisesRegex(ProtocolError, "unreadable or corrupt"),
            ):
                validate_preserved_backup(path)

    def test_overlong_json_integer_is_rejected_cleanly(self):
        image = b"\0" * FLASH_SIZE
        integer_digits = sys.get_int_max_str_digits() + 1
        manifest_bytes = b'{"value":' + b"1" * integer_digits + b"}"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.zip"
            with zipfile.ZipFile(path, mode="w") as archive:
                archive.writestr(BACKUP_IMAGE_MEMBER, image)
                archive.writestr(BACKUP_MANIFEST_MEMBER, manifest_bytes)
            with self.assertRaisesRegex(ProtocolError, "manifest is unreadable"):
                validate_preserved_backup(path)

    def test_excessively_nested_json_is_rejected_cleanly(self):
        image = b"\0" * FLASH_SIZE
        nesting = sys.getrecursionlimit() * 10
        manifest_bytes = b"[" * nesting + b"0" + b"]" * nesting
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.zip"
            with zipfile.ZipFile(path, mode="w") as archive:
                archive.writestr(BACKUP_IMAGE_MEMBER, image)
                archive.writestr(BACKUP_MANIFEST_MEMBER, manifest_bytes)
            with self.assertRaisesRegex(ProtocolError, "manifest is unreadable"):
                validate_preserved_backup(path)

    def test_adb_absence_is_distinguished_for_bootstrap(self):
        result = SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=b"error: no devices/emulators found\n",
        )
        with mock.patch("cc2flash.adb_backup.subprocess.run", return_value=result):
            with self.assertRaises(AdbUnavailable):
                AdbClient().ensure_available()

    def test_adb_offline_is_distinguished_for_bootstrap(self):
        result = SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=b"error: device offline\n",
        )
        with mock.patch("cc2flash.adb_backup.subprocess.run", return_value=result):
            with self.assertRaisesRegex(AdbUnavailable, "not online"):
                AdbClient().ensure_available()

    def test_identity_and_partition_map_use_legacy_text_shell(self):
        responses = [
            SimpleNamespace(returncode=0, stdout=b"device\n", stderr=b""),
            SimpleNamespace(
                returncode=0,
                stdout=b"uid=0(root) gid=0(root)\r\r\n",
                stderr=b"",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=PROC_MTD.replace("\n", "\r\r\n").encode("ascii"),
                stderr=b"",
            ),
        ]
        client = AdbClient(serial="Ucamera001")
        with mock.patch(
            "cc2flash.adb_backup.subprocess.run", side_effect=responses
        ) as run:
            identity, parts = client.identity_and_partitions()
        self.assertEqual(identity, "uid=0(root) gid=0(root)")
        self.assertEqual(len(parts), 6)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            commands,
            [
                ["adb", "-s", "Ucamera001", "get-state"],
                ["adb", "-s", "Ucamera001", "shell", "id"],
                ["adb", "-s", "Ucamera001", "shell", "cat", "/proc/mtd"],
            ],
        )

    def test_flash_read_pulls_every_partition_in_binary_safe_order(self):
        parts = parse_proc_mtd(PROC_MTD)
        client = AdbClient()

        def fake_pull(remote_path, local_path, *, timeout):
            index = int(remote_path.removeprefix("/dev/mtd"))
            Path(local_path).write_bytes(bytes((index,)) * parts[index].size)

        with mock.patch.object(client, "pull", side_effect=fake_pull) as pull:
            image = client.read_flash_once(parts)
        self.assertEqual(len(image), FLASH_SIZE)
        self.assertEqual(pull.call_count, 6)
        offset = 0
        for index, part in enumerate(parts):
            self.assertEqual(image[offset], index)
            self.assertEqual(image[offset + part.size - 1], index)
            offset += part.size

    def test_flash_pull_rejects_short_partition_and_cleans_temporary_file(self):
        part = parse_proc_mtd(PROC_MTD)[0]
        temporary_paths = []
        client = AdbClient()

        def fake_short_pull(_remote_path, local_path, *, timeout):
            temporary_paths.append(Path(local_path))
            Path(local_path).write_bytes(b"x" * (part.size - 1))

        with (
            mock.patch.object(client, "pull", side_effect=fake_short_pull),
            self.assertRaisesRegex(ProtocolError, "short read from /dev/mtd0"),
        ):
            client.read_flash_once([part])
        self.assertEqual(len(temporary_paths), 1)
        self.assertFalse(temporary_paths[0].exists())

    def test_pull_requires_adb_to_create_the_requested_local_file(self):
        client = AdbClient(serial="Ucamera001")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "mtd4.bin"
            with (
                mock.patch.object(client, "run", return_value=b"") as run,
                self.assertRaisesRegex(ProtocolError, "created no file"),
            ):
                client.pull("/dev/mtd4", destination, timeout=7)
        run.assert_called_once_with(
            "pull", "/dev/mtd4", str(destination), timeout=7
        )

    def test_wait_for_device_retries_closed_and_offline_until_online(self):
        client = AdbClient()
        states = [
            ProtocolError("ADB device is not usable (error: closed)"),
            AdbUnavailable("ADB camera is not online (error: device offline)"),
            None,
        ]
        with (
            mock.patch.object(client, "ensure_available", side_effect=states) as check,
            mock.patch("cc2flash.adb_backup.time.sleep") as sleep,
        ):
            client.wait_for_device(timeout=30)
        self.assertEqual(check.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_wait_for_device_does_not_retry_unrelated_adb_failure(self):
        client = AdbClient()
        with (
            mock.patch.object(
                client,
                "ensure_available",
                side_effect=ProtocolError("ADB device is not usable (unauthorized)"),
            ) as check,
            mock.patch("cc2flash.adb_backup.time.sleep") as sleep,
            self.assertRaisesRegex(ProtocolError, "unauthorized"),
        ):
            client.wait_for_device(timeout=30)
        check.assert_called_once()
        self.assertGreater(check.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(check.call_args.kwargs["timeout"], 30)
        sleep.assert_not_called()

    def test_wait_for_device_does_not_retry_adb_subprocess_timeout(self):
        client = AdbClient()
        with (
            mock.patch.object(
                client,
                "ensure_available",
                side_effect=ProtocolError("ADB availability check timed out: adb get-state"),
            ) as check,
            mock.patch("cc2flash.adb_backup.time.sleep") as sleep,
            self.assertRaisesRegex(ProtocolError, "availability check timed out"),
        ):
            client.wait_for_device(timeout=30)
        check.assert_called_once()
        sleep.assert_not_called()

    def test_wait_for_device_caps_get_state_at_remaining_deadline(self):
        client = AdbClient()
        offline = SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=b"error: device offline\n",
        )
        with (
            mock.patch(
                "cc2flash.adb_backup.subprocess.run", return_value=offline
            ) as run,
            mock.patch(
                "cc2flash.adb_backup.time.monotonic",
                side_effect=[100.0, 100.25, 101.0],
            ),
            mock.patch("cc2flash.adb_backup.time.sleep") as sleep,
            self.assertRaisesRegex(ProtocolError, "timed out waiting for ADB"),
        ):
            client.wait_for_device(timeout=1)
        self.assertEqual(run.call_args.kwargs["timeout"], 0.75)
        sleep.assert_not_called()

    def test_wait_for_device_timeout_reports_last_transition_state(self):
        client = AdbClient()
        with (
            mock.patch.object(
                client,
                "ensure_available",
                side_effect=AdbUnavailable("ADB camera is not online (offline)"),
            ),
            mock.patch(
                "cc2flash.adb_backup.time.monotonic",
                side_effect=[10.0, 10.25, 11.0],
            ),
            mock.patch("cc2flash.adb_backup.time.sleep") as sleep,
            self.assertRaisesRegex(ProtocolError, "last state:.*offline"),
        ):
            client.wait_for_device(timeout=1)
        sleep.assert_not_called()

    def test_adb_ambiguity_does_not_enable_bootstrap(self):
        result = SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=b"error: more than one device/emulator\n",
        )
        with mock.patch("cc2flash.adb_backup.subprocess.run", return_value=result):
            with self.assertRaisesRegex(ProtocolError, "not usable") as raised:
                AdbClient().ensure_available()
        self.assertNotIsInstance(raised.exception, AdbUnavailable)


class HidStateMachineTests(unittest.TestCase):
    def test_adb_startup_upload_state_machine(self):
        class FakeHandle:
            instance = None

            def __init__(self, vid, pid):
                self.vid_pid = (vid, pid)
                self.writes = []
                FakeHandle.instance = self

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def write_exact(self, report):
                self.writes.append(report)

            def read(self, size, timeout):
                self.assert_read = (size, timeout)
                request = parse_normal_report(self.writes[-1])
                return normal_response(request.command)

        with mock.patch.object(hid_transport, "HidHandle", FakeHandle):
            hid_transport.install_adb_startup()

        frames = [parse_normal_report(item) for item in FakeHandle.instance.writes]
        self.assertEqual([item.command for item in frames], [0x3000, 0x3110, 0x3200, 0x3300])
        self.assertEqual(frames[1].payload, b"/etc/conf.d/system.sh")
        self.assertEqual(frames[2].payload, b"/bin/adbd &")
        self.assertEqual(frames[2].frame_type, 2)
        self.assertEqual(frames[2].status, 0)

    def test_adb_startup_upload_rejects_device_error(self):
        replies = [normal_response(0x3000), normal_response(0x3110, status=1)]

        class FakeHandle:
            def __init__(self, _vid, _pid):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def write_exact(self, _report):
                pass

            def read(self, _size, _timeout):
                return replies.pop(0)

        with mock.patch.object(hid_transport, "HidHandle", FakeHandle):
            with self.assertRaisesRegex(ProtocolError, "status=1"):
                hid_transport.install_adb_startup()

    def test_temporary_adb_upload_command_expects_commit_failure(self):
        replies = [
            normal_response(0x3000),
            normal_response(0x3110),
            normal_response(0x3200),
            normal_response(0x3300, status=1),
        ]

        class FakeHandle:
            instance = None

            def __init__(self, vid, pid):
                self.vid_pid = (vid, pid)
                self.writes = []
                FakeHandle.instance = self

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def write_exact(self, report):
                self.writes.append(report)

            def read(self, _size, _timeout):
                return replies.pop(0)

        with mock.patch.object(hid_transport, "HidHandle", FakeHandle):
            hid_transport.start_adb_through_upload_command()

        self.assertFalse(replies)
        frames = [parse_normal_report(item) for item in FakeHandle.instance.writes]
        self.assertEqual([item.command for item in frames], [0x3000, 0x3110, 0x3200, 0x3300])
        self.assertEqual(
            frames[1].payload,
            b"/tmp/.cc2flash-adbd-bootstrap;/bin/adbd&",
        )
        self.assertEqual((frames[2].payload, frames[2].frame_type), (b"\n", 2))

    def test_temporary_adb_upload_command_tolerates_commit_disconnect_only(self):
        class FakeHandle:
            def __init__(self, _vid, _pid):
                self.writes = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def write_exact(self, report):
                self.writes.append(report)

            def read(self, _size, _timeout):
                request = parse_normal_report(self.writes[-1])
                if request.command == 0x3300:
                    raise OSError("USB re-enumerated")
                return normal_response(request.command)

        with mock.patch.object(hid_transport, "HidHandle", FakeHandle):
            hid_transport.start_adb_through_upload_command()

        class EarlyFailureHandle(FakeHandle):
            def read(self, _size, _timeout):
                request = parse_normal_report(self.writes[-1])
                if request.command == 0x3110:
                    raise OSError("target exchange failed")
                return normal_response(request.command)

        with mock.patch.object(hid_transport, "HidHandle", EarlyFailureHandle):
            with self.assertRaisesRegex(OSError, "target exchange"):
                hid_transport.start_adb_through_upload_command()

    def test_small_restore_state_machine(self):
        image = bytes(range(256)) * 2
        blob, plan = build_update_blob(image)
        replies = [
            b"\x01" + response_frame(2, b"\0" * 4).ljust(3071, b"\0"),
            b"\x01" + response_frame(5, b"\x01").ljust(3071, b"\0"),
        ]

        class FakeHandle:
            instance = None

            def __init__(self, _vid, _pid):
                self.writes = []
                FakeHandle.instance = self

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def write_exact(self, report):
                self.writes.append(report)

            def read(self, _size, _timeout):
                return replies.pop(0)

        progress = []
        with mock.patch.object(hid_transport, "HidHandle", FakeHandle):
            hid_transport.restore_blob(blob, plan, progress=lambda done, total: progress.append((done, total)))
        self.assertFalse(replies)
        self.assertEqual(len(FakeHandle.instance.writes), 2)
        self.assertTrue(all(len(report) == BOOT_REPORT_SIZE for report in FakeHandle.instance.writes))
        self.assertEqual(progress, [(1, 1)])

    def test_multi_packet_restore_uses_next_expected_ack(self):
        image = bytes(range(256)) * 16
        blob, plan = build_update_blob(image)
        self.assertEqual(plan.packet_count, 2)
        replies = [
            b"\x01" + response_frame(2, struct.pack("<I", 0)).ljust(3071, b"\0"),
            b"\x01" + response_frame(2, struct.pack("<I", 1)).ljust(3071, b"\0"),
            b"\x01" + response_frame(5, b"\x01").ljust(3071, b"\0"),
        ]

        class FakeHandle:
            def __init__(self, _vid, _pid):
                self.writes = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def write_exact(self, report):
                self.writes.append(report)

            def read(self, _size, _timeout):
                return replies.pop(0)

        with mock.patch.object(hid_transport, "HidHandle", FakeHandle):
            hid_transport.restore_blob(blob, plan)
        self.assertFalse(replies)


class CliAdbWorkflowTests(unittest.TestCase):
    @staticmethod
    def _manifest(boot_hash=KNOWN_BOOTLOADER_SHA256, *, known=True):
        return {
            "size": 5,
            "sha256": hashlib.sha256(b"image").hexdigest(),
            "md5": hashlib.md5(b"image").hexdigest(),
            "read_passes": 3,
            "bootloader": {
                "sha256": boot_hash,
                "known_reference": known,
            },
        }

    def test_backup_offline_is_strictly_read_only_and_instructs(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(
                cli, "acquire_stable", side_effect=AdbUnavailable("device offline")
            ),
            mock.patch.object(cli, "start_adb_through_upload_command") as start,
            mock.patch.object(cli, "install_adb_startup") as install,
            mock.patch.object(cli, "save_backup") as save,
            redirect_stdout(io.StringIO()),
            redirect_stderr(stderr),
        ):
            status = cli.main(["backup", "backup.zip"])
        self.assertEqual(status, 2)
        start.assert_not_called()
        install.assert_not_called()
        save.assert_not_called()
        error = stderr.getvalue()
        self.assertIn("strictly read-only", error)
        self.assertIn("cc2flash start-adb", error)
        self.assertIn("cc2flash install-adb-startup", error)

    def test_plan_restore_can_prove_preserved_backup_compatibility(self):
        image = b"replacement image"
        plan = SimpleNamespace(
            flash_offset=0,
            transfer_size=len(image),
            packet_count=1,
            packet_payload_size=1024,
        )
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "replacement.bin"
            image_path.write_bytes(image)
            args = SimpleNamespace(
                image=str(image_path),
                backup="backup.zip",
            )
            output = io.StringIO()
            with (
                mock.patch.object(cli, "validate_full_restore_image"),
                mock.patch.object(
                    cli,
                    "load_preserved_backup",
                    return_value=(image, {"sha256": "preserved-hash"}),
                ),
                mock.patch.object(
                    cli, "validate_replacement_against_backup"
                ) as compatible,
                mock.patch.object(
                    cli, "build_update_blob", return_value=(b"blob", plan)
                ),
                redirect_stdout(output),
            ):
                status = cli.command_plan(args)
        self.assertEqual(status, 0)
        compatible.assert_called_once_with(image, image)
        result = json.loads(output.getvalue())
        self.assertTrue(result["preserved_backup_compatible"])
        self.assertEqual(result["preserved_backup_sha256"], "preserved-hash")

    def test_restore_refuses_wrong_unit_before_opening_usb(self):
        image = b"replacement image"
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "replacement.bin"
            image_path.write_bytes(image)
            args = SimpleNamespace(
                image=str(image_path),
                backup="backup.zip",
                yes=True,
                enumeration_timeout=30,
                reboot_timeout=180,
                no_post_verify=True,
                adb_timeout=60,
                adb="adb",
                serial=None,
            )
            with (
                mock.patch.object(
                    cli,
                    "load_preserved_backup",
                    return_value=(b"preserved image", {"sha256": "preserved-hash"}),
                ),
                mock.patch.object(cli, "validate_full_restore_image"),
                mock.patch.object(
                    cli,
                    "validate_replacement_against_backup",
                    side_effect=ProtocolError("wrong-unit image"),
                ),
                mock.patch.object(cli, "enter_bootloader") as enter,
                self.assertRaisesRegex(ProtocolError, "wrong-unit"),
            ):
                cli.command_restore(args)
        enter.assert_not_called()

    def test_backup_rejects_non_zip_output_before_camera_read(self):
        with (
            mock.patch.object(cli, "acquire_stable") as acquire,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = cli.main(["backup", "backup.bin"])
        self.assertEqual(status, 2)
        acquire.assert_not_called()

    def test_start_adb_rejects_nonpositive_or_nonfinite_timeout_before_hid(self):
        for value in ("0", "-1", "nan", "inf", "-inf"):
            with self.subTest(value=value):
                with (
                    mock.patch.object(
                        cli, "start_adb_through_upload_command"
                    ) as start,
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    cli.main(["start-adb", "--timeout", value])
                self.assertEqual(raised.exception.code, 2)
                start.assert_not_called()

    def test_start_adb_is_separate_and_validates_root(self):
        fake_adb = mock.Mock()
        fake_adb.ensure_available.side_effect = AdbUnavailable("device offline")
        fake_adb.identity_and_partitions.return_value = ("uid=0(root)", mock.sentinel.parts)
        with (
            mock.patch.object(cli, "_adb", return_value=fake_adb),
            mock.patch.object(cli, "start_adb_through_upload_command") as start,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = cli.main(["start-adb", "--timeout", "7"])
        self.assertEqual(status, 0)
        start.assert_called_once_with()
        fake_adb.wait_for_device.assert_called_once_with(timeout=7.0)
        fake_adb.identity_and_partitions.assert_called_once_with()

    def test_start_adb_online_does_not_send_hid(self):
        fake_adb = mock.Mock()
        fake_adb.identity_and_partitions.return_value = ("uid=0(root)", mock.sentinel.parts)
        with (
            mock.patch.object(cli, "_adb", return_value=fake_adb),
            mock.patch.object(cli, "start_adb_through_upload_command") as start,
            redirect_stdout(io.StringIO()),
        ):
            status = cli.main(["start-adb"])
        self.assertEqual(status, 0)
        start.assert_not_called()

    def test_start_adb_does_not_mask_other_adb_errors(self):
        fake_adb = mock.Mock()
        fake_adb.ensure_available.side_effect = ProtocolError("unauthorized")
        with (
            mock.patch.object(cli, "_adb", return_value=fake_adb),
            mock.patch.object(cli, "start_adb_through_upload_command") as start,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = cli.main(["start-adb"])
        self.assertEqual(status, 2)
        start.assert_not_called()

    def test_interactive_install_adb_startup_requires_exact_phrase(self):
        fake_adb = mock.Mock()
        fake_adb.ensure_available.side_effect = AdbUnavailable("device offline")
        interactive_stdin = SimpleNamespace(isatty=lambda: True)
        with (
            mock.patch.object(cli, "_adb", return_value=fake_adb),
            mock.patch.object(cli, "install_adb_startup") as install,
            mock.patch.object(sys, "stdin", interactive_stdin),
            mock.patch("builtins.input", return_value="ENABLE-ADB"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = cli.main(["install-adb-startup"])
        self.assertEqual(status, 3)
        install.assert_called_once_with()

    def test_install_confirmation_mismatch_does_not_write(self):
        fake_adb = mock.Mock()
        fake_adb.ensure_available.side_effect = AdbUnavailable("device offline")
        interactive_stdin = SimpleNamespace(isatty=lambda: True)
        with (
            mock.patch.object(cli, "_adb", return_value=fake_adb),
            mock.patch.object(cli, "install_adb_startup") as install,
            mock.patch.object(sys, "stdin", interactive_stdin),
            mock.patch("builtins.input", return_value="no"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = cli.main(["install-adb-startup"])
        self.assertEqual(status, 2)
        install.assert_not_called()

    def test_noninteractive_install_requires_yes(self):
        fake_adb = mock.Mock()
        fake_adb.ensure_available.side_effect = AdbUnavailable("device offline")
        with (
            mock.patch.object(cli, "_adb", return_value=fake_adb),
            mock.patch.object(cli, "install_adb_startup") as install,
            mock.patch.object(sys, "stdin", io.StringIO()),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = cli.main(["install-adb-startup"])
        self.assertEqual(status, 2)
        install.assert_not_called()

    def test_explicit_install_requires_restart(self):
        fake_adb = mock.Mock()
        fake_adb.ensure_available.side_effect = AdbUnavailable("device offline")
        stdout = io.StringIO()
        with (
            mock.patch.object(cli, "_adb", return_value=fake_adb),
            mock.patch.object(cli, "install_adb_startup") as install,
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            status = cli.main(["install-adb-startup", "--yes"])
        self.assertEqual(status, 3)
        install.assert_called_once_with()
        self.assertIn("restart", stdout.getvalue().casefold())

    def test_install_when_adb_online_does_not_write(self):
        fake_adb = mock.Mock()
        with (
            mock.patch.object(cli, "_adb", return_value=fake_adb),
            mock.patch.object(cli, "install_adb_startup") as install,
            redirect_stdout(io.StringIO()),
        ):
            status = cli.main(["install-adb-startup", "--yes"])
        self.assertEqual(status, 0)
        install.assert_not_called()

    def test_known_bootloader_is_accepted_after_stable_acquisition(self):
        image = b"image"
        manifest = self._manifest()
        with (
            mock.patch.object(cli, "acquire_stable", return_value=(image, manifest)) as acquire,
            mock.patch.object(
                cli, "save_backup", return_value=Path("backup.zip")
            ) as save,
            redirect_stdout(io.StringIO()),
        ):
            status = cli.main(["backup", "backup.zip"])
        self.assertEqual(status, 0)
        acquire.assert_called_once()
        self.assertEqual(manifest["bootloader"]["acceptance"], "known-reference")
        save.assert_called_once_with(Path("backup.zip"), image, manifest)

    def test_unknown_bootloader_failure_prints_exact_hash_after_reads(self):
        image = b"image"
        observed = "12" * 32
        manifest = self._manifest(observed, known=False)
        stderr = io.StringIO()
        with (
            mock.patch.object(cli, "acquire_stable", return_value=(image, manifest)) as acquire,
            mock.patch.object(cli, "save_backup") as save,
            redirect_stdout(io.StringIO()),
            redirect_stderr(stderr),
        ):
            status = cli.main(["backup", "backup.zip"])
        self.assertEqual(status, 2)
        acquire.assert_called_once()
        save.assert_not_called()
        error = stderr.getvalue()
        self.assertIn(observed, error)
        self.assertIn(f"--accept-bootloader-hash {observed}", error)

    def test_exact_unknown_bootloader_hash_can_be_accepted(self):
        image = b"image"
        observed = "ab" * 32
        manifest = self._manifest(observed, known=False)
        with (
            mock.patch.object(cli, "acquire_stable", return_value=(image, manifest)),
            mock.patch.object(
                cli, "save_backup", return_value=Path("backup.zip")
            ) as save,
            redirect_stdout(io.StringIO()),
        ):
            status = cli.main(
                ["backup", "backup.zip", "--accept-bootloader-hash", observed]
            )
        self.assertEqual(status, 0)
        self.assertEqual(manifest["bootloader"]["acceptance"], "explicit-hash")
        save.assert_called_once_with(Path("backup.zip"), image, manifest)

    def test_wrong_bootloader_override_prints_observed_and_publishes_nothing(self):
        image = b"image"
        observed = "ab" * 32
        supplied = "cd" * 32
        manifest = self._manifest(observed, known=False)
        stderr = io.StringIO()
        with (
            mock.patch.object(cli, "acquire_stable", return_value=(image, manifest)),
            mock.patch.object(cli, "save_backup") as save,
            redirect_stdout(io.StringIO()),
            redirect_stderr(stderr),
        ):
            status = cli.main(
                ["backup", "backup.zip", "--accept-bootloader-hash", supplied]
            )
        self.assertEqual(status, 2)
        save.assert_not_called()
        self.assertIn(observed, stderr.getvalue())

    def test_restore_adb_timeout_only_bounds_availability_wait(self):
        image = b"replacement image"
        fake_adb = mock.Mock()
        plan = mock.sentinel.plan
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "replacement.bin"
            image_path.write_bytes(image)
            args = SimpleNamespace(
                image=str(image_path),
                backup="backup.zip",
                yes=True,
                enumeration_timeout=30,
                reboot_timeout=180,
                no_post_verify=False,
                adb_timeout=7,
                adb="adb",
                serial=None,
            )
            with (
                mock.patch.object(
                    cli,
                    "load_preserved_backup",
                    return_value=(image, {"sha256": "different"}),
                ),
                mock.patch.object(cli, "validate_full_restore_image"),
                mock.patch.object(cli, "validate_replacement_against_backup"),
                mock.patch.object(
                    cli, "build_update_blob", return_value=(b"blob", plan)
                ),
                mock.patch.object(cli, "enter_bootloader"),
                mock.patch.object(cli, "wait_for_hid"),
                mock.patch.object(cli, "restore_blob"),
                mock.patch.object(cli, "_adb", return_value=fake_adb),
                mock.patch.object(
                    cli, "acquire_stable", return_value=(image, {})
                ) as acquire,
                redirect_stdout(io.StringIO()),
            ):
                status = cli.command_restore(args)
        self.assertEqual(status, 0)
        fake_adb.wait_for_device.assert_called_once_with(timeout=7)
        acquire.assert_called_once_with(fake_adb, progress=cli._read_progress)


if __name__ == "__main__":
    unittest.main()
