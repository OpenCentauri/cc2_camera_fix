# CC2 Camera Recovery Builder

A strict, auditable recovery-image builder for the stock Elegoo Centauri Carbon 2 camera firmware observed in two independently obtained 8 MiB SPI-NOR images with different unit identities.

The tool validates the dump, recovers and preserves that camera's own `serial.cfg`, rebuilds the exhausted JFFS2 `config` partition, and applies the audited `/home/bashrc.sh` mitigation that stops the five default configuration files from being rewritten on every boot.

It uses only the Python standard library. It does **not** contain a firmware image or opaque binary patch blob. The system change is generated from a readable shell-source replacement, deterministic XZ construction, and exact input/output hashes.

## Supported image family

This release intentionally accepts only the exact firmware family already verified in both reference cameras:

- 8 MiB `ZB25VQ64` SPI NOR
- Ingenic T23N
- Linux `3.10.14__isvp_pike_1.0__`
- kernel build dated 2025-12-03
- Jovision `ucamera` v1.1.83
- partition layout:

```text
0x000000–0x03FFFF  boot
0x040000–0x18FFFF  kernel
0x190000–0x2E7FFF  root
0x2E8000–0x7CFFFF  system
0x7D0000–0x7DFFFF  HWCONFIG
0x7E0000–0x7FFFFF  config
```

There is no override or `--force` option for a mismatching firmware build.

## What “100% match apart from unit data” means

A complete raw dump cannot be compared by ignoring only the visible serial string: JFFS2 appends new nodes on each boot, so two otherwise identical cameras naturally have different raw `config` histories. The HWCONFIG partition also contains a per-unit UOID and an associated two-byte value.

The validator therefore performs the strongest safe equivalent:

1. Every firmware byte that should be invariant must match the two independently obtained references exactly.
2. The 32 KiB SquashFS window containing `bashrc.sh` must equal either the exact stock hash or the exact audited patched hash.
3. Only these unit-specific/mutable fields are excluded from the invariant hash:

```text
0x7D200B–0x7D200C  two-byte HWCONFIG unit check value
0x7D2011–0x7D206E  94-byte HWCONFIG UOID
0x7E0000–0x7FFFFF  writable JFFS2 config log
```

4. The excluded fields are not ignored blindly:
   - the UOID must have the expected 94-byte structure;
   - `config` must contain CRC-valid JFFS2 nodes;
   - only the six known filenames are accepted;
   - exactly one unambiguous `serial.cfg` value must be recoverable;
   - the serial and UOID must share the expected 12-byte unit prefix.

No other differences are accepted. See `REFERENCE_FINGERPRINTS.json`.

## What the builder changes

### Persistent startup mitigation

The system patch changes the active copies in `/home/bashrc.sh` from unconditional writes such as:

```sh
cp /system/config/uvc.attr /etc/conf.d/uvc.attr
```

to copy-once checks:

```sh
[ -f /etc/conf.d/uvc.attr ] || cp /system/config/uvc.attr /etc/conf.d/uvc.attr
```

The original and patched scripts are included for audit. The patch preserves the vendor's existing source path for `uvc.config`.

The full system filesystem is not rebuilt. The tool verifies and decompresses the exact stock SquashFS fragment, applies the complete readable source transformation embedded near the top of the Python file, reconstructs the single-block XZ stream with fixed parameters, and updates its SquashFS size field. The reconstructed bytes must equal the exact audited patched hashes. Only this window is changed:

```text
0x463000–0x46AFFF
```

The input window, compressed fragment, decompressed fragment, original `bashrc.sh`, generated `bashrc.sh`, generated XZ stream, and final window each have independent expected SHA-256 checks. The shorter patched XZ stream intentionally leaves the now-unaddressed trailing stock bytes untouched; the authoritative SquashFS fragment size excludes them.

### Compact config recovery

The builder extracts the camera's own CRC-valid `serial.cfg` and creates a minimal 128 KiB JFFS2 partition containing:

- one cleanmarker;
- one `serial.cfg` directory entry;
- one uncompressed `serial.cfg` inode;
- the original serial payload byte-for-byte;
- valid JFFS2 header, node, name, and data CRCs.

The five standard UVC/config files are intentionally omitted. The patched startup script creates each missing default once on the first successful boot.

## Requirements

- Python 3.10 or later
- one full 8 MiB flash dump
- preferably three independently read dumps with identical SHA-256 hashes
- either `flashrom` with a suitable SPI programmer, or NeoProgrammer with a voltage-verified CH341A/CH341B, for the eventual write

## Usage

Run the internal self-test. Supplying a supported ROM also exercises the complete readable SquashFS patch path:

```bat
py cc2_sig_tool.py self-test cc2-camera-1.bin
```

Analyze without creating anything:

```bat
py cc2_sig_tool.py analyze cc2-camera-1.bin
```

