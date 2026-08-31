#!/usr/bin/env python3
"""Minimal read-only SquashFS v4 extractor used for the CC2 flash analysis.

This intentionally supports only the features present in the supplied image:
little-endian SquashFS v4, XZ-compressed metadata/data, basic/extended regular
files and directories, symlinks, and fragment tails.  It never writes to the
input image.
"""

from __future__ import annotations

import argparse
import dataclasses
import lzma
import os
import stat
import struct
from pathlib import Path, PurePosixPath


SQUASHFS_MAGIC = 0x73717368
METADATA_UNCOMPRESSED = 0x8000
DATA_UNCOMPRESSED = 0x01000000
INVALID_FRAGMENT = 0xFFFFFFFF
METADATA_BLOCK_SIZE = 8192


@dataclasses.dataclass(frozen=True)
class Superblock:
    magic: int
    inode_count: int
    mkfs_time: int
    block_size: int
    fragment_count: int
    compression: int
    block_log: int
    flags: int
    id_count: int
    major: int
    minor: int
    root_inode: int
    bytes_used: int
    id_table_start: int
    xattr_table_start: int
    inode_table_start: int
    directory_table_start: int
    fragment_table_start: int
    export_table_start: int

    STRUCT = struct.Struct("<5I6H8Q")

    @classmethod
    def read(cls, image, base: int) -> "Superblock":
        image.seek(base)
        raw = image.read(cls.STRUCT.size)
        if len(raw) != cls.STRUCT.size:
            raise ValueError("truncated SquashFS superblock")
        sb = cls(*cls.STRUCT.unpack(raw))
        if sb.magic != SQUASHFS_MAGIC:
            raise ValueError(f"bad SquashFS magic: 0x{sb.magic:08x}")
        if (sb.major, sb.minor) != (4, 0):
            raise ValueError(f"unsupported SquashFS version {sb.major}.{sb.minor}")
        if sb.compression != 4:
            raise ValueError(f"unsupported compressor id {sb.compression}; expected XZ (4)")
        return sb


@dataclasses.dataclass
class Inode:
    ref: int
    kind: int
    mode: int
    inode_number: int
    start_block: int | None = None
    file_size: int = 0
    fragment: int = INVALID_FRAGMENT
    fragment_offset: int = 0
    block_sizes: tuple[int, ...] = ()
    directory_offset: int = 0
    symlink: bytes | None = None
    rdev: int | None = None


