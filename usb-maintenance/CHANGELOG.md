# Changelog

## v0.6.0 — temporary ADB startup through upload command

- Bumped the Python client to v0.4.0.
- Added `backup --start-adb-through-upload-command`, which uses the recovered
  normal-HID uploader's unquoted target handling to start `/bin/adbd` for the
  current boot and then continues directly into the existing two-pass read.
- Kept the ordinary upload target builder's shell-metacharacter rejection and
  added one immutable, audit-oriented builder for the exact no-space target
  `/tmp/.cc2flash-adbd-bootstrap;/bin/adbd&`.
- Treats only the final `0x3300` failure, timeout, or HID disconnect as expected;
  initialize, target, and final-data failures still abort before injection.
- Preserved `--bootstrap-adb` as a mutually exclusive persistent alternative.
- Expanded the offline suite from 41 to 46 tests, including the exact injected
  frame, expected commit rejection, HID re-enumeration, and same-run backup.

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
