"""Review regressions; no camera access."""
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cc2flash import cli, image
from cc2flash.adb_backup import AdbUnavailable
from cc2flash.bundle import staged_directory
from cc2flash.protocol import ProtocolError


class StartupTests(unittest.TestCase):
    def test_install_with_online_or_offline_adb(self):
        for online in (True, False):
            with self.subTest(online=online), mock.patch.object(cli, "_adb") as adb, mock.patch.object(cli, "install_adb_startup") as install, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                adb.return_value.identity_and_partitions.return_value = ("root", [])
                if not online:
                    adb.return_value.ensure_available.side_effect = AdbUnavailable("offline")
                self.assertEqual(cli.main(["install-adb-startup", "--yes"]), 0)
                install.assert_called_once_with()

    def test_online_install_still_requires_consent_and_valid_device(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid), mock.patch.object(cli, "_adb") as adb, mock.patch.object(cli, "install_adb_startup") as install, mock.patch.object(cli.sys.stdin, "isatty", return_value=False), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                adb.return_value.identity_and_partitions.return_value = ("root", [])
                if invalid:
                    adb.return_value.identity_and_partitions.side_effect = ProtocolError("unsupported layout")
                self.assertEqual(cli.main(["install-adb-startup"] + (["--yes"] if invalid else [])), 2)
                install.assert_not_called()


class BundleTests(unittest.TestCase):
    def test_builder_failure_does_not_publish_partial_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = {"image_member": None, "evidenced_identical_reads": 3}
            analysis = {"system_state": "stock-unpatched"}
            with mock.patch.object(image, "read_image_source", return_value=(b"image", source)), mock.patch.object(image, "analyze_image", return_value=analysis), mock.patch.object(image, "apply_system_patch", side_effect=OSError("build failed")), self.assertRaises(OSError):
                image.build_recovery(root / "input.bin", root / "recovery", confirmation_paths=[], config_mode="serial-only", wipe_unknown_config=False, show_serial=False, allow_fewer_reads=False)
            self.assertEqual(list(root.iterdir()), [])

    def test_failure_cleans_staging_and_retry_publishes_complete_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "recovery"
            with self.assertRaises(OSError):
                with staged_directory(target) as staging:
                    (staging / "image.bin").write_bytes(b"partial")
                    self.assertFalse(target.exists())
                    raise OSError("disk full")
            self.assertEqual(list(root.iterdir()), [])
            with staged_directory(target) as staging:
                (staging / "image.bin").write_bytes(b"complete")
                (staging / "manifest.txt").write_text("validated")
                self.assertFalse(target.exists())
            self.assertEqual(sorted(p.name for p in target.iterdir()), ["image.bin", "manifest.txt"])
            self.assertEqual(list(root.iterdir()), [target])

    def test_existing_and_concurrently_created_destinations_are_preserved(self):
        for concurrent in (False, True):
            for populated in (False, True):
                with self.subTest(concurrent=concurrent, populated=populated), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    target = root / "recovery"
                    def create_destination():
                        target.mkdir()
                        if populated:
                            (target / "keep.txt").write_text("original")
                    if not concurrent:
                        create_destination()
                    with self.assertRaises(OSError):
                        with staged_directory(target) as staging:
                            (staging / "new.txt").write_text("new")
                            if concurrent:
                                create_destination()
                    self.assertFalse((target / "new.txt").exists())
                    if populated:
                        self.assertEqual((target / "keep.txt").read_text(), "original")
                    self.assertEqual(list(root.iterdir()), [target])

    def test_publication_error_cleans_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "recovery"
            with mock.patch("cc2flash.bundle.rename_new", side_effect=OSError("rename failed")), self.assertRaises(OSError):
                with staged_directory(target) as staging:
                    (staging / "image.bin").write_bytes(b"complete")
            self.assertEqual(list(Path(directory).iterdir()), [])
