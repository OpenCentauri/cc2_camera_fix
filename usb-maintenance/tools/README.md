# Analysis helpers

These dependency-free tools make the static analysis reproducible without
redistributing the supplied firmware:

- `squashfs_extract.py` reads the XZ-compressed SquashFS 4 images used by the
  dump and extracts their files read-only.
- `mips32_disasm.py` is the small little-endian MIPS32/MIPS32r2 disassembler
  used for the address anchors in `EVIDENCE.md`.
- `mips_pic_xrefs.py` locates PIC/GOT references to strings in stripped MIPS
  ELFs and the linked U-Boot image.

Run each tool with `--help` for its exact arguments.  Never run an extractor
against a mounted device; give it an offline image file.

