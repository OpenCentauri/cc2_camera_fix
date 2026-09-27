"""Unified CLI contracts; all hardware calls are mocked."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from cc2camera import cli, image
from cc2camera.protocol import ProtocolError


class CliTests(unittest.TestCase):
    def test_command_name_in_help_and_errors(self):
        self.assertEqual(cli.parser().prog, "cc2camera")
        self.assertIn("usage: cc2camera ", cli.parser().format_help())
        with mock.patch.object(cli, "_adb") as adb, redirect_stderr(io.StringIO()) as error:
            self.assertEqual(cli.main(["restore", "missing.bin", "--backup", "missing.zip", "--dry-run"]), 2)
        self.assertIn("cc2camera: error:", error.getvalue())
        adb.assert_not_called()

    def test_suggested_python_invocation_uses_camera_package(self):
        from cc2camera import display
        with mock.patch.object(display.sys, "frozen", False, create=True):
            self.assertIn("-m cc2camera", display.invocation("backup", "backup.zip"))

    def test_exact_command_surface(self):
        import argparse
        action = next(a for a in cli.parser()._actions if isinstance(a, argparse._SubParsersAction))
        self.assertEqual(set(action.choices), {
            "devices", "device-info", "identify-camera", "start-adb", "install-adb-startup",
            "backup", "inspect-image", "build-image", "restore", "install-erase-fix",
        })

    def test_identify_camera_is_read_only_and_reports_known_family(self):
        fingerprint = cli.stream_identify.JpegFingerprint(
            640, 360, ((1, 2, 2), (2, 1, 1), (3, 1, 1)),
            ("DQT", "DQT", "SOF0", "DHT", "DHT", "DHT", "DHT", "SOS"),
            "13660d69eacf5a054bdf3d88d8aead55e5857f704b6f3d9fa098ca93ecf9c44b",
            None, None, 0,
        )
        result = cli.stream_identify.CameraIdentification(
            "EF-S7-V1.0.30B", fingerprint, 3
        )
        with mock.patch.object(cli.stream_identify, "identify_camera_stream", return_value=result) as identify, mock.patch.object(cli, "_adb") as adb, mock.patch.object(cli, "expected_devices") as hid, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["identify-camera", "printer.local"]), 0)
        identify.assert_called_once_with("printer.local", timeout=5.0)
        adb.assert_not_called()
        hid.assert_not_called()
        report = output.getvalue()
        self.assertIn("EF-S7-V1.0.30B", report)
        self.assertIn("0226, 0526 and 1526", report)
        self.assertIn("4025", report)
        self.assertIn("stream cannot distinguish", report)
        self.assertIn("read-only family-identification aid", report)
        self.assertNotIn("failure applies to the 30B family", report)

    def test_identify_camera_refuses_unknown_signature(self):
        fingerprint = cli.stream_identify.JpegFingerprint(
            1, 1, (), (), "0" * 64, None, None, 0
        )
        result = cli.stream_identify.CameraIdentification(None, fingerprint, 3)
        with mock.patch.object(cli.stream_identify, "identify_camera_stream", return_value=result), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(cli.main(["identify-camera", "printer.local"]), 2)
        self.assertIn("not recognized", error.getvalue())

    def test_image_defaults_and_repeatable_reads(self):
        args = cli.parser().parse_args(["build-image", "one.bin", "--confirm-read", "two.bin", "--confirm-read", "three.bin"])
        self.assertEqual(args.config_mode, "serial-only")
        self.assertEqual(args.confirm_read, ["two.bin", "three.bin"])
        self.assertFalse(args.allow_fewer_reads)

    def test_removed_options_are_rejected(self):
        commands = [
            ["build-image", "input.bin", "--keep-config"],
            ["build-image", "input.bin", "--overwrite"],
            ["build-image", "input.bin", "--config-mode", "unchanged"],
            ["restore", "input.bin", "--backup", "backup.zip", "--yes"],
            ["restore", "input.bin", "--backup", "backup.zip", "--no-post-verify"],
            ["restore", "input.bin", "--dry-run"],
            ["self-test"], ["verify-readback", "a", "b"], ["plan-restore", "a"],
        ]
        for argv in commands:
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(argv)

    def test_dry_run_accepts_hardware_options_without_transport(self):
        for option, value in [
            ("--adb", "adb"), ("--serial", "TEST"),
            ("--bootloader-timeout", "30"), ("--reboot-timeout", "180"),
            ("--adb-timeout", "60"),
            ("--bootloader-t", "31"),
        ]:
            for suffix in ([option, value], [option + "=" + value]):
                with self.subTest(suffix=suffix), mock.patch.object(cli, "command_plan", return_value=0) as plan, mock.patch.object(cli, "_adb", side_effect=AssertionError("hardware accessed")):
                    self.assertEqual(cli.main(["restore", "image", "--backup", "backup.zip", "--dry-run", *suffix]), 0)
                    plan.assert_called_once()

    def test_dry_run_validates_every_local_gate_without_transport(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory) / "image.bin"
            path.write_bytes(b"image")
            events = []
            stack.enter_context(mock.patch.object(cli, "load_preserved_backup", return_value=(b"backup", {"sha256": "hash"})))
            for name in ("validate_full_restore_image", "validate_replacement_against_backup", "validate_preparation_image"):
                stack.enter_context(mock.patch.object(cli, name, side_effect=lambda *a, n=name: events.append(n)))
            plan = mock.Mock(flash_offset=0, transfer_size=10, packet_count=1, packet_payload_size=3060)
            stack.enter_context(mock.patch.object(cli, "build_update_blob", return_value=(b"blob", plan)))
            guards = [stack.enter_context(mock.patch.object(cli, name, side_effect=AssertionError("hardware accessed")))
                      for name in ("_adb", "expected_devices", "prepare_restore", "enter_bootloader", "restore_blob", "_confirm", "start_adb_through_upload_command")]
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            self.assertEqual(cli.main(["restore", str(path), "--backup", "backup.zip", "--dry-run", "--adb", "unused-adb", "--serial", "TEST", "--bootloader-timeout", "31", "--reboot-timeout", "181", "--adb-timeout", "61"]), 0)
            self.assertEqual(events, ["validate_full_restore_image", "validate_replacement_against_backup", "validate_preparation_image"])
            self.assertFalse(json.loads(output.getvalue())["writes_performed"])
            for guard in guards:
                guard.assert_not_called()

    def test_bad_dry_run_before_hardware(self):
        with mock.patch.object(cli, "_adb") as adb, redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["restore", "missing.bin", "--backup", "missing.zip", "--dry-run"]), 2)
        adb.assert_not_called()

    def test_write_confirmation_is_required(self):
        for answer in ("", "yes", "RESTORE-OTHER"):
            with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", return_value=answer), redirect_stdout(io.StringIO()), self.assertRaises(ProtocolError):
                cli._confirm(Path("image"), {"sha256": "hash"}, Path("backup"))
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=False), self.assertRaises(ProtocolError):
            cli._confirm(Path("image"), {"sha256": "hash"}, Path("backup"))

    def test_fewer_reads_does_not_bypass_image_validation(self):
        with mock.patch.object(image, "read_image_source", return_value=(b"bad", {"image_member": None})), mock.patch.object(image, "analyze_image", side_effect=image.ValidationError("unsupported firmware")), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["build-image", "input", "--allow-fewer-reads"]), 2)

    def test_every_raw_image_requires_read_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "existing"
            output.mkdir()
            sentinel = output / "unrelated.txt"
            sentinel.write_text("keep me")
            source = {"image_member": None, "evidenced_identical_reads": 1}
            # A legacy whole-image reference marker must not relax the read gate.
            analysis = {"exact_reference": "former-reference-dump"}
            with mock.patch.object(image, "read_image_source", return_value=(b"image", source)), mock.patch.object(image, "analyze_image", return_value=analysis):
                kwargs = dict(confirmation_paths=[], config_mode="serial-only",
                              wipe_unknown_config=False, show_serial=False)
                with self.assertRaisesRegex(image.ValidationError, "higher risk"):
                    image.build_recovery(root / "input", output, allow_fewer_reads=False, **kwargs)
                # The explicit opt-out passes the read gate, but cannot overwrite.
                with self.assertRaises(FileExistsError):
                    image.build_recovery(root / "input", output, allow_fewer_reads=True, **kwargs)
            self.assertEqual(sentinel.read_text(), "keep me")