class SquashFS:
    def __init__(self, image_path: Path, base: int):
        self.image_path = image_path
        self.base = base
        self.image = image_path.open("rb")
        self.sb = Superblock.read(self.image, base)
        self._metadata_cache: dict[int, tuple[bytes, int]] = {}
        self._fragment_cache: dict[int, bytes] = {}

    def close(self) -> None:
        self.image.close()

    def _read_at(self, relative_offset: int, size: int) -> bytes:
        self.image.seek(self.base + relative_offset)
        data = self.image.read(size)
        if len(data) != size:
            raise ValueError(f"truncated read at 0x{relative_offset:x}: wanted {size}, got {len(data)}")
        return data

    def _metadata_block(self, relative_offset: int) -> tuple[bytes, int]:
        cached = self._metadata_cache.get(relative_offset)
        if cached is not None:
            return cached
        header = struct.unpack("<H", self._read_at(relative_offset, 2))[0]
        stored_size = header & 0x7FFF
        payload = self._read_at(relative_offset + 2, stored_size)
        if header & METADATA_UNCOMPRESSED:
            data = payload
        else:
            data = lzma.decompress(payload)
        if len(data) > METADATA_BLOCK_SIZE:
            raise ValueError(f"oversized metadata block at 0x{relative_offset:x}")
        result = (data, relative_offset + 2 + stored_size)
        self._metadata_cache[relative_offset] = result
        return result

    def _read_metadata(self, table_start: int, block_offset: int, inner_offset: int, size: int) -> bytes:
        out = bytearray()
        block = table_start + block_offset
        offset = inner_offset
        while len(out) < size:
            data, next_block = self._metadata_block(block)
            take = min(size - len(out), len(data) - offset)
            if take <= 0:
                raise ValueError("metadata reference points outside block")
            out += data[offset : offset + take]
            block = next_block
            offset = 0
        return bytes(out)

    def _metadata_stream(self, table_start: int, block_offset: int, inner_offset: int):
        block = table_start + block_offset
        offset = inner_offset
        while True:
            data, next_block = self._metadata_block(block)
            while offset < len(data):
                yield data[offset]
                offset += 1
            block = next_block
            offset = 0

    @staticmethod
    def _take(stream, size: int) -> bytes:
        return bytes(next(stream) for _ in range(size))

    def read_inode(self, ref: int) -> Inode:
        block_offset, inner_offset = ref >> 16, ref & 0xFFFF
        stream = self._metadata_stream(self.sb.inode_table_start, block_offset, inner_offset)
        base = self._take(stream, 16)
        kind, mode, _uid, _gid, _mtime, inode_number = struct.unpack("<4H2I", base)

        if kind == 1:  # basic directory
            start, _nlink, file_size, offset, _parent = struct.unpack("<IIHHI", self._take(stream, 16))
            return Inode(ref, kind, mode, inode_number, start, file_size, directory_offset=offset)
        if kind == 8:  # extended directory
            _nlink, file_size, start, _parent, _icount, offset, _xattr = struct.unpack(
                "<4I2HI", self._take(stream, 24)
            )
            return Inode(ref, kind, mode, inode_number, start, file_size, directory_offset=offset)
        if kind == 2:  # basic regular file
            start, fragment, frag_offset, file_size = struct.unpack("<4I", self._take(stream, 16))
            full_blocks = file_size // self.sb.block_size
            block_count = full_blocks + (1 if fragment == INVALID_FRAGMENT and file_size % self.sb.block_size else 0)
            sizes = struct.unpack(f"<{block_count}I", self._take(stream, 4 * block_count)) if block_count else ()
            return Inode(ref, kind, mode, inode_number, start, file_size, fragment, frag_offset, sizes)
        if kind == 9:  # extended regular file
            start, file_size, _sparse, _nlink, fragment, frag_offset, _xattr = struct.unpack(
                "<3Q4I", self._take(stream, 40)
            )
            full_blocks = file_size // self.sb.block_size
            block_count = full_blocks + (1 if fragment == INVALID_FRAGMENT and file_size % self.sb.block_size else 0)
            sizes = struct.unpack(f"<{block_count}I", self._take(stream, 4 * block_count)) if block_count else ()
            return Inode(ref, kind, mode, inode_number, start, file_size, fragment, frag_offset, sizes)
        if kind in (3, 10):  # symlink / extended symlink
            _nlink, target_size = struct.unpack("<II", self._take(stream, 8))
            target = self._take(stream, target_size)
            if kind == 10:
                self._take(stream, 4)  # xattr index
            return Inode(ref, kind, mode, inode_number, symlink=target, file_size=target_size)
        if kind in (4, 5, 11, 12):  # block/char device, basic/extended
            _nlink, rdev = struct.unpack("<II", self._take(stream, 8))
            if kind in (11, 12):
                self._take(stream, 4)
            return Inode(ref, kind, mode, inode_number, rdev=rdev)
        if kind in (6, 7, 13, 14):  # fifo/socket, basic/extended
            self._take(stream, 8 if kind in (13, 14) else 4)
            return Inode(ref, kind, mode, inode_number)
        raise ValueError(f"unsupported inode type {kind} at ref 0x{ref:x}")

    def read_directory(self, inode: Inode) -> list[tuple[str, int, int]]:
        if inode.kind not in (1, 8) or inode.start_block is None:
            raise ValueError("not a directory inode")
        # SquashFS directory sizes include three bytes of historical overhead.
        size = max(0, inode.file_size - 3)
        raw = self._read_metadata(
            self.sb.directory_table_start, inode.start_block, inode.directory_offset, size
        )
        out: list[tuple[str, int, int]] = []
        pos = 0
        while pos < len(raw):
            if pos + 12 > len(raw):
                raise ValueError("truncated directory header")
            count_minus_one, inode_block, inode_base = struct.unpack_from("<III", raw, pos)
            pos += 12
            for _ in range(count_minus_one + 1):
                if pos + 8 > len(raw):
                    raise ValueError("truncated directory entry")
                inode_offset, inode_delta, kind, name_size_minus_one = struct.unpack_from("<HhHH", raw, pos)
                pos += 8
                name_size = name_size_minus_one + 1
                name_raw = raw[pos : pos + name_size]
                if len(name_raw) != name_size:
                    raise ValueError("truncated directory name")
                pos += name_size
                name = name_raw.decode("utf-8", "surrogateescape")
                out.append((name, (inode_block << 16) | inode_offset, kind))
        return out

    def _fragment_entry(self, index: int) -> tuple[int, int]:
        entries_per_metadata_block = METADATA_BLOCK_SIZE // 16
        pointer_index = index // entries_per_metadata_block
        entry_index = index % entries_per_metadata_block
        table_block = struct.unpack(
            "<Q", self._read_at(self.sb.fragment_table_start + pointer_index * 8, 8)
        )[0]
        raw = self._read_metadata(table_block, 0, entry_index * 16, 16)
        start, stored_size, _unused = struct.unpack("<QII", raw)
        return start, stored_size

    def _fragment_data(self, index: int) -> bytes:
        cached = self._fragment_cache.get(index)
        if cached is not None:
            return cached
        start, size_word = self._fragment_entry(index)
        stored_size = size_word & 0x00FFFFFF
        payload = self._read_at(start, stored_size)
        data = payload if size_word & DATA_UNCOMPRESSED else lzma.decompress(payload)
        self._fragment_cache[index] = data
        return data

    def read_file(self, inode: Inode) -> bytes:
        if inode.kind not in (2, 9) or inode.start_block is None:
            raise ValueError("not a regular-file inode")
        out = bytearray()
        data_offset = inode.start_block
        remaining = inode.file_size
        for size_word in inode.block_sizes:
            logical_size = min(remaining, self.sb.block_size)
            stored_size = size_word & 0x00FFFFFF
            if stored_size == 0:
                out += bytes(logical_size)
            else:
                payload = self._read_at(data_offset, stored_size)
                data_offset += stored_size
                block = payload if size_word & DATA_UNCOMPRESSED else lzma.decompress(payload)
                if len(block) != logical_size:
                    raise ValueError(f"bad data block size: expected {logical_size}, got {len(block)}")
                out += block
            remaining -= logical_size
        if remaining:
            if inode.fragment == INVALID_FRAGMENT:
                raise ValueError("regular file has missing data")
            fragment = self._fragment_data(inode.fragment)
            tail = fragment[inode.fragment_offset : inode.fragment_offset + remaining]
            if len(tail) != remaining:
                raise ValueError("fragment tail is truncated")
            out += tail
        if len(out) != inode.file_size:
            raise ValueError(f"bad extracted size: expected {inode.file_size}, got {len(out)}")
        return bytes(out)

    @staticmethod
    def _safe_component(name: str) -> str:
        if not name or name in (".", "..") or "/" in name or "\x00" in name:
            raise ValueError(f"unsafe directory entry {name!r}")
        return name

    def extract(self, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        root = self.read_inode(self.sb.root_inode)
        self._extract_inode(root, destination, PurePosixPath("/"))

    def _extract_inode(self, inode: Inode, target: Path, display_path: PurePosixPath) -> None:
        if inode.kind in (1, 8):
            target.mkdir(parents=True, exist_ok=True)
            for name, ref, _kind in self.read_directory(inode):
                safe_name = self._safe_component(name)
                self._extract_inode(self.read_inode(ref), target / safe_name, display_path / safe_name)
            os.chmod(target, stat.S_IMODE(inode.mode))
        elif inode.kind in (2, 9):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.read_file(inode))
            os.chmod(target, stat.S_IMODE(inode.mode))
        elif inode.kind in (3, 10):
            assert inode.symlink is not None
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(inode.symlink.decode("utf-8", "surrogateescape"), target)
        else:
            # Device nodes, fifos, and sockets are recorded but deliberately not
            # recreated by this unprivileged analysis helper.
            target.parent.mkdir(parents=True, exist_ok=True)
            target.with_name(target.name + ".special").write_text(
                f"SquashFS special inode type={inode.kind} mode=0{inode.mode:o} rdev={inode.rdev}\n"
            )


def parse_int(value: str) -> int:
    return int(value, 0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--offset", type=parse_int, required=True)
    args = parser.parse_args()
    fs = SquashFS(args.image, args.offset)
    try:
        print(dataclasses.asdict(fs.sb))
        fs.extract(args.destination)
    finally:
        fs.close()


if __name__ == "__main__":
    main()
