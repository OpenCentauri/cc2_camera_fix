# Validation and regression test results

Tool version: `1.2.1`

Review date: 2026-09-04

## USB-backup interoperability regression tests

- A strict `cc2flash-backup-v2` ZIP with exactly `flash.bin` and
  `manifest.json` is accepted without extraction.
- Image size, SHA-256, MD5, boot fingerprint and acceptance basis, exact MTD
  partition map, and three-consecutive-of-five acquisition policy are checked
  before the flash image reaches the firmware validator.
- A manifest/image hash mismatch and any extra ZIP member are rejected.
- The validated manifest contributes exactly three evidenced physical reads,
  satisfying the build gate for every image without `--allow-fewer-reads`.
- The hardware-recovery self-test and all nine focused
  interoperability/config-mode tests pass on Python 3.12.
- Oversized zero-filled fragments are rejected before allocation. Zlib
  fragments are rejected before decompression when their declared size exceeds
  the config partition, and decompression output is bounded when the stream
  expands beyond its declared size.

## Attached current USB-backup interoperability

The locally supplied current-format ZIP was used only as a test input and was
not added to the repository. Its manifest records three consecutive identical
reads, the known bootloader fingerprint, and the camera's six-partition map.

- The unmodified ZIP passed strict archive, evidence, partition-map, firmware,
  identity, and JFFS2 validation without reduced-read overrides.
- Default clean-data correctly refused the live unfamiliar `system.sh` name.
- Clean-data with `--wipe-unknown-config` and preserve-data both built directly
  from the ZIP without extraction.
- Preserve-data retained the contents and relevant metadata of all seven live
  regular files: `dev_config.cfg`, `serial.cfg`, `system.sh`, `uvc.attr`,
  `uvc.config`, `uvc2.attr`, and `uvc_dualstream.config`.
- The preserved config contains one cleanmarker, one dirent and one inode per
  file, and zero obsolete nodes.
- Both generated full images passed USB maintenance's image safety checks and
  same-camera allowed-region comparison against `flash.bin` from the ZIP.

The table below retains the earlier two-unit regression record. Unit B's clean
output/readback equality was re-executed with the newly attached files; unit A
was not present in this interoperability run.

## Full-ROM regression record

| Input | Unit identity | Input SHA-256 | Config state | Generated SHA-256 | Result |
|---|---|---|---|---|---|
| Shared bricked camera | distinct unit A | `4325aebe84d70dd937de1790aa48f4b36ee2731def0ee5504ed080e4df13e819` | 98.567% non-`FF`; 628 obsolete nodes; only `serial.cfg` live | `5939d62a32fd97ab15a5d9b8ab71571831f0f2a30d66019c23deef892905ec09` | passed |
| Second bricked camera | distinct unit B | `ff9c8962abd06a14661db1857f6318b06f6378e93c4d812d96224094063bbcf9` | 98.486% non-`FF`; 653 obsolete nodes; only `serial.cfg` live | `269f1b3b205e2ac30ada7cb98a7aeb9ada2e76786dc14abe95ea9c56dce73d1f` | passed |

Both inputs have the same exact invariant firmware fingerprint and stock SquashFS window, but different serials, UOIDs, two-byte HWCONFIG check values, and raw JFFS2 histories. Each output preserves its own complete HWCONFIG partition and exact recovered `serial.cfg` payload.

The attached unit-B hardware readback was independently hashed and compared
byte-for-byte with the newly generated clean-data image in this review.

## Independent filesystem and patch checks

- A separate read-only SquashFS v4/XZ parser extracted `/home/bashrc.sh` from stock and generated images.
- The stock extraction was byte-for-byte equal to `bashrc-original.sh` (`984e1d34…a48417d`).
- The generated extraction was byte-for-byte equal to `bashrc-patched.sh` (`eab95c4e…e0f88`) and passed `sh -n`.
- The v1.1 tool contains no Base85/XOR binary patch. It applies a readable shell block replacement, constructs the exact XZ container, and reproduces the same patched 32 KiB SHA-256 (`36e9b9b2…51b2b`).
- Recovered configs contain one cleanmarker, one `serial.cfg` dirent, one uncompressed inode, and valid JFFS2 header/node/name/data CRCs; all remaining bytes are `FF`.
- The generated ROMs pass the complete strict validator again after construction.

## Regression and rejection tests

- The observed type-12 HWCONFIG records with 256-byte and 261-byte payloads are
  accepted. Unknown contents in the five-byte extension are normalized and
  preserved exactly.
- A payload shorter than the known 256-byte prefix, unobserved 257-, 262-, and
  512-byte payloads, and a non-type-12 record are rejected. Mutations after the
  declared extension remain covered by invariant validation.
- A locally reconstructed full image with the third camera's invariant variant
  bytes passed strict analysis and recovery generation; the generated image
  retained HWCONFIG byte-for-byte. This is software validation, not a physical
  flash or boot test of that camera.
- Two distinct real serial/UOID pairs: accepted and preserved.
- Structurally valid synthetic third unit with two confirmation reads (three total): accepted and preserved.
- A raw image with only one read: rejected unless `--allow-fewer-reads` is explicit.
- One-bit kernel mutation: rejected.
- One-bit system-patch-window mutation: rejected.
- Structurally valid serial and UOID values with different prefixes: accepted
  and preserved independently.
- `--keep-config` on either exhausted bricked partition: rejected.
- One-bit full-programmer readback mismatch: rejected with the affected region.
- Rebuilding an already patched canonical image: idempotent; no write region reported.
- Existing output directory containing an unknown entry: rejected instead of deleting it.

## Scope of the recovery claim

Software-level recovery is verified: both bricked dumps are transformed into strict, canonical images with the documented hashes, readable patched startup logic, preserved unit identity, and valid filesystem structures. One output also equals the earlier recorded permanent readback hash.

Hardware-level recovery is not newly proven by this review. That requires flashing a physical bricked camera, making a full byte-identical readback, and observing a successful boot/USB enumeration. The tool's generated `FLASHING.txt` makes that the required final validation step.