Build a recovery bundle and require three physical reads to be byte-identical:

```bat
py cc2_sig_tool.py build cc2-camera-1.bin --confirm cc2-camera-2.bin cc2-camera-3.bin
```

For a non-reference unit, three byte-identical physical reads are required by default. With fewer reads, the tool refuses unless the higher risk is explicitly accepted:

```bat
py cc2_sig_tool.py build cc2-camera-1.bin --allow-fewer-reads
```

Exact full reference dumps listed in `REFERENCE_FINGERPRINTS.json` may be rebuilt from one copy because their complete 8 MiB hashes already match known inputs.

The default output directory is:

```text
cc2-camera-1-cc2-recovery
```

It contains:

```text
cc2-camera-recovery.bin
cc2-camera-layout.txt
config-restored.bin
serial.cfg
MANIFEST.json
VALIDATION.txt
FLASHING.txt
SHA256SUMS.txt
```

`FLASHING.txt` contains a command assembled from the regions that actually changed. For example:

- exhausted stock image: `system_bashrc_patch` and `config`;
- already config-recovered image: normally `system_bashrc_patch` only;
- already permanently patched image: no write required.

To patch only `bashrc.sh` while preserving the current config partition byte-for-byte:

```bat
py cc2_sig_tool.py build cc2-camera-1.bin --keep-config
```

This option is accepted only when the partition already equals the exact canonical rebuild. It refuses exhausted or otherwise noncanonical config, because preserving one could leave the device bricked.

## Programmer instructions

### Option A: flashrom with Bus Pirate

Use the command generated in `FLASHING.txt`. It writes only the regions reported as changed and deliberately does not enable programmer-supplied target power. Verify the exact chip voltage, pinout, and programmer wiring independently before connecting it.

### Option B: NeoProgrammer with CH341A/CH341B

NeoProgrammer does not use `cc2-camera-layout.txt`. This workflow erases, writes, and verifies the **complete 8 MiB** `cc2-camera-recovery.bin`, including the unit-specific data copied from the validated input dump.

#### Electrical checks

1. Read the complete part marking on the camera's SPI flash. The verified firmware family uses a 64 Mbit/8 MiB `ZB25VQ64`-family 3 V SPI NOR, but the exact chip and voltage on the board in front of you take precedence. The manufacturer describes `ZB25VQ64` as a 3 V device; do not infer voltage from programmer color, seller description, or a jumper alone. See the [ZB25VQ64 product information](https://www.gdzhianxin.com/index.php?a=show&c=index&catid=108&id=91&m=content).
2. Measure the CH341 adapter's VCC and SPI logic levels before attaching the flash. CH341A boards and clip adapters have multiple revisions and clones. Use a proper level/voltage adapter if the measured levels do not match the exact flash datasheet.
3. Disconnect the camera's USB cable and every other source of board power. Do not power the camera normally while the programmer supplies the flash. If your successful read setup uses a different, independently verified power arrangement, reproduce that exact arrangement; never connect two power sources blindly.
4. Connect the clip before plugging the CH341 programmer into USB. Align the clip's pin-1/red-stripe conductor with the flash's pin-1 dot/notch, and place the adapter in the programmer's **25-series SPI** position, not the 24-series I²C position.

Standard SOIC-8 SPI NOR signals are:

| Flash pin | Signal | CH341 label |
|---:|---|---|
| 1 | `CS#` | `CS` |
| 2 | `IO1/DO` | `MISO` |
| 3 | `IO2/WP#` | `WP` or pulled high |
| 4 | `GND` | `GND` |
| 5 | `IO0/DI` | `MOSI` |
| 6 | `CLK` | `CLK` |
| 7 | `IO3/HOLD#/RESET#` | `HOLD` or pulled high |
| 8 | `VCC` | verified 3 V supply |

Confirm this mapping against the exact flash datasheet and adapter silkscreen. Do not rely on the table if the package or adapter differs.

#### Establish a trustworthy backup

1. Install the appropriate CH341 driver and start NeoProgrammer. Obtain executables/drivers from a source you trust and scan them before use.
2. Use **Detect IC**, then confirm that the reported manufacturer/device ID, capacity, and exact selected profile correspond to the chip marking. Select `ZB25VQ64` only when that is the actual part. If the exact chip is unavailable, detection is inconsistent, or NeoProgrammer proposes only a vaguely similar `25Q64`, stop rather than guessing.
3. Click **Read IC**, wait for the complete 8 MiB buffer, and save it as `cc2-camera-1.bin`.
4. Without moving the clip, repeat the read twice and save `cc2-camera-2.bin` and `cc2-camera-3.bin`.
5. Require all three files to be exactly 8,388,608 bytes and have identical SHA-256 hashes. On Windows, for example:

