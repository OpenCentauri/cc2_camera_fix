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

from .protocol import FLASH_SIZE, ProtocolError


class AdbUnavailable(ProtocolError):
    """No selected ADB device is connected in the usable ``device`` state."""


EXPECTED_PARTITIONS = (
    ("boot", 0x040000),
    ("kernel", 0x150000),
    ("root", 0x158000),
    ("system", 0x4E8000),
    ("hwconfig", 0x010000),
    ("config", 0x020000),
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
        return self.run("exec-out", *remote_args, timeout=timeout)

    def ensure_available(self) -> None:
        """Distinguish an absent device from local ADB/setup errors.

        Only ``AdbUnavailable`` is eligible for the normal-HID startup fallback.
        Missing executables, ambiguous device selections, authorization errors,
        and other local failures remain ordinary ``ProtocolError`` instances so
        they cannot cause an unnecessary persistent-device write.
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
        ):
            raise AdbUnavailable(f"no usable ADB camera is connected ({detail})")
        raise ProtocolError(f"ADB device is not usable ({detail})")

    def identity_and_partitions(self) -> tuple[str, list[MtdPartition]]:
        self.ensure_available()
        identity = self.exec_out("id").decode("utf-8", "replace").strip()
        if "uid=0" not in identity:
            raise ProtocolError("ADB shell is not root; refusing raw MTD access")
        text = self.exec_out("cat", "/proc/mtd").decode("ascii", "strict")
        parts = parse_proc_mtd(text)
        validate_partition_map(parts)
        return identity, parts

    def read_flash_once(self, parts: list[MtdPartition]) -> bytes:
        image = bytearray()
        for part in parts:
            data = self.exec_out("cat", part.device, timeout=120)
            if len(data) != part.size:
                raise ProtocolError(
                    f"short read from {part.device}: got {len(data)}, expected {part.size}"
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


def acquire_twice(adb: AdbClient) -> tuple[bytes, dict]:
    identity, parts = adb.identity_and_partitions()
    first = adb.read_flash_once(parts)
    second = adb.read_flash_once(parts)
    if first != second:
        raise ProtocolError(
            "two complete flash reads differ; no backup was accepted or written"
        )
    digest = hashes(first)
    manifest = {
        "format": "cc2flash-backup-v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "adb_serial": adb.serial,
        "device_identity": identity,
        "read_passes": 2,
        **digest,
        "partitions": [asdict(part) for part in parts],
    }
    return first, manifest


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
    if manifest.get("format") != "cc2flash-backup-v1":
        raise ProtocolError("preserved backup manifest has an unknown format")
    for key in ("size", "sha256", "md5"):
        if manifest.get(key) != result[key]:
            raise ProtocolError(f"preserved backup manifest {key} does not match the file")
    if manifest.get("read_passes", 0) < 2:
        raise ProtocolError("preserved backup was not acquired twice")
    recorded_parts = manifest.get("partitions")
    if not isinstance(recorded_parts, list):
        raise ProtocolError("preserved backup manifest has no partition map")
    try:
        parts = [MtdPartition(**item) for item in recorded_parts]
    except (TypeError, ValueError) as exc:
        raise ProtocolError("preserved backup manifest partition map is invalid") from exc
    validate_partition_map(parts)
    return result
