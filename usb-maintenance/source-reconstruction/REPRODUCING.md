# Reproducing the static analysis

These commands assume the main project archive's `tools/` directory and a local
copy of the supplied 8 MiB flash image. They are read-only.

```sh
IMAGE=cc2-camera-after-bashrc.bin
sha256sum "$IMAGE"
```

The expected SHA-256 is:

```text
269f1b3b205e2ac30ada7cb98a7aeb9ada2e76786dc14abe95ea9c56dce73d1f
```

## Extract `/bin/hid_update`

The root SquashFS starts at flash offset `0x190000`:

```sh
python3 tools/squashfs_extract.py \
  --offset 0x190000 "$IMAGE" extracted-root
sha256sum extracted-root/bin/hid_update
md5sum extracted-root/bin/hid_update
readelf -h -S -r extracted-root/bin/hid_update
```

Expected hashes:

```text
SHA-256 bcbdd698b4b9208f73f7ef1f9846b3a96130a1bcc5c118c0e71543cf4e1ed503
MD5     8091751fdd4d0d50ea31901663797a86
```

The ELF's `.text` VMA and file offsets coincide relative to `0x00400000`.
Examples:

```sh
python3 tools/mips32_disasm.py extracted-root/bin/hid_update \
  --base 0x00400000 --file-offset 0 \
  --start 0x00400fe8 --end 0x0040123c

python3 tools/mips32_disasm.py extracted-root/bin/hid_update \
  --base 0x00400000 --file-offset 0 \
  --start 0x0040123c --end 0x004017d4

python3 tools/mips32_disasm.py extracted-root/bin/hid_update \
  --base 0x00400000 --file-offset 0 \
  --start 0x004024c0 --end 0x00403480
```

Use `.pdr` records to recover the stripped function starts and `.rel.plt` to map
PLT slots to imported libc/pthread names. `readelf -r` lists all 42 PLT entries.

## Disassemble the main-U-Boot updater

The main U-Boot image begins at flash file offset `0x6800`, linked at
`0x80100000`:

```sh
python3 tools/mips32_disasm.py "$IMAGE" \
  --base 0x80100000 --file-offset 0x6800 \
  --start 0x80117948 --end 0x801187cc
```

Thus, for any main-U-Boot address in this reconstruction:

```text
flash_file_offset = VA - 0x80100000 + 0x6800
```

The transport-selection fallthrough can be checked separately:

```sh
python3 tools/mips32_disasm.py "$IMAGE" \
  --base 0x80100000 --file-offset 0x6800 \
  --start 0x80118714 --end 0x801187cc
```

It compares word 1 with `0x010203a0`, then `0x010203a1`, and substitutes
`0x010203a1` for any other value.

## Disassemble the SPL gate

The early SPL image is mapped at `0xf0000000` from flash offset zero:

```sh
python3 tools/mips32_disasm.py "$IMAGE" \
  --base 0xf0000000 --file-offset 0 \
  --start 0xf0001c28 --end 0xf0001c80
```

The read starts at `0xf0001c28`; the comparison with `0x55504454` is anchored at
`0xf0001c54`.

## Verify protocol vectors

From the reconstruction directory:

```sh
python3 verify_protocol_vectors.py
```

This checks the unusual normal-HID CRC, the canonical eight-byte upgrade trigger,
the 17-byte bootloader metadata frame, nonzero ACK packet values, and the 128-byte
image header/MD5 layout without opening a USB device.
