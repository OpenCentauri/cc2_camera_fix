"""Wire formats recovered from the supplied CC2 camera flash image.

This module is deliberately side-effect free.  In particular, building or
parsing a frame never opens a USB device.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import struct


FLASH_SIZE = 0x800000
UPGRADE_FLAG_OFFSET = 0x7F8000
UPGRADE_FLAG_0 = 0x55504454
UPGRADE_MODE_CDC = 0x010203A0
UPGRADE_MODE_HID = 0x010203A1

NORMAL_VID = 0xA108
NORMAL_PID = 0x2240
BOOT_VID = 0xA108
BOOT_HID_PID = 0xFF08

NORMAL_REPORT_SIZE = 1024
BOOT_REPORT_SIZE = 3072
BOOT_REPORT_ID = 1
BOOT_FRAME_CAPACITY = BOOT_REPORT_SIZE - 1
BOOT_DATA_OVERHEAD = 11
BOOT_DATA_SIZE = BOOT_FRAME_CAPACITY - BOOT_DATA_OVERHEAD  # 3060
BOOT_IMAGE_HEADER_SIZE = 128


class ProtocolError(ValueError):
    """A frame or image violates the recovered protocol."""


def crc16_normal(data: bytes) -> int:
    """Return hid_update's unusual LSB-first 0x1021 CRC.

    This is intentionally not described as CRC-16/CCITT: the implementation
    shifts right while using the non-reflected 0x1021 polynomial.
    """

    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc >> 1) ^ 0x1021) if (crc & 1) else (crc >> 1)
            crc &= 0xFFFF
    return crc


@dataclass(frozen=True)
class NormalFrame:
    command: int
    frame_type: int
    status: int
    payload: bytes


def build_normal_report(
    command: int,
    payload: bytes = b"",
    *,
    frame_type: int = 1,
    status: int = 0,
) -> bytes:
    """Build the fixed 1024-byte Linux-mode HID report."""

    if not 0 <= command <= 0xFFFF:
        raise ProtocolError("normal-mode command must fit in 16 bits")
    if frame_type not in (1, 2):
        raise ProtocolError("payload-bearing normal frame type must be 1 or 2")
    if len(payload) > NORMAL_REPORT_SIZE - 14:
        raise ProtocolError("normal-mode payload is too large")
    report = bytearray(NORMAL_REPORT_SIZE)
    report[0] = 1
    report[1:3] = b"\x5a\x5a"
    struct.pack_into("<H", report, 3, command)
    report[5] = frame_type
    struct.pack_into("<H", report, 6, len(payload))
    struct.pack_into("<I", report, 8, status)
    report[14 : 14 + len(payload)] = payload
    crc_input = bytes(report[:12]) + payload
    struct.pack_into("<H", report, 12, crc16_normal(crc_input))
    return bytes(report)


def parse_normal_report(report: bytes) -> NormalFrame:
    """Validate and decode a Linux-mode HID report."""

    if len(report) == NORMAL_REPORT_SIZE - 1 and report[:2] == b"\x5a\x5a":
        report = b"\x01" + report
    if len(report) < 14:
        raise ProtocolError("short normal-mode report")
    if report[0] != 1:
        raise ProtocolError("unexpected normal-mode report ID")
    if report[1:3] != b"\x5a\x5a":
        raise ProtocolError("bad normal-mode magic")
    command = struct.unpack_from("<H", report, 3)[0]
    frame_type = report[5]
    length = struct.unpack_from("<H", report, 6)[0]
    status = struct.unpack_from("<I", report, 8)[0]
    if 14 + length > len(report):
        raise ProtocolError("normal-mode payload length exceeds report")
    payload = bytes(report[14 : 14 + length])
    stored_crc = struct.unpack_from("<H", report, 12)[0]
    crc_input = bytes(report[:12]) + (payload if frame_type in (1, 2) else b"")
    if crc16_normal(crc_input) != stored_crc:
        raise ProtocolError("normal-mode CRC mismatch")
    return NormalFrame(command, frame_type, status, payload)


def upgrade_flag_payload(mode: int = UPGRADE_MODE_HID) -> bytes:
    if mode not in (UPGRADE_MODE_CDC, UPGRADE_MODE_HID):
        raise ProtocolError("unknown bootloader transport flag")
    return struct.pack("<II", UPGRADE_FLAG_0, mode)


def additive_checksum(data: bytes) -> int:
    return sum(data) & 0xFF


@dataclass(frozen=True)
class BootFrame:
    frame_type: int
    payload: bytes
    total_length: int


def build_boot_frame(frame_type: int, payload: bytes = b"") -> bytes:
    """Build a host-to-device bootloader frame (magic 80 00 ee)."""

    total = len(payload) + 7
    if total > 0xFFFF or total > BOOT_FRAME_CAPACITY:
        raise ProtocolError("bootloader frame is too large")
    frame = bytearray(total)
    frame[:3] = b"\x80\x00\xee"
    frame[3] = frame_type & 0xFF
    struct.pack_into("<H", frame, 4, total)
    frame[6:-1] = payload
    frame[-1] = additive_checksum(frame[:-1])
    return bytes(frame)


def parse_boot_frame(data: bytes, *, device_response: bool = True) -> BootFrame:
    """Decode one bootloader frame, ignoring HID padding after its length."""

    expected_magic = b"\x81\x00\xee" if device_response else b"\x80\x00\xee"
    if len(data) < 7:
        raise ProtocolError("short bootloader frame")
    if data[:3] != expected_magic:
        raise ProtocolError("bad bootloader frame magic")
    total = struct.unpack_from("<H", data, 4)[0]
    if not 7 <= total <= len(data):
        raise ProtocolError("invalid bootloader frame length")
    frame = data[:total]
    if additive_checksum(frame[:-1]) != frame[-1]:
        raise ProtocolError("bootloader additive checksum mismatch")
    return BootFrame(frame[3], bytes(frame[6:-1]), total)


def to_boot_hid_report(frame: bytes) -> bytes:
    if len(frame) > BOOT_FRAME_CAPACITY:
        raise ProtocolError("frame does not fit in a bootloader HID report")
    return bytes((BOOT_REPORT_ID,)) + frame.ljust(BOOT_FRAME_CAPACITY, b"\0")


def from_boot_hid_report(report: bytes) -> BootFrame:
    if len(report) >= 1 and report[0] == BOOT_REPORT_ID:
        report = report[1:]
    return parse_boot_frame(report, device_response=True)


@dataclass(frozen=True)
class UpdatePlan:
    flash_offset: int
    image_size: int
    transfer_size: int
    image_md5: str
    packet_count: int
    packet_payload_size: int
    packets_per_ack: int


def validate_flash_range(offset: int, length: int) -> None:
    if offset < 0 or length <= 0:
        raise ProtocolError("flash offset must be nonnegative and length must be positive")
    if offset + length > FLASH_SIZE:
        raise ProtocolError("flash range exceeds the 8 MiB device")


def build_update_blob(image: bytes, *, flash_offset: int = 0) -> tuple[bytes, UpdatePlan]:
    """Prepend the 128-byte header consumed by the bootloader updater."""

    validate_flash_range(flash_offset, len(image))
    header = bytearray(BOOT_IMAGE_HEADER_SIZE)
    # Bytes 0..3 and 28..127 are not inspected by this bootloader build.
    struct.pack_into("<II", header, 4, flash_offset, len(image))
    digest = hashlib.md5(image).digest()
    header[12:28] = digest
    blob = bytes(header) + image
    plan = UpdatePlan(
        flash_offset=flash_offset,
        image_size=len(image),
        transfer_size=len(blob),
        image_md5=digest.hex(),
        packet_count=math.ceil(len(blob) / BOOT_DATA_SIZE),
        packet_payload_size=BOOT_DATA_SIZE,
        packets_per_ack=1,
    )
    return blob, plan


def build_metadata_frame(plan: UpdatePlan, *, firmware_version: int = 0) -> bytes:
    payload = struct.pack(
        "<HIHH",
        plan.packet_payload_size,
        plan.transfer_size,
        plan.packets_per_ack,
        firmware_version,
    )
    if len(payload) != 10:
        raise AssertionError("metadata layout changed")
    return build_boot_frame(1, payload)


def iter_data_frames(blob: bytes):
    """Yield numbered type-3 frames; each fits one 3072-byte HID report."""

    for sequence, start in enumerate(range(0, len(blob), BOOT_DATA_SIZE)):
        chunk = blob[start : start + BOOT_DATA_SIZE]
        yield sequence, build_boot_frame(3, struct.pack("<I", sequence) + chunk)


def validate_full_restore_image(image: bytes) -> None:
    if len(image) != FLASH_SIZE:
        raise ProtocolError(f"full restore requires exactly {FLASH_SIZE} bytes")
    flag = struct.unpack_from("<I", image, UPGRADE_FLAG_OFFSET)[0]
    if flag == UPGRADE_FLAG_0:
        raise ProtocolError(
            "image contains the persistent upgrade magic at 0x7f8000 and would boot-loop"
        )