```bat
certutil -hashfile cc2-camera-1.bin SHA256
certutil -hashfile cc2-camera-2.bin SHA256
certutil -hashfile cc2-camera-3.bin SHA256
```

All-`00`, all-`FF`, unstable, differently hashed, wrong-size, or intermittently detected reads mean the clip/power/in-circuit setup is not trustworthy. Stop and correct it before any erase or write.

Build the recovery image from those reads:

```bat
py cc2_sig_tool.py build cc2-camera-1.bin --confirm cc2-camera-2.bin cc2-camera-3.bin
```

#### Full-chip erase, program, and verify

1. In NeoProgrammer, re-detect and re-confirm the exact chip profile and 8 MiB capacity.
2. Open the generated `cc2-camera-recovery.bin`. Confirm the loaded buffer/file is exactly 8,388,608 bytes. Do **not** load `config-restored.bin`, `serial.cfg`, or an individual layout region as the full-chip image.
3. Save the three original dumps somewhere separate before continuing. They contain the only verified copy of this camera's unit identity.
4. Use NeoProgrammer's automatic write sequence with **Erase**, **Blank Check**, **Write/Program**, and **Verify** enabled. Depending on the version, this is exposed through **Write IC** or its adjacent operation menu.
5. Do not edit status registers, OTP/security areas, unique IDs, or protection bits pre-emptively. If NeoProgrammer reports write protection, stop and identify the exact status-register meaning from the selected chip's datasheet before changing it.
6. Require NeoProgrammer's verify operation to complete without any mismatch. A failed erase, blank check, write, or verify is not a usable result; keep the setup connected and diagnose it rather than trying to boot.
7. Without moving the clip, run **Read IC** again and save the complete result as `cc2-camera-readback.bin`.
8. Verify that readback with the recovery tool:

```bat
py cc2_sig_tool.py verify cc2-camera-1-cc2-recovery\cc2-camera-recovery.bin cc2-camera-readback.bin
```

Only after the tool reports a byte-for-byte match should you unplug the CH341 programmer, remove the clip, reconnect normal camera power, and test boot/USB enumeration.

NeoProgrammer's labels can vary slightly by release, but the required operation order does not: **three stable reads → build → full-chip erase → blank check → program → internal verify → full readback → tool verify**. NeoProgrammer describes the same backup/erase/write/verify capabilities in its [software overview](https://neoprogrammer.org/).

## Readback verification

After writing, make a full 8 MiB readback without disturbing the programmer connection. Then run:

```bat
py cc2_sig_tool.py verify cc2-camera-recovery.bin cc2-camera-readback.bin
```

Success is reported only when the files are byte-for-byte identical.

`verify` first requires the expected file to be a strict, patched, canonical recovery image, then requires the full readback to be byte-for-byte identical.

The generated Bus Pirate command uses only current documented `dev` and `spispeed` parameters and deliberately does not enable programmer-supplied power. Verify the exact flash voltage and wiring independently. Never connect normal device/USB power and programmer target power simultaneously.

## Safety properties

The builder:

- can require any additional physical reads supplied with `--confirm` to be byte-for-byte identical;
- requires three identical reads for a non-reference unit unless reduced confidence is explicitly accepted;
- refuses files that are not exactly 8 MiB;
- validates exact hashes for every invariant segment;
- accepts only the exact known stock or known patched SquashFS window;
- never edits boot, kernel, root, or HWCONFIG;
- preserves the entire input HWCONFIG partition;
- preserves the original serial payload;
- validates the generated JFFS2 image by parsing it again;
- refuses `--keep-config` for a noncanonical/exhausted partition;
- refuses unsafe output-directory reuse that could delete inputs or unrelated files;
- validates the complete generated recovery image again;
- proves no bytes changed outside the selected patch/config regions;
- creates a manifest and hashes for every output.

## Limitations

This is a mitigation for the deterministic per-boot write leak. It does not repair the underlying Ingenic SFC/JFFS2 erase-size defect. Other software that performs persistent writes could still consume config space.

The tool supports only the exact firmware build represented by the embedded fingerprints. A future Elegoo/Jovision build must be analyzed and fingerprinted separately.

Two real unit identities were directly tested. Cross-serial support is fail-closed: invariant firmware must match exactly; the UOID and serial must each match their observed structure and share the observed 12-byte prefix; all unit-specific HWCONFIG bytes and the recovered serial payload are preserved exactly. Because the proprietary full serial↔UOID derivation is unknown, the tool cannot prove more than that relationship for a previously unseen unit.

Static analysis can prove that a bricked dump is transformed into the exact known recovery bytes and that all relevant filesystems/checksums validate. Actual boot recovery still requires a controlled flash, full readback verification, and an observed successful boot; software-only review cannot replace that hardware test.

`serial.cfg` and the generated recovery image contain a unit-specific identifier. Do not publish them unredacted.
