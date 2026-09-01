"""Read-only full-flash acquisition over a validated root ADB connection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Callable

from .protocol import FLASH_SIZE, ProtocolError


class AdbUnavailable(ProtocolError):
    """The selected camera has no usable daemon-backed ADB transport yet.

    Stock firmware exposes the USB ADB function before ``adbd`` is started, so
    the camera appears as ``offline`` rather than disappearing from
    ``adb devices``.  Both that exact state and a genuinely absent device are
    eligible for an explicitly guarded HID startup path.
    """


EXPECTED_PARTITIONS = (
    ("boot", 0x040000),
    ("kernel", 0x150000),
    ("root", 0x158000),
    ("system", 0x4E8000),
    ("hwconfig", 0x010000),
    ("config", 0x020000),
)

# Keep these aligned with hardware-recovery/cc2_sig_tool.py.  Three matching
# physical reads are the repository-wide minimum for a non-reference image.
# USB acquisition gets two extra chances because live testing showed JFFS2 may
# still change immediately after the one-shot ADB startup command.
REQUIRED_IDENTICAL_READS = 3
MAX_READ_ATTEMPTS = 5
ReadProgress = Callable[[int, int, int, int], None]

# Full SHA-256 of the 256 KiB ``boot`` MTD partition. It is byte-identical in
# the supplied reference dump and the first independently acquired live ADB
# backup. Config is deliberately excluded because it is device/live-state data.
KNOWN_BOOTLOADER_SHA256 = (
    "5602ec961b4410ccceea0d4910e4fa768c6998bd4ba86143ba50855bdd0b7a54"
)


@dataclass(frozen=True)
class MtdPartition:
    index: int
    size: int
    erase_size: int
    name: str

    @property
    def device(self) -> str:
        return f"/dev/mtd{self.index}"


MTD_LINE = re.compile(
    r'^mtd(?P<index>\d+):\s+(?P<size>[0-9a-fA-F]+)\s+'
    r'(?P<erase>[0-9a-fA-F]+)\s+"(?P<name>[^"]+)"$'
)


def parse_proc_mtd(text: str) -> list[MtdPartition]:
    parts: list[MtdPartition] = []
    for raw in text.replace("\r", "").splitlines():
        match = MTD_LINE.match(raw.strip())
        if match:
            parts.append(
                MtdPartition(
                    int(match["index"]),
                    int(match["size"], 16),
                    int(match["erase"], 16),
                    match["name"],
                )
            )
    parts.sort(key=lambda item: item.index)
    if not parts:
        raise ProtocolError("camera returned no parseable MTD partitions")
    return parts


def validate_partition_map(parts: list[MtdPartition]) -> None:
    if [part.index for part in parts] != list(range(len(parts))):
        raise ProtocolError("MTD indices are not contiguous from mtd0")
    observed_sizes = tuple(part.size for part in parts)
    expected_sizes = tuple(size for _, size in EXPECTED_PARTITIONS)
    if observed_sizes != expected_sizes:
        got = ", ".join(f"0x{size:x}" for size in observed_sizes)
        want = ", ".join(f"0x{size:x}" for size in expected_sizes)
        raise ProtocolError(f"unexpected CC2 partition sizes ({got}); expected {want}")
    for part, (expected_name, _) in zip(parts, EXPECTED_PARTITIONS):
        if part.name.casefold() != expected_name:
            raise ProtocolError(
                f"unexpected mtd{part.index} name {part.name!r}; expected {expected_name!r}"
            )
    if sum(part.size for part in parts) != FLASH_SIZE:
        raise ProtocolError("MTD partitions do not cover exactly 8 MiB")


class AdbClient:
    def __init__(self, executable: str = "adb", serial: str | None = None):
        self.executable = executable
        self.serial = serial

    def _base(self) -> list[str]:
        command = [self.executable]
        if self.serial:
            command += ["-s", self.serial]
        return command

    def run(self, *args: str, timeout: float = 30) -> bytes:
        command = self._base() + list(args)
        try:
            result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise ProtocolError(f"ADB executable not found: {self.executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProtocolError(f"ADB command timed out: {' '.join(command)}") from exc
        if result.returncode:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            raise ProtocolError(f"ADB command failed ({result.returncode}): {stderr}")
        return result.stdout

    def exec_out(self, *remote_args: str, timeout: float = 30) -> bytes:
        """Run ADB's modern raw-output service.

        This remains available to library callers, but the stock CC2 daemon
        closes this service immediately.  Backup acquisition therefore uses
        :meth:`shell` only for text and :meth:`pull` for binary MTD bytes.
        """

        return self.run("exec-out", *remote_args, timeout=timeout)

    def shell(self, *remote_args: str, timeout: float = 30) -> bytes:
        """Run a text-only remote command through the legacy shell service."""

        return self.run("shell", *remote_args, timeout=timeout)

    def pull(self, remote_path: str, local_path: Path, *, timeout: float = 120) -> None:
        """Copy one remote path through ADB's binary-safe sync service.

        Live Windows testing against the stock daemon established that
        ``exec-out`` returns ``error: closed``, while ``adb pull /dev/mtd4``
        produced exactly 65,536 bytes whose local MD5 matched the camera's
        ``md5sum /dev/mtd4``.  Explicit local files also keep binary data out of
        terminal-oriented shell transports that may transform newlines.

        Callers remain responsible for validating the resulting size and for
        choosing a private destination.  The backup path does both.
        """

        self.run("pull", remote_path, str(local_path), timeout=timeout)
        if not local_path.is_file():
            raise ProtocolError(
                f"ADB pull reported success but created no file for {remote_path}"
            )

    def wait_for_device(self, *, timeout: float) -> None:
        """Poll across the normal-HID-to-ADB USB transport transition.

        Live Windows testing established that the successful HID injection
        closes the old composite-device transport.  During that expected
        handoff, ``adb wait-for-device`` may exit immediately with
        ``error: closed`` even though the newly started daemon subsequently
        becomes usable.  Polling ``get-state`` through :meth:`ensure_available`
        avoids treating that one old-transport result as terminal.

        Only three transition states are retried: absent, stock ``offline``,
        and the exact ADB ``error: closed`` result.  Authorization, device
        ambiguity, missing executables, and every other setup error still abort
        immediately.  The caller's deadline bounds the whole transition.
        """

        deadline = time.monotonic() + timeout
        last_state = "ADB has not been checked"
        while True:
            try:
                self.ensure_available()
                return
            except AdbUnavailable as exc:
                last_state = str(exc)
            except ProtocolError as exc:
                # ``ensure_available`` deliberately does not classify a closed
                # transport as generally startup-eligible: doing so before an
                # explicit HID action could trigger an unnecessary device
                # mutation.  It is transient only here, after that action.
                if (
                    "adb device is not usable (error: closed)"
                    not in str(exc).casefold()
                ):
                    raise
                last_state = str(exc)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError(
                    "timed out waiting for ADB to become online; "
                    f"last state: {last_state}"
                )
            time.sleep(min(0.5, remaining))

    def ensure_available(self) -> None:
        """Distinguish an absent device from local ADB/setup errors.

        Only ``AdbUnavailable`` is eligible for a normal-HID startup fallback.
        It covers both a genuinely absent camera and the stock camera's fixed
        pre-daemon ``offline`` state. Missing executables, ambiguous selections,
        authorization errors, and other local failures remain ordinary
        ``ProtocolError`` instances so they cannot cause an unnecessary write.
        """

        command = self._base() + ["get-state"]
        try:
            result = subprocess.run(
                command, capture_output=True, timeout=10, check=False
            )
        except FileNotFoundError as exc:
            raise ProtocolError(f"ADB executable not found: {self.executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProtocolError(f"ADB availability check timed out: {' '.join(command)}") from exc
        state = result.stdout.decode("utf-8", "replace").strip()
        stderr = result.stderr.decode("utf-8", "replace").strip()
        if result.returncode == 0 and state == "device":
            return
        detail = stderr or state or "no device"
        lowered = detail.casefold()
        if (
            "no devices" in lowered
            or ("device" in lowered and "not found" in lowered)
            or (self.serial is None and detail == "no device")
            or "device offline" in lowered
            or detail == "offline"
        ):
            raise AdbUnavailable(f"ADB camera is not online ({detail})")
        raise ProtocolError(f"ADB device is not usable ({detail})")

    def identity_and_partitions(self) -> tuple[str, list[MtdPartition]]:
        self.ensure_available()
        # These outputs are text, so the stock daemon's legacy shell service is
        # appropriate. parse_proc_mtd removes the doubled CR characters seen on
        # Windows before interpreting any sizes or names.
        identity = self.shell("id").decode("utf-8", "replace").strip()
        if "uid=0" not in identity:
            raise ProtocolError("ADB shell is not root; refusing raw MTD access")
        text = self.shell("cat", "/proc/mtd").decode("ascii", "strict")
        parts = parse_proc_mtd(text)
        validate_partition_map(parts)
        return identity, parts

    def read_flash_once(self, parts: list[MtdPartition]) -> bytes:
        image = bytearray()
        # Never route flash bytes through ``adb shell``: older Windows shell
        # transports can alter line endings. Each partition instead goes to a
        # fresh private host file via the ADB sync protocol, is size-checked,
        # appended in validated MTD order, and is removed with the directory.
        with tempfile.TemporaryDirectory(prefix="cc2flash-adb-pull-") as directory:
            temporary_directory = Path(directory)
            for part in parts:
                local_path = temporary_directory / f"mtd{part.index}.bin"
                self.pull(part.device, local_path, timeout=120)
                data = local_path.read_bytes()
                if len(data) != part.size:
                    raise ProtocolError(
                        f"short read from {part.device}: got {len(data)}, "
                        f"expected {part.size}"
                    )
                image.extend(data)
        if len(image) != FLASH_SIZE:
            raise ProtocolError("assembled flash image is not exactly 8 MiB")
        return bytes(image)


def hashes(data: bytes) -> dict[str, str | int]:
    return {
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "md5": hashlib.md5(data).hexdigest(),
    }


def bootloader_reference(image: bytes) -> dict[str, str | int | bool]:
    """Return the observed boot-partition hash and built-in reference signal."""

    boot_size = EXPECTED_PARTITIONS[0][1]
    if len(image) != FLASH_SIZE:
        raise ProtocolError("cannot fingerprint a non-8-MiB flash image")
    observed = hashlib.sha256(image[:boot_size]).hexdigest()
    return {
        "partition": "boot",
        "size": boot_size,
        "sha256": observed,
        "known_sha256": KNOWN_BOOTLOADER_SHA256,
        "known_reference": observed == KNOWN_BOOTLOADER_SHA256,
    }


def acquire_stable(
    adb: AdbClient,
    *,
    progress: ReadProgress | None = None,
) -> tuple[bytes, dict]:
    """Require three consecutive identical full reads within five attempts.

    A three-of-five majority is intentionally insufficient.  Requiring the
    same 8 MiB image three times *consecutively* demonstrates that live JFFS2
    activity settled, rather than accepting an image that merely alternated
    with another state.  Only the immediately preceding image is retained, so
    retries do not accumulate five full dumps in host memory.
    """

    identity, parts = adb.identity_and_partitions()
    previous: bytes | None = None
    accepted: bytes | None = None
    consecutive = 0
    attempts = 0

    for attempts in range(1, MAX_READ_ATTEMPTS + 1):
        current = adb.read_flash_once(parts)
        if previous is not None and current == previous:
            consecutive += 1
        else:
            consecutive = 1
        if progress is not None:
            progress(
                attempts,
                MAX_READ_ATTEMPTS,
                consecutive,
                REQUIRED_IDENTICAL_READS,
            )
        if consecutive >= REQUIRED_IDENTICAL_READS:
            accepted = current
            break
        previous = current

    if accepted is None:
        raise ProtocolError(
            "five complete flash reads did not produce three consecutive "
            "identical images; no backup was accepted or written"
        )
    digest = hashes(accepted)
    manifest = {
        "format": "cc2flash-backup-v2",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "adb_serial": adb.serial,
        "device_identity": identity,
        "read_passes": attempts,
        "required_identical_reads": REQUIRED_IDENTICAL_READS,
        "consecutive_identical_reads": consecutive,
        "maximum_read_attempts": MAX_READ_ATTEMPTS,
        "bootloader": bootloader_reference(accepted),
        **digest,
        "partitions": [asdict(part) for part in parts],
    }
    return accepted, manifest


def acquire_twice(adb: AdbClient) -> tuple[bytes, dict]:
    """Compatibility alias; acquisition now applies the stricter v2 policy."""

    return acquire_stable(adb)


def save_backup(path: Path, image: bytes, manifest: dict) -> Path:
    manifest_path = path.with_name(path.name + ".json")
    if path.exists() or manifest_path.exists():
        raise ProtocolError(f"refusing to overwrite existing backup: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", delete=False) as tmp:
        temp_path = Path(tmp.name)
        tmp.write(image)
        tmp.flush()
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=manifest_path.name + ".",
        delete=False,
    ) as tmp_manifest:
        temp_manifest_path = Path(tmp_manifest.name)
        tmp_manifest.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        tmp_manifest.flush()
    try:
        temp_path.replace(path)
        temp_manifest_path.replace(manifest_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        temp_manifest_path.unlink(missing_ok=True)
        raise
    return manifest_path


def validate_preserved_backup(path: Path) -> dict[str, str | int]:
    if not path.is_file():
        raise ProtocolError(f"preserved backup does not exist: {path}")
    data = path.read_bytes()
    if len(data) != FLASH_SIZE:
        raise ProtocolError("preserved backup is not exactly 8 MiB")
    result = hashes(data)
    manifest_path = path.with_name(path.name + ".json")
    if not manifest_path.is_file():
        raise ProtocolError("preserved backup has no cc2flash JSON manifest")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolError("preserved backup manifest is unreadable") from exc
    if manifest.get("format") != "cc2flash-backup-v2":
        raise ProtocolError(
            "preserved backup requires a v2 manifest with three identical reads"
        )
    for key in ("size", "sha256", "md5"):
        if manifest.get(key) != result[key]:
            raise ProtocolError(f"preserved backup manifest {key} does not match the file")
    expected_bootloader = bootloader_reference(data)
    recorded_bootloader = manifest.get("bootloader")
    if not isinstance(recorded_bootloader, dict):
        raise ProtocolError("preserved backup manifest has no bootloader fingerprint")
    for key, expected in expected_bootloader.items():
        if recorded_bootloader.get(key) != expected:
            raise ProtocolError(
                f"preserved backup manifest bootloader {key} does not match the file"
            )
    acceptance = recorded_bootloader.get("acceptance")
    if expected_bootloader["known_reference"]:
        if acceptance != "known-reference":
            raise ProtocolError("known bootloader lacks known-reference acceptance")
    elif acceptance != "explicit-hash":
        raise ProtocolError("unknown bootloader lacks explicit exact-hash acceptance")
    if manifest.get("required_identical_reads") != REQUIRED_IDENTICAL_READS:
        raise ProtocolError("preserved backup does not require three identical reads")
    if manifest.get("consecutive_identical_reads", 0) < REQUIRED_IDENTICAL_READS:
        raise ProtocolError(
            "preserved backup lacks three consecutive identical physical reads"
        )
    read_passes = manifest.get("read_passes", 0)
    if not isinstance(read_passes, int) or not (
        REQUIRED_IDENTICAL_READS <= read_passes <= MAX_READ_ATTEMPTS
    ):
        raise ProtocolError("preserved backup has an invalid physical-read count")
    if manifest.get("maximum_read_attempts") != MAX_READ_ATTEMPTS:
        raise ProtocolError("preserved backup has an unexpected read-attempt policy")
    recorded_parts = manifest.get("partitions")
    if not isinstance(recorded_parts, list):
        raise ProtocolError("preserved backup manifest has no partition map")
    try:
        parts = [MtdPartition(**item) for item in recorded_parts]
    except (TypeError, ValueError) as exc:
        raise ProtocolError("preserved backup manifest partition map is invalid") from exc
    validate_partition_map(parts)
    return result
