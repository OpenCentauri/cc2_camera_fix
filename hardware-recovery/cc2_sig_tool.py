#!/usr/bin/env python3
"""
Strict, self-contained recovery builder for the Elegoo Centauri Carbon 2
stock camera firmware family observed in two independent 8 MiB dumps.

The tool:
  * validates every invariant firmware byte against fingerprints derived
    independently from the Discord sample and a second physical camera dump;
  * accepts only the exact known stock / known-patched system fragment;
  * extracts and preserves the camera's own serial.cfg from JFFS2;
  * rebuilds a minimal, CRC-valid 128 KiB JFFS2 config partition;
  * applies the audited /home/bashrc.sh mitigation;
  * writes a full recovery image plus a flashrom layout and validation report.

It has no third-party Python dependencies and intentionally has no "force"
option for unknown firmware.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import re
import struct
import sys
import zipfile
import zlib
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

TOOL_VERSION = "1.2.0"
MIN_IDENTICAL_READS = 3
MAX_USB_READ_ATTEMPTS = 5

FLASH_SIZE = 0x800000
PATCH_START = 0x463000
PATCH_END = 0x46B000
PATCH_SIZE = PATCH_END - PATCH_START
CONFIG_START = 0x7E0000
CONFIG_END = 0x800000
CONFIG_SIZE = CONFIG_END - CONFIG_START

USB_BACKUP_FORMAT = "cc2flash-backup-v2"
USB_BACKUP_IMAGE_MEMBER = "flash.bin"
USB_BACKUP_MANIFEST_MEMBER = "manifest.json"
USB_BACKUP_MEMBERS = frozenset(
    (USB_BACKUP_IMAGE_MEMBER, USB_BACKUP_MANIFEST_MEMBER)
)
MAX_USB_MANIFEST_SIZE = 1024 * 1024
KNOWN_BOOTLOADER_SHA256 = (
    "5602ec961b4410ccceea0d4910e4fa768c6998bd4ba86143ba50855bdd0b7a54"
)
EXPECTED_USB_PARTITIONS = (
    (0, 0x040000, 0x8000, "boot"),
    (1, 0x150000, 0x8000, "kernel"),
    (2, 0x158000, 0x8000, "root"),
    (3, 0x4E8000, 0x8000, "system"),
    (4, 0x010000, 0x8000, "hwconfig"),
    (5, 0x020000, 0x8000, "config"),
)

# The HWCONFIG record contains a unit-specific two-byte check value and a
# 94-byte encrypted/encoded UOID. Everything around these two fields is
# byte-identical between the independently obtained reference images.
HW_CHECK_START = 0x7D200B
HW_CHECK_END = 0x7D200D
HW_UOID_START = 0x7D2011
HW_UOID_END = 0x7D206F

JFFS2_MAGIC = 0x1985
JFFS2_NODE_ACCURATE = 0x2000
JFFS2_DIRENT = 0x0001
JFFS2_INODE = 0x0002
JFFS2_CLEANMARKER = 0x0003

KNOWN_CONFIG_NAMES = {
    b"serial.cfg",
    b"uvc.attr",
    b"uvc2.attr",
    b"uvc.config",
    b"uvc_dualstream.config",
    b"dev_config.cfg",
}

SERIAL_PATTERN = re.compile(rb"^serial=(12PSSSS4[A-Z0-9]{28})\n$")
UOID_PATTERN = re.compile(rb"^12PSSSS4[A-Za-z0-9+/=]{86}$")

REFERENCE_FULL_SHA256 = {
    # The full hashes are informational: per-unit data and JFFS2 history mean
    # most valid cameras will not equal a complete reference image.
    "discord_bricked": "4325aebe84d70dd937de1790aa48f4b36ee2731def0ee5504ed080e4df13e819",
    "discord_config_recovery": "a25d83ae5786fbc3306ba6f6403e9f174969e53ea3aeb6355e129463b3a5644a",
    "second_camera_original": "ff9c8962abd06a14661db1857f6318b06f6378e93c4d812d96224094063bbcf9",
    "second_camera_permanent_readback": "269f1b3b205e2ac30ada7cb98a7aeb9ada2e76786dc14abe95ea9c56dce73d1f",
}

# Exact byte ranges that must match the independently compared references.
REFERENCE_SEGMENTS = {
    "boot": (0x000000, 0x040000, "5602ec961b4410ccceea0d4910e4fa768c6998bd4ba86143ba50855bdd0b7a54"),
    "kernel": (0x040000, 0x190000, "0855c3a93f571c3130f1bdd38469e7c806ce713550a3ce536c81167579341545"),
    "root": (0x190000, 0x2E8000, "6049eaacfaba6a2c3db2ed7a5f31cd02df8d793e166249f6a8a2180e89824cba"),
    "system_before_patch": (0x2E8000, 0x463000, "0e76c9eb0dfb499c7556a6a111608757b6c9e32705c07492f172253255fe8041"),
    "system_after_patch": (0x46B000, 0x7D0000, "07a3b00fe224a8e121f760753998335e7f6d59f1cfe2dc92087c040083f46b3a"),
    "hwconfig_before_identity_check": (0x7D0000, 0x7D200B, "0e1514680c4e25e5c431adae5d4cb98bac14746b6e8fc30eb346ec75249f7cc5"),
    "hwconfig_between_identity_fields": (0x7D200D, 0x7D2011, "3c3351dc1dedcd627419e02de4fc8202e2d507d786c26f142b767fd9859d0cb4"),
    "hwconfig_after_uoid": (0x7D206F, 0x7E0000, "98d0beba4c7328a7237bc1a18fdd5e3da64c9f009e3b1c253ea39167a6dabd97"),
}

# SHA-256 of all bytes from 0x000000 through 0x7DFFFF after omitting only
# the known bashrc patch window and the two unit-specific HWCONFIG fields.
# Both independent stock images and the verified patched readback produce
# this same fingerprint.
INVARIANT_SHA256 = "7346221d7814c8ef4412ced5f3795891f077f89ba64cf61d087e01fc5344f62f"

ORIGINAL_PATCH_SHA256 = "5591f5350feabb73fd29e21ae72ee9c3c9dab0c6bb78e02178267e5cb2ab4780"
PATCHED_PATCH_SHA256 = "36e9b9b29dffcd775b094ff67a121fbb78b1871a7eda059371cbafc562251b2b"
ORIGINAL_BASHRC_SHA256 = "984e1d34fb69bd83d43335e62911503461b389f96f86c5c837f7aab57a48417d"
PATCHED_BASHRC_SHA256 = "eab95c4ef39ba900fdd87684feee916f36e2901d3a7abe3fe65d2cde459e0f88"

# The stock SquashFS has one 100167-byte fragment. bashrc.sh occupies a
# same-length slice within it. These offsets and hashes are verified before
# anything is modified, and the final 32 KiB window must match its known hash.
SQUASHFS_FRAGMENT_START = 0x463600
SQUASHFS_FRAGMENT_SIZE_FIELD = 0x46A11A
ORIGINAL_FRAGMENT_XZ_SIZE = 26268
PATCHED_FRAGMENT_XZ_SIZE = 25564
FRAGMENT_UNCOMPRESSED_SIZE = 100167
BASHRC_FRAGMENT_OFFSET = 29000
LZMA2_DICT_SIZE = 131072
ORIGINAL_FRAGMENT_XZ_SHA256 = "4c6d2cf7ce219721a8f027056c207d05fb01e1ae2165bf17e5208f37d690869f"
PATCHED_FRAGMENT_XZ_SHA256 = "ca9162b4b6b7a898901be0503d5c4cb10e94f01c44e79caf4f7c3a5172e768f5"
ORIGINAL_FRAGMENT_SHA256 = "322825a20f30d04d5ad6fc2f677cd8fce2b0c43189d802d1fbb05d2049c07329"
PATCHED_FRAGMENT_SHA256 = "a6a65889a5d79a8b8935cbf1602c1a152a52c2d28f66821f0a5adaac9546c905"

# This is the complete human-readable source transformation. The replacement
# is padded on its last line so bashrc.sh remains exactly 5699 bytes.
ORIGINAL_COPY_BLOCK = b"""# [ ! -f /etc/conf.d/uvc.attr ] && cp /system/config/uvc.attr /etc/conf.d/uvc.attr
# [ ! -f /etc/conf.d/uvc.config ] && cp /system/config/uvc.config /etc/conf.d/uvc.config
# [ ! -f /etc/conf.d/dev_config.cfg ] && cp /system/config/dev_config.cfg /etc/conf.d/dev_config.cfg
cp /system/config/uvc.attr /etc/conf.d/uvc.attr
cp /system/config/uvc2.attr /etc/conf.d/uvc2.attr
cp /system/config/uvc_dualstream.config /etc/conf.d/uvc.config
cp /system/config/uvc_dualstream.config /etc/conf.d/uvc_dualstream.config
cp /system/config/dev_config.cfg /etc/conf.d/dev_config.cfg

