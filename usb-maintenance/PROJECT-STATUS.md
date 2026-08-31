# Project status

**Documentation checkpoint: 2026-08-31**

## Completed

- Confirmed the 8 MiB flash/MTD map and both SquashFS filesystems.
- Recovered and disassembled the exact 19,048-byte `/bin/hid_update`.
- Documented the complete 1,024-byte normal-HID frame and CRC.
- Cataloged the version command, all 42 configuration commands, all 13 Linux
  file-upload commands, and the group-wide `0x4xxx` bootloader trigger.
- Documented U-Boot HID/CDC framing, metadata, numbered data, ACK values, retry
  behavior, the 128-byte image header, MD5 check, and SPI erase/write sequence.
- Established that neither updater stage exposes flash or file download.
- Corrected the earlier ADB assumption: `adbd` exists but is not autostarted.
- Confirmed that root `rcS` mounts JFFS2 at `/etc/conf.d` and executes an
  optional `/etc/conf.d/system.sh` before mounting `/system`.
- Integrated a guarded normal-HID fallback that installs the runtime-validated
  `/bin/adbd &` startup hook, stops, and requires a manual restart.
- Added an explicit nonpersistent alternative that starts `/bin/adbd` through
  the uploader's unquoted `rm` target, waits for ADB, and continues the two-pass
  backup in the same invocation.
- Corrected the bootloader ACK interpretation: type-2 payload is
  `u32le(next_expected_packet)`.
- Reconstructed the complete update-relevant Linux daemon, SPL gate, and main
  U-Boot state machine as annotated C-like source with exact address anchors.
- Added reproducible extraction/disassembly commands and dependency-free packet
  vectors for independent review.
- Added `cc2flash.commands`, a public typed catalog for all 57 exact normal-HID
  commands, the full 16-bit dispatcher behavior, and all four U-Boot frame types.
- Added side-effect-free builders for every recovered operation, plus typed ACK
  and terminal-status decoders, with source anchors and safety comments beside
  the implementation.
- Refactored the CLI-used HID paths to consume the public builders rather than
  maintaining a second set of command literals.

## Client status

The included Python client is now v0.4.0 with 50 passing offline tests. Its
two-pass backup path is implemented, including both the temporary upload-command
ADB start and the guarded persistent ADB-startup fallback.
The restore path now parses type-2 payloads as the next expected absolute packet
and its two-packet mock proves the `ACK 0 → packet 0 → ACK 1 → final packet →
type-5` sequence. It rejects a retransmission request with an explicit error;
automatic retransmission is not implemented. Restore therefore remains
hardware-unverified rather than known wire-incompatible.

The expanded tests verify exact catalog completeness, all 21 configuration
pairs, all 13 uploader commands, all four U-Boot frame types, group-wide
`0x4xxx` behavior, builders/decoders, the exact
`3000 → 3110 → 3200(final) → 3300` ADB-startup upload, interactive guards, and
the temporary command-injection transaction and the prior backup/image safety
checks.

Without any setup, the stock USB gadget always enumerates an ADB transport as
`offline` until `adbd` starts. The availability classifier treats that exact
state, as well as a genuinely absent device, as eligible for both the temporary
and persistent startup mechanisms. Ambiguous, unauthorized, non-root, and
malformed-device states remain refusals.

## Hardware status

- No destructive camera write was performed in this analysis.
- Normal and bootloader enumeration/timing have not been captured here.
- No direct USB flash-read command was found. The temporary ADB route avoids
  installing a persistent file: it attempts a tmpfs removal, starts `/bin/adbd`,
  and then reaches the vendor's `sync` plus expected failing `fopen`. Normal
  firmware activity may still have pending JFFS2 writes, so this is not a claim
  that every flash byte remains unchanged during a live boot.
- The device owner manually verified that `/etc/conf.d/system.sh` containing
  `/bin/adbd &` starts ADB on the next boot. The client-generated HID transaction
  for that persistent path remains hardware-unverified in this work.
- The temporary upload-command ADB start is derived from the reconstructed
  `hid_update` control flow. Its first physical-camera test successfully started
  `/bin/adbd` and allowed `adb shell`; the Windows host exposed a now-corrected
  retry bug by returning `error: closed` from the old USB transport first.

The authoritative protocol and full command catalog are in `PROTOCOL.md`; static
anchors and hashes are in `EVIDENCE.md`. The human-readable reconstruction and
its audit guide are in `source-reconstruction/`.
