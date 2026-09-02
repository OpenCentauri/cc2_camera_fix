# Project status

**Documentation checkpoint: 2026-09-02**

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
- Added a guarded `install-adb-startup` command that installs the
  runtime-validated `/bin/adbd &` startup hook, stops, and requires a manual
  restart.
- Added a separate nonpersistent `start-adb` command that starts `/bin/adbd`
  through the uploader's unquoted `rm` target, waits for root ADB, and exits
  without reading flash or installing a persistent file.
- Made `backup` strictly read-only. It requires three consecutive identical
  full reads within five attempts before evaluating a known boot-partition
  SHA-256 or an exact user-reviewed override.
- Publishes `flash.bin` and `manifest.json` inside one verified ZIP through an
  atomic same-filesystem create-if-absent hard link; a concurrently created
  destination is never overwritten, and restore validates the archive directly.
- Bounds every post-HID ADB probe by the remaining startup deadline, rejects
  invalid durations before HID, and rejects malformed manifest field types as
  ordinary protocol errors.
- Replaced backup use of unsupported `adb exec-out` with legacy text `shell`
  plus binary-safe sync/`pull`. A live Windows pull of `/dev/mtd4` returned the
  exact expected 65,536 bytes and matched the camera-side MD5.
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
- Added direct interoperability with hardware recovery: its builder consumes
  the preserved USB ZIP, while `plan-restore --backup` and `restore` prove the
  generated image matches that same camera outside the two audited edit
  regions.
- Made post-write verification compatible with both hardware-recovery config
  modes: after a clean-data boot it separately prompts for permission to start
  ADB temporarily, requires an exact match through HWCONFIG, and separately
  reports config changes made by that boot. It never treats restore `--yes` as
  consent to start ADB.

## Client status

The included Python client is now v0.6.0 with 85 passing offline tests. Its
backup path is strictly read-only and requires three consecutive identical full
reads within five attempts. Temporary and persistent ADB setup are separate
commands rather than fallback flags on `backup`. An accepted backup is one ZIP
archive, so the raw image cannot be published without its evidence manifest.
The restore path now parses type-2 payloads as the next expected absolute packet
and its two-packet mock proves the `ACK 0 → packet 0 → ACK 1 → final packet →
type-5` sequence. It rejects a retransmission request with an explicit error;
automatic retransmission is not implemented. Restore therefore remains
hardware-unverified rather than known wire-incompatible.

Before opening USB, restore now compares the replacement with `flash.bin` from
the validated preserved ZIP. Only hardware recovery's exact SquashFS patch
window and config partition may differ. This rejects a wrong-camera recovery
image or unsupported additional edits. The same check is available read-only
through `plan-restore --backup`.

After restore, the client acquires another three consecutive reads. All bytes
through `0x7dffff` must match the candidate exactly. The writable config
partition is compared and reported separately because the first successful
clean-data boot recreates missing defaults before ADB can read it back.

The expanded tests verify exact catalog completeness, all 21 configuration
pairs, all 13 uploader commands, all four U-Boot frame types, group-wide
`0x4xxx` behavior, builders/decoders, stable-read/hash/archive gates, bounded
deadline propagation, post-restore availability-timeout separation,
malformed-manifest, JSON decoder-limit, unsupported-ZIP, and corrupt
compressed-member rejection,
hard failure on a hung ADB subprocess, the exact
`3000 → 3110 → 3200(final) → 3300` ADB-startup upload, interactive guards, and
proof that declined or non-interactive post-restore prompts send no HID command,
the temporary command-injection transaction, and the prior backup/image safety
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
- The same live session confirmed that stock `adbd` rejects `exec-out`, but its
  sync service pulls raw MTD devices without byte changes. A later invocation
  acquired a complete 8 MiB image with SHA-256
  `bccc6818a998d1c143c94543194e2d314cdee7a5f43b62db1fc6bb0b038c22a7`.
  Its boot partition is byte-identical to the supplied reference image and has
  SHA-256
  `5602ec961b4410ccceea0d4910e4fa768c6998bd4ba86143ba50855bdd0b7a54`.
  The strengthened three-consecutive-read policy is not yet physically rerun.

The authoritative protocol and full command catalog are in `PROTOCOL.md`; static
anchors and hashes are in `EVIDENCE.md`. The human-readable reconstruction and
its audit guide are in `source-reconstruction/`.