# [ ! -f /etc/conf.d/uvc2.attr ] && cp /system/config/uvc2.attr /etc/conf.d/uvc2.attr
# [ ! -f /etc/conf.d/uvc_dualstream.config ] && cp /system/config/uvc_dualstream.config /etc/conf.d/uvc_dualstream.config

"""

PATCHED_COPY_BLOCK_VISIBLE = b"""# Copy defaults only when absent.  The original unconditional copies
# consume JFFS2 space on every boot, while this firmware cannot reclaim it.
[ -f /etc/conf.d/uvc.attr ] || cp /system/config/uvc.attr /etc/conf.d/uvc.attr
[ -f /etc/conf.d/uvc2.attr ] || cp /system/config/uvc2.attr /etc/conf.d/uvc2.attr
[ -f /etc/conf.d/uvc.config ] || cp /system/config/uvc_dualstream.config /etc/conf.d/uvc.config
[ -f /etc/conf.d/uvc_dualstream.config ] || cp /system/config/uvc_dualstream.config /etc/conf.d/uvc_dualstream.config
[ -f /etc/conf.d/dev_config.cfg ] || cp /system/config/dev_config.cfg /etc/conf.d/dev_config.cfg

# SquashFS in-place patch padding; keep bashrc.sh length unchanged.
"""

OUTPUT_FILE_NAMES = {
    "cc2-camera-recovery.bin",
    "cc2-camera-layout.txt",
    "config-restored.bin",
    "serial.cfg",
    "MANIFEST.json",
    "VALIDATION.txt",
    "FLASHING.txt",
    "SHA256SUMS.txt",
}

class ValidationError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def image_hashes(data: bytes) -> dict[str, str | int]:
    return {
        "size": len(data),
        "sha256": sha256(data),
        "md5": hashlib.md5(data).hexdigest(),
    }


def _validate_usb_backup_manifest(manifest: Any, image: bytes) -> dict[str, Any]:
    """Validate acquisition evidence produced by usb-maintenance/cc2flash."""

    if not isinstance(manifest, dict):
        raise ValidationError("USB backup manifest must be a JSON object")
    if manifest.get("format") != USB_BACKUP_FORMAT:
        raise ValidationError(
            "USB backup requires a cc2flash-backup-v2 manifest with three "
            "identical reads"
        )

    actual_hashes = image_hashes(image)
    for key, expected in actual_hashes.items():
        if manifest.get(key) != expected:
            raise ValidationError(
                f"USB backup manifest {key} does not match flash.bin"
            )

    boot_hash = sha256(image[:0x040000])
    expected_bootloader = {
        "partition": "boot",
        "size": 0x040000,
        "sha256": boot_hash,
        "known_sha256": KNOWN_BOOTLOADER_SHA256,
        "known_reference": boot_hash == KNOWN_BOOTLOADER_SHA256,
    }
    bootloader = manifest.get("bootloader")
    if not isinstance(bootloader, dict):
        raise ValidationError("USB backup manifest has no bootloader fingerprint")
    for key, expected in expected_bootloader.items():
        if bootloader.get(key) != expected:
            raise ValidationError(
                f"USB backup manifest bootloader {key} does not match flash.bin"
            )
    acceptance = bootloader.get("acceptance")
    expected_acceptance = (
        "known-reference"
        if expected_bootloader["known_reference"]
        else "explicit-hash"
    )
    if acceptance != expected_acceptance:
        raise ValidationError(
            "USB backup manifest does not contain the required bootloader "
            "hash acceptance"
        )

    if manifest.get("required_identical_reads") != MIN_IDENTICAL_READS:
        raise ValidationError("USB backup does not require three identical reads")
    consecutive = manifest.get("consecutive_identical_reads")
    if type(consecutive) is not int or consecutive != MIN_IDENTICAL_READS:
        raise ValidationError(
            "USB backup lacks three consecutive identical physical reads"
        )
    read_passes = manifest.get("read_passes")
    if type(read_passes) is not int or not (
        MIN_IDENTICAL_READS <= read_passes <= MAX_USB_READ_ATTEMPTS
    ):
        raise ValidationError("USB backup has an invalid physical-read count")
    if manifest.get("maximum_read_attempts") != MAX_USB_READ_ATTEMPTS:
        raise ValidationError("USB backup has an unexpected read-attempt policy")

    partitions = manifest.get("partitions")
    if not isinstance(partitions, list) or len(partitions) != len(
        EXPECTED_USB_PARTITIONS
    ):
        raise ValidationError("USB backup manifest has an invalid partition map")
    for item, (index, size, erase_size, name) in zip(
        partitions, EXPECTED_USB_PARTITIONS
    ):
        if not isinstance(item, dict) or item != {
            "index": index,
            "size": size,
            "erase_size": erase_size,
            "name": name,
        }:
            raise ValidationError("USB backup manifest has an invalid partition map")

    return {
        "format": USB_BACKUP_FORMAT,
        "image_member": USB_BACKUP_IMAGE_MEMBER,
        "read_passes": read_passes,
        "required_identical_reads": MIN_IDENTICAL_READS,
        "consecutive_identical_reads": consecutive,
        "maximum_read_attempts": MAX_USB_READ_ATTEMPTS,
        "bootloader_acceptance": acceptance,
        **actual_hashes,
    }


def read_image_source(path: Path) -> tuple[bytes, dict[str, Any]]:
    """Read a raw dump or a strict cc2flash backup ZIP without extracting it."""

    if path.suffix.casefold() != ".zip":
        image = path.read_bytes()
        return image, {
            "format": "raw-flash-image",
            "image_member": None,
            "evidenced_identical_reads": 1,
            **image_hashes(image),
        }

    try:
        with zipfile.ZipFile(path, mode="r") as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != 2 or set(names) != USB_BACKUP_MEMBERS:
                raise ValidationError(
                    "USB backup ZIP must contain exactly flash.bin and manifest.json"
                )
            by_name = {item.filename: item for item in infos}
            if any(item.flag_bits & 0x1 for item in infos):
                raise ValidationError("USB backup ZIP must not be encrypted")
            if by_name[USB_BACKUP_IMAGE_MEMBER].file_size != FLASH_SIZE:
                raise ValidationError("USB backup flash.bin is not exactly 8 MiB")
            if (
                by_name[USB_BACKUP_MANIFEST_MEMBER].file_size
                > MAX_USB_MANIFEST_SIZE
            ):
                raise ValidationError("USB backup manifest is unexpectedly large")
            image = archive.read(USB_BACKUP_IMAGE_MEMBER)
            manifest_bytes = archive.read(USB_BACKUP_MANIFEST_MEMBER)
    except ValidationError:
        raise
    except (
        OSError,
        RuntimeError,
        NotImplementedError,
        zipfile.BadZipFile,
        zlib.error,
        lzma.LZMAError,
    ) as exc:
        raise ValidationError("USB backup ZIP is unreadable or corrupt") from exc

    if len(image) != FLASH_SIZE:
        raise ValidationError("USB backup flash.bin is not exactly 8 MiB")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValidationError("USB backup manifest is unreadable") from exc
    evidence = _validate_usb_backup_manifest(manifest, image)
    evidence["evidenced_identical_reads"] = MIN_IDENTICAL_READS
    return image, evidence


def jffs2_crc(data: bytes) -> int:
    return (zlib.crc32(data, 0xFFFFFFFF) ^ 0xFFFFFFFF) & 0xFFFFFFFF


def align4(value: int) -> int:
    return (value + 3) & ~3


def mask_value(value: bytes, head: int = 12, tail: int = 7) -> str:
    text = value.decode("ascii", errors="replace")
    if len(text) <= head + tail + 1:
        return text
    return f"{text[:head]}…{text[-tail:]}"


def xz_vli(value: int) -> bytes:
    """Encode an XZ variable-length integer."""
    if value < 0:
        raise ValueError("XZ VLI values cannot be negative")
    encoded = bytearray()
    while True:
        remaining = value >> 7
        encoded.append((value & 0x7F) | (0x80 if remaining else 0))
        if not remaining:
            return bytes(encoded)
        value = remaining


def build_squashfs_xz_fragment(data: bytes) -> bytes:
    """Build the single-block XZ stream format emitted by mksquashfs here."""
    compressed = lzma.compress(
        data,
        format=lzma.FORMAT_RAW,
        filters=[{"id": lzma.FILTER_LZMA2, "dict_size": LZMA2_DICT_SIZE}],
    )

    # XZ stream header: magic, CRC32 check type, and CRC of the two flags.
    stream_flags = b"\x00\x01"
    stream_header = (
        b"\xFD7zXZ\x00"
        + stream_flags
        + struct.pack("<I", zlib.crc32(stream_flags) & 0xFFFFFFFF)
    )

    # SquashFS requests both compressed and uncompressed sizes in the XZ
    # block header. The only filter is LZMA2 (0x21), property 0x0A = 128 KiB.
    block_header = bytearray(b"\x00\xC0")
    block_header += xz_vli(len(compressed))
    block_header += xz_vli(len(data))
    block_header += b"\x21\x01\x0A"
    block_header += b"\x00" * (-(len(block_header) + 4) % 4)
    block_header[0] = (len(block_header) + 4) // 4 - 1
    block_header += struct.pack(
        "<I", zlib.crc32(block_header) & 0xFFFFFFFF
    )

    block = bytes(block_header) + compressed
    block += b"\x00" * (-len(compressed) % 4)
    block += struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)

    unpadded_size = len(block_header) + len(compressed) + 4
    index = bytearray(b"\x00\x01")
    index += xz_vli(unpadded_size) + xz_vli(len(data))
    index += b"\x00" * (-len(index) % 4)
    index += struct.pack("<I", zlib.crc32(index) & 0xFFFFFFFF)

    footer_fields = struct.pack("<I", len(index) // 4 - 1) + stream_flags
    footer = (
        struct.pack("<I", zlib.crc32(footer_fields) & 0xFFFFFFFF)
        + footer_fields
        + b"YZ"
    )
    return stream_header + block + bytes(index) + footer


def patch_bashrc(original: bytes) -> bytes:
    """Apply the complete readable shell-source change, preserving length."""
    if sha256(original) != ORIGINAL_BASHRC_SHA256:
        raise ValidationError("Extracted bashrc.sh failed its stock SHA-256 check")
    if original.count(ORIGINAL_COPY_BLOCK) != 1:
        raise ValidationError("Stock bashrc.sh does not contain one exact copy block")

    padding = len(ORIGINAL_COPY_BLOCK) - len(PATCHED_COPY_BLOCK_VISIBLE)
    if padding < 0:
        raise ValidationError("Internal bashrc replacement is too large")
    replacement = (
        PATCHED_COPY_BLOCK_VISIBLE[:-1] + b" " * padding + b"\n"
    )
    patched = original.replace(ORIGINAL_COPY_BLOCK, replacement)
    if len(patched) != len(original):
        raise ValidationError("Internal bashrc patch changed the script length")
    if sha256(patched) != PATCHED_BASHRC_SHA256:
        raise ValidationError("Generated bashrc.sh failed its patched SHA-256 check")
    return patched


def build_system_patch_window(image: bytes) -> bytes:
    """Rebuild the audited SquashFS fragment without an opaque binary delta."""
    stock_window = image[PATCH_START:PATCH_END]
    if sha256(stock_window) != ORIGINAL_PATCH_SHA256:
        raise ValidationError("Refusing to patch a non-stock system window")

    original_xz = image[
        SQUASHFS_FRAGMENT_START:
        SQUASHFS_FRAGMENT_START + ORIGINAL_FRAGMENT_XZ_SIZE
    ]
    if sha256(original_xz) != ORIGINAL_FRAGMENT_XZ_SHA256:
        raise ValidationError("Stock SquashFS fragment failed its SHA-256 check")
    try:
        fragment = lzma.decompress(original_xz, format=lzma.FORMAT_XZ)
    except lzma.LZMAError as exc:
        raise ValidationError(f"Stock SquashFS fragment failed to decompress: {exc}") from exc
    if (
        len(fragment) != FRAGMENT_UNCOMPRESSED_SIZE
        or sha256(fragment) != ORIGINAL_FRAGMENT_SHA256
    ):
        raise ValidationError("Stock SquashFS fragment content is not the audited version")

    bashrc_end = BASHRC_FRAGMENT_OFFSET + 5699
    original_bashrc = fragment[BASHRC_FRAGMENT_OFFSET:bashrc_end]
    patched_bashrc = patch_bashrc(original_bashrc)
    patched_fragment = (
        fragment[:BASHRC_FRAGMENT_OFFSET]
        + patched_bashrc
        + fragment[bashrc_end:]
    )
    if sha256(patched_fragment) != PATCHED_FRAGMENT_SHA256:
        raise ValidationError("Patched SquashFS fragment failed its content hash")

    patched_xz = build_squashfs_xz_fragment(patched_fragment)
    if (
        len(patched_xz) != PATCHED_FRAGMENT_XZ_SIZE
        or sha256(patched_xz) != PATCHED_FRAGMENT_XZ_SHA256
    ):
        raise ValidationError(
            "Deterministic XZ output differs from the audited patched fragment; "
            "check the Python/liblzma implementation"
        )

    patched_window = bytearray(stock_window)
    fragment_relative = SQUASHFS_FRAGMENT_START - PATCH_START
    patched_window[
        fragment_relative:fragment_relative + len(patched_xz)
    ] = patched_xz
    # The shorter stream intentionally leaves the old trailing bytes untouched;
    # SquashFS ignores them after this authoritative size field is updated.
    size_relative = SQUASHFS_FRAGMENT_SIZE_FIELD - PATCH_START
    struct.pack_into("<I", patched_window, size_relative, len(patched_xz))

    result = bytes(patched_window)
    if sha256(result) != PATCHED_PATCH_SHA256:
        raise ValidationError("Generated system window failed its audited SHA-256")
    return result


def restored_accurate_slice(raw: bytes, node_type: int, obsolete: bool) -> bytes:
    if not obsolete:
        return raw
    fixed = bytearray(raw)
    struct.pack_into("<H", fixed, 2, node_type | JFFS2_NODE_ACCURATE)
    return bytes(fixed)


def parse_jffs2(data: bytes) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    for offset in range(0, len(data) - 11, 4):
        magic, node_type = struct.unpack_from("<HH", data, offset)
        if magic != JFFS2_MAGIC:
            continue

        total_length, stored_header_crc = struct.unpack_from("<II", data, offset + 4)
        if total_length < 12 or offset + total_length > len(data):
            continue

        header = data[offset : offset + 8]
        current = jffs2_crc(header) == stored_header_crc
        obsolete = False

        if not current and not (node_type & JFFS2_NODE_ACCURATE):
            fixed_header = bytearray(header)
            struct.pack_into("<H", fixed_header, 2, node_type | JFFS2_NODE_ACCURATE)
            obsolete = jffs2_crc(bytes(fixed_header)) == stored_header_crc

        if not (current or obsolete):
            continue

        base_type = node_type & 0x0FFF
        node: dict[str, Any] = {
            "offset": offset,
            "node_type": node_type,
            "total_length": total_length,
            "current": current,
            "obsolete": obsolete,
        }

        if base_type == JFFS2_DIRENT and total_length >= 40:
            parent_inode, version, inode, mctime = struct.unpack_from(
                "<IIII", data, offset + 12
            )
            name_size = data[offset + 28]
            dtype = data[offset + 29]
            stored_node_crc, stored_name_crc = struct.unpack_from(
                "<II", data, offset + 32
            )
            name = data[offset + 40 : offset + 40 + name_size]

            node_crc_bytes = restored_accurate_slice(
                data[offset : offset + 32], node_type, obsolete
            )
            if jffs2_crc(node_crc_bytes) != stored_node_crc:
                continue
            if jffs2_crc(name) != stored_name_crc:
                continue

            node.update(
                kind="dirent",
                parent_inode=parent_inode,
                version=version,
                inode=inode,
                mctime=mctime,
                dtype=dtype,
                name=name,
            )

        elif base_type == JFFS2_INODE and total_length >= 68:
            values = struct.unpack_from("<IIIHHIIIIIIIBBHII", data, offset + 12)
            (
                inode,
                version,
                mode,
                uid,
                gid,
                file_size,
                atime,
                mtime,
                ctime,
                file_offset,
                compressed_size,
                decompressed_size,
                compression,
                user_compression,
                flags,
                stored_data_crc,
                stored_node_crc,
            ) = values

            payload = data[
                offset + 68 : offset + 68 + compressed_size
            ]
            node_crc_bytes = restored_accurate_slice(
                data[offset : offset + 60], node_type, obsolete
            )
            if jffs2_crc(node_crc_bytes) != stored_node_crc:
                continue
            if jffs2_crc(payload) != stored_data_crc:
                continue

            node.update(
                kind="inode",
                inode=inode,
                version=version,
                mode=mode,
                uid=uid,
                gid=gid,
                file_size=file_size,
                atime=atime,
                mtime=mtime,
                ctime=ctime,
                file_offset=file_offset,
                compressed_size=compressed_size,
                decompressed_size=decompressed_size,
                compression=compression,
                user_compression=user_compression,
                flags=flags,
                payload=payload,
            )

        elif base_type == JFFS2_CLEANMARKER:
            node.update(kind="cleanmarker")

        else:
            node.update(kind=f"other_0x{base_type:03x}")

        nodes.append(node)

    return nodes


def serial_payload_is_valid(payload: bytes) -> bool:
    return SERIAL_PATTERN.fullmatch(payload) is not None


def extract_serial_and_config_info(config: bytes) -> dict[str, Any]:
    nodes = parse_jffs2(config)
    if not nodes:
        raise ValidationError("No CRC-valid JFFS2 nodes were found in config")

    valid_dirents = [n for n in nodes if n.get("kind") == "dirent"]
    all_names = {n["name"] for n in valid_dirents}
    unexpected_names = sorted(all_names - KNOWN_CONFIG_NAMES)
    if unexpected_names:
        printable = ", ".join(repr(name) for name in unexpected_names)
        raise ValidationError(
            "Config contains unexpected CRC-valid names that this tool will not "
            f"discard automatically: {printable}"
        )

    current_dirents: dict[tuple[int, bytes], dict[str, Any]] = {}
    for node in valid_dirents:
        if not node["current"]:
            continue
        key = (node["parent_inode"], node["name"])
        previous = current_dirents.get(key)
        if previous is None or node["version"] > previous["version"]:
            current_dirents[key] = node

    live_entries = {
        name: node
        for (parent, name), node in current_dirents.items()
        if parent == 1 and node["inode"] != 0
    }

    serial_source = "live-current"
    serial_payload: bytes | None = None

    serial_dirent = live_entries.get(b"serial.cfg")
    if serial_dirent is not None:
        inode_candidates = [
            n
            for n in nodes
            if n.get("kind") == "inode"
            and n["current"]
            and n["inode"] == serial_dirent["inode"]
            and n["file_offset"] == 0
            and n["compression"] == 0
            and n["compressed_size"] == n["decompressed_size"]
            and n["file_size"] == n["decompressed_size"]
            and serial_payload_is_valid(n["payload"])
        ]
        if inode_candidates:
            inode_candidates.sort(key=lambda item: item["version"], reverse=True)
            newest_version = inode_candidates[0]["version"]
            newest = [
                item for item in inode_candidates if item["version"] == newest_version
            ]
            if len(newest) != 1:
                raise ValidationError(
                    "Multiple equally current serial.cfg inode candidates were found"
                )
            serial_payload = newest[0]["payload"]

    if serial_payload is None:
        # Conservative fallback for a damaged live directory entry: accept a
        # historical serial only if every CRC-valid serial-looking inode agrees.
        historical = {
            n["payload"]
            for n in nodes
            if n.get("kind") == "inode"
            and n["file_offset"] == 0
            and n["compression"] == 0
            and n["compressed_size"] == n["decompressed_size"]
            and n["file_size"] == n["decompressed_size"]
            and serial_payload_is_valid(n["payload"])
        }
        if len(historical) != 1:
            raise ValidationError(
                "Could not recover exactly one unambiguous CRC-valid serial.cfg "
                f"payload (found {len(historical)})"
            )
        serial_payload = next(iter(historical))
        serial_source = "unique-historical-fallback"

    serial_match = SERIAL_PATTERN.fullmatch(serial_payload)
    assert serial_match is not None
    serial_value = serial_match.group(1)

    current_count = sum(1 for n in nodes if n["current"])
    obsolete_count = sum(1 for n in nodes if n["obsolete"])
    kind_counts = Counter(n["kind"] for n in nodes)

    return {
        "nodes": nodes,
        "node_count": len(nodes),
        "current_node_count": current_count,
        "obsolete_node_count": obsolete_count,
        "kind_counts": dict(sorted(kind_counts.items())),
        "all_names": sorted(
            name.decode("ascii", errors="replace") for name in all_names
        ),
        "live_names": sorted(
            name.decode("ascii", errors="replace") for name in live_entries
        ),
        "serial_payload": serial_payload,
        "serial_value": serial_value,
        "serial_source": serial_source,
        "non_ff_bytes": sum(byte != 0xFF for byte in config),
        "ff_bytes": config.count(0xFF),
    }


def build_minimal_config(serial_payload: bytes) -> bytes:
    if not serial_payload_is_valid(serial_payload):
        raise ValidationError("Refusing to build config from an invalid serial payload")

    output = bytearray(b"\xFF" * CONFIG_SIZE)

    # Cleanmarker at the beginning of the first 16 KiB logical eraseblock.
    clean_header = struct.pack("<HHI", JFFS2_MAGIC, 0x2003, 12)
    output[0:12] = clean_header + struct.pack("<I", jffs2_crc(clean_header))

    # Root directory entry for serial.cfg. The metadata values reproduce the
    # known-good vendor recovery layout; only payload length/CRCs are dynamic.
    name = b"serial.cfg"
    dirent_offset = 12
    dirent_total = 40 + len(name)
    dirent_header = struct.pack("<HHI", JFFS2_MAGIC, 0xE001, dirent_total)
    dirent_header_crc = struct.pack("<I", jffs2_crc(dirent_header))
    dirent_fields = struct.pack("<IIII", 1, 123, 7, 1)
    dirent_fields += struct.pack("<BBH", len(name), 8, 0)
    dirent_prefix = dirent_header + dirent_header_crc + dirent_fields
    dirent = (
        dirent_prefix
        + struct.pack("<II", jffs2_crc(dirent_prefix), jffs2_crc(name))
        + name
    )
    output[dirent_offset : dirent_offset + len(dirent)] = dirent

    inode_offset = align4(dirent_offset + dirent_total)
    inode_total = 68 + len(serial_payload)
    inode_header = struct.pack("<HHI", JFFS2_MAGIC, 0xE002, inode_total)
    inode_header_crc = struct.pack("<I", jffs2_crc(inode_header))
    inode_fields = struct.pack(
        "<IIIHHIIIIIIIBBH",
        7,                  # inode
        2,                  # version
        0x81A4,             # regular file, mode 0644
        0,                  # uid
        0,                  # gid
        len(serial_payload),
        18,                 # atime
        18,                 # mtime
        18,                 # ctime
        0,                  # file offset
        len(serial_payload),
        len(serial_payload),
        0,                  # no compression
        0,
        0,
    )
    inode_prefix = inode_header + inode_header_crc + inode_fields
    inode = (
        inode_prefix
        + struct.pack(
            "<II",
            jffs2_crc(serial_payload),
            jffs2_crc(inode_prefix),
        )
        + serial_payload
    )
    if inode_offset + len(inode) > 0x4000:
        raise ValidationError("Generated serial.cfg does not fit the first eraseblock")
    output[inode_offset : inode_offset + len(inode)] = inode

    built = bytes(output)
    parsed = extract_serial_and_config_info(built)
    if parsed["serial_payload"] != serial_payload:
        raise ValidationError("Internal JFFS2 round-trip validation failed")
    if parsed["current_node_count"] != 3:
        raise ValidationError(
            "Internal JFFS2 validation expected exactly three current nodes"
        )
    return built


def invariant_bytes(image: bytes) -> bytes:
    return (
        image[:PATCH_START]
        + image[PATCH_END:HW_CHECK_START]
        + image[HW_CHECK_END:HW_UOID_START]
        + image[HW_UOID_END:CONFIG_START]
    )


def verify_confirmation_reads(
    primary: bytes, confirmation_paths: Iterable[Path]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    primary_hash = sha256(primary)

    for path in confirmation_paths:
        data, source = read_image_source(path)
        if len(data) != len(primary):
            raise ValidationError(
                f"Confirmation read {path} has {len(data)} bytes; "
                f"the primary has {len(primary)}"
            )
        if data != primary:
            summary = difference_summary(primary, data)
            raise ValidationError(
                f"Confirmation read {path} is not byte-identical to the primary: "
                f"{summary['different_bytes']} differing bytes, first=0x"
                f"{summary['first_difference']:06X}, last=0x"
                f"{summary['last_difference']:06X}"
            )
        results.append(
            {
                "filename": str(path),
                "size": len(data),
                "sha256": primary_hash,
                "byte_identical": True,
                "source_format": source["format"],
                "evidenced_identical_reads": source[
                    "evidenced_identical_reads"
                ],
            }
        )

    return results


def analyze_image(image: bytes, source_name: str = "<memory>") -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    if len(image) != FLASH_SIZE:
        raise ValidationError(
            f"{source_name}: expected exactly {FLASH_SIZE} bytes, got {len(image)}"
        )

    full_hash = sha256(image)
    exact_reference = next(
        (name for name, digest in REFERENCE_FULL_SHA256.items() if digest == full_hash),
        None,
    )

    segment_results: dict[str, Any] = {}
    for name, (start, end, expected_hash) in REFERENCE_SEGMENTS.items():
        actual_hash = sha256(image[start:end])
        ok = actual_hash == expected_hash
        segment_results[name] = {
            "start": start,
            "end_exclusive": end,
            "size": end - start,
            "expected_sha256": expected_hash,
            "actual_sha256": actual_hash,
            "match": ok,
        }
        if not ok:
            errors.append(
                f"{name} 0x{start:06X}-0x{end - 1:06X} does not match "
                "the supported reference firmware"
            )

    actual_invariant_hash = sha256(invariant_bytes(image))
    if actual_invariant_hash != INVARIANT_SHA256:
        errors.append(
            "The combined invariant firmware fingerprint does not match both "
            "independent references"
        )

    patch_region = image[PATCH_START:PATCH_END]
    patch_hash = sha256(patch_region)
    if patch_hash == ORIGINAL_PATCH_SHA256:
        system_state = "stock-unpatched"
    elif patch_hash == PATCHED_PATCH_SHA256:
        system_state = "known-bashrc-patched"
    else:
        system_state = "unknown"
        errors.append(
            "The /home/bashrc.sh SquashFS patch window is neither the exact "
            "stock version nor the exact audited patched version"
        )

    uoid = image[HW_UOID_START:HW_UOID_END]
    if UOID_PATTERN.fullmatch(uoid) is None:
        errors.append(
            "The unit-specific HWCONFIG UOID field does not have the expected "
            "94-byte ASCII structure"
        )

    check_value = image[HW_CHECK_START:HW_CHECK_END]
    if check_value in (b"\x00\x00", b"\xFF\xFF"):
        warnings.append(
            "The unit-specific two-byte HWCONFIG check value is all-zero/all-FF"
        )

    config = image[CONFIG_START:CONFIG_END]
    try:
        config_info = extract_serial_and_config_info(config)
    except ValidationError as exc:
        errors.append(str(exc))
        config_info = None

    if config_info is not None and UOID_PATTERN.fullmatch(uoid) is not None:
        serial_value = config_info["serial_value"]
        if serial_value[:12] != uoid[:12]:
            errors.append(
                "serial.cfg and the HWCONFIG UOID do not share the expected "
                "12-byte unit prefix"
            )
        if config_info["serial_source"] != "live-current":
            warnings.append(
                "serial.cfg was recovered from a unique historical JFFS2 node "
                "because no valid live serial entry was available"
            )

    if errors:
        details = "\n  - ".join(errors)
        raise ValidationError(
            f"{source_name} failed strict validation:\n  - {details}"
        )

    assert config_info is not None
    canonical_config = build_minimal_config(config_info["serial_payload"])

    return {
        "source_name": source_name,
        "size": len(image),
        "sha256": full_hash,
        "exact_reference": exact_reference,
        "invariant_sha256": actual_invariant_hash,
        "invariant_match": True,
        "segment_results": segment_results,
        "system_state": system_state,
        "system_patch_sha256": patch_hash,
        "hwconfig_check_hex": check_value.hex(),
        "uoid": uoid,
        "uoid_sha256": sha256(uoid),
        "serial_payload": config_info["serial_payload"],
        "serial_value": config_info["serial_value"],
        "serial_sha256": sha256(config_info["serial_payload"]),
        "serial_source": config_info["serial_source"],
        "serial_uoid_prefix_match": True,
        "config_node_count": config_info["node_count"],
        "config_current_node_count": config_info["current_node_count"],
        "config_obsolete_node_count": config_info["obsolete_node_count"],
        "config_kind_counts": config_info["kind_counts"],
        "config_all_names": config_info["all_names"],
        "config_live_names": config_info["live_names"],
        "config_non_ff_bytes": config_info["non_ff_bytes"],
        "config_ff_bytes": config_info["ff_bytes"],
        "config_usage_percent": config_info["non_ff_bytes"] * 100.0 / CONFIG_SIZE,
        "config_is_canonical": config == canonical_config,
        "canonical_config_sha256": sha256(canonical_config),
        "warnings": warnings,
    }


def apply_system_patch(image: bytes, state: str) -> tuple[bytes, bool]:
    if state == "known-bashrc-patched":
        return image[PATCH_START:PATCH_END], False
    if state != "stock-unpatched":
        raise ValidationError(f"Cannot patch system state {state!r}")

    return build_system_patch_window(image), True


def difference_summary(before: bytes, after: bytes) -> dict[str, Any]:
    if len(before) != len(after):
        raise ValueError("difference_summary requires equal-size buffers")

    count = 0
    first: int | None = None
    last: int | None = None
    for index, (old, new) in enumerate(zip(before, after)):
        if old != new:
            count += 1
            if first is None:
                first = index
            last = index

    return {
        "different_bytes": count,
        "first_difference": first,
        "last_difference": last,
    }


def format_analysis(analysis: dict[str, Any], show_serial: bool = False) -> str:
    serial_display = (
        analysis["serial_value"].decode("ascii")
        if show_serial
        else mask_value(analysis["serial_value"])
    )
    uoid_display = (
        analysis["uoid"].decode("ascii")
        if show_serial
        else mask_value(analysis["uoid"])
    )
    exact = analysis["exact_reference"] or "none (normal for another unit/JFFS2 history)"

    segment_lines = []
    for name, result in analysis["segment_results"].items():
        segment_lines.append(
            f"  PASS  {name:<34} "
            f"0x{result['start']:06X}-0x{result['end_exclusive'] - 1:06X}"
        )

    warning_block = ""
    if analysis["warnings"]:
        warning_block = "\nWarnings\n--------\n" + "\n".join(
            f"- {warning}" for warning in analysis["warnings"]
        ) + "\n"

    return f"""\
