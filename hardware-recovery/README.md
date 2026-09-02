# CC2 Camera Recovery Builder

This project can recover a stock Elegoo Centauri Carbon 2 camera that no longer boots, and it patches the firmware so the same failure should not recur from normal boot cycles.

The repair is designed to preserve the identity of **your** camera. It starts from your own flash dump; there is no generic firmware image in this repository and you should not write another camera's dump to your device.

> **AI-development note:** This fix was developed with the assistance of AI. It has produced the expected result in two tested cases, but if you have any doubts, independently audit the script before using it on your hardware.

## Why the stock camera stops working

The stock camera is a small Linux computer, not just an image sensor. Its firmware has a writable 128 KiB JFFS2 `config` partition. During every boot, the vendor startup script unconditionally copies the same five default configuration files into that partition again.

Normally JFFS2 garbage collection would reclaim the obsolete copies. On this firmware and flash layout, erase/garbage collection does not work correctly. The old records accumulate until the partition cannot accept the next boot-time writes. The camera then fails to finish booting and no longer appears as a working USB camera. From the printer, the symptom is simply a missing or permanently dead camera feed.

This tool repairs both parts of that problem:

- it rebuilds the exhausted `config` partition, either keeping only the
  camera's own serial data or recreating every live regular file once;
- it changes the startup script to create each default file only when it is missing, stopping the deterministic write leak.

## Check that the camera is actually the problem

A missing image does not by itself prove that the camera flash is corrupted. Isolate the camera from the printer before buying or connecting a programmer:

1. Power the printer off and unplug it from mains power.
2. Unscrew the stock camera module and unplug its four-wire cable from the printer. Do not work on the connector while the printer is powered.
3. Connect a known-working, ordinary USB webcam to the printer's front USB port while the printer is still off.
4. Power the printer back on with the stock camera disconnected.
5. If that webcam produces a feed, the printer mainboard, software, and USB-camera path are working; failure of the stock camera module is then likely. If the replacement webcam also fails, diagnose the printer side before attempting this repair.

Connecting the replacement webcam before boot is the safest way to have it appear as `/dev/video0`, which is the device the CC2 uses. Connecting it later can also work, but unplugging and reconnecting USB cameras repeatedly may assign different `/dev/video*` identifiers.

This is a useful isolation test, not proof of this exact flash failure. The builder provides the final check: it refuses a dump unless its firmware structure and invariant bytes match the supported camera firmware exactly.

## What this repair requires

This is a hardware recovery. You must read and rewrite the camera's eight-pin SPI flash memory with an external programmer.

The recommended beginner setup is:

- a CH341A/CH341B USB programmer that has been verified for 3.3 V operation;
- an SOIC-8 test clip and cable, allowing the chip to be accessed on the camera board;
- a multimeter;
- NeoProgrammer on a Windows computer;
- Python 3.10 or later for validating the dump and building the repaired image.

A CH341 programmer is inexpensive and worked in-circuit on the tested camera: the flash did not need to be unsoldered. A Bus Pirate is still documented as an alternative, but in-circuit access may fail because other components on the board load or interfere with the SPI bus; on the tested setup it worked only after the flash was unsoldered.

