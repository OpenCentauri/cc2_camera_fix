#!/usr/bin/env python3
"""Dependency-free consistency checks for the reconstructed packet formats."""

from __future__ import annotations

import hashlib
import struct


NORMAL_SIZE = 1024


def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    for byte in data:
        crc ^= byte
        for _ in range(8):
            old_lsb = crc & 1
            crc >>= 1
            if old_lsb:
                crc ^= 0x1021
    return crc & 0xFFFF


def normal_request(command: int, payload: bytes = b"", frame_type: int = 1,
                   auxiliary: int = 0) -> bytes:
    report = bytearray(NORMAL_SIZE)
    struct.pack_into("<B2sHBHI", report, 0, 1, b"\x5a\x5a", command,
                     frame_type, len(payload), auxiliary)
    report[14:14 + len(payload)] = payload
    covered = bytes(report[:12])
    if frame_type in (1, 2):
        covered += payload
    struct.pack_into("<H", report, 12, crc16(covered))
    return bytes(report)


def updater_frame(direction_magic: bytes, frame_type: int,
                  payload: bytes) -> bytes:
    frame = bytearray(direction_magic)
    frame.append(frame_type)
    frame += struct.pack("<H", len(payload) + 7)
    frame += payload
    frame.append(sum(frame) & 0xFF)
    return bytes(frame)


def check() -> None:
    assert crc16(b"123456789") == 0x1DBA

    config_get = {
        0x2000, 0x2010, 0x2020, 0x2030, 0x2040, 0x2050,
        0x2F00, 0x2F10, 0x2F20, 0x2F30, 0x2F40, 0x2F50,
        0x2F60, 0x2F70, 0x2F80, 0x2F90, 0x2FA0, 0x2FB0,
        0x2FC0, 0x2FD0, 0x2FE0,
    }
    config_all = config_get | {command + 1 for command in config_get}
    assert len(config_get) == 21
    assert len(config_all) == 42

    upload_commands = {
        0x3000, 0x3110, 0x3111, 0x3112, 0x3113, 0x3114, 0x3115,
        0x3116, 0x3120, 0x3150, 0x31F0, 0x3200, 0x3300,
    }
    assert len(upload_commands) == 13

    trigger_payload = struct.pack("<II", 0x55504454, 0x010203A1)
    trigger = normal_request(0x4000, trigger_payload)
    assert len(trigger) == 1024
    assert trigger[:14].hex() == "015a5a004001080000000000f80e"
    assert trigger[14:22] == b"TDPU\xa1\x03\x02\x01"

    metadata_payload = struct.pack("<HIHH", 3060, 128 + 4096, 1, 0)
    metadata = updater_frame(b"\x80\x00\xee", 1, metadata_payload)
    assert len(metadata) == 17
    assert struct.unpack_from("<H", metadata, 4)[0] == 17
    assert sum(metadata[:-1]) & 0xFF == metadata[-1]

    ack7 = updater_frame(b"\x81\x00\xee", 2, struct.pack("<I", 7))
    assert ack7[6:10] == b"\x07\x00\x00\x00"
    assert len(ack7) == 11

    image = bytes(range(256)) * 16
    header = bytearray(128)
    struct.pack_into("<II", header, 4, 0x00040000, len(image))
    header[12:28] = hashlib.md5(image).digest()
    stream = bytes(header) + image
    assert len(stream) == 128 + len(image)
    assert hashlib.md5(stream[128:]).digest() == stream[12:28]


if __name__ == "__main__":
    check()
    print("all reconstructed protocol vectors pass")