CC2 camera image validation
===========================

Input
-----
Name:                    {analysis['source_name']}
Size:                    {analysis['size']} bytes
SHA-256:                 {analysis['sha256']}
Exact full reference:    {exact}

Reference match
---------------
Invariant fingerprint:   PASS
Invariant SHA-256:       {analysis['invariant_sha256']}
System patch state:      {analysis['system_state']}
System window SHA-256:   {analysis['system_patch_sha256']}

Exact invariant segments:
{chr(10).join(segment_lines)}

Unit-specific data
------------------
HWCONFIG check bytes:    {analysis['hwconfig_check_hex']}
HWCONFIG UOID:           {uoid_display}
UOID SHA-256:            {analysis['uoid_sha256']}
serial.cfg source:       {analysis['serial_source']}
serial.cfg value:        {serial_display}
serial.cfg SHA-256:      {analysis['serial_sha256']}
Serial/UOID prefix:      PASS

JFFS2 config
------------
Physical use:            {analysis['config_usage_percent']:.3f}%
Non-0xFF bytes:          {analysis['config_non_ff_bytes']}
CRC-valid nodes:         {analysis['config_node_count']}
Current nodes:           {analysis['config_current_node_count']}
Obsolete nodes:          {analysis['config_obsolete_node_count']}
Names ever observed:     {', '.join(analysis['config_all_names']) or '(none)'}
Live names:              {', '.join(analysis['config_live_names']) or '(none)'}
Already canonical:       {'yes' if analysis['config_is_canonical'] else 'no'}
Canonical config SHA-256:{analysis['canonical_config_sha256']}
{warning_block}
Result: STRICT VALIDATION PASSED
"""


def write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def build_recovery(
    input_path: Path,
    output_dir: Path,
    *,
    confirmation_paths: Iterable[Path],
    keep_config: bool,
    overwrite: bool,
    show_serial: bool,
    allow_fewer_reads: bool,
) -> dict[str, Any]:
    confirmation_paths = tuple(confirmation_paths)
    image, input_source = read_image_source(input_path)
    confirmed_reads = verify_confirmation_reads(image, confirmation_paths)
    source_name = str(input_path)
    if input_source["image_member"] is not None:
        source_name += f"!{input_source['image_member']}"
    analysis = analyze_image(image, source_name)

    total_reads = max(
        input_source["evidenced_identical_reads"],
        1 + len(confirmed_reads),
        *(item["evidenced_identical_reads"] for item in confirmed_reads),
    )
    insufficient_reads = (
        total_reads < MIN_IDENTICAL_READS and analysis["exact_reference"] is None
    )
    if insufficient_reads and not allow_fewer_reads:
        raise ValidationError(
            f"This dump is not an exact known reference and only {total_reads} "
            f"identical read(s) were supplied. Provide {MIN_IDENTICAL_READS} "
            "total physical reads with --confirm, or explicitly accept the "
            "higher risk with --allow-fewer-reads."
        )

    if keep_config and not analysis["config_is_canonical"]:
        raise ValidationError(
            "--keep-config is allowed only for the exact canonical rebuilt "
            "config. Preserving a noncanonical or exhausted JFFS2 partition "
            "could leave the camera bricked."
        )

    output_resolved = output_dir.resolve()
    protected_inputs = (input_path, *confirmation_paths)
    for protected in protected_inputs:
        protected_resolved = protected.resolve()
        if protected_resolved == output_resolved or protected_resolved.is_relative_to(
            output_resolved
        ):
            raise ValidationError(
                f"Refusing an output directory that contains input dump {protected}"
            )

    if output_dir.exists():
        if output_dir.is_symlink():
            raise ValidationError(f"Refusing symlink output directory: {output_dir}")
        if not overwrite:
            raise ValidationError(
                f"Output directory already exists: {output_dir}. "
                "Use --overwrite only after checking its contents."
            )
        if output_dir.is_file():
            raise ValidationError(f"Output path is a file: {output_dir}")
        for child in output_dir.iterdir():
            if child.name not in OUTPUT_FILE_NAMES or child.is_dir():
                raise ValidationError(
                    "Refusing to overwrite a directory containing an unknown "
                    f"entry: {child}"
                )
        for child in output_dir.iterdir():
            child.unlink()
    else:
        output_dir.mkdir(parents=True)

    recovery = bytearray(image)

    patched_region, patch_changed = apply_system_patch(
        image, analysis["system_state"]
    )
    recovery[PATCH_START:PATCH_END] = patched_region

    canonical_config = build_minimal_config(analysis["serial_payload"])
    if keep_config:
        config_changed = False
    else:
        config_changed = image[CONFIG_START:CONFIG_END] != canonical_config
        recovery[CONFIG_START:CONFIG_END] = canonical_config

    recovery_bytes = bytes(recovery)

    # Prove that no bytes outside the two allowed regions changed.
    if (
        recovery_bytes[:PATCH_START] != image[:PATCH_START]
        or recovery_bytes[PATCH_END:CONFIG_START]
        != image[PATCH_END:CONFIG_START]
        or recovery_bytes[CONFIG_END:] != image[CONFIG_END:]
    ):
        raise ValidationError("Internal safety check: bytes changed outside allowed regions")

    # Re-run the complete strict validator on the generated image.
    output_analysis = analyze_image(
        recovery_bytes, source_name="generated recovery image"
    )
    if output_analysis["system_state"] != "known-bashrc-patched":
        raise ValidationError("Generated image is not in the known patched system state")
    if not keep_config and not output_analysis["config_is_canonical"]:
        raise ValidationError("Generated config is not canonical")

    changed_regions: list[dict[str, Any]] = []
    if patch_changed:
        changed_regions.append(
            {
                "name": "system_bashrc_patch",
                "start": PATCH_START,
                "end_inclusive": PATCH_END - 1,
                "size": PATCH_SIZE,
            }
        )
    if config_changed:
        changed_regions.append(
            {
                "name": "config",
                "start": CONFIG_START,
                "end_inclusive": CONFIG_END - 1,
                "size": CONFIG_SIZE,
            }
        )

    output_name = "cc2-camera-recovery.bin"
    layout_name = "cc2-camera-layout.txt"
    output_path = output_dir / output_name
    output_path.write_bytes(recovery_bytes)
    (output_dir / "config-restored.bin").write_bytes(canonical_config)
    (output_dir / "serial.cfg").write_bytes(analysis["serial_payload"])

    layout = """\
