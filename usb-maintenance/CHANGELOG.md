# Changelog

## v0.8.0 — hardware/USB recovery interoperability

- Bumped the Python client to v0.6.0.
- Added `plan-restore --backup` so the candidate/preserved-backup pair can be
  validated without opening USB.
- Made both `plan-restore --backup` and `restore` require the replacement to
  match the preserved camera backup byte-for-byte outside hardware recovery's
  audited `0x463000–0x46afff` SquashFS window and
  `0x7e0000–0x7fffff` config partition.
- Split strict preserved-backup loading from its compatibility hash API so the
  restore guard compares actual bytes, not only filenames or unit identifiers.
- Documented the complete
  `cc2flash backup → cc2_sig_tool build → cc2flash restore` workflow.
- Expanded the offline suite from 78 to 82 tests, including allowed-region
  interoperability, wrong-unit rejection, offline pair planning, and proof that
  a compatibility failure occurs before USB is opened.

## v0.7.0 — read-only backup and stable boot fingerprint

- Bumped the Python client to v0.5.0.
- Made `backup` strictly read-only. An offline camera now receives instructions
  for the separate `start-adb` or `install-adb-startup` commands; it is never
  mutated as an automatic fallback.
- Replaced the two-read rule with three consecutive byte-identical 8 MiB reads
  within at most five attempts, matching the repository's hardware-recovery
  minimum while allowing live JFFS2 state two additional chances to settle.
- Added a known SHA-256 gate for the complete 256 KiB boot partition:
  `5602ec961b4410ccceea0d4910e4fa768c6998bd4ba86143ba50855bdd0b7a54`.
- Unknown boot partitions fail only after the three-read gate, print the exact
  observed hash, and require an exact reviewed
  `--accept-bootloader-hash <sha256>` override before any backup is published.
- Publishes the raw image and `cc2flash-backup-v2` manifest as the two members
  of one ordinary ZIP. The completed archive is flushed, CRC-checked, and
  exposed through an atomic same-filesystem create-if-absent hard link. This
  eliminates both the prior half-pair window and concurrent overwrite race.
  Restore consumes the ZIP directly and rejects legacy two-read manifests.
- Makes the ADB startup deadline bound each `get-state` subprocess and rejects
  zero, negative, infinite, and NaN durations before sending HID.
- Type-checks untrusted manifest counters before comparison, so malformed JSON
  is rejected as a protocol error instead of escaping as `TypeError`.
- Rejects impossible v2 stable-read counts and unsupported ZIP compression as
  clean protocol errors.
- Rejects damaged DEFLATE/LZMA member streams as clean protocol errors instead
  of allowing decompressor exceptions to escape with a traceback.
- Rejects overlong JSON integers and excessive manifest nesting as clean
  protocol errors instead of allowing decoder exceptions to escape.
- Treats an ADB subprocess timeout as an immediate hard failure, keeping the
  startup poll's retry set limited to absent, stock `offline`, and exact
  `error: closed` transition states.
- Defines restore `--adb-timeout` as the bounded online-availability wait; the
  stable readback then runs as a separate operation under per-command timeouts.
- Expanded the offline suite from 54 to 78 tests, including stability ordering,
  hash output/override behavior, create-if-absent ZIP publication, malformed
  manifests, bounded availability timeouts, read-only backup, and separate ADB
  commands.

## v0.6.0 — temporary ADB startup through upload command

- Bumped the Python client to v0.4.0.
- Added `backup --start-adb-through-upload-command`, which uses the recovered
  normal-HID uploader's unquoted target handling to start `/bin/adbd` for the
  current boot and then continues directly into the existing two-pass read.
- Classifies the stock camera's expected pre-daemon `offline` ADB state as
  startup-eligible; ambiguity, authorization, and other ADB failures still
  refuse both HID fallbacks.
