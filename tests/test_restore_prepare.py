from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cc2flash import cli, restore_prepare as prep
from cc2flash.adb_backup import AdbClient, AdbUnavailable, EXPECTED_PARTITIONS, MtdPartition
from cc2flash.protocol import FLASH_SIZE, ProtocolError


class PreparationTests(unittest.TestCase):
    def setUp(self):
        # Synthetic data only. Patch the reference digest, not the validator.
        self.image = bytes(FLASH_SIZE)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(
            prep, "KERNEL_SHA256", hashlib.sha256(self.image[0x40000:0x190000]).hexdigest()
        ))
        self.acquire = self.stack.enter_context(mock.patch.object(
            prep, "acquire_stable", return_value=(self.image, {})
        ))
        self.hid = self.stack.enter_context(mock.patch.object(
            prep, "expected_devices", return_value=[{
                "vendor_id": prep.NORMAL_VID, "product_id": prep.NORMAL_PID,
                "serial_number": "TEST-CAMERA",
            }]
        ))
        self.adb = mock.Mock(spec=AdbClient)
        self.adb.serial = "TEST-CAMERA"
        self.adb.run.return_value = b"List of devices attached\nTEST-CAMERA\tdevice\n"
        self.parts = [MtdPartition(i, size, 0x4000, name)
                      for i, (name, size) in enumerate(EXPECTED_PARTITIONS)]
        self.adb.identity_and_partitions.return_value = ("uid=0(root)", self.parts)
        self.pointer = 0x818B4800
        self.value = 0x4000
        self.words = dict(prep.INSTRUCTIONS)
        self.symbols = "\n".join(f"{a:08x} T {n}" for n, a in prep.SYMBOLS.items()).encode()
        self.commands = []
        self.adb.shell.side_effect = self.shell

    def shell(self, *args):
        if args == ("cat", "/proc/kallsyms"):
            return self.symbols
        if args == ("sync && echo CC2_SYNC_OK",):
            return b"CC2_SYNC_OK\r\n"
        if args[0] == "busybox":
            address = int(args[2], 16)
            if address == prep.POINTER_PHYSICAL:
                value = self.pointer
            elif address == prep._field_address(self.pointer):
                value = self.value
            else:
                value = self.words[address]
            return f"0x{value:08X}\r\n".encode()
        self.commands.append(args[0])
        self.value = 0x1000
        return b"0x00001000\r\nCC2_ERASE_READY\r\n"

    def run_prepare(self):
        prep.prepare_restore(self.adb, self.image)

    def assert_refused_without_write(self):
        with self.assertRaises(ProtocolError):
            self.run_prepare()
        self.assertEqual(self.commands, [])

    def test_success_and_dynamic_heap_address(self):
        self.pointer = 0x819C5800
        self.run_prepare()
        self.acquire.assert_called_once_with(self.adb, progress=None)
        self.assertEqual(len(self.commands), 1)
        self.assertIn("busybox devmem 0x019c5810 32 0x00001000", self.commands[0])
        self.assertNotIn("018b4810", self.commands[0])
        self.assertNotIn("flash_erase", self.commands[0])
        self.assertNotIn("kill", self.commands[0])

    def test_already_patched_only_verifies(self):
        self.value = 0x1000
        self.run_prepare()
        self.assertNotIn("32 0x00001000", self.commands[0])

    def test_bad_local_kernel_before_device_access(self):
        with self.assertRaises(ProtocolError):
            prep.prepare_restore(self.adb, b"bad")
        self.adb.ensure_available.assert_not_called()
        self.hid.assert_not_called()

    def test_offline_no_hid(self):
        self.adb.ensure_available.side_effect = AdbUnavailable("offline")
        self.assert_refused_without_write()
        self.hid.assert_not_called()

    def test_multiple_adb_devices(self):
        self.adb.run.return_value += b"OTHER\tdevice\n"
        self.assert_refused_without_write()
        self.acquire.assert_not_called()

    def test_wrong_hid_serial(self):
        self.hid.return_value[0]["serial_number"] = "OTHER"
        self.assert_refused_without_write()

    def test_multiple_hid_devices(self):
        self.hid.return_value *= 2
        self.assert_refused_without_write()

    def test_wrong_selected_serial(self):
        self.adb.serial = "OTHER"
        self.assert_refused_without_write()

    def test_wrong_unit_and_live_kernel(self):
        for offset in (0x7D2010, 0x40000):
            with self.subTest(offset=offset):
                data = bytearray(self.image)
                data[offset] = 1
                self.acquire.return_value = (bytes(data), {})
                self.assert_refused_without_write()

    def test_unstable_reads(self):
        self.acquire.side_effect = ProtocolError("unstable flash")
        self.assert_refused_without_write()

    def test_unknown_geometry(self):
        self.parts[0] = MtdPartition(0, 0x40000, 0x1000, "boot")
        self.assert_refused_without_write()

    def test_missing_or_duplicate_symbols(self):
        original = self.symbols
        for symbols in (b"", original + b"\n" + original):
            with self.subTest(symbols=symbols):
                self.symbols = symbols
                self.assert_refused_without_write()

    def test_wrong_instruction(self):
        self.words[next(iter(self.words))] = 0
        self.assert_refused_without_write()

    def test_bad_pointers(self):
        for pointer in (0, 0x80010000, 0xA18B4800, 0x818B4801, 0x84000000):
            with self.subTest(pointer=pointer):
                self.pointer = pointer
                self.assert_refused_without_write()

    def test_unexpected_erase_size(self):
        self.value = 0x8000
        self.assert_refused_without_write()

    def test_malformed_read_and_sync_failure(self):
        original = self.shell
        for target in ("busybox", "sync && echo CC2_SYNC_OK"):
            with self.subTest(target=target):
                self.adb.shell.side_effect = lambda *a: b"error\n" if a[0] == target else original(*a)
                self.assert_refused_without_write()

    def test_guard_failure_and_timeout_no_retry(self):
        original = self.shell
        for response in (b"", b"0x00004000\nCC2_ERASE_READY\n", ProtocolError("timeout")):
            with self.subTest(response=response):
                calls = []
                def shell(*args):
                    if "CC2_ERASE_READY" in args[0]:
                        calls.append(args)
                        if isinstance(response, Exception):
                            raise response
                        return response
                    return original(*args)
                self.adb.shell.side_effect = shell
                with self.assertRaisesRegex(ProtocolError, "RAM may already be patched"):
                    self.run_prepare()
                self.assertEqual(len(calls), 1)

    def test_pointer_changes_after_write(self):
        original = self.shell
        def shell(*args):
            result = original(*args)
            if "CC2_ERASE_READY" in args[0]:
                self.pointer += 0x1000
            return result
        self.adb.shell.side_effect = shell
        with self.assertRaisesRegex(ProtocolError, "pointer or erase-size"):
            self.run_prepare()