00000000:00462fff immutable_before_patch
00463000:0046afff system_bashrc_patch
0046b000:007dffff immutable_after_patch
007e0000:007fffff config
"""
    write_text(output_dir / layout_name, layout)

    tool_filename = Path(__file__).name
    manifest = {
        "tool": {
            "name": tool_filename,
            "version": TOOL_VERSION,
        },
        "input": {
            "filename": input_path.name,
            "source_format": input_source["format"],
            "image_member": input_source["image_member"],
            "size": len(image),
            "sha256": sha256(image),
            "acquisition_evidence": {
                key: value
                for key, value in input_source.items()
                if key
                not in {
                    "format",
                    "image_member",
                    "size",
                    "sha256",
                    "md5",
                    "evidenced_identical_reads",
                }
            },
            "confirmed_reads": confirmed_reads,
            "total_identical_reads": total_reads,
            "fewer_reads_explicitly_allowed": (
                insufficient_reads and allow_fewer_reads
            ),
        },
        "validation": {
            "invariant_sha256": analysis["invariant_sha256"],
            "invariant_match": True,
            "exact_full_reference": analysis["exact_reference"],
            "system_state_before": analysis["system_state"],
            "system_state_after": output_analysis["system_state"],
            "serial_source": analysis["serial_source"],
            "serial_masked": mask_value(analysis["serial_value"]),
            "serial_sha256": analysis["serial_sha256"],
            "uoid_masked": mask_value(analysis["uoid"]),
            "serial_uoid_prefix_match": True,
            "config_usage_percent_before": round(
                analysis["config_usage_percent"], 6
            ),
            "config_obsolete_nodes_before": analysis[
                "config_obsolete_node_count"
            ],
        },
        "output": {
            "filename": output_name,
            "size": len(recovery_bytes),
            "sha256": sha256(recovery_bytes),
            "canonical_config_sha256": sha256(canonical_config),
        },
        "changed_regions": changed_regions,
        "keep_config": keep_config,
        "reference_full_sha256": REFERENCE_FULL_SHA256,
    }
    write_text(
        output_dir / "MANIFEST.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )

    source_report = format_analysis(analysis, show_serial=show_serial)
    source_report += (
        f"\nPhysical-read confirmation\n--------------------------\n"
        f"Byte-identical reads supplied: {total_reads}\n"
    )
    for item in confirmed_reads:
        source_report += f"- {item['filename']}  {item['sha256']}\n"
    if insufficient_reads:
        source_report += (
            "WARNING: fewer than three reads were supplied for a non-reference "
            "image; the higher risk was explicitly accepted.\n"
        )
    output_report = format_analysis(output_analysis, show_serial=show_serial)
    validation_text = (
        source_report
        + "\nGenerated image\n===============\n\n"
        + f"Output SHA-256:         {sha256(recovery_bytes)}\n"
        + f"System patch changed:   {'yes' if patch_changed else 'no'}\n"
        + f"Config changed:         {'yes' if config_changed else 'no'}\n"
        + f"keep_config requested:  {'yes' if keep_config else 'no'}\n"
        + f"Changed regions:        "
        + (
            ", ".join(region["name"] for region in changed_regions)
            if changed_regions
            else "(none; input already equals generated image)"
        )
        + "\n\nPost-build validation\n---------------------\n"
        + output_report
    )
    write_text(output_dir / "VALIDATION.txt", validation_text)

    selected = " ".join(
        f"-i {region['name']}" for region in changed_regions
    )
    if changed_regions:
        write_command = (
            "flashrom "
            "-p buspirate_spi:dev=COM11,spispeed=1M "
            f"-l {layout_name} {selected} -w {output_name}"
        )
        write_section = f"""\
