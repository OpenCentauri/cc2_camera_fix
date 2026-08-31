#!/usr/bin/env python3
"""Small MIPS32 little-endian disassembler/annotator for reproducible analysis.

It is intentionally dependency-free.  It covers the integer, branch, load,
store, COP0, and common MIPS32r2 instructions used by the supplied CC2
bootloader and hid_update binaries. Unknown/DSP opcodes remain as .word values.
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path


REG = (
    "zero", "at", "v0", "v1", "a0", "a1", "a2", "a3",
    "t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7",
    "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7",
    "t8", "t9", "k0", "k1", "gp", "sp", "fp", "ra",
)


def sx(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return (value & (sign - 1)) - (value & sign)


def target26(pc: int, word: int) -> int:
    return ((pc + 4) & 0xF0000000) | ((word & 0x03FFFFFF) << 2)


def branch_target(pc: int, imm: int) -> int:
    return (pc + 4 + (sx(imm, 16) << 2)) & 0xFFFFFFFF


def decode(pc: int, word: int) -> tuple[str, str, tuple[int, ...]]:
    op = word >> 26
    rs = (word >> 21) & 31
    rt = (word >> 16) & 31
    rd = (word >> 11) & 31
    sa = (word >> 6) & 31
    fn = word & 63
    imm = word & 0xFFFF

    if word == 0:
        return "nop", "", ()
    if op == 0:
        shifts = {0: "sll", 2: "srl", 3: "sra"}
        vshifts = {4: "sllv", 6: "srlv", 7: "srav"}
        rr = {
            10: "movz", 11: "movn", 32: "add", 33: "addu", 34: "sub",
            35: "subu", 36: "and", 37: "or", 38: "xor", 39: "nor",
            42: "slt", 43: "sltu", 48: "tge", 49: "tgeu", 50: "tlt",
            51: "tltu", 52: "teq", 54: "tne",
        }
        if fn in shifts:
            return shifts[fn], f"${REG[rd]}, ${REG[rt]}, {sa}", (rd, rt, sa)
        if fn in vshifts:
            return vshifts[fn], f"${REG[rd]}, ${REG[rt]}, ${REG[rs]}", (rd, rt, rs)
        if fn == 8:
            return "jr", f"${REG[rs]}", (rs,)
        if fn == 9:
            return "jalr", f"${REG[rd]}, ${REG[rs]}", (rd, rs)
        if fn == 12:
            return "syscall", f"0x{(word >> 6) & 0xfffff:x}", ()
        if fn == 13:
            return "break", f"0x{(word >> 6) & 0xfffff:x}", ()
        if fn in (16, 18):
            name = "mfhi" if fn == 16 else "mflo"
            return name, f"${REG[rd]}", (rd,)
        if fn in (17, 19):
            name = "mthi" if fn == 17 else "mtlo"
            return name, f"${REG[rs]}", (rs,)
        if fn in (24, 25, 26, 27):
            name = ("mult", "multu", "div", "divu")[fn - 24]
            return name, f"${REG[rs]}, ${REG[rt]}", (rs, rt)
        if fn in rr:
            return rr[fn], f"${REG[rd]}, ${REG[rs]}, ${REG[rt]}", (rd, rs, rt)
    if op == 1:
        names = {
            0: "bltz", 1: "bgez", 2: "bltzl", 3: "bgezl",
            8: "tgei", 9: "tgeiu", 10: "tlti", 11: "tltiu",
            12: "teqi", 14: "tnei", 16: "bltzal", 17: "bgezal",
            18: "bltzall", 19: "bgezall",
        }
        name = names.get(rt)
        if name:
            if name.startswith(("b",)):
                t = branch_target(pc, imm)
                return name, f"${REG[rs]}, 0x{t:08x}", (rs, t)
            return name, f"${REG[rs]}, {sx(imm, 16)}", (rs, sx(imm, 16))
    if op in (2, 3):
        t = target26(pc, word)
        return ("j" if op == 2 else "jal"), f"0x{t:08x}", (t,)
    if op in (4, 5, 20, 21):
        name = {4: "beq", 5: "bne", 20: "beql", 21: "bnel"}[op]
        t = branch_target(pc, imm)
        return name, f"${REG[rs]}, ${REG[rt]}, 0x{t:08x}", (rs, rt, t)
    if op in (6, 7, 22, 23):
        name = {6: "blez", 7: "bgtz", 22: "blezl", 23: "bgtzl"}[op]
        t = branch_target(pc, imm)
        return name, f"${REG[rs]}, 0x{t:08x}", (rs, t)
    if op in (8, 9, 10, 11):
        name = ("addi", "addiu", "slti", "sltiu")[op - 8]
        return name, f"${REG[rt]}, ${REG[rs]}, {sx(imm, 16)}", (rt, rs, sx(imm, 16))
    if op in (12, 13, 14):
        name = ("andi", "ori", "xori")[op - 12]
        return name, f"${REG[rt]}, ${REG[rs]}, 0x{imm:x}", (rt, rs, imm)
    if op == 15:
        return "lui", f"${REG[rt]}, 0x{imm:x}", (rt, imm)
    if op == 16:
        cop_rs = rs
        sel = word & 7
        if cop_rs in (0, 4):
            name = "mfc0" if cop_rs == 0 else "mtc0"
            return name, f"${REG[rt]}, ${rd}, {sel}", (rt, rd, sel)
        if word == 0x42000018:
            return "eret", "", ()
        if word == 0x42000020:
            return "wait", "", ()
    if op == 17:
        fmt = rs
        ft = rt
        fs = rd
        fd = sa
        if fmt in (0, 2, 4, 6):
            name = {0: "mfc1", 2: "cfc1", 4: "mtc1", 6: "ctc1"}[fmt]
            return name, f"${REG[rt]}, $f{rd}", (rt, rd)
        fpops = {0: "add", 1: "sub", 2: "mul", 3: "div", 4: "sqrt", 5: "abs", 6: "mov", 7: "neg"}
        if fmt in (16, 17) and fn in fpops:
            suffix = "s" if fmt == 16 else "d"
            return f"{fpops[fn]}.{suffix}", f"$f{fd}, $f{fs}, $f{ft}", (fd, fs, ft)
        if rs == 8:
            t = branch_target(pc, imm)
            return ("bc1t" if rt & 1 else "bc1f"), f"0x{t:08x}", (t,)
    if op == 28:
        if fn in (0, 1, 4, 5):
            name = {0: "madd", 1: "maddu", 4: "msub", 5: "msubu"}[fn]
            return name, f"${REG[rs]}, ${REG[rt]}", (rs, rt)
        if fn == 2:
            return "mul", f"${REG[rd]}, ${REG[rs]}, ${REG[rt]}", (rd, rs, rt)
        if fn in (32, 33):
            return ("clz" if fn == 32 else "clo"), f"${REG[rd]}, ${REG[rs]}", (rd, rs)
    if op == 31:
        if fn in (0, 4):
            pos = sa
            size = rd + 1 if fn == 0 else rd - sa + 1
            return ("ext" if fn == 0 else "ins"), f"${REG[rt]}, ${REG[rs]}, {pos}, {size}", (rt, rs, pos, size)
        if fn == 32:
            names = {2: "wsbh", 16: "seb", 24: "seh"}
            if sa in names:
                return names[sa], f"${REG[rd]}, ${REG[rt]}", (rd, rt)
        if fn == 59:
            return "rdhwr", f"${REG[rt]}, ${rd}", (rt, rd)
    loads = {32: "lb", 33: "lh", 34: "lwl", 35: "lw", 36: "lbu", 37: "lhu", 38: "lwr", 48: "ll", 49: "lwc1", 53: "ldc1"}
    stores = {40: "sb", 41: "sh", 42: "swl", 43: "sw", 46: "swr", 56: "sc", 57: "swc1", 61: "sdc1"}
    if op in loads:
        return loads[op], f"${REG[rt]}, {sx(imm, 16)}(${REG[rs]})", (rt, rs, sx(imm, 16))
    if op in stores:
        return stores[op], f"${REG[rt]}, {sx(imm, 16)}(${REG[rs]})", (rt, rs, sx(imm, 16))
    if op == 47:
        return "cache", f"0x{rt:x}, {sx(imm, 16)}(${REG[rs]})", (rt, rs, sx(imm, 16))
    return ".word", f"0x{word:08x}", (word,)


def cstring(data: bytes, address: int, base: int, file_offset: int) -> str | None:
    offset = file_offset + address - base
    if not (0 <= offset < len(data)):
        return None
    end = data.find(b"\0", offset, min(len(data), offset + 400))
    if end < 0 or end == offset:
        return None
    raw = data[offset:end]
    if any(b < 0x20 or b > 0x7E for b in raw):
        return None
    return raw.decode("ascii")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("--start", type=lambda s: int(s, 0), required=True, help="virtual start address")
    parser.add_argument("--end", type=lambda s: int(s, 0), required=True, help="virtual end address")
    parser.add_argument("--base", type=lambda s: int(s, 0), required=True, help="virtual address corresponding to file-offset")
    parser.add_argument("--file-offset", type=lambda s: int(s, 0), default=0)
    parser.add_argument("--gp", type=lambda s: int(s, 0))
    parser.add_argument("--label", action="append", default=[], help="ADDR=NAME")
    args = parser.parse_args()

    data = args.binary.read_bytes()
    labels: dict[int, str] = {}
    for item in args.label:
        addr, name = item.split("=", 1)
        labels[int(addr, 0)] = name

    regs: list[int | None] = [None] * 32
    regs[0] = 0
    if args.gp is not None:
        regs[28] = args.gp

    for pc in range(args.start, args.end, 4):
        offset = args.file_offset + pc - args.base
        if offset < 0 or offset + 4 > len(data):
            break
        word = struct.unpack_from("<I", data, offset)[0]
        name, operands, fields = decode(pc, word)
        annotations: list[str] = []
        if pc in labels:
            print(f"\n{labels[pc]}:")

        op = word >> 26
        rs, rt, rd = (word >> 21) & 31, (word >> 16) & 31, (word >> 11) & 31
        imm = word & 0xFFFF
        result_reg: int | None = None
        result: int | None = None

        if name == "lui":
            result_reg, result = rt, (imm << 16) & 0xFFFFFFFF
        elif name in ("addiu", "addi") and regs[rs] is not None:
            result_reg, result = rt, (regs[rs] + sx(imm, 16)) & 0xFFFFFFFF
        elif name in ("ori", "xori", "andi") and regs[rs] is not None:
            result_reg = rt
            result = {"ori": regs[rs] | imm, "xori": regs[rs] ^ imm, "andi": regs[rs] & imm}[name] & 0xFFFFFFFF
        elif name in ("addu", "add", "or"):
            if regs[rs] is not None and regs[rt] is not None:
                result_reg, result = rd, (regs[rs] + regs[rt] if name != "or" else regs[rs] | regs[rt]) & 0xFFFFFFFF
        elif name in ("subu", "sub") and regs[rs] is not None and regs[rt] is not None:
            result_reg, result = rd, (regs[rs] - regs[rt]) & 0xFFFFFFFF
        elif name == "sll" and regs[rt] is not None:
            result_reg, result = rd, (regs[rt] << ((word >> 6) & 31)) & 0xFFFFFFFF
        elif name == "srl" and regs[rt] is not None:
            result_reg, result = rd, regs[rt] >> ((word >> 6) & 31)
        elif name == "lw" and regs[rs] is not None:
            address = (regs[rs] + sx(imm, 16)) & 0xFFFFFFFF
            memoff = args.file_offset + address - args.base
            if 0 <= memoff <= len(data) - 4:
                result_reg = rt
                result = struct.unpack_from("<I", data, memoff)[0]
                annotations.append(f"[0x{address:08x}] = 0x{result:08x}")
        elif name in ("lb", "lbu", "lh", "lhu") and regs[rs] is not None:
            result_reg = rt
        elif name in ("jal", "jalr"):
            regs[31] = (pc + 8) & 0xFFFFFFFF
            if name == "jal":
                target = fields[0]
                annotations.append(labels.get(target, f"call 0x{target:08x}"))
            elif regs[rs] is not None:
                target = regs[rs]
                annotations.append(labels.get(target, f"call 0x{target:08x}"))
        elif op in (32, 33, 34, 35, 36, 37, 38, 48, 49, 53):
            result_reg = rt

        if result_reg not in (None, 0):
            regs[result_reg] = result
            if result is not None:
                s = cstring(data, result, args.base, args.file_offset)
                if s is not None:
                    annotations.append(repr(s))
                if result in labels:
                    annotations.append(labels[result])

        # Conservative invalidation for instructions which visibly assign a GPR.
        if result_reg is None:
            if name in ("slti", "sltiu") or name in ("mfc0", "mfc1", "cfc1", "mfhi", "mflo", "rdhwr"):
                regs[rt if name not in ("mfhi", "mflo") else rd] = None
            elif name in ("slt", "sltu", "xor", "nor", "and", "movz", "movn", "mul", "clz", "clo", "ext", "ins", "wsbh", "seb", "seh"):
                regs[rd if name not in ("ext", "ins") else rt] = None

        suffix = " ; " + ", ".join(dict.fromkeys(annotations)) if annotations else ""
        print(f"{pc:08x}: {word:08x}  {name:<8} {operands:<38}{suffix}")


if __name__ == "__main__":
    main()