class RestoreOrderingTests(unittest.TestCase):
    def test_confirmation_and_preparation_precede_trigger(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory) / "candidate.bin"
            path.write_bytes(b"candidate")
            args = cli.parser().parse_args([
                "restore", str(path), "--backup", "backup.zip"
            ])
            events = []
            stack.enter_context(mock.patch.object(cli, "load_preserved_backup", return_value=(b"backup", {"sha256": "x"})))
            for name in ("validate_full_restore_image", "validate_replacement_against_backup"):
                stack.enter_context(mock.patch.object(cli, name))
            stack.enter_context(mock.patch.object(cli, "build_update_blob", return_value=(b"blob", None)))
            for name in ("validate_preparation_image", "_confirm", "prepare_restore", "enter_bootloader", "wait_for_hid", "restore_blob"):
                stack.enter_context(mock.patch.object(cli, name, side_effect=lambda *a, _name=name, **k: events.append(_name)))
            stack.enter_context(mock.patch.object(cli, "_adb", return_value=mock.Mock()))
            stack.enter_context(mock.patch.object(cli, "acquire_stable", return_value=(b"candidate", {})))
            stack.enter_context(mock.patch.object(cli, "validate_post_restore_readback", return_value=True))
            stack.enter_context(redirect_stdout(io.StringIO()))
            self.assertEqual(cli.command_restore(args), 0)
            self.assertEqual(events, ["validate_preparation_image", "_confirm", "prepare_restore", "enter_bootloader", "wait_for_hid", "restore_blob", "wait_for_hid"])

    def test_local_kernel_or_confirmation_failure_before_adb(self):
        for failure in ("validate_preparation_image", "_confirm"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                path = Path(directory) / "candidate.bin"
                path.write_bytes(b"candidate")
                args = cli.parser().parse_args(["restore", str(path), "--backup", "backup.zip"])
                stack.enter_context(mock.patch.object(cli, "load_preserved_backup", return_value=(b"backup", {"sha256": "x"})))
                for name in ("validate_full_restore_image", "validate_replacement_against_backup", "validate_preparation_image", "_confirm"):
                    stack.enter_context(mock.patch.object(cli, name, side_effect=ProtocolError("refused") if name == failure else None))
                stack.enter_context(mock.patch.object(cli, "build_update_blob", return_value=(b"blob", None)))
                adb = stack.enter_context(mock.patch.object(cli, "_adb"))
                trigger = stack.enter_context(mock.patch.object(cli, "enter_bootloader"))
                with self.assertRaises(ProtocolError):
                    cli.command_restore(args)
                adb.assert_not_called()
                trigger.assert_not_called()

    def test_preparation_failure_never_triggers_hid(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory) / "candidate.bin"
            path.write_bytes(b"candidate")
            args = cli.parser().parse_args(["restore", str(path), "--backup", "backup.zip"])
            stack.enter_context(mock.patch.object(cli, "load_preserved_backup", return_value=(b"backup", {"sha256": "x"})))
            for name in ("validate_full_restore_image", "validate_replacement_against_backup", "validate_preparation_image", "_confirm"):
                stack.enter_context(mock.patch.object(cli, name))
            stack.enter_context(mock.patch.object(cli, "build_update_blob", return_value=(b"blob", None)))
            prepare = stack.enter_context(mock.patch.object(cli, "prepare_restore", side_effect=ProtocolError("refused")))
            trigger = stack.enter_context(mock.patch.object(cli, "enter_bootloader"))
            transfer = stack.enter_context(mock.patch.object(cli, "restore_blob"))
            stack.enter_context(redirect_stdout(io.StringIO()))
            with self.assertRaisesRegex(ProtocolError, "refused"):
                cli.command_restore(args)
            prepare.assert_called_once()
            trigger.assert_not_called()
            transfer.assert_not_called()