WRITE COMMAND TEMPLATE
----------------------
Edit COM11 and speed settings for your programmer, then run from this folder:

{write_command}

The command intentionally omits --progress because some flashrom versions
spam progress-accounting warnings. Flashrom still performs verification.

The command intentionally does not enable programmer-supplied power. Verify
the flash chip's required voltage and your wiring first, then power it using
the hardware method you have independently tested while reading.
"""
    else:
        write_section = """\
WRITE COMMAND
-------------
No write is necessary: the input already equals the generated recovery image.
"""

    read_warning = ""
    if insufficient_reads:
        read_warning = """\
READ-CONFIDENCE WARNING
-----------------------
This non-reference image was built from fewer than three byte-identical
physical reads. Re-read the flash and rebuild before writing if at all possible.

"""

    flashing = f"""\
CC2 CAMERA RECOVERY — GENERATED INSTRUCTIONS
============================================

Input SHA-256:
{sha256(image)}

Recovery SHA-256:
{sha256(recovery_bytes)}

{read_warning}
{write_section}
FULL READBACK
-------------
Keep the same stable programmer connection and make a complete 8 MiB read:

flashrom -p buspirate_spi:dev=COM11,spispeed=1M -r cc2-camera-readback.bin

Then verify it with this tool:

py {tool_filename} verify {output_name} cc2-camera-readback.bin

