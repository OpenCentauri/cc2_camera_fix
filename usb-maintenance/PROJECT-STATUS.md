# Project status

**Documentation checkpoint: 2026-09-04**

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
- The guarded `install-adb-startup --backup BACKUP.zip` command requires online
  root ADB, stable reads and clean space. It installs/readbacks a shared runner
  and separate ADB hook through ADB, refuses different contents at `system.sh`
  or the selected `enabled/90-adb.sh` path, and requires a manual restart.
  Unrelated regular enabled hooks remain and execute in filename order.
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
  the preserved USB ZIP, while `restore --dry-run --backup` and `restore` prove the
  generated image matches that same camera outside the two audited edit
  regions.
- Made post-write verification compatible with both hardware-recovery config
  modes: after a serial-only boot it separately prompts for permission to start
  ADB temporarily, requires an exact match through HWCONFIG, and separately
  reports config changes made by that boot. Starting temporary ADB requires
  separate consent.

## Client status

The included Python client is now v0.6.0 with 106 passing offline tests. Its
backup path is strictly read-only and requires three consecutive identical full
reads within five attempts. Temporary and persistent ADB setup are separate
commands rather than fallback flags on `backup`. An accepted backup is one ZIP
archive, so the raw image cannot be published without its evidence manifest.
The restore path parses type-2 payloads as the next expected absolute packet and
rejects a retransmission request with an explicit error; automatic
retransmission is not implemented. A physical integrated restore completed the automatic live SFC preparation,
all 2,742 packets, full-flash erase/write, normal reboot, and required
three-read post-write verification. The earlier unprepared trigger failure is
now detected and avoided by exact-kernel, live-state, and readback gates.

Before opening USB, restore now compares the replacement with `flash.bin` from
the validated preserved ZIP. Only hardware recovery's exact SquashFS patch
window and config partition may differ. This rejects a wrong-camera recovery
image or unsupported additional edits. The same check is available read-only
through `restore --dry-run --backup`.

After restore, the client acquires another three consecutive reads. All bytes
through `0x7dffff` must match the candidate exactly. The writable config
partition is compared and reported separately because the first successful
serial-only boot recreates missing defaults before ADB can read it back.

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

Post-restore ADB availability uses one deadline for the preliminary probe and
the post-HID wait. The preliminary `get-state` subprocess cannot exceed
`--adb-timeout`, and it no longer adds its default ten-second budget before the
documented wait.

Without any setup, the stock USB gadget always enumerates an ADB transport as
`offline` until `adbd` starts. The availability classifier treats that exact
state, as well as a genuinely absent device, as eligible for both the temporary
and persistent startup mechanisms. Ambiguous, unauthorized, non-root, and
malformed-device states remain refusals.

## Hardware status

- A destructive physical experiment was completed on one
  `EF-S7-V1.0.30B`/T23N/ZB25VQ64 camera. The sanitized record is in
  [PHYSICAL-VALIDATION.md](PHYSICAL-VALIDATION.md).
- The first normal-mode trigger attempted to write the eight-byte upgrade flag
  over a JFFS2 cleanmarker at `0x7f8000`. Its erase failed, and the observed
  bytes were exactly the bitwise AND of the old data and requested flag. SPL
  read `0x00000004, 0x00000000`, so it booted Linux rather than U-Boot HID.
- Runtime logging and exact-kernel disassembly identified an erase-geometry
  mismatch: the driver was configured for `0x4000`, while the sector-erase
  routine has opcode cases only for `0x1000`, `0x8000`, and `0x10000`.
  Temporarily changing the boot-specific live field to `0x1000` made complete
  MTD erases succeed. Restore now derives, validates, changes, and reads back
  that field immediately before the stock flag operation. Reboot resets it, so
  this remains temporary preparation rather than a persistent fix.
- After experimental config preconditioning, SPL read the exact upgrade words
  and bootloader HID `a108:ff08` enumerated. A one-off continuation transmitted
  all 2,742 packets. U-Boot accepted the RAM-image MD5, erased and wrote the
  complete 8 MiB image, and returned to normal USB.
- The camera produced live video after the write. Temporary ADB startup then
  worked, and a post-write backup obtained three consecutive identical 8 MiB
  reads in three attempts. Boot through HWCONFIG matched the candidate exactly;
  strict validation passed, while config differed only as expected from the
  serial-only first boot. `system.sh` was absent.
- A later test exercised the integrated client as one uninterrupted operation,
  without a manual RAM edit, config erase, process suspension, or separate
  continuation. It obtained three stable pre-write reads, completed the dynamic
  preparation and stock HID transition, wrote the image, returned to normal
  USB, and obtained three stable post-write reads matching every boot-stable
  byte through HWCONFIG.
- No direct USB flash-read command was found. The temporary ADB route avoids
  installing a persistent file: it attempts a tmpfs removal, starts `/bin/adbd`,
  and then reaches the vendor's `sync` plus expected failing `fopen`. Normal
  firmware activity may still have pending JFFS2 writes, so this is not a claim
  that every flash byte remains unchanged during a live boot.
- The device owner physically verified the shared startup runner, persistent
  ADB, erase correction and repeated boots on one supported camera.
  [Hardware findings](../docs/STARTUP-HOOKS-VALIDATION.md) distinguish direct
  erase observations from automatic-GC inference and record the test limits.
- The temporary upload-command ADB start is derived from the reconstructed
  `hid_update` control flow. Its first physical-camera test successfully started
  `/bin/adbd` and allowed `adb shell`; the Windows host exposed a now-corrected
  retry bug by returning `error: closed` from the old USB transport first.
- An earlier live session confirmed that stock `adbd` rejects `exec-out`, but its
  sync service pulls raw MTD devices without byte changes. A later invocation
  acquired a complete 8 MiB image with SHA-256
  `bccc6818a998d1c143c94543194e2d314cdee7a5f43b62db1fc6bb0b038c22a7`.
  Its boot partition is byte-identical to the supplied reference image and has
  SHA-256
  `5602ec961b4410ccceea0d4910e4fa768c6998bd4ba86143ba50855bdd0b7a54`.
  The strengthened three-consecutive-read policy has now also passed both the
  pre-write and post-write physical acquisitions used for this experiment.

The authoritative protocol and full command catalog are in `PROTOCOL.md`; static
anchors and hashes are in `EVIDENCE.md`. The human-readable reconstruction and
its audit guide are in `source-reconstruction/`.