> **Quick voltage safety check:** with the camera disconnected and the programmer powered from USB, measure CH341A pin 28 (`VCC`) relative to ground. It should be approximately **3.3 V**; if it is near 5 V, stop and do not connect the camera. Pin 28 is opposite pin 1 across the notched end of the 28-pin package—confirm the pinout before probing.
>
> This is a useful screening check, not complete certification. Some older CH341A board revisions need a soldered modification or external 3.3 V level shifting, while safe revisions exist and newer stock is often already corrected. Labels and purchase date are not proof: research your exact board and, if uncertain, also measure flash `VCC` and the SPI logic levels. See the [CH341 datasheet](https://www.wch-ic.com/downloads/CH341DS1_PDF.html) and the [older black-board voltage issue](https://wej.k.vu/electronics/ch341a-mini-programmer-fix/) for background.

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

### Config recovery modes

`clean-data` is the default and retains the original recovery behavior. The
builder extracts the camera's own CRC-valid `serial.cfg` and creates a minimal
128 KiB JFFS2 partition containing:

- one cleanmarker;
- one `serial.cfg` directory entry;
- one uncompressed `serial.cfg` inode;
- the original serial payload byte-for-byte;
- valid JFFS2 header, node, name, and data CRCs.

The five standard UVC/config files are intentionally omitted. The patched startup script creates each missing default once on the first successful boot.

Clean mode refuses to discard CRC-valid names outside the audited stock set.
If you have inspected the source and intentionally want to wipe unfamiliar
config contents too, add `--wipe-unknown-config`. That override still preserves
and cross-checks `serial.cfg`; it is not permission to ignore identity or
firmware validation failures.

`preserve-data` instead resolves the current root-directory view and recreates
every live regular file once in a fresh compact JFFS2 image. It preserves file
contents, mode, owner, timestamps, flags, inode identity, and directory-entry
metadata, while dropping obsolete/dead historical nodes. It supports
uncompressed, zero-filled, and zlib-compressed source fragments and fails
closed on directories, links/special entries, ambiguous metadata, unsupported
compression, or a live set too large for the audited compact layout.

## Usage

Download `cc2_sig_tool.py` from this repository and place it in a working directory. You need Python 3.10 or later and either a raw full 8 MiB dump from your camera or an unmodified `cc2flash-backup-v2` ZIP created by the USB-maintenance tool. Do not continue to a write until you have evidence for three identical physical reads.

For raw programmer dumps, supply three independently read files with identical
SHA-256 hashes as shown below. For a USB backup ZIP, the builder validates
`flash.bin`, `manifest.json`, the exact member set, hashes, boot fingerprint,
partition map, and three-consecutive-read acquisition evidence. A valid ZIP
therefore satisfies the three-read gate directly; do not extract or rewrite it.

The command-line examples in this section use a normal Unix shell and
`python3`. The later NeoProgrammer walkthrough uses Windows syntax where the
programmer software requires it.

Run the internal self-test. Supplying a supported ROM also exercises the complete readable SquashFS patch path:

```sh
python3 cc2_sig_tool.py self-test cc2-camera-1.bin
```

Analyze without creating anything:

```sh
python3 cc2_sig_tool.py analyze cc2-camera-1.bin
```

The equivalent USB-backup workflow is:

```sh
python3 cc2_sig_tool.py analyze backup.zip
python3 cc2_sig_tool.py build backup.zip
```

The second command creates
`backup-cc2-recovery/cc2-camera-recovery.bin`. Keep the original
`backup.zip` unchanged: USB maintenance uses it both as the preserved
three-read backup and to prove that the recovery image belongs to the same
camera before restoring it.

Choose the config treatment explicitly when the default is not appropriate:

```sh
# Default: wipe ordinary config and preserve only serial.cfg
python3 cc2_sig_tool.py build backup.zip --config-mode clean-data

# Same clean rebuild, after explicitly approving unfamiliar config names
python3 cc2_sig_tool.py build backup.zip --wipe-unknown-config

# Recreate every live regular config file once, without dead JFFS2 copies
python3 cc2_sig_tool.py build backup.zip --config-mode preserve-data
```

Raw 8 MiB backups are first-class inputs. For a bricked camera, raw dumps made
with an external programmer are the only acquisition path; supply three stable
reads with `--confirm` as described below. A USB export may consist of a raw
`.bin` plus `cc2flash-backup-v1` JSON. The raw image remains valid input, but
that two-read JSON is not accepted as three-read evidence. Supply independent
confirmation dumps or use `--allow-fewer-reads`; the tool does not upgrade or
invent missing acquisition evidence.

Build a recovery bundle and require three physical reads to be byte-identical:

```sh
python3 cc2_sig_tool.py build cc2-camera-1.bin --confirm cc2-camera-2.bin cc2-camera-3.bin
```

For a non-reference unit, three byte-identical physical reads are required by default. With fewer reads, the tool refuses unless the higher risk is explicitly accepted:

```sh
python3 cc2_sig_tool.py build cc2-camera-1.bin --allow-fewer-reads
```

Exact full reference dumps listed in `REFERENCE_FINGERPRINTS.json` may be rebuilt from one copy because their complete 8 MiB hashes already match known inputs.

To validate the complete USB round trip without opening USB:

```sh
cc2flash plan-restore backup-cc2-recovery/cc2-camera-recovery.bin --backup backup.zip
```

If that passes, the corresponding guarded write command is:

```sh
cc2flash restore backup-cc2-recovery/cc2-camera-recovery.bin --backup backup.zip
```

USB maintenance refuses the candidate if any byte outside this builder's
audited `0x463000–0x46AFFF` system window and
`0x7E0000–0x7FFFFF` config partition differs from the preserved backup.

For the raw `cc2-camera-1.bin` example, the default output directory is:

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

```sh
python3 cc2_sig_tool.py build cc2-camera-1.bin --keep-config
```

This option is accepted only when the partition already equals the exact canonical rebuild. It refuses exhausted or otherwise noncanonical config, because preserving one could leave the device bricked.
It cannot be combined with `--config-mode preserve-data` or
`--wipe-unknown-config`.

## Programmer instructions

### Recommended: NeoProgrammer with CH341A/CH341B

NeoProgrammer does not use `cc2-camera-layout.txt`. This workflow erases, writes, and verifies the **complete 8 MiB** `cc2-camera-recovery.bin`, including the unit-specific data copied from the validated input dump.

#### Electrical checks

1. Read the complete part marking on the camera's SPI flash. The verified firmware family uses a 64 Mbit/8 MiB `ZB25VQ64`-family 3 V SPI NOR, but the exact chip and voltage on the board in front of you take precedence. The manufacturer describes `ZB25VQ64` as a 3 V device; do not infer voltage from programmer color, seller description, or a jumper alone. See the [ZB25VQ64 product information](https://www.gdzhianxin.com/index.php?a=show&c=index&catid=108&id=91&m=content).
2. With no camera or clip attached, perform the pin-28 voltage check described above. Also measure the programmer's flash `VCC` output and, if possible, its SPI logic-high levels. Use a proper level/voltage adapter if any measured level does not match the exact flash datasheet.
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

### Alternative: flashrom with a Bus Pirate

The generated `FLASHING.txt` also contains a `flashrom` command for the Bus Pirate. Unlike the NeoProgrammer workflow, it writes only the regions reported as changed and deliberately does not enable programmer-supplied target power.

In-circuit Bus Pirate access may not work on this camera because the rest of the board can interfere with the SPI bus. In the tested setup, the CH341 worked with the flash still soldered to the board, while the Bus Pirate worked only after the chip was unsoldered. Do not interpret unstable or failed reads as permission to write: obtain three identical full dumps first, or switch to the CH341 approach.

If you do use a Bus Pirate, independently verify the exact flash voltage, pinout, wiring, and power arrangement. Never power the camera normally while the programmer is connected unless you have explicitly designed and verified that arrangement.

## Readback verification

After writing, make a full 8 MiB readback without disturbing the programmer connection. Then run:

```bat
py cc2_sig_tool.py verify cc2-camera-recovery.bin cc2-camera-readback.bin
```

Success is reported only when the files are byte-for-byte identical.

`verify` first requires the expected file to be a strict, patched recovery image
with a safely reconstructable config, then requires the full readback to be
byte-for-byte identical. Both clean-data and preserve-data outputs are valid.

Only disconnect the programmer and attempt a normal boot after this comparison succeeds. Never connect normal camera/USB power and programmer target power simultaneously.

## Supported image family

This release intentionally accepts only the exact firmware family already verified in two independent camera dumps with different unit identities:

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

There is no override or `--force` option for a mismatching firmware build. A refusal is a safety feature: do not weaken the checks or copy a reference camera's complete image over an unsupported unit.

## How validation handles different camera identities

A complete raw dump cannot be compared by ignoring only the visible serial string. JFFS2 appends new nodes on each boot, so two otherwise identical cameras naturally have different raw `config` histories. The HWCONFIG partition also contains a per-unit UOID and an associated two-byte value.

The validator therefore performs the strongest safe equivalent of “100% match apart from unit data”:

1. Every firmware byte that should be invariant must match the two independently obtained references exactly.
2. The 32 KiB SquashFS window containing `bashrc.sh` must equal either the exact stock hash or the exact audited patched hash.
3. Only these unit-specific or mutable fields are excluded from the invariant hash:

```text
0x7D200B–0x7D200C  two-byte HWCONFIG unit check value
0x7D2011–0x7D206E  94-byte HWCONFIG UOID
0x7E0000–0x7FFFFF  writable JFFS2 config log
```

4. The excluded fields are still validated:
   - the UOID must have the expected 94-byte structure;
   - `config` must contain CRC-valid JFFS2 nodes;
   - clean-data accepts only the six known filenames unless
     `--wipe-unknown-config` is explicit;
   - preserve-data accepts additional live names only when every entry can be
     safely reconstructed as a regular root file;
   - exactly one unambiguous `serial.cfg` value must be recoverable;
   - the serial and UOID must share the expected 12-byte unit prefix.

No other differences are accepted. See `REFERENCE_FINGERPRINTS.json`.

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
- validates the generated JFFS2 image by parsing it again, including a complete
  content/metadata round trip in preserve-data mode;
- refuses `--keep-config` for a noncanonical/exhausted partition;
- refuses ambiguous, unsupported, or oversized preserve-data layouts;
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
