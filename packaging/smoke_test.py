"""Exercise an installed/frozen console application without a physical camera."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    executable = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory() as directory:
        def run(*args, expected=0):
            result = subprocess.run(
                [executable, *args], cwd=directory, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != expected:
                raise AssertionError(f"{args}: exit {result.returncode}\n{result.stdout}\n{result.stderr}")
            return result

        run("--version")
        help_text = run("--help").stdout
        for command in ("devices", "device-info", "start-adb", "install-adb-startup",
                        "backup", "inspect-image", "build-image", "restore"):
            assert command in help_text, command
            run(command, "--help")
        # Enumeration is read-only. No device selection, startup or write occurs.
        devices = json.loads(run("devices", "--adb", str(Path(directory) / "missing-adb")).stdout)
        assert "hid_error" not in devices, devices
        assert "ADB executable not found" in devices["adb_error"], devices
        run("restore", "missing.bin", "--backup", "missing.zip", "--dry-run", expected=2)
        Path(directory, "invalid.bin").write_bytes(b"not a supported image")
        failure = run("build-image", "invalid.bin", expected=2)
        assert "cc2flash: error:" in failure.stderr, failure.stderr
        assert not Path(directory, "invalid-cc2-recovery").exists()


if __name__ == "__main__":
    main()
