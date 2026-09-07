# Hardware-recovery technical details

The normal prevention and recovery paths are documented in the [main user guide](../README.md).
This document records the implementation, validation model, supported image family, and limitations.

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

For a failed camera, read and rewrite the eight-pin SPI flash with an external programmer. A working camera can use the USB prevention workflow in the main guide. Both use the same image builder.

The recommended setup is:

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

`serial-only` is the default. The
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

`preserve-files` instead resolves the current root-directory view and recreates
every live regular file once in a fresh compact JFFS2 image. It preserves file
contents, mode, owner, timestamps, flags, inode identity, and directory-entry
metadata, while dropping obsolete/dead historical nodes. It supports
uncompressed, zero-filled, and zlib-compressed source fragments and fails
closed on directories, links/special entries, ambiguous metadata, unsupported
compression, fragments larger than the config partition, zlib streams that
expand beyond their declared size, or a live set too large for the audited
compact layout. Zlib decoding caps output at the declared size plus one byte.

## Supported image family

This release intentionally accepts only the exact firmware family already verified in three independent camera dumps with different unit identities:

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

Two exact shapes of its type-12 length-prefixed record are supported:

| Variant | Encoded payload length | Bytes after the original 256-byte payload |
|---|---:|---|
| `type12-length256` | 256 (`0x0100`) | none |
| `type12-length261-trailer-0000029840` | 261 (`0x0105`) | `00 00 02 98 40` |

The second shape was observed in a third stable three-read dump. Its boot,
kernel, root, system, stock startup-script window, partition layout, identity
structure, and exhausted JFFS2 failure state match the supported family. The
record length exactly includes the five added bytes. The camera's own
`ucamera` executable advances over this structure as a little-endian 16-bit
type, a little-endian 16-bit payload length, and that many payload bytes.

The meaning of the five-byte extension is unknown. The validator therefore
accepts only the exact observed length/trailer combination and its complete
variant-specific invariant fingerprints. It rejects arbitrary extensions,
nearby lengths, and mixtures of the two variants. Recovery preserves the
complete input HWCONFIG partition byte-for-byte.

The validator therefore performs the strongest safe equivalent of “100% match apart from unit data”:

1. Every firmware byte that should be invariant must match the exact fingerprints for one recognized HWCONFIG record variant.
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
   - serial-only accepts only the six known filenames unless
     `--wipe-unknown-config` is explicit;
   - preserve-files accepts additional live names only when every entry can be
     safely reconstructed as a regular root file;
   - exactly one unambiguous `serial.cfg` value must be recoverable;
   - the serial and UOID must share the expected 12-byte unit prefix.

No other differences are accepted. See `REFERENCE_FINGERPRINTS.json`.

## Safety properties

The builder:

- can require any additional physical reads supplied with `--confirm-read` to be byte-for-byte identical;
- requires three identical reads for every unit unless reduced confidence is explicitly accepted;
- refuses files that are not exactly 8 MiB;
- validates exact hashes for every invariant segment;
- accepts only the exact known stock or known patched SquashFS window;
- never edits boot, kernel, root, or HWCONFIG;
- preserves the entire input HWCONFIG partition;
- preserves the original serial payload;
- validates the generated JFFS2 image by parsing it again, including a complete
  content/metadata round trip in preserve-files mode;
- refuses ambiguous, unsupported, or oversized preserve-files layouts;
- refuses unsafe output-directory reuse that could delete inputs or unrelated files;
- validates the complete generated recovery image again;
- proves no bytes changed outside the selected patch/config regions;
- creates a manifest and hashes for every output.

## Limitations

This is a mitigation for the deterministic per-boot write leak. It does not repair the underlying Ingenic SFC/JFFS2 erase-size defect. Other software that performs persistent writes could still consume config space.

The tool supports only the exact firmware build and HWCONFIG shapes represented by the embedded fingerprints. A future Elegoo/Jovision build or another HWCONFIG record shape must be analyzed and fingerprinted separately.

Three real unit identities were directly inspected, including one with the
261-byte HWCONFIG record. Cross-serial support is fail-closed: invariant
firmware must match the identified variant exactly; the UOID and serial must
each match their observed structure and share the observed 12-byte prefix; all
unit-specific HWCONFIG bytes and the recovered serial payload are preserved
exactly. Because the proprietary full serial↔UOID derivation is unknown, the
tool cannot prove more than that relationship for a previously unseen unit.

Static analysis can prove that a bricked dump is transformed into the exact known recovery bytes and that all relevant filesystems/checksums validate. Actual boot recovery still requires a controlled flash, full readback verification, and an observed successful boot; software-only review cannot replace that hardware test.

`serial.cfg` and the generated recovery image contain a unit-specific identifier. Do not publish them unredacted.
