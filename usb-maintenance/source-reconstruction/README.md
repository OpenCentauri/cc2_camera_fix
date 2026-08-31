# CC2 update-path source reconstruction

This directory is a human-readable, C-like reconstruction of the update-related
code in the supplied Elegoo Centauri Carbon 2 camera flash image.

It is **not the vendor's original source code**. Names, types, and control-flow
shape were chosen for readability. Constants, command branches, packet fields,
state transitions, paths, checks, and omissions are derived from the supplied
binaries. Every important reconstructed function is annotated with its original
address so it can be checked against the disassembly.

## Files

- `hid_update_reconstructed.c` — Linux `/bin/hid_update`: normal-mode HID
  framing, version/configuration commands, arbitrary-file upload, upgrade flag,
  and HID event loop.
- `uboot_updater_reconstructed.c` — main U-Boot updater: common frame format,
  metadata/data state machine, ACK/retry behavior, MD5 validation, SPI erase and
  write, HID/CDC transport choice.
- `spl_upgrade_gate_reconstructed.c` — SPL decision that detects the persistent
  upgrade magic and transfers control to the updater.
- `TRACEABILITY.md` — source-address map, confidence notes, and a list of the
  material unsafe behaviors retained in the reconstruction.
- `REPRODUCING.md` — commands for extracting the daemon and disassembling the
  address ranges from the supplied 8 MiB image.
- `protocol_reference.md` — the companion byte-level command and protocol
  reference.

## Analyzed inputs

| Artifact | Size | SHA-256 |
|---|---:|---|
| Supplied SPI-NOR image | 8,388,608 | `269f1b3b205e2ac30ada7cb98a7aeb9ada2e76786dc14abe95ea9c56dce73d1f` |
| Extracted `/bin/hid_update` | 19,048 | `bcbdd698b4b9208f73f7ef1f9846b3a96130a1bcc5c118c0e71543cf4e1ed503` |

The extracted daemon is a stripped little-endian MIPS32r2 ELF with MD5
`8091751fdd4d0d50ea31901663797a86`.

## Scope

The reconstruction covers the complete command surface found in the Linux
maintenance daemon and bootloader updater. It does not attempt to reproduce the
Linux kernel, UVC streaming application, codecs, sensor drivers, or unrelated
U-Boot commands.

No physical-camera write was performed while producing this reconstruction.
USB endpoint assignment and timing therefore remain runtime-unverified.

## How to read the code

Comments use these labels:

- `CONFIRMED` — visible directly in instructions, constants, descriptors, or
  filesystem contents.
- `INFERRED` — a readable name/type or library-level interpretation reconstructed
  from calling convention and use.
- `BUG-COMPAT` — deliberately shows unsafe or surprising behavior present in the
  binary; it is documentation, not recommended implementation.

The source is intended for audit and reasoning, not compilation or deployment.
External helpers such as `transport_send`, `spi_erase`, and `vendor_flash_ioctl`
stand in for vendor/library functions whose internals are outside the relevant
protocol path.

Run the dependency-free protocol checks with:

```sh
python3 verify_protocol_vectors.py
```
