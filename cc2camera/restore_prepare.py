"""Fail-closed, temporary SFC preparation for the known stock restore path."""

from __future__ import annotations

import hashlib
import re

from .adb_backup import (
    AdbClient, AdbUnavailable, acquire_stable,
    validate_replacement_against_backup,
)
from .hid_transport import expected_devices
from .protocol import FLASH_SIZE, NORMAL_PID, NORMAL_VID, ProtocolError

KERNEL_SHA256 = "0855c3a93f571c3130f1bdd38469e7c806ce713550a3ce536c81167579341545"
POINTER_PHYSICAL = 0x0043B190
ERASE_OFFSET = 0x10
SYMBOLS = {
    "recovery_norflash_erase": 0x801F06CC,
    "jz_spi_norflash_erase_sector": 0x801EFC74,
    "direct_erase_norflash": 0x801F080C,
}
# Independently decoded checks: load the global pointer and erase-size member.
INSTRUCTIONS = {
    0x001F06D4: 0x3C138044,  # lui s3, 0x8044
    0x001F06E0: 0x8E64B190,  # lw a0, -20080(s3)
    0x001F0764: 0x8C420010,  # lw v0, 16(v0)
    0x001EFCA4: 0x8E220010,  # lw v0, 16(s1)
}


def validate_preparation_image(image: bytes) -> None:
    """Refuse unsupported kernel builds before accessing any device."""
    if len(image) != FLASH_SIZE or hashlib.sha256(
        image[0x40000:0x190000]
    ).hexdigest() != KERNEL_SHA256:
        raise ProtocolError("restore preparation requires the exact known stock kernel")


def _word(adb: AdbClient, address: int) -> int:
    output = adb.shell("busybox", "devmem", f"0x{address:08x}", "32")
    if not re.fullmatch(rb"\s*0x[0-9a-fA-F]{8}\s*", output):
        raise ProtocolError("invalid devmem readback; no HID trigger was sent")
    return int(output.strip(), 16)


def _field_address(pointer: int) -> int:
    # Known 64 MiB board, cached KSEG0. Exclude kernel/static memory, MMIO,
    # null, uncached aliases, unaligned and end-of-RAM objects.
    if pointer % 4 or not 0x80450000 <= pointer <= 0x84000000 - 0x264:
        raise ProtocolError("SFC object pointer is outside the supported RAM range")
    return pointer - 0x80000000 + ERASE_OFFSET


def _single_camera(adb: AdbClient) -> None:
    output = adb.run("devices").decode("ascii", "replace").replace("\r", "")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines or lines[0] != "List of devices attached":
        raise ProtocolError("unexpected ADB device list")
    devices = [line.split() for line in lines[1:]]
    if len(devices) != 1 or len(devices[0]) != 2 or devices[0][1] != "device":
        raise ProtocolError("restore preparation requires exactly one online ADB device")
    serial = devices[0][0]
    if adb.serial is not None and adb.serial != serial:
        raise ProtocolError("selected ADB serial does not match the only online device")
    hid = expected_devices()
    if len(hid) != 1 or (
        hid[0].get("vendor_id"), hid[0].get("product_id")
    ) != (NORMAL_VID, NORMAL_PID) or hid[0].get("serial_number") != serial:
        raise ProtocolError("normal HID and ADB must identify the same single camera")


def prepare_restore(adb: AdbClient, preserved: bytes, *, progress=None) -> None:
    """Validate the live camera, then set and verify its temporary erase size.

    Requires explicit restore consent from the caller and already-online ADB.
    No config erase, process suspension, persistent hook or retry is performed.
    A failure after the write may leave RAM patched; never assume rollback.
    """
    validate_preparation_image(preserved)
    try:
        adb.ensure_available()
    except AdbUnavailable as exc:
        raise ProtocolError(
            "restore requires online root ADB before its flag write; run the "
            "explicit start-adb command with the same --adb/--serial selection "
            "and retry. No HID trigger was sent"
        ) from exc
    _single_camera(adb)
    live, _manifest = acquire_stable(adb, progress=progress)
    validate_preparation_image(live)
    validate_replacement_against_backup(live, preserved)
    _identity, parts = adb.identity_and_partitions()
    if any(part.erase_size != 0x4000 for part in parts):
        raise ProtocolError("unexpected MTD erase geometry; refusing RAM modification")
    symbols = adb.shell("cat", "/proc/kallsyms").decode("ascii", "replace")
    for name, address in SYMBOLS.items():
        matches = re.findall(
            rf"^([0-9a-fA-F]{{8}}) [tT] {name}$", symbols.replace("\r", ""), re.M
        )
        if matches != [f"{address:08x}"]:
            raise ProtocolError(f"missing, ambiguous or unexpected kernel symbol: {name}")
    for address, expected in INSTRUCTIONS.items():
        if _word(adb, address) != expected:
            raise ProtocolError("live SFC instructions do not match the known kernel")
    pointer = _word(adb, POINTER_PHYSICAL)
    field = _field_address(pointer)
    old = _word(adb, field)
    if old not in (0x4000, 0x1000):
        raise ProtocolError("unexpected live SFC erase size; refusing RAM modification")
    # Legacy shell may not propagate remote exit status; require success markers.
    if adb.shell("sync && echo CC2_SYNC_OK").strip() != b"CC2_SYNC_OK":
        raise ProtocolError("sync failed; no RAM write or HID trigger was sent")
    _single_camera(adb)
    # Recheck pointer/value in the same shell invocation as the write. All
    # interpolated values are validated integers, never remote command text.
    command = (
        f'[ "$(busybox devmem 0x{POINTER_PHYSICAL:08x} 32)" = "0x{pointer:08X}" ] && '
        f'[ "$(busybox devmem 0x{field:08x} 32)" = "0x{old:08X}" ] && '
    )
    if old != 0x1000:
        command += f"busybox devmem 0x{field:08x} 32 0x00001000 && "
    command += f"busybox devmem 0x{field:08x} 32 && echo CC2_ERASE_READY"
    try:
        output = adb.shell(command).replace(b"\r", b"").strip()
        if output != b"0x00001000\nCC2_ERASE_READY":
            raise ProtocolError("guarded SFC write/readback did not confirm 0x1000")
        if _word(adb, POINTER_PHYSICAL) != pointer or _word(adb, field) != 0x1000:
            raise ProtocolError("SFC pointer or erase-size readback changed")
    except ProtocolError as exc:
        raise ProtocolError(
            f"{exc}; no HID trigger was sent. RAM may already be patched; "
            "no automatic retry or rollback was attempted"
        ) from exc

