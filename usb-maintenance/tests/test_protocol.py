from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace

from cc2flash.adb_backup import (
    AdbClient,
    AdbUnavailable,
    EXPECTED_PARTITIONS,
    acquire_twice,
    parse_proc_mtd,
    save_backup,
    validate_partition_map,
    validate_preserved_backup,
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

    def test_two_pass_backup_and_manifest(self):
        parts = parse_proc_mtd(PROC_MTD)
        image = b"".join(
            bytes((index,)) * size for index, (_name, size) in enumerate(EXPECTED_PARTITIONS)
        )

        class FakeAdb:
            serial = "fake"

            def identity_and_partitions(self):
                return "uid=0(root)", parts

            def read_flash_once(self, _parts):
                return image

        acquired, manifest = acquire_twice(FakeAdb())
        self.assertEqual(acquired, image)
        self.assertEqual(manifest["read_passes"], 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.bin"
            manifest_path = save_backup(path, acquired, manifest)
            self.assertTrue(manifest_path.is_file())
            self.assertEqual(validate_preserved_backup(path)["sha256"], manifest["sha256"])
            with self.assertRaisesRegex(ProtocolError, "overwrite"):
                save_backup(path, acquired, manifest)

    def test_two_pass_mismatch_is_rejected(self):
        parts = parse_proc_mtd(PROC_MTD)

        class FakeAdb:
            serial = "fake"
            calls = 0

            def identity_and_partitions(self):
                return "uid=0(root)", parts

            def read_flash_once(self, _parts):
                self.calls += 1
                return bytes((self.calls,)) * FLASH_SIZE

        with self.assertRaisesRegex(ProtocolError, "differ"):
            acquire_twice(FakeAdb())

    def test_adb_absence_is_distinguished_for_bootstrap(self):
        result = SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=b"error: no devices/emulators found\n",
        )
        with mock.patch("cc2flash.adb_backup.subprocess.run", return_value=result):
            with self.assertRaises(AdbUnavailable):
                AdbClient().ensure_available()

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


class CliBootstrapTests(unittest.TestCase):
    def test_interactive_confirmation_installs_startup_hook(self):
        interactive_stdin = SimpleNamespace(isatty=lambda: True)
        with (
            mock.patch.object(
                cli, "acquire_twice", side_effect=AdbUnavailable("no ADB device")
            ),
            mock.patch.object(cli, "install_adb_startup") as install,
            mock.patch.object(sys, "stdin", interactive_stdin),
            mock.patch("builtins.input", return_value="ENABLE-ADB"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = cli.main(["backup", "backup.bin"])
        self.assertEqual(status, 3)
        install.assert_called_once_with()

    def test_interactive_confirmation_mismatch_does_not_write(self):
        interactive_stdin = SimpleNamespace(isatty=lambda: True)
        with (
            mock.patch.object(
                cli, "acquire_twice", side_effect=AdbUnavailable("no ADB device")
            ),
            mock.patch.object(cli, "install_adb_startup") as install,
            mock.patch.object(sys, "stdin", interactive_stdin),
            mock.patch("builtins.input", return_value="no"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = cli.main(["backup", "backup.bin"])
        self.assertEqual(status, 2)
        install.assert_not_called()

    def test_explicit_bootstrap_installs_and_requires_restart(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                cli, "acquire_twice", side_effect=AdbUnavailable("no ADB device")
            ),
            mock.patch.object(cli, "install_adb_startup") as install,
            mock.patch.object(cli, "save_backup") as save,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = cli.main(["backup", "backup.bin", "--bootstrap-adb"])
        self.assertEqual(status, 3)
        install.assert_called_once_with()
        save.assert_not_called()
        self.assertIn("No flash was read", stdout.getvalue())
        self.assertIn("restart", stdout.getvalue().casefold())
        self.assertIn("overwrite /etc/conf.d/system.sh", stderr.getvalue())

    def test_temporary_upload_command_starts_adb_and_continues_backup(self):
        image = b"image"
        manifest = {
            "size": len(image),
            "sha256": hashlib.sha256(image).hexdigest(),
            "md5": hashlib.md5(image).hexdigest(),
        }
        fake_adb = mock.Mock()
        with (
            mock.patch.object(cli, "_adb", return_value=fake_adb),
            mock.patch.object(
                cli,
                "acquire_twice",
                side_effect=[AdbUnavailable("no ADB device"), (image, manifest)],
            ),
            mock.patch.object(cli, "start_adb_through_upload_command") as start,
            mock.patch.object(cli, "install_adb_startup") as persistent,
            mock.patch.object(cli, "save_backup", return_value=Path("backup.bin.json")) as save,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = cli.main(
                [
                    "backup",
                    "backup.bin",
                    "--start-adb-through-upload-command",
                    "--adb-startup-timeout",
                    "7",
                ]
            )
        self.assertEqual(status, 0)
        start.assert_called_once_with()
        persistent.assert_not_called()
        fake_adb.wait_for_device.assert_called_once_with(timeout=7.0)
        save.assert_called_once_with(Path("backup.bin"), image, manifest)

    def test_temporary_upload_command_does_not_mask_other_adb_errors(self):
        with (
            mock.patch.object(
                cli, "acquire_twice", side_effect=ProtocolError("ADB shell is not root")
            ),
            mock.patch.object(cli, "start_adb_through_upload_command") as start,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = cli.main(
                ["backup", "backup.bin", "--start-adb-through-upload-command"]
            )
        self.assertEqual(status, 2)
        start.assert_not_called()

    def test_noninteractive_bootstrap_requires_explicit_option(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                cli, "acquire_twice", side_effect=AdbUnavailable("no ADB device")
            ),
            mock.patch.object(cli, "install_adb_startup") as install,
            mock.patch.object(sys, "stdin", io.StringIO()),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = cli.main(["backup", "backup.bin"])
        self.assertEqual(status, 2)
        install.assert_not_called()
        self.assertIn("--bootstrap-adb", stderr.getvalue())

    def test_other_adb_errors_never_install_startup_hook(self):
        with (
            mock.patch.object(
                cli, "acquire_twice", side_effect=ProtocolError("ADB shell is not root")
            ),
            mock.patch.object(cli, "install_adb_startup") as install,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            status = cli.main(["backup", "backup.bin", "--bootstrap-adb"])
        self.assertEqual(status, 2)
        install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