Never connect normal USB/device power and programmer-supplied target power at
the same time. Confirm the SPI voltage from the exact flash part marking or
datasheet before making any powered connection.

PRIVACY
-------
serial.cfg and the generated recovery image contain this camera's unique
identifier. Do not publish either file unredacted.
"""
    write_text(output_dir / "FLASHING.txt", flashing)

    hashes = []
    for name in [
        output_name,
        "config-restored.bin",
        "serial.cfg",
        layout_name,
        "MANIFEST.json",
        "VALIDATION.txt",
        "FLASHING.txt",
    ]:
        content = (output_dir / name).read_bytes()
        hashes.append(f"{sha256(content)}  {name}")
    write_text(output_dir / "SHA256SUMS.txt", "\n".join(hashes) + "\n")

    return {
        "analysis": analysis,
        "output_analysis": output_analysis,
        "manifest": manifest,
        "confirmed_reads": confirmed_reads,
        "output_dir": output_dir,
        "output_path": output_path,
    }


def verify_readback(expected_path: Path, readback_path: Path) -> None:
    expected = expected_path.read_bytes()
    actual = readback_path.read_bytes()

    expected_analysis = analyze_image(expected, str(expected_path))
    if expected_analysis["system_state"] != "known-bashrc-patched":
        raise ValidationError("Expected image is not in the audited patched state")
    if not expected_analysis["config_is_canonical"]:
        raise ValidationError("Expected image does not contain canonical recovered config")

    if len(expected) != len(actual):
        raise ValidationError(
            f"Size mismatch: expected {len(expected)} bytes, "
            f"readback has {len(actual)} bytes"
        )

    if expected == actual:
        actual_analysis = analyze_image(actual, str(readback_path))
        if (
            actual_analysis["system_state"] != "known-bashrc-patched"
            or not actual_analysis["config_is_canonical"]
        ):
            raise ValidationError("Readback is identical but not a valid recovery image")
        print("READBACK VERIFIED: byte-for-byte identical")
        print(f"SHA-256: {sha256(actual)}")
        return

    summary = difference_summary(expected, actual)
    affected_flags = {
        "system_bashrc_patch": False,
        "config": False,
        "other": False,
    }
    for index, (left, right) in enumerate(zip(expected, actual)):
        if left == right:
            continue
        if PATCH_START <= index < PATCH_END:
            affected_flags["system_bashrc_patch"] = True
        elif CONFIG_START <= index < CONFIG_END:
            affected_flags["config"] = True
        else:
            affected_flags["other"] = True
    affected = [name for name, value in affected_flags.items() if value]

    raise ValidationError(
        "Readback differs from the expected image: "
        f"{summary['different_bytes']} bytes; first=0x"
        f"{summary['first_difference']:06X}, last=0x"
        f"{summary['last_difference']:06X}; affected={', '.join(affected)}"
    )


def self_test(image_path: Path | None = None) -> None:
    padding = len(ORIGINAL_COPY_BLOCK) - len(PATCHED_COPY_BLOCK_VISIBLE)
    if padding < 0:
        raise ValidationError("Readable bashrc patch block is too large")
    replacement = PATCHED_COPY_BLOCK_VISIBLE[:-1] + b" " * padding + b"\n"
    if len(replacement) != len(ORIGINAL_COPY_BLOCK):
        raise ValidationError("Readable bashrc patch does not preserve length")

    xz_vector = (b"CC2 auditable SquashFS patch self-test\n" * 257) + b"end"
    xz_stream = build_squashfs_xz_fragment(xz_vector)
    if lzma.decompress(xz_stream) != xz_vector:
        raise ValidationError("Deterministic XZ builder self-test failed")

    dummy = b"serial=12PSSSS4TEST000000000000000000000000\n"
    # Dummy must retain the exact 36-character serial value format.
    if not serial_payload_is_valid(dummy):
        raise ValidationError("Internal serial regex self-test vector is invalid")
    config = build_minimal_config(dummy)
    parsed = extract_serial_and_config_info(config)
    if parsed["serial_payload"] != dummy:
        raise ValidationError("JFFS2 self-test failed")

    image_result = "not requested"
    if image_path is not None:
        image, source = read_image_source(image_path)
        source_name = str(image_path)
        if source["image_member"] is not None:
            source_name += f"!{source['image_member']}"
        analysis = analyze_image(image, source_name)
        if analysis["system_state"] == "stock-unpatched":
            patched = build_system_patch_window(image)
            if sha256(patched) != PATCHED_PATCH_SHA256:
                raise ValidationError("Full system patch self-test failed")
            image_result = "stock image generated the exact audited patch window"
        else:
            image_result = "image already contains the exact audited patch window"

    print("SELF-TEST PASSED")
    print(f"Tool version: {TOOL_VERSION}")
    print(f"Readable bashrc replacement padding: {padding} bytes")
    print(f"Deterministic XZ test SHA-256: {sha256(xz_stream)}")
    print(f"Generated JFFS2 SHA-256: {sha256(config)}")
    print(f"Image patch test: {image_result}")


def cmd_analyze(args: argparse.Namespace) -> None:
    path = Path(args.image)
    image, source = read_image_source(path)
    confirmations = verify_confirmation_reads(
        image, [Path(item) for item in args.confirm]
    )
    source_name = str(path)
    if source["image_member"] is not None:
        source_name += f"!{source['image_member']}"
    print(format_analysis(analyze_image(image, source_name), show_serial=args.show_serial))
    evidenced_reads = max(
        source["evidenced_identical_reads"],
        1 + len(confirmations),
        *(item["evidenced_identical_reads"] for item in confirmations),
    )
    print(f"Byte-identical physical reads evidenced: {evidenced_reads}")
    if source["format"] == USB_BACKUP_FORMAT:
        print(
            "  USB backup: "
            f"{source['consecutive_identical_reads']} consecutive identical "
            f"reads in {source['read_passes']} attempt(s)"
        )
    for item in confirmations:
        print(f"  {item['filename']}  {item['sha256']}")


def cmd_build(args: argparse.Namespace) -> None:
    input_path = Path(args.image)
    output_dir = (
        Path(args.output)
        if args.output
        else input_path.with_name(f"{input_path.stem}-cc2-recovery")
    )
    result = build_recovery(
        input_path,
        output_dir,
        confirmation_paths=[Path(item) for item in args.confirm],
        keep_config=args.keep_config,
        overwrite=args.overwrite,
        show_serial=args.show_serial,
        allow_fewer_reads=args.allow_fewer_reads,
    )
    manifest = result["manifest"]
    print("RECOVERY BUILD PASSED")
    print(f"Output directory: {result['output_dir']}")
    print(f"Recovery image:   {result['output_path']}")
    print(f"SHA-256:          {manifest['output']['sha256']}")
    print(
        f"Identical reads:  {manifest['input']['total_identical_reads']}"
    )
    if manifest["changed_regions"]:
        print(
            "Regions to write: "
            + ", ".join(region["name"] for region in manifest["changed_regions"])
        )
    else:
        print("Regions to write: none")


def cmd_verify(args: argparse.Namespace) -> None:
    verify_readback(Path(args.expected), Path(args.readback))


def cmd_self_test(args: argparse.Namespace) -> None:
    self_test(Path(args.image) if args.image else None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Strict validator and recovery-image builder for the exact CC2 "
            "camera firmware family observed in two independent dumps."
        )
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {TOOL_VERSION}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser(
        "analyze", help="validate a dump without creating a recovery image"
    )
    analyze.add_argument(
        "image", help="primary raw 8 MiB dump or cc2flash backup ZIP"
    )
    analyze.add_argument(
        "--confirm",
        nargs="*",
        default=[],
        metavar="DUMP",
        help="additional physical reads that must be byte-identical",
    )
    analyze.add_argument(
        "--show-serial",
        action="store_true",
        help="print the full unit identifiers instead of masking them",
    )
    analyze.set_defaults(func=cmd_analyze)

    build = subparsers.add_parser(
        "build", help="validate a dump and generate a recovery bundle"
    )
    build.add_argument(
        "image", help="primary raw 8 MiB dump or cc2flash backup ZIP"
    )
    build.add_argument(
        "--confirm",
        nargs="*",
        default=[],
        metavar="DUMP",
        help="additional physical reads that must be byte-identical",
    )
    build.add_argument(
        "-o", "--output", help="output directory (default: beside the input)"
    )
    build.add_argument(
        "--keep-config",
        action="store_true",
        help=(
            "leave config untouched only if it is already the exact canonical "
            "rebuild; exhausted/noncanonical config is refused"
        ),
    )
    build.add_argument(
        "--allow-fewer-reads",
        action="store_true",
        help=(
            "explicitly accept fewer than three identical physical reads for "
            "a dump that is not an exact known reference"
        ),
    )
    build.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing output directory",
    )
    build.add_argument(
        "--show-serial",
        action="store_true",
        help="include full identifiers in VALIDATION.txt",
    )
    build.set_defaults(func=cmd_build)

    verify = subparsers.add_parser(
        "verify", help="compare a full programmer readback with a generated image"
    )
    verify.add_argument("expected", help="generated recovery image")
    verify.add_argument("readback", help="full 8 MiB readback")
    verify.set_defaults(func=cmd_verify)

    test = subparsers.add_parser(
        "self-test", help="check the readable patch, XZ, and JFFS2 implementation"
    )
    test.add_argument(
        "image",
        nargs="?",
        help=(
            "optional supported raw image or cc2flash backup ZIP for a "
            "complete system-patch self-test"
        ),
    )
    test.set_defaults(func=cmd_self_test)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (OSError, ValidationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