- Replaces the single post-injection `adb wait-for-device` call with a bounded
  state poll. Live Windows testing showed that a successful startup closes the
  old USB transport first, causing `wait-for-device` to exit with
  `error: closed` even though the new daemon then accepts `adb shell`.
- Uses the stock daemon's legacy `shell` service for text and its binary-safe
  sync/`pull` service for MTD acquisition. Live Windows testing established that
  `exec-out` is unsupported, while a 65,536-byte `/dev/mtd4` pull exactly
  matched the camera-side MD5.
- Kept the ordinary upload target builder's shell-metacharacter rejection and
  added one immutable, audit-oriented builder for the exact no-space target
  `/tmp/.cc2flash-adbd-bootstrap;/bin/adbd&`.
- Treats only the final `0x3300` failure, timeout, or HID disconnect as expected;
  initialize, target, and final-data failures still abort before injection.
- Preserved `--bootstrap-adb` as a mutually exclusive persistent alternative.
- Expanded the offline suite from 41 to 54 tests, including ADB-offline
  classification, the exact injected
  frame, expected commit rejection, HID re-enumeration, legacy text-shell
  parsing, ordered binary pulls, and same-run backup.

## v0.5.0 — complete public Python command library

- Bumped the Python client to v0.3.0.
- Added the audit-oriented `cc2flash.commands` module with all 57 exact
  normal-mode command values, all 21 configuration GET/SET pairs, all 13 upload
  operations, the complete 16-bit dispatcher map, and group-wide `0x4xxx`
  matching.
- Added typed, side-effect-free builders for version/configuration/upload/
  upgrade operations and U-Boot metadata/data frames.
- Added all four U-Boot frame types and strict next-expected-packet/terminal
  response decoders.
- Refactored HID transport to use the shared builders; ordinary in-order restore
  ACK validation now matches the disassembly. Retransmission remains explicit
  but unimplemented, and no destructive physical test was performed.
- Expanded the offline suite from 22 to 41 tests, including a two-packet ACK
  sequence, metadata safety validation, and exact catalog/table checks.

## v0.4.0 — guarded ADB-startup fallback

- Bumped the Python client to v0.2.0.
- Confirmed the root `rcS` boot hook that executes persistent
  `/etc/conf.d/system.sh` before mounting `/system`.
- Added an ADB availability classifier so only a genuinely absent selected
  device can enter recovery; local ADB errors, ambiguity, authorization/root
  failures, and partition mismatches do not trigger a device write.
- Added the exact normal-HID upload of `/bin/adbd &`, interactive typed
  confirmation, explicit `--bootstrap-adb` automation opt-in, exit status `3`,
  and manual-restart instructions.
- Expanded the offline suite from 13 to 22 tests. Restore remains blocked by
  the known next-expected-packet ACK incompatibility.

## v0.3.0 — source-reconstruction checkpoint

- Added annotated C-like reconstructions of Linux `/bin/hid_update`, the SPL
  upgrade gate, and the main-U-Boot update state machine.
- Added exact function/address traceability and read-only reproduction commands.
- Added dependency-free vectors for the normal CRC, upgrade trigger, bootloader
  metadata/ACK framing, and image-header MD5 layout.
- Documented the configuration SET routine's tail buffering and zero-padding.
- Retained the prior corrections that U-Boot ACKs carry the next expected packet
  number and that stock startup scripts do not autostart `adbd`.

At this checkpoint, the Python client was still the original research
implementation and was blocked by the two issues then described in
`PROJECT-STATUS.md`; v0.4.0 resolves the ADB-startup half only.

## v0.2.0 — complete protocol reference

- Added the complete normal-HID and U-Boot HID/CDC protocol catalog.
- Corrected the bootloader ACK interpretation.
- Corrected the earlier assumption that ADB is immediately available on stock
  USB.

## v0.1.0 — initial research client

- Added offline protocol builders/parsers, HID transport scaffolding, ADB backup
  logic, safety gates, and 13 mocked/offline tests.
