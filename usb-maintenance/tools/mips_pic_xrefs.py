#!/usr/bin/env python3
"""Find GP-relative string/data references in a raw MIPS PIC image."""

from __future__ import annotations

import argparse
import re
import struct
from pathlib import Path


def sx16(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def read_word(data: bytes, offset: int) -> int | None:
    if offset < 0 or offset + 4 > len(data):
        return None
    return struct.unpack_from("<I", data, offset)[0]


def ascii_at(data: bytes, offset: int, limit: int = 500) -> str | None:
    if not (0 <= offset < len(data)):
        return None
    end = data.find(b"\0", offset, min(len(data), offset + limit))
    if end <= offset:
        return None
    raw = data[offset:end]
    if len(raw) < 3 or any(b not in b"\t\r\n" and not 0x20 <= b <= 0x7E for b in raw):
        return None
    return raw.decode("ascii", "replace")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("--base", type=lambda s: int(s, 0), required=True)
    parser.add_argument("--start", type=lambda s: int(s, 0), default=0)
    parser.add_argument("--end", type=lambda s: int(s, 0))
    parser.add_argument("--match", default=".", help="case-insensitive regex for referenced strings")
    args = parser.parse_args()
    data = args.binary.read_bytes()
    end = len(data) if args.end is None else min(args.end, len(data))
    pattern = re.compile(args.match, re.I)

    functions: list[tuple[int, int]] = []
    for off in range(args.start, end - 12, 4):
        a, b, c = read_word(data, off), read_word(data, off + 4), read_word(data, off + 8)
        if a is None or b is None or c is None:
            continue
        if (a & 0xFFFF0000) == 0x3C1C0000 and (b & 0xFFFF0000) == 0x279C0000 and c == 0x0399E021:
            delta = ((a & 0xFFFF) << 16) + sx16(b & 0xFFFF)
            va = args.base + off
            gp = (va + delta) & 0xFFFFFFFF
            functions.append((off, gp))

    for index, (start, gp) in enumerate(functions):
        stop = functions[index + 1][0] if index + 1 < len(functions) else end
        # A few leaf/helper functions can sit between PIC prologues. Cap absurd
        # gaps so unrelated data is not treated as code.
        stop = min(stop, start + 0x6000)
        regs: list[int | None] = [None] * 32
        regs[0], regs[28], regs[25] = 0, gp, args.base + start
        found: list[tuple[int, str, int]] = []

        for off in range(start, stop, 4):
            word = read_word(data, off)
            if word is None:
                break
            op = word >> 26
            rs, rt, rd = (word >> 21) & 31, (word >> 16) & 31, (word >> 11) & 31
            imm = word & 0xFFFF
            pc = args.base + off
            out_reg: int | None = None
            value: int | None = None

            if op == 15:  # lui
                out_reg, value = rt, imm << 16
            elif op in (8, 9) and regs[rs] is not None:  # addi/addiu
                out_reg, value = rt, (regs[rs] + sx16(imm)) & 0xFFFFFFFF
            elif op == 13 and regs[rs] is not None:  # ori
                out_reg, value = rt, (regs[rs] | imm) & 0xFFFFFFFF
            elif op == 35 and regs[rs] is not None:  # lw
                address = (regs[rs] + sx16(imm)) & 0xFFFFFFFF
                memoff = address - args.base
                pointed = read_word(data, memoff)
                out_reg, value = rt, pointed
            elif op == 0:
                fn = word & 63
                if fn in (32, 33) and regs[rs] is not None and regs[rt] is not None:
                    out_reg, value = rd, (regs[rs] + regs[rt]) & 0xFFFFFFFF
                elif fn in (34, 35) and regs[rs] is not None and regs[rt] is not None:
                    out_reg, value = rd, (regs[rs] - regs[rt]) & 0xFFFFFFFF
                elif fn == 37 and regs[rs] is not None and regs[rt] is not None:
                    out_reg, value = rd, regs[rs] | regs[rt]

            if out_reg not in (None, 0):
                regs[out_reg] = value
                if value is not None:
                    string = ascii_at(data, value - args.base)
                    if string is not None and pattern.search(string):
                        item = (pc, string, value)
                        if item not in found:
                            found.append(item)

            # Do not model call clobbers here. This is a reference finder rather
            # than an executor, and MIPS delay-slot address formation frequently
            # relies on a value loaded immediately before the call instruction.

        if found:
            print(f"function 0x{args.base + start:08x}, gp=0x{gp:08x}, file+0x{start:x}")
            for pc, string, address in found:
                print(f"  0x{pc:08x} -> 0x{address:08x}: {string!r}")


if __name__ == "__main__":
    main()
